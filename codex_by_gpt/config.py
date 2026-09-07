from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

APP_DIR = Path(os.environ.get("C2C_HOME", Path.home() / ".codex-by-gpt"))
STATE_FILE = APP_DIR / "machine.json"
MAILBOX_FILE = APP_DIR / "mailbox.jsonl"
EXECUTIONS_FILE = APP_DIR / "executions.jsonl"

@dataclass(frozen=True)
class WorkspaceConfig:
    id: str
    name: str
    root: str


def _default_state() -> dict[str, Any]:
    return {"version": 1, "workspaces": {}}


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return _default_state()
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise RuntimeError(f"Unsupported state file: {STATE_FILE}")
    data.setdefault("workspaces", {})
    return data


def save_state(state: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def workspace_id(root: Path) -> str:
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"ws_{digest}"


def add_workspace(root: str, name: str | None = None) -> WorkspaceConfig:
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Workspace is not a directory: {path}")
    cfg = WorkspaceConfig(id=workspace_id(path), name=name or path.name, root=str(path))
    state = load_state()
    state["workspaces"][cfg.id] = asdict(cfg)
    save_state(state)
    return cfg


def remove_workspace(identifier: str) -> bool:
    state = load_state()
    workspaces = state["workspaces"]
    target = identifier if identifier in workspaces else next((k for k, v in workspaces.items() if v["name"] == identifier), None)
    if not target:
        return False
    del workspaces[target]
    save_state(state)
    return True


def list_workspaces() -> list[WorkspaceConfig]:
    state = load_state()
    return [WorkspaceConfig(**v) for _, v in sorted(state["workspaces"].items())]


def get_workspace(identifier: str) -> WorkspaceConfig:
    state = load_state()
    workspaces = state["workspaces"]
    if identifier in workspaces:
        return WorkspaceConfig(**workspaces[identifier])
    matches = [WorkspaceConfig(**v) for v in workspaces.values() if v["name"] == identifier]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise KeyError(f"Unknown workspace: {identifier}")
    raise KeyError(f"Ambiguous workspace name: {identifier}; use workspace_id")
