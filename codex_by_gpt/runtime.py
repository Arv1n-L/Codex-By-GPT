from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Sequence

from . import __version__
from .config import (
    APP_DIR,
    EXECUTION_CLAIMS_FILE,
    EXECUTIONS_FILE,
    MAILBOX_FILE,
    STATE_FILE,
    WorkspaceConfig,
    list_workspaces,
)
from .mailbox import _redact_execution_output
from .mcp import TOOLS as MCP_TOOLS

LISTENER_DIR = APP_DIR / "listeners"
CLAIM_STALE_SECONDS = 2 * 60 * 60 + 60
DEFAULT_TUNNEL_HEALTH_URL = "http://127.0.0.1:8080"
MAX_TUNNEL_PROBE_BODY_CHARS = 2048
CORE_MCP_TOOLS = frozenset(
    {
        "workspace_list",
        "workspace_info",
        "list_directory",
        "read_file",
        "search_workspace",
        "git_status",
        "git_diff",
        "submit_result",
        "wait_execution",
    }
)
EXPECTED_MCP_TOOLS = {tool["name"]: tool for tool in MCP_TOOLS}
_PROCESS_LOCK = RLock()
_ACTIVE_LISTENERS: set[str] = set()


class ListenerAlreadyActiveError(RuntimeError):
    def __init__(self, cfg: WorkspaceConfig, metadata: dict[str, Any] | None = None):
        details = metadata or {}
        lines = [f"Codex listener is already active for workspace {cfg.name}"]
        if details.get("pid") is not None:
            lines.append(f"pid: {details['pid']}")
        if details.get("started_at"):
            lines.append(f"started_at: {details['started_at']}")
        super().__init__("\n".join(lines))
        self.workspace_id = cfg.id
        self.metadata = details


def _ensure_lock_byte(handle) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)


def _try_lock(handle) -> bool:
    _ensure_lock_byte(handle)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise


def _unlock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_path(workspace_id: str) -> Path:
    return LISTENER_DIR / f"{workspace_id}.lock"


def _metadata_path(workspace_id: str) -> Path:
    return LISTENER_DIR / f"{workspace_id}.json"


def _read_lock_metadata(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes().lstrip(b"\0").decode("utf-8").strip()
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


class ListenerLock:
    def __init__(self, cfg: WorkspaceConfig):
        self.cfg = cfg
        self.path = _lock_path(cfg.id)
        self.metadata_path = _metadata_path(cfg.id)
        self._handle = None

    def acquire(self) -> "ListenerLock":
        LISTENER_DIR.mkdir(parents=True, exist_ok=True)
        with _PROCESS_LOCK:
            if self.cfg.id in _ACTIVE_LISTENERS:
                raise ListenerAlreadyActiveError(self.cfg, _read_lock_metadata(self.metadata_path))
            handle = self.path.open("a+b")
            locked = False
            metadata_tmp: Path | None = None
            try:
                locked = _try_lock(handle)
                if not locked:
                    metadata = _read_lock_metadata(self.metadata_path)
                    raise ListenerAlreadyActiveError(self.cfg, metadata)
                metadata = {
                    "workspace_id": self.cfg.id,
                    "workspace_name": self.cfg.name,
                    "pid": os.getpid(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
                metadata_tmp = self.metadata_path.with_suffix(".tmp")
                metadata_tmp.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
                os.replace(metadata_tmp, self.metadata_path)
            except Exception:
                if locked:
                    _unlock(handle)
                handle.close()
                if metadata_tmp is not None:
                    try:
                        metadata_tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            self._handle = handle
            _ACTIVE_LISTENERS.add(self.cfg.id)
        return self

    def release(self) -> None:
        with _PROCESS_LOCK:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
            try:
                _unlock(handle)
            finally:
                handle.close()
                _ACTIVE_LISTENERS.discard(self.cfg.id)

    def __enter__(self) -> "ListenerLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@contextmanager
def listener_ownership(configs: Sequence[WorkspaceConfig]) -> Iterator[None]:
    ordered = sorted(configs, key=lambda cfg: cfg.id)
    if not ordered:
        raise ValueError("at least one workspace is required")
    if len({cfg.id for cfg in ordered}) != len(ordered):
        raise ValueError("each workspace may be specified only once")
    acquired: list[ListenerLock] = []
    try:
        for cfg in ordered:
            acquired.append(ListenerLock(cfg).acquire())
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


def listener_status(cfg: WorkspaceConfig) -> dict[str, Any]:
    path = _lock_path(cfg.id)
    metadata = _read_lock_metadata(_metadata_path(cfg.id))
    with _PROCESS_LOCK:
        if cfg.id in _ACTIVE_LISTENERS:
            return {"status": "RUNNING", **(metadata or {})}
        if not path.exists():
            return {"status": "STOPPED"}
        with path.open("r+b") as handle:
            if _try_lock(handle):
                _unlock(handle)
                return {"status": "STOPPED"}
    return {"status": "RUNNING", **(metadata or {})}


def _read_jsonl(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.exists():
        return {"status": "MISSING", "rows": 0}, []
    rows: list[dict[str, Any]] = []
    line_number = 0
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("row is not an object")
            rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "INVALID", "rows": len(rows), "line": line_number or None, "error": str(exc)}, []
    return {"status": "OK", "rows": len(rows)}, rows


def _timestamp(row: dict[str, Any], default: float = 0.0) -> float:
    try:
        return float(row.get("created_at", default))
    except (TypeError, ValueError):
        return default


def _gateway_status(host: str, port: int) -> dict[str, Any]:
    endpoint = f"http://{host}:{port}"
    try:
        with urllib.request.urlopen(f"{endpoint}/healthz", timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status_code = response.status
        if status_code == 200 and isinstance(payload, dict) and payload.get("ok") is True:
            return {"status": "HEALTHY", "endpoint": endpoint, "server": payload.get("server")}
        return {"status": "UNHEALTHY", "endpoint": endpoint}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return {"status": "STOPPED", "endpoint": endpoint, "error": str(exc)}


def _mcp_preflight(host: str = "127.0.0.1", port: int = 8765) -> dict[str, Any]:
    """Exercise the local MCP protocol without mutating workspace or mailbox state."""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        return {"status": "FAILED", "code": "MCP_PREFLIGHT_NON_LOOPBACK", "message": "non-loopback Gateway rejected"}
    endpoint = f"http://{host}:{port}/mcp"

    def call(rpc_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or "error" in payload or not isinstance(payload.get("result"), dict):
            raise RuntimeError(f"invalid {method} response")
        return payload["result"]

    try:
        initialized = call(1, "initialize")
        if initialized.get("protocolVersion") != "2025-06-18":
            return {
                "status": "FAILED",
                "code": "VERSION_MISMATCH",
                "endpoint": endpoint,
                "message": f"unsupported MCP protocol: {initialized.get('protocolVersion')}",
            }
        server = initialized.get("serverInfo")
        if not isinstance(server, dict) or server.get("name") != "codex-by-gpt-gateway" or server.get("version") != __version__:
            return {
                "status": "FAILED",
                "code": "VERSION_MISMATCH",
                "endpoint": endpoint,
                "message": "Gateway server metadata does not match the local package",
            }
        listed = call(2, "tools/list")
        tools = listed.get("tools")
        if not isinstance(tools, list):
            return {"status": "FAILED", "code": "MCP_CATALOG_INVALID", "endpoint": endpoint, "message": "tools/list returned no tool list"}
        names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
        if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
            return {"status": "FAILED", "code": "MCP_CATALOG_INVALID", "endpoint": endpoint, "message": "tools/list contains invalid or duplicate tool names"}
        by_name = {tool["name"]: tool for tool in tools}
        missing = sorted(CORE_MCP_TOOLS - set(names))
        if missing:
            return {"status": "FAILED", "code": "ACTION_SET_INCOMPLETE", "endpoint": endpoint, "missing": missing}
        invalid = []
        for name, expected in EXPECTED_MCP_TOOLS.items():
            actual = by_name.get(name)
            if not isinstance(actual, dict) or actual.get("inputSchema") != expected.get("inputSchema") or actual.get("annotations") != expected.get("annotations"):
                invalid.append(name)
        if invalid:
            return {"status": "FAILED", "code": "MCP_CATALOG_INVALID", "endpoint": endpoint, "invalid": sorted(invalid)}
        workspace_result = call(3, "tools/call", {"name": "workspace_list", "arguments": {}})
        content = workspace_result.get("content")
        if not isinstance(content, list) or not content or not isinstance(content[0], dict):
            raise RuntimeError("workspace_list returned no content")
        text = content[0].get("text")
        workspaces = json.loads(text) if isinstance(text, str) else None
        if not isinstance(workspaces, dict) or not isinstance(workspaces.get("workspaces"), list):
            raise RuntimeError("workspace_list returned invalid data")
        return {
            "status": "READY",
            "endpoint": endpoint,
            "protocolVersion": initialized.get("protocolVersion"),
            "server": server,
            "toolNames": sorted(names),
            "workspaceCount": len(workspaces["workspaces"]),
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError, RuntimeError, TypeError, ValueError) as exc:
        return {"status": "FAILED", "code": "MCP_CALL_FAILED", "endpoint": endpoint, "message": str(exc)}


def _tunnel_process_running() -> bool:
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq tunnel-client.exe", "/FO", "CSV", "/NH"],
                text=True,
                capture_output=True,
                timeout=2,
            )
            return completed.returncode == 0 and "tunnel-client.exe" in completed.stdout.lower()
        pgrep = shutil.which("pgrep")
        if pgrep:
            return subprocess.run([pgrep, "-x", "tunnel-client"], capture_output=True, timeout=2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def _tunnel_executable() -> str | None:
    configured = os.environ.get("C2C_TUNNEL_CLIENT")
    if configured:
        path = Path(configured).expanduser()
        return str(path.resolve()) if path.is_file() else None
    return shutil.which("tunnel-client")


def _redact_probe_body(value: str) -> str:
    redacted = value
    for key, secret in os.environ.items():
        if secret and len(secret) >= 8 and re.search(r"(?i)(api|key|token|secret|password|auth)", key):
            redacted = redacted.replace(secret, "<redacted>")
    return _redact_execution_output(redacted)


def _probe_tunnel_endpoint_detailed(url: str, max_body_chars: int = MAX_TUNNEL_PROBE_BODY_CHARS) -> tuple[int | None, str | None, str | None]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None, None, "non-loopback URL rejected"
    limit = max(1, int(max_body_chars))

    def read_body(response) -> str:
        raw = response.read(limit + 1)
        body = raw.decode("utf-8", errors="replace")
        if len(body) > limit:
            body = body[:limit] + "...<truncated>"
        return _redact_probe_body(body)

    try:
        with urllib.request.urlopen(url, timeout=1.0) as response:
            return response.status, read_body(response), None
    except urllib.error.HTTPError as exc:
        try:
            body = read_body(exc)
        except OSError:
            body = None
        return exc.code, body, None
    except (OSError, urllib.error.URLError) as exc:
        return None, None, str(exc)


def _probe_tunnel_endpoint(url: str) -> tuple[int | None, str | None]:
    status, _body, error = _probe_tunnel_endpoint_detailed(url)
    return status, error


def _tunnel_status(profile: str, health_url: str | None = None) -> dict[str, Any]:
    executable = _tunnel_executable()
    running = _tunnel_process_running()
    health_url = (health_url or os.environ.get("C2C_TUNNEL_HEALTH_URL") or DEFAULT_TUNNEL_HEALTH_URL).rstrip("/")
    result: dict[str, Any] = {
        "status": "UNAVAILABLE" if not executable and not running else "STOPPED" if not running else "STARTING",
        "profile": profile,
        "executable": executable,
        "healthUrl": health_url,
        "healthz": None,
        "readyz": None,
    }
    if not running:
        return result

    healthz, health_error = _probe_tunnel_endpoint(f"{health_url}/healthz")
    readyz, ready_error = _probe_tunnel_endpoint(f"{health_url}/readyz") if healthz == 200 else (None, None)
    result["healthz"] = healthz
    result["readyz"] = readyz
    if health_error or ready_error:
        result["error"] = health_error or ready_error
    if healthz == 200 and readyz == 200:
        result["status"] = "READY"
    elif healthz == 200:
        result["status"] = "NOT_READY"
    else:
        result["status"] = "STARTING"
    return result


def collect_status(host: str = "127.0.0.1", port: int = 8765, profile: str = "codex-by-gpt") -> dict[str, Any]:
    state_files: dict[str, dict[str, Any]] = {}
    rows: dict[str, list[dict[str, Any]]] = {}
    for name, path in {
        "mailbox.jsonl": MAILBOX_FILE,
        "executions.jsonl": EXECUTIONS_FILE,
        "execution_claims.jsonl": EXECUTION_CLAIMS_FILE,
    }.items():
        state_files[name], rows[name] = _read_jsonl(path)

    config_state: dict[str, Any] = {"status": "OK"}
    try:
        configs = list_workspaces()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError, TypeError, KeyError, ValueError) as exc:
        configs = []
        config_state = {"status": "INVALID", "path": str(STATE_FILE), "error": str(exc)}

    now = time.time()
    workspace_rows = []
    for cfg in configs:
        pending = [
            row
            for row in rows["mailbox.jsonl"]
            if row.get("workspace_id") == cfg.id
            and not row.get("acked")
            and row.get("kind") in {"PLAN", "REVIEW"}
        ]
        claims = [row for row in rows["execution_claims.jsonl"] if row.get("workspace_id") == cfg.id]
        executions = [row for row in rows["executions.jsonl"] if row.get("workspace_id") == cfg.id]
        latest = max(executions, key=_timestamp, default=None)
        last_execution = None
        if latest:
            test_status = latest.get("test_status")
            last_execution = {
                "state": latest.get("state"),
                "testStatus": test_status.get("status") if isinstance(test_status, dict) else None,
                "exitCode": latest.get("codex_exit_code"),
                "createdAt": latest.get("created_at"),
            }
        ages = [max(0.0, now - _timestamp(row, now)) for row in claims]
        workspace_rows.append(
            {
                "workspaceId": cfg.id,
                "name": cfg.name,
                "root": cfg.root,
                "rootValid": Path(cfg.root).is_dir(),
                "listener": listener_status(cfg),
                "pending": len(pending),
                "claims": len(claims),
                "oldestClaimAgeSeconds": round(max(ages), 1) if ages else None,
                "lastExecution": last_execution,
            }
        )
    result = {
        "version": __version__,
        "gateway": _gateway_status(host, port),
        "tunnel": _tunnel_status(profile),
        "workspaces": workspace_rows,
        "state": {"machine.json": config_state, **state_files},
    }
    # Service Manager is additive; manual mode remains unchanged when no config exists.
    try:
        from .service import service_status
        service = service_status()
        if service.get("configured"):
            result["service"] = service
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return result


def diagnose(status: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def add(severity: str, code: str, message: str) -> None:
        issues.append({"severity": severity, "code": code, "message": message})

    if not shutil.which("git"):
        add("ERROR", "GIT_NOT_FOUND", "git executable was not found")
    gateway = status["gateway"]
    if gateway["status"] != "HEALTHY":
        add("ERROR", "GATEWAY_UNAVAILABLE", f"gateway is {gateway['status'].lower()} at {gateway['endpoint']}")
    elif (gateway.get("server") or {}).get("version") not in {None, status["version"]}:
        add(
            "WARNING",
            "GATEWAY_VERSION_MISMATCH",
            f"gateway reports {(gateway.get('server') or {}).get('version')} but local package is {status['version']}",
        )
    mcp = status.get("mcpPreflight")
    if mcp and mcp.get("status") != "READY":
        code = str(mcp.get("code") or "MCP_PREFLIGHT_FAILED")
        message = str(mcp.get("message") or code)
        if mcp.get("missing"):
            message = f"missing MCP actions: {', '.join(mcp['missing'])}"
        add("ERROR", code, message)
    elif mcp and mcp.get("status") == "READY":
        add("INFO", "CLIENT_ACTIONS_UNVERIFIED", "local MCP is ready; the current ChatGPT session still requires connector refresh or a new chat to verify action mounting")
    tunnel = status["tunnel"]
    if tunnel["status"] == "UNAVAILABLE":
        add("ERROR", "TUNNEL_CLIENT_NOT_FOUND", "tunnel-client is not running and its executable was not found")
    elif tunnel["status"] == "STOPPED":
        add("ERROR", "TUNNEL_STOPPED", f"tunnel profile {tunnel['profile']} is not running")
    elif tunnel["status"] in {"STARTING", "NOT_READY"}:
        add(
            "ERROR",
            "TUNNEL_NOT_READY",
            f"tunnel profile {tunnel['profile']} is {tunnel['status'].lower()} at {tunnel.get('healthUrl', DEFAULT_TUNNEL_HEALTH_URL)}",
        )
    elif not tunnel.get("executable"):
        add("WARNING", "TUNNEL_PATH_UNKNOWN", "tunnel-client is running but its executable is not on PATH or C2C_TUNNEL_CLIENT")

    for name, file_status in status["state"].items():
        if file_status["status"] == "INVALID":
            add("ERROR", "STATE_INVALID", f"{name} cannot be parsed: {file_status.get('error', 'invalid data')}")

    for workspace in status["workspaces"]:
        if not workspace["rootValid"]:
            add("ERROR", "WORKSPACE_ROOT_INVALID", f"workspace {workspace['name']} path is not a directory")
        if workspace["claims"]:
            listener_running = workspace["listener"]["status"] == "RUNNING"
            age = workspace["oldestClaimAgeSeconds"] or 0
            if not listener_running or age > CLAIM_STALE_SECONDS:
                add(
                    "WARNING",
                    "ABNORMAL_CLAIM",
                    f"workspace {workspace['name']} has {workspace['claims']} claim(s) requiring listener recovery",
                )
    service = status.get("service")
    if service and service.get("configured"):
        if service.get("status") == "INVALID":
            add("ERROR", "SERVICE_CONFIG_INVALID", service.get("error", "service configuration is invalid"))
        elif service.get("status") == "STOPPED":
            add("ERROR", "SUPERVISOR_STOPPED", "service is configured but supervisor is not running")
        elif service.get("status") == "FAILED":
            add("ERROR", "SERVICE_COMPONENT_FAILED", "a managed service component has failed")
        runtime = service.get("runtime") or {}
        pid = runtime.get("supervisor_pid")
        if runtime.get("status") == "RUNNING" and pid:
            try:
                from .service import _pid_alive
                alive = _pid_alive(pid)
            except (OSError, ValueError, TypeError):
                alive = False
            if not alive:
                add("ERROR", "SUPERVISOR_STALE", "service runtime points to a stopped supervisor")
    return issues


def doctor_report(host: str = "127.0.0.1", port: int = 8765, profile: str = "codex-by-gpt") -> dict[str, Any]:
    status = collect_status(host, port, profile)
    status["mcpPreflight"] = _mcp_preflight(host, port)
    issues = diagnose(status)
    return {
        "ok": not any(issue["severity"] == "ERROR" for issue in issues),
        "environment": {
            "python": sys.version.split()[0],
            "git": shutil.which("git"),
            "tunnelClient": _tunnel_executable(),
            "stateHome": str(APP_DIR),
        },
        "issues": issues,
        "runtime": status,
    }
