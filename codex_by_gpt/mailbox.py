from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterator

from .config import APP_DIR, EXECUTION_CLAIMS_FILE, EXECUTIONS_FILE, MAILBOX_FILE

ALLOWED_KINDS = {"PLAN", "REVIEW", "DONE", "BLOCKED", "RESEARCH"}
MAX_PAYLOAD_CHARS = 64_000
MAX_EXECUTION_OUTPUT_CHARS = 256_000
MAX_TEST_FIELD_CHARS = 4_000
TEST_STATUSES = {"passed", "failed", "not_run", "unknown"}
LOCK_TIMEOUT_SECONDS = 10
_PROCESS_LOCK = threading.RLock()
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(\b(?:authorization|api[-_]?key|access[-_]?token|control_plane_api_key|chatgpt-apikey|password|client[-_]?secret)\b\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\r\n]+)"
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----", re.DOTALL),
)

@dataclass
class Result:
    id: str
    workspace_id: str
    task_id: str
    iteration: int
    kind: str
    payload: str
    created_at: float
    acked: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None
    cancelled_at: float | None = None
    blocked: bool = False
    block_reason: str | None = None
    blocked_at: float | None = None


@dataclass
class ExecutionRecord:
    id: str
    workspace_id: str
    task_id: str
    iteration: int
    source_result_id: str
    state: str
    codex_exit_code: int | None
    execution_output: str
    test_status: dict[str, Any]
    created_at: float


@dataclass
class ExecutionClaim:
    workspace_id: str
    task_id: str
    iteration: int
    source_result_id: str
    created_at: float
    process_pid: int | None = None


class SubmissionConflictError(ValueError):
    """A logical task iteration was resubmitted with different content."""


def _logical_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (row["workspace_id"], row["task_id"], int(row["iteration"]))


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialize a state-file transaction across threads and local processes."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _PROCESS_LOCK, lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out acquiring state lock: {lock_path}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _redact_execution_output(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return _SECRET_ASSIGNMENT_PATTERN.sub(r"\1<redacted>", redacted)


def _load() -> list[dict[str, Any]]:
    if not MAILBOX_FILE.exists():
        return []
    rows = []
    for line in MAILBOX_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _save(rows: list[dict[str, Any]]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MAILBOX_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, MAILBOX_FILE)


def submit(workspace_id: str, task_id: str, iteration: int, kind: str, payload: str) -> Result:
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {sorted(ALLOWED_KINDS)}")
    if iteration < 0:
        raise ValueError("iteration must be >= 0")
    if len(payload) > MAX_PAYLOAD_CHARS:
        raise ValueError("payload too large")
    with _locked(MAILBOX_FILE):
        rows = _load()
        key = (workspace_id, task_id, iteration)
        matches = [row for row in rows if _logical_key(row) == key]
        if matches:
            if all(row["kind"] == kind and row["payload"] == payload for row in matches):
                return Result(**matches[0])
            raise SubmissionConflictError(
                "task_id and iteration already contain different content; use a new iteration"
            )
        result = Result(str(uuid.uuid4()), workspace_id, task_id, iteration, kind, payload, time.time())
        rows.append(asdict(result))
        _save(rows)
    return result


def list_results(workspace_id: str | None = None, task_id: str | None = None, include_acked: bool = False) -> list[dict[str, Any]]:
    rows = _load()
    return [r for r in rows if (include_acked or not r.get("acked")) and (workspace_id is None or r["workspace_id"] == workspace_id) and (task_id is None or r["task_id"] == task_id)]


def ack(result_id: str) -> bool:
    with _locked(MAILBOX_FILE):
        rows = _load()
        changed = False
        for row in rows:
            if row["id"] == result_id:
                row["acked"] = True
                changed = True
                break
        if changed:
            _save(rows)
    return changed


def is_cancelled(result_id: str) -> bool:
    row = next((row for row in _load() if row["id"] == result_id), None)
    if row and row.get("cancelled"):
        return True
    record = execution_for_source(result_id)
    return bool(record and record.get("state") in {"CANCELLED", "SUPERSEDED", "BLOCKED"})


def _load_executions() -> list[dict[str, Any]]:
    if not EXECUTIONS_FILE.exists():
        return []
    rows = []
    for line in EXECUTIONS_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _save_executions(rows: list[dict[str, Any]]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = EXECUTIONS_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, EXECUTIONS_FILE)


def _load_execution_claims() -> list[dict[str, Any]]:
    if not EXECUTION_CLAIMS_FILE.exists():
        return []
    rows = []
    for line in EXECUTION_CLAIMS_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _save_execution_claims(rows: list[dict[str, Any]]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = EXECUTION_CLAIMS_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, EXECUTION_CLAIMS_FILE)


def get_execution_claim(workspace_id: str, task_id: str, iteration: int) -> dict[str, Any] | None:
    key = (workspace_id, task_id, iteration)
    return next((row for row in reversed(_load_execution_claims()) if _logical_key(row) == key), None)


def list_execution_claims(workspace_id: str | None = None) -> list[dict[str, Any]]:
    return [
        row
        for row in _load_execution_claims()
        if workspace_id is None or row["workspace_id"] == workspace_id
    ]


def claim_execution(workspace_id: str, task_id: str, iteration: int, source_result_id: str) -> ExecutionClaim:
    if iteration < 0:
        raise ValueError("iteration must be >= 0")
    if not source_result_id:
        raise ValueError("source_result_id is required")
    with _locked(EXECUTION_CLAIMS_FILE):
        rows = _load_execution_claims()
        key = (workspace_id, task_id, iteration)
        existing = next((row for row in reversed(rows) if _logical_key(row) == key), None)
        if existing:
            if existing["source_result_id"] != source_result_id:
                raise RuntimeError("logical task iteration is already claimed by a different mailbox result")
            return ExecutionClaim(**existing)
        claim = ExecutionClaim(workspace_id, task_id, iteration, source_result_id, time.time())
        rows.append(asdict(claim))
        _save_execution_claims(rows)
    return claim


def set_execution_claim_process(workspace_id: str, task_id: str, iteration: int, process_pid: int) -> bool:
    if process_pid <= 0:
        raise ValueError("process_pid must be > 0")
    with _locked(EXECUTION_CLAIMS_FILE):
        rows = _load_execution_claims()
        key = (workspace_id, task_id, iteration)
        for row in reversed(rows):
            if _logical_key(row) == key:
                row["process_pid"] = process_pid
                _save_execution_claims(rows)
                return True
    return False


def clear_execution_claim(workspace_id: str, task_id: str, iteration: int) -> bool:
    with _locked(EXECUTION_CLAIMS_FILE):
        rows = _load_execution_claims()
        key = (workspace_id, task_id, iteration)
        kept = [row for row in rows if _logical_key(row) != key]
        if len(kept) == len(rows):
            return False
        _save_execution_claims(kept)
    return True


def _normalize_test_status(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    status = value.get("status")
    if status not in TEST_STATUSES:
        status = "unknown"
    command = value.get("command")
    summary = value.get("summary")
    return {
        "status": status,
        "command": _redact_execution_output(str(command))[:MAX_TEST_FIELD_CHARS] if command is not None else None,
        "summary": _redact_execution_output(str(summary or ""))[:MAX_TEST_FIELD_CHARS],
    }


def execution_for_source(source_result_id: str) -> dict[str, Any] | None:
    return next((row for row in reversed(_load_executions()) if row["source_result_id"] == source_result_id), None)


def record_execution(
    workspace_id: str,
    task_id: str,
    iteration: int,
    source_result_id: str,
    codex_exit_code: int | None,
    execution_output: str,
    test_status: dict[str, Any] | None,
    state: str = "EXECUTED",
) -> ExecutionRecord:
    if iteration < 0:
        raise ValueError("iteration must be >= 0")
    if not source_result_id:
        raise ValueError("source_result_id is required")
    with _locked(EXECUTIONS_FILE):
        rows = _load_executions()
        key = (workspace_id, task_id, iteration)
        existing = next((row for row in reversed(rows) if _logical_key(row) == key), None)
        if not existing:
            existing = next((row for row in reversed(rows) if row["source_result_id"] == source_result_id), None)
        if existing:
            return ExecutionRecord(**existing)
        clipped_output = _redact_execution_output(str(execution_output))[:MAX_EXECUTION_OUTPUT_CHARS]
        normalized_test_status = _normalize_test_status(test_status)
        if codex_exit_code not in {None, 0} and normalized_test_status["status"] == "passed":
            normalized_test_status = {
                **normalized_test_status,
                "status": "unknown",
                "summary": "Codex exited non-zero; a passing test claim cannot be trusted.",
            }
        if state not in {"EXECUTED", "CANCELLED", "SUPERSEDED", "BLOCKED"}:
            raise ValueError("invalid execution state")
        record = ExecutionRecord(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            task_id=task_id,
            iteration=iteration,
            source_result_id=source_result_id,
            state=state,
            codex_exit_code=codex_exit_code,
            execution_output=clipped_output,
            test_status=normalized_test_status,
            created_at=time.time(),
        )
        rows.append(asdict(record))
        _save_executions(rows)
    return record


def record_cancellation(
    workspace_id: str,
    task_id: str,
    iteration: int,
    source_result_id: str,
    reason: str,
    state: str = "CANCELLED",
) -> ExecutionRecord:
    reason = _redact_execution_output(str(reason)).strip()
    if not reason:
        raise ValueError("reason is required")
    return record_execution(
        workspace_id,
        task_id,
        iteration,
        source_result_id,
        None,
        reason,
        {"status": "not_run", "command": None, "summary": reason},
        state=state,
    )


def record_blocked(
    workspace_id: str,
    task_id: str,
    iteration: int,
    source_result_id: str,
    reason: str,
) -> ExecutionRecord:
    reason = _redact_execution_output(str(reason)).strip()
    if not reason:
        raise ValueError("reason is required")
    return record_execution(
        workspace_id,
        task_id,
        iteration,
        source_result_id,
        None,
        reason,
        {"status": "not_run", "command": None, "summary": reason},
        state="BLOCKED",
    )


def _terminate_claimed_process(process_pid: int) -> bool:
    """Terminate a persisted worker child from the connector process."""
    if process_pid <= 0:
        return False
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill", "/PID", str(process_pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return completed.returncode == 0
        os.kill(process_pid, signal.SIGTERM)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def cancel_result(result_id: str, reason: str) -> ExecutionRecord:
    """Cancel one exact mailbox result and persist an immutable terminal receipt."""
    reason = _redact_execution_output(str(reason)).strip()
    if not reason:
        raise ValueError("reason is required")
    existing = execution_for_source(result_id)
    if existing:
        if existing.get("state") in {"CANCELLED", "SUPERSEDED"}:
            return ExecutionRecord(**existing)
        raise RuntimeError("result already has EXECUTED evidence and cannot be cancelled")
    with _locked(MAILBOX_FILE):
        rows = _load()
        row = next((row for row in rows if row["id"] == result_id), None)
        if row is None:
            raise ValueError(f"mailbox result not found: {result_id}")
        if row.get("cancelled"):
            existing = execution_for_source(result_id)
            if existing:
                return ExecutionRecord(**existing)
        row["cancelled"] = True
        row["cancel_reason"] = reason
        row["cancelled_at"] = time.time()
        row["acked"] = True
        _save(rows)
    state = "SUPERSEDED" if reason.upper().startswith("SUPERSEDED") else "CANCELLED"
    claim = get_execution_claim(row["workspace_id"], row["task_id"], int(row["iteration"]))
    process_pid = int(claim["process_pid"]) if claim and claim.get("process_pid") else None
    if process_pid:
        _terminate_claimed_process(process_pid)
    record = record_cancellation(row["workspace_id"], row["task_id"], int(row["iteration"]), result_id, reason, state)
    if not process_pid:
        clear_execution_claim(row["workspace_id"], row["task_id"], int(row["iteration"]))
    return record


def block_result(result_id: str, reason: str) -> ExecutionRecord:
    """Fail closed for one exact result and persist an immutable BLOCKED receipt."""
    reason = _redact_execution_output(str(reason)).strip()
    if not reason:
        raise ValueError("reason is required")
    existing = execution_for_source(result_id)
    if existing:
        if existing.get("state") == "BLOCKED":
            return ExecutionRecord(**existing)
        raise RuntimeError("result already has terminal execution evidence and cannot be blocked")
    with _locked(MAILBOX_FILE):
        rows = _load()
        row = next((row for row in rows if row["id"] == result_id), None)
        if row is None:
            raise ValueError(f"mailbox result not found: {result_id}")
        if row.get("blocked"):
            existing = execution_for_source(result_id)
            if existing:
                return ExecutionRecord(**existing)
        row["blocked"] = True
        row["block_reason"] = reason
        row["blocked_at"] = time.time()
        row["acked"] = True
        _save(rows)
    claim = get_execution_claim(row["workspace_id"], row["task_id"], int(row["iteration"]))
    process_pid = int(claim["process_pid"]) if claim and claim.get("process_pid") else None
    if process_pid:
        _terminate_claimed_process(process_pid)
    record = record_blocked(row["workspace_id"], row["task_id"], int(row["iteration"]), result_id, reason)
    if not process_pid:
        clear_execution_claim(row["workspace_id"], row["task_id"], int(row["iteration"]))
    return record


def get_execution(workspace_id: str, task_id: str, iteration: int) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in reversed(_load_executions())
            if row["workspace_id"] == workspace_id
            and row["task_id"] == task_id
            and row["iteration"] == iteration
        ),
        None,
    )


def wait_for_execution(workspace_id: str, task_id: str, iteration: int, timeout_seconds: int = 0) -> dict[str, Any]:
    if timeout_seconds < 0 or timeout_seconds > 30:
        raise ValueError("timeout_seconds must be between 0 and 30")
    deadline = time.monotonic() + timeout_seconds
    while True:
        record = get_execution(workspace_id, task_id, iteration)
        if record:
            return {"state": record.get("state", "EXECUTED"), "record": record}
        if time.monotonic() >= deadline:
            return {
                "state": "PENDING",
                "workspaceId": workspace_id,
                "taskId": task_id,
                "iteration": iteration,
            }
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
