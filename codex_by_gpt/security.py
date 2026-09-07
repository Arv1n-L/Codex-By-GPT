from __future__ import annotations

from pathlib import Path

SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", ".npmrc", ".pypirc",
    "id_rsa", "id_ed25519", "credentials", "credentials.json",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".idea", ".vscode"}

class SecurityError(RuntimeError):
    pass


def resolve_under(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise SecurityError("Path escapes workspace root") from exc
    return candidate


def assert_readable(path: Path) -> None:
    lower = path.name.lower()
    if lower in SENSITIVE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
        raise SecurityError(f"Sensitive file is blocked: {path.name}")
    for part in path.parts:
        if part in SKIP_DIRS:
            raise SecurityError(f"Excluded directory is blocked: {part}")
