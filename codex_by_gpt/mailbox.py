from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .config import APP_DIR, MAILBOX_FILE

ALLOWED_KINDS = {"PLAN", "REVIEW", "DONE", "BLOCKED", "RESEARCH"}
MAX_PAYLOAD_CHARS = 64_000

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
    result = Result(str(uuid.uuid4()), workspace_id, task_id, iteration, kind, payload, time.time())
    rows = _load()
    rows.append(asdict(result))
    _save(rows)
    return result


def list_results(workspace_id: str | None = None, task_id: str | None = None, include_acked: bool = False) -> list[dict[str, Any]]:
    rows = _load()
    return [r for r in rows if (include_acked or not r.get("acked")) and (workspace_id is None or r["workspace_id"] == workspace_id) and (task_id is None or r["task_id"] == task_id)]


def ack(result_id: str) -> bool:
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
