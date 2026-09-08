from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config
from .config import WorkspaceConfig, get_workspace
from .runtime import (
    DEFAULT_TUNNEL_HEALTH_URL,
    _mcp_preflight,
    _gateway_status,
    _probe_tunnel_endpoint_detailed,
    _tunnel_executable,
    _tunnel_process_running,
    listener_status,
)
from .mailbox import _redact_execution_output

DEFAULT_RESTART = {"enabled": True, "initial_delay_seconds": 1.0, "max_delay_seconds": 30.0, "failure_window_seconds": 60.0, "max_failures": 5}
DEFAULT_LOGS = {"max_bytes": 5 * 1024 * 1024, "backup_count": 3}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel.GetExitCodeProcess.restype = ctypes.c_int
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000 | 0x00100000, 0, int(pid))  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
            if not handle:
                return False
            try:
                code = ctypes.c_uint32()
                return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
            finally:
                kernel.CloseHandle(handle)
        except (OSError, AttributeError, TypeError, ValueError):
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _lock(handle, blocking: bool) -> bool:
    if os.name == "nt":
        import msvcrt
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
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
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SupervisorLock:
    def __init__(self, path: Path | None = None):
        self.path = path or config.SERVICE_LOCK_FILE
        self.handle = None

    def acquire(self, blocking: bool = False) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        if not _lock(self.handle, blocking):
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.handle is not None:
            try:
                _unlock(self.handle)
            finally:
                self.handle.close()
                self.handle = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Service supervisor is already running")
        return self

    def __exit__(self, *_args):
        self.release()


@dataclass
class ServiceConfig:
    tunnel_id: str
    workspace_ids: list[str]
    api_key_env: str = "chatgpt-apikey"
    tunnel_client: str | None = None
    profile: str = "codex-by-gpt"
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8765
    tunnel_health_url: str = DEFAULT_TUNNEL_HEALTH_URL
    poll_interval: float = 1.0
    restart: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_RESTART))
    logs: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LOGS))
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "gateway": {"host": self.gateway_host, "port": self.gateway_port},
            "tunnel": {"tunnel_id": self.tunnel_id, "profile": self.profile, "executable": self.tunnel_client, "api_key_env": self.api_key_env, "health_url": self.tunnel_health_url},
            "listener": {"workspace_ids": list(self.workspace_ids), "poll_interval": self.poll_interval},
            "restart": dict(self.restart),
            "logs": dict(self.logs),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ServiceConfig":
        if not isinstance(value, dict) or value.get("version", 1) != 1:
            raise ValueError("Unsupported service configuration version")
        gateway = value.get("gateway") or {}
        tunnel = value.get("tunnel") or {}
        listener = value.get("listener") or {}
        cfg = cls(
            tunnel_id=str(tunnel.get("tunnel_id", "")),
            workspace_ids=[str(x) for x in listener.get("workspace_ids", [])],
            api_key_env=str(tunnel.get("api_key_env", "chatgpt-apikey")),
            tunnel_client=tunnel.get("executable"),
            profile=str(tunnel.get("profile", "codex-by-gpt")),
            gateway_host=str(gateway.get("host", "127.0.0.1")),
            gateway_port=int(gateway.get("port", 8765)),
            tunnel_health_url=str(tunnel.get("health_url", DEFAULT_TUNNEL_HEALTH_URL)).rstrip("/"),
            poll_interval=float(listener.get("poll_interval", 1.0)),
            restart={**DEFAULT_RESTART, **(value.get("restart") or {})},
            logs={**DEFAULT_LOGS, **(value.get("logs") or {})},
            version=1,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.tunnel_id or any(ch.isspace() for ch in self.tunnel_id):
            raise ValueError("tunnel_id is required")
        if not self.workspace_ids:
            raise ValueError("at least one workspace is required")
        if len(set(self.workspace_ids)) != len(self.workspace_ids):
            raise ValueError("each workspace may be specified only once")
        if not _ENV_NAME.fullmatch(self.api_key_env):
            raise ValueError("api_key_env must be a valid environment variable name")
        if self.gateway_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("gateway must bind to loopback")
        if self.gateway_port < 1 or self.gateway_port > 65535 or self.poll_interval <= 0:
            raise ValueError("invalid gateway port or poll interval")
        if self.tunnel_client and not Path(self.tunnel_client).is_file():
            raise ValueError(f"tunnel-client executable was not found: {self.tunnel_client}")
        for key in ("initial_delay_seconds", "max_delay_seconds", "failure_window_seconds"):
            if float(self.restart[key]) <= 0:
                raise ValueError(f"restart.{key} must be > 0")
        if int(self.restart["max_failures"]) < 1:
            raise ValueError("restart.max_failures must be > 0")


def load_service_config() -> ServiceConfig:
    value = config.load_json_object(config.SERVICE_FILE)
    if value is None:
        raise FileNotFoundError(f"Service is not configured: {config.SERVICE_FILE}")
    return ServiceConfig.from_dict(value)


def save_service_config(service_config: ServiceConfig) -> None:
    service_config.validate()
    config.save_json_atomic(config.SERVICE_FILE, service_config.to_dict())


def configure_service(
    tunnel_id: str,
    workspaces: Iterable[str],
    api_key_env: str = "chatgpt-apikey",
    tunnel_client: str | None = None,
    profile: str = "codex-by-gpt",
    gateway_host: str = "127.0.0.1",
    gateway_port: int = 8765,
    tunnel_health_url: str = DEFAULT_TUNNEL_HEALTH_URL,
    poll_interval: float = 1.0,
) -> ServiceConfig:
    selected: list[WorkspaceConfig] = []
    seen: set[str] = set()
    for identifier in workspaces:
        cfg = get_workspace(identifier)
        if cfg.id in seen:
            raise ValueError(f"each workspace may be specified only once: {identifier}")
        seen.add(cfg.id)
        selected.append(cfg)
    executable = str(Path(tunnel_client).expanduser().resolve()) if tunnel_client else _tunnel_executable()
    if not executable:
        raise ValueError("tunnel-client executable was not found; pass --tunnel-client")
    result = ServiceConfig(tunnel_id=tunnel_id, workspace_ids=[cfg.id for cfg in selected], api_key_env=api_key_env, tunnel_client=executable, profile=profile, gateway_host=gateway_host, gateway_port=gateway_port, tunnel_health_url=tunnel_health_url.rstrip("/"), poll_interval=poll_interval)
    save_service_config(result)
    return result


def _runtime_value() -> dict[str, Any] | None:
    try:
        return config.load_json_object(config.SERVICE_RUNTIME_FILE)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "INVALID"}


def _supervisor_lock_held() -> bool:
    """Return whether another process currently owns the OS supervisor lock."""
    probe = SupervisorLock()
    try:
        if probe.acquire():
            probe.release()
            return False
        return True
    except OSError:
        probe.release()
        return False


def _write_runtime(value: dict[str, Any]) -> None:
    config.save_json_atomic(config.SERVICE_RUNTIME_FILE, value)


def _read_control() -> dict[str, Any] | None:
    try:
        return config.load_json_object(config.SERVICE_CONTROL_FILE)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _write_control(value: dict[str, Any]) -> None:
    config.save_json_atomic(config.SERVICE_CONTROL_FILE, value)


def _clear_control() -> None:
    try:
        config.SERVICE_CONTROL_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _child_status(name: str, state: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, **state}


def service_status() -> dict[str, Any]:
    configured = config.SERVICE_FILE.exists()
    result: dict[str, Any] = {"configured": configured, "status": "STOPPED", "runtime": None, "children": [], "workspaceIds": []}
    if not configured:
        return result
    try:
        cfg = load_service_config()
        result["workspaceIds"] = list(cfg.workspace_ids)
    except Exception as exc:
        result.update({"status": "INVALID", "error": str(exc)})
        return result
    runtime = _runtime_value()
    result["runtime"] = runtime
    lock_held = _supervisor_lock_held()
    result["ownership"] = "MANAGED" if lock_held else "NONE"
    if runtime and runtime.get("status") in {"STARTING", "RUNNING", "STOPPING", "FAILED"} and lock_held and _pid_alive(runtime.get("supervisor_pid")):
        result["status"] = runtime.get("status")
        result["supervisorPid"] = runtime.get("supervisor_pid")
        result["runId"] = runtime.get("run_id")
        result["heartbeat"] = runtime.get("heartbeat")
        result["children"] = [_child_status(k, v) for k, v in (runtime.get("children") or {}).items()]
    elif runtime and runtime.get("status") == "FAILED":
        result["status"] = "FAILED"
        if lock_held and _pid_alive(runtime.get("supervisor_pid")):
            result["supervisorPid"] = runtime.get("supervisor_pid")
            result["runId"] = runtime.get("run_id")
            result["heartbeat"] = runtime.get("heartbeat")
        result["children"] = [_child_status(k, v) for k, v in (runtime.get("children") or {}).items()]
    return result


def start_service(timeout: float = 8.0) -> dict[str, Any]:
    cfg = load_service_config()
    if not os.environ.get(cfg.api_key_env):
        raise RuntimeError(f"configured API-key environment variable is missing: {cfg.api_key_env}")
    current = service_status()
    if current.get("status") in {"STARTING", "RUNNING", "STOPPING", "FAILED"} and current.get("supervisorPid"):
        return current
    command = [sys.executable, "-m", "codex_by_gpt.cli", "service", "run"]
    kwargs: dict[str, Any] = {"cwd": str(Path(__file__).resolve().parents[1]), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    subprocess.Popen(command, **kwargs)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service_status()
        if state.get("status") in {"RUNNING", "FAILED"}:
            return state
        time.sleep(0.1)
    return service_status()


def stop_service(timeout: float = 10.0) -> dict[str, Any]:
    state = service_status()
    runtime = state.get("runtime") or {}
    run_id = runtime.get("run_id")
    if state.get("status") not in {"STARTING", "RUNNING", "STOPPING", "FAILED"} or not run_id:
        return state
    _write_control({"action": "stop", "run_id": run_id, "requested_at": _now()})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service_status()
        if state.get("status") not in {"STARTING", "RUNNING", "STOPPING", "FAILED"} or not state.get("supervisorPid"):
            return state
        time.sleep(0.1)
    return service_status()


def restart_service() -> dict[str, Any]:
    stop_service()
    return start_service()


def read_logs(component: str = "supervisor", lines: int = 100, follow: bool = False):
    if component not in {"supervisor", "gateway", "tunnel", "listener"}:
        raise ValueError("unknown service log component")
    path = config.LOG_DIR / f"{component}.log"
    lines = max(1, int(lines))
    if not follow:
        if not path.exists():
            return ""
        return "".join(deque(path.read_text(encoding="utf-8", errors="replace").splitlines(True), maxlen=lines))
    def follow_lines():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+", encoding="utf-8") as handle:
                handle.seek(0, os.SEEK_END)
                while True:
                    line = handle.readline()
                    if line:
                        yield line
                    else:
                        time.sleep(0.2)
        except KeyboardInterrupt:
            return
    return follow_lines()


class WindowsJobObject:
    """Small stdlib-only wrapper; a no-op on non-Windows hosts."""
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel = kernel
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel.CreateJobObjectW.restype = ctypes.c_void_p
        kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel.AssignProcessToJobObject.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32), ("Affinity", ctypes.c_void_p), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]
        class IoCounters(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong), ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong), ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong)]
        class LimitInfo(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimitInformation), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        info = LimitInfo()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        kernel.SetInformationJobObject.restype = ctypes.c_int
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel.CloseHandle(self.handle); self.handle = None
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, process: subprocess.Popen) -> None:
        if self.handle and not self._kernel.AssignProcessToJobObject(self.handle, process._handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def close(self) -> None:
        if self.handle:
            self._kernel.CloseHandle(self.handle)
            self.handle = None


@dataclass
class _Child:
    name: str
    command: list[str]
    process: subprocess.Popen | None = None
    restarts: int = 0
    failures: list[float] = field(default_factory=list)
    last_exit: int | None = None
    next_restart: float = 0.0
    state: str = "STOPPED"
    last_transition: str | None = None


class ServiceSupervisor:
    def __init__(self, service_config: ServiceConfig | None = None):
        self.config = service_config or load_service_config()
        self.config.validate()
        self.run_id = uuid.uuid4().hex
        self.lock = SupervisorLock()
        self.job = WindowsJobObject()
        self.stop_requested = False
        self.children: dict[str, _Child] = {}
        self._threads: list[threading.Thread] = []
        self._last_state_write = 0.0
        self._final_status = "STOPPED"

    def _log(self, message: str) -> None:
        _append_log("supervisor", message, self.config.logs)

    def _state(self, status: str) -> dict[str, Any]:
        children = {}
        for name, child in self.children.items():
            children[name] = {"state": child.state, "pid": child.process.pid if child.process else None, "restart_count": child.restarts, "last_exit_code": child.last_exit, "last_transition": child.last_transition}
        return {"version": 1, "status": status, "run_id": self.run_id, "supervisor_pid": os.getpid(), "heartbeat": _now(), "children": children, "workspace_ids": list(self.config.workspace_ids)}

    def _persist(self, status: str) -> None:
        _write_runtime(self._state(status))

    def _overall_status(self) -> str:
        return "FAILED" if any(child.state == "FAILED" for child in self.children.values()) else "RUNNING"

    def _preflight(self) -> list[WorkspaceConfig]:
        if not os.environ.get(self.config.api_key_env):
            raise RuntimeError(f"configured API-key environment variable is missing: {self.config.api_key_env}")
        if not self.config.tunnel_client or not Path(self.config.tunnel_client).is_file():
            raise RuntimeError("tunnel-client executable was not found")
        configs = [get_workspace(identifier) for identifier in self.config.workspace_ids]
        for cfg in configs:
            if not Path(cfg.root).is_dir():
                raise RuntimeError(f"workspace root is not a directory: {cfg.root}")
            if listener_status(cfg).get("status") == "RUNNING":
                raise RuntimeError(f"LISTENER_ALREADY_ACTIVE: {cfg.name}")
        if _gateway_status(self.config.gateway_host, self.config.gateway_port).get("status") == "HEALTHY":
            raise RuntimeError("EXTERNAL_GATEWAY_RUNNING: configured endpoint is already healthy")
        if _tunnel_process_running():
            raise RuntimeError("EXTERNAL_TUNNEL_RUNNING: tunnel-client is already running")
        return configs

    def _specs(self, configs: list[WorkspaceConfig]) -> dict[str, list[str]]:
        python = sys.executable
        return {
            "gateway": [python, "-m", "codex_by_gpt.cli", "gateway", "serve", "--host", self.config.gateway_host, "--port", str(self.config.gateway_port)],
            "tunnel": [self.config.tunnel_client or "tunnel-client", "run", "--control-plane.api-key", f"env:{self.config.api_key_env}", "--control-plane.tunnel-id", self.config.tunnel_id, "--mcp.server-url", f"http://{self.config.gateway_host}:{self.config.gateway_port}/mcp"],
            "listener": [python, "-m", "codex_by_gpt.cli", "codex", "listen", *sum((["--workspace", cfg.id] for cfg in configs), []), "--poll-interval", str(self.config.poll_interval)],
        }

    def _spawn(self, child: _Child) -> None:
        kwargs: dict[str, Any] = {"cwd": str(Path(__file__).resolve().parents[1]), "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True, "bufsize": 1, "shell": False, "close_fds": os.name != "nt"}
        child_env = os.environ.copy()
        if child.name != "tunnel":
            for key in list(child_env):
                if key.lower() == self.config.api_key_env.lower():
                    child_env.pop(key, None)
        kwargs["env"] = child_env
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        child.process = subprocess.Popen(child.command, **kwargs)
        try:
            self.job.assign(child.process)
        except Exception:
            self._terminate(child)
            child.process = None
            self._log(f"job assignment failed component={child.name}")
            raise
        child.state = "STARTING"
        child.last_transition = _now()
        self._log(f"start component={child.name} pid={child.process.pid}")
        thread = threading.Thread(target=self._pump, args=(child,), daemon=True)
        thread.start(); self._threads.append(thread)
        self._persist("STARTING")

    def _pump(self, child: _Child) -> None:
        assert child.process is not None and child.process.stdout is not None
        for line in child.process.stdout:
            text = line.rstrip("\r\n")
            secret = os.environ.get(self.config.api_key_env)
            if secret:
                text = text.replace(secret, "<redacted>")
            _append_log(child.name, _redact_execution_output(text), self.config.logs)

    def _stop_controlled(self) -> bool:
        control = _read_control()
        return bool(control and control.get("run_id") == self.run_id and control.get("action") == "stop")

    def _wait_gateway(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.stop_requested:
            if self._stop_controlled():
                self.stop_requested = True
                return False
            if _gateway_status(self.config.gateway_host, self.config.gateway_port).get("status") == "HEALTHY": return True
            time.sleep(0.2)
        return False

    def _wait_tunnel(self, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        url = self.config.tunnel_health_url.rstrip("/")
        last_probe: dict[str, Any] = {"healthz": None, "readyz": None, "reason": "no probe completed"}
        while time.monotonic() < deadline and not self.stop_requested:
            if self._stop_controlled():
                self.stop_requested = True
                return False
            health, health_body, health_error = _probe_tunnel_endpoint_detailed(url + "/healthz")
            ready, ready_body, ready_error = _probe_tunnel_endpoint_detailed(url + "/readyz") if health == 200 else (None, None, None)
            last_probe = {
                "healthz": health,
                "readyz": ready,
                "reason": ready_body or ready_error or health_body or health_error or "no response body",
            }
            if health == 200 and ready == 200: return True
            time.sleep(0.5)
        if not self.stop_requested:
            reason = _redact_execution_output(str(last_probe["reason"]))
            secret = os.environ.get(self.config.api_key_env)
            if secret:
                reason = reason.replace(secret, "<redacted>")
            reason = reason.replace("\r", " ").replace("\n", " ")[:2048]
            self._log(f"tunnel readiness timeout healthz={last_probe['healthz']} readyz={last_probe['readyz']} reason={reason}")
        return False

    def _terminate(self, child: _Child) -> None:
        process = child.process
        if not process or process.poll() is not None: return
        try: process.terminate()
        except OSError: return
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: process.kill(); process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired): pass

    def _shutdown(self, status: str = "STOPPED") -> None:
        self.stop_requested = True
        self._final_status = status
        for name in ("listener", "tunnel", "gateway"):
            child = self.children.get(name)
            if child: self._terminate(child); child.state = "STOPPED"; child.last_transition = _now()
        self._persist("STOPPING" if status == "STOPPED" else status)

    def _install_signal_handlers(self):
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig:
                try: signal.signal(sig, lambda *_: setattr(self, "stop_requested", True))
                except (ValueError, OSError): pass

    def run(self) -> int:
        configs: list[WorkspaceConfig] = []
        if not self.lock.acquire():
            self._log("already running")
            return 1
        try:
            self._install_signal_handlers()
            try:
                configs = self._preflight()
                specs = self._specs(configs)
                self.children = {name: _Child(name, command) for name, command in specs.items()}
                _clear_control()
                self._persist("STARTING")
                self._spawn(self.children["gateway"])
                if not self._wait_gateway(): raise RuntimeError("gateway did not become HEALTHY")
                self.children["gateway"].state = "RUNNING"
                mcp = _mcp_preflight(self.config.gateway_host, self.config.gateway_port)
                if mcp.get("status") != "READY":
                    code = mcp.get("code", "MCP_PREFLIGHT_FAILED")
                    raise RuntimeError(f"{code}: {mcp.get('message', 'local MCP preflight failed')}")
                self._spawn(self.children["tunnel"])
                if not self._wait_tunnel(): raise RuntimeError("tunnel did not become READY")
                self.children["tunnel"].state = "RUNNING"
                self._spawn(self.children["listener"])
                self.children["listener"].state = "RUNNING"
                self._persist("RUNNING")
                self._log("supervisor running")
            except Exception as exc:
                self._log(f"startup failed: {exc}")
                self._shutdown("STOPPED" if self.stop_requested else "FAILED")
                return 1
            while not self.stop_requested:
                control = _read_control()
                if control and control.get("action") == "stop" and control.get("run_id") == self.run_id:
                    self._log("stop requested")
                    break
                now = time.monotonic()
                for child in self.children.values():
                    if child.process is None:
                        if child.state == "BACKOFF" and now >= child.next_restart:
                            child.next_restart = 0.0
                            child.restarts += 1
                            self._spawn(child)
                            ready = child.name == "gateway" and self._wait_gateway() or child.name == "tunnel" and self._wait_tunnel() or child.name == "listener"
                            if not ready:
                                self._terminate(child)
                                child.process = None
                                failure_now = time.monotonic()
                                child.failures = [stamp for stamp in child.failures if failure_now - stamp <= float(self.config.restart["failure_window_seconds"])]
                                child.failures.append(failure_now)
                                child.state = "FAILED" if not self.config.restart.get("enabled", True) or len(child.failures) >= int(self.config.restart["max_failures"]) else "BACKOFF"
                                delay = min(float(self.config.restart["max_delay_seconds"]), float(self.config.restart["initial_delay_seconds"]) * (2 ** child.restarts))
                                child.next_restart = failure_now + delay
                            else:
                                child.state = "RUNNING"
                            self._persist(self._overall_status())
                        continue
                    code = child.process.poll()
                    if code is None: continue
                    if self.stop_requested: break
                    # Consume this exit once; the restart scheduler owns the next spawn.
                    child.process = None
                    child.last_exit = code
                    child.failures = [stamp for stamp in child.failures if now - stamp <= float(self.config.restart["failure_window_seconds"])]
                    child.failures.append(now)
                    child.state = "FAILED" if not self.config.restart.get("enabled", True) or len(child.failures) >= int(self.config.restart["max_failures"]) else "BACKOFF"
                    child.last_transition = _now()
                    self._log(f"exit component={child.name} code={code} restart_count={child.restarts}")
                    if child.state == "FAILED" or not self.config.restart.get("enabled", True):
                        self._persist("FAILED")
                        continue
                    delay = min(float(self.config.restart["max_delay_seconds"]), float(self.config.restart["initial_delay_seconds"]) * (2 ** child.restarts))
                    child.next_restart = now + delay
                    self._persist("RUNNING")
                if time.monotonic() - self._last_state_write > 1:
                    self._persist(self._overall_status())
                    self._last_state_write = time.monotonic()
                time.sleep(0.2)
            self._shutdown()
            return 0
        finally:
            self.job.close()
            self.lock.release()
            self._persist(self._final_status)


def _append_log(component: str, message: str, settings: dict[str, Any]) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = config.LOG_DIR / f"{component}.log"
    text = str(message).replace("\x00", "") + "\n"
    try:
        if path.exists() and path.stat().st_size + len(text.encode("utf-8")) > int(settings.get("max_bytes", DEFAULT_LOGS["max_bytes"])):
            backups = int(settings.get("backup_count", DEFAULT_LOGS["backup_count"]))
            for index in range(backups, 0, -1):
                src = path.with_name(f"{path.name}.{index - 1}") if index > 1 else path
                dst = path.with_name(f"{path.name}.{index}")
                if src.exists():
                    src.replace(dst)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        pass


def run_service() -> int:
    return ServiceSupervisor().run()
