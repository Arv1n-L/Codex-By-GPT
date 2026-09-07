from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .config import WorkspaceConfig
from .security import SKIP_DIRS, assert_readable, resolve_under

MAX_FILE_BYTES = 512 * 1024
MAX_SEARCH_MATCHES = 200
MAX_DIFF_BYTES = 512 * 1024

class Workspace:
    def __init__(self, cfg: WorkspaceConfig):
        self.cfg = cfg
        self.root = Path(cfg.root).resolve()

    def info(self) -> dict[str, Any]:
        git = self._git(["rev-parse", "--is-inside-work-tree"], check=False)
        branch_cp = self._git(["branch", "--show-current"], check=False)
        commit_cp = self._git(["rev-parse", "HEAD"], check=False)
        status_cp = self._git(["status", "--porcelain"], check=False)
        branch = branch_cp.stdout.strip() or None if git.returncode == 0 else None
        commit = commit_cp.stdout.strip() or None if git.returncode == 0 else None
        status = status_cp.stdout if git.returncode == 0 else ""
        return {
            "workspaceId": self.cfg.id,
            "workspaceName": self.cfg.name,
            "rootAlias": "workspace:/",
            "git": {"isRepo": git.returncode == 0, "branch": branch, "commit": commit, "dirty": bool(status.strip())},
        }

    def list_directory(self, relative: str = ".", depth: int = 1, limit: int = 200, offset: int = 0) -> dict[str, Any]:
        base = resolve_under(self.root, relative)
        if not base.is_dir():
            raise FileNotFoundError(relative)
        entries: list[dict[str, Any]] = []
        base_depth = len(base.parts)
        for current, dirs, files in os.walk(base):
            cur = Path(current)
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            level = len(cur.parts) - base_depth
            if level >= depth:
                dirs[:] = []
            for d in dirs:
                p = cur / d
                entries.append({"path": p.relative_to(self.root).as_posix(), "type": "dir"})
            for f in sorted(files):
                p = cur / f
                try:
                    assert_readable(p)
                except Exception:
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                entries.append({"path": p.relative_to(self.root).as_posix(), "type": "file", "sizeBytes": size})
            if len(entries) > offset + limit + 100:
                break
        entries.sort(key=lambda x: (x["path"].count("/"), x["path"]))
        total = len(entries)
        page = entries[offset:offset + limit]
        return {"path": relative, "entries": page, "total": total, "offset": offset, "limit": limit, "hasMore": offset + len(page) < total}

    def read_file(self, relative: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        path = resolve_under(self.root, relative)
        assert_readable(path)
        if not path.is_file():
            raise FileNotFoundError(relative)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"File exceeds {MAX_FILE_BYTES} bytes")
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if start_line < 1:
            raise ValueError("start_line must be >= 1")
        last = min(end_line or (start_line + 399), len(lines))
        selected = lines[start_line - 1:last]
        return {
            "path": relative,
            "sizeBytes": size,
            "totalLines": len(lines),
            "startLine": start_line,
            "endLine": last,
            "truncated": last < len(lines),
            "nextStartLine": last + 1 if last < len(lines) else None,
            "content": "\n".join(selected),
        }

    def search(self, query: str, path: str = ".", limit: int = 50) -> dict[str, Any]:
        if not query:
            raise ValueError("query is required")
        base = resolve_under(self.root, path)
        matches: list[dict[str, Any]] = []
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                p = Path(current) / f
                try:
                    assert_readable(p)
                    if p.stat().st_size > MAX_FILE_BYTES:
                        continue
                    for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if query.lower() in line.lower():
                            matches.append({"path": p.relative_to(self.root).as_posix(), "line": lineno, "text": line[:500]})
                            if len(matches) >= min(limit, MAX_SEARCH_MATCHES):
                                return {"matches": matches, "matchCount": len(matches), "truncated": True, "engine": "python"}
                except (OSError, UnicodeError):
                    continue
        return {"matches": matches, "matchCount": len(matches), "truncated": False, "engine": "python"}

    def git_status(self) -> dict[str, Any]:
        cp = self._git(["status", "--porcelain=v1", "--branch"], check=False)
        if cp.returncode != 0:
            return {"isRepo": False, "raw": ""}
        return {"isRepo": True, "raw": cp.stdout[:MAX_DIFF_BYTES]}

    def git_diff(self, mode: str = "unstaged") -> dict[str, Any]:
        args = ["diff"]
        if mode == "staged":
            args.append("--cached")
        elif mode == "head":
            args.extend(["HEAD"])
        elif mode != "unstaged":
            raise ValueError("mode must be unstaged, staged, or head")
        cp = self._git(args, check=False)
        if cp.returncode != 0:
            return {"isRepo": False, "mode": mode, "diff": ""}
        raw = cp.stdout.encode("utf-8", errors="replace")
        clipped = raw[:MAX_DIFF_BYTES]
        return {"isRepo": True, "mode": mode, "totalBytes": len(raw), "truncated": len(raw) > len(clipped), "diff": clipped.decode("utf-8", errors="replace")}

    def _git(self, args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(self.root), *args], text=True, capture_output=True, check=check, timeout=10)
