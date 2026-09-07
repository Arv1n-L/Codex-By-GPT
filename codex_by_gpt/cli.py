from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import add_workspace, get_workspace, list_workspaces, remove_workspace
from .mailbox import ack, list_results
from .mcp import serve


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="c2c", description="Machine-wide C2C Gateway")
    sub = p.add_subparsers(dest="cmd", required=True)
    gw = sub.add_parser("gateway")
    gws = gw.add_subparsers(dest="action", required=True)
    serve_p = gws.add_parser("serve")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8765)
    ws = sub.add_parser("workspace")
    wss = ws.add_subparsers(dest="action", required=True)
    add = wss.add_parser("add"); add.add_argument("path"); add.add_argument("--name")
    wss.add_parser("list")
    rm = wss.add_parser("remove"); rm.add_argument("workspace")
    info = wss.add_parser("info"); info.add_argument("workspace")
    mb = sub.add_parser("mailbox")
    mbs = mb.add_subparsers(dest="action", required=True)
    ls = mbs.add_parser("list"); ls.add_argument("--workspace"); ls.add_argument("--task"); ls.add_argument("--all", action="store_true")
    ak = mbs.add_parser("ack"); ak.add_argument("result_id")
    tun = sub.add_parser("tunnel")
    tuns = tun.add_subparsers(dest="action", required=True)
    init = tuns.add_parser("init-command"); init.add_argument("--tunnel-id", required=True); init.add_argument("--profile", default="codex-by-gpt"); init.add_argument("--port", type=int, default=8765)
    run = tuns.add_parser("run-command"); run.add_argument("--profile", default="codex-by-gpt")
    sub.add_parser("doctor")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "gateway" and args.action == "serve":
        serve(args.host, args.port); return 0
    if args.cmd == "workspace":
        if args.action == "add": emit(add_workspace(args.path, args.name).__dict__); return 0
        if args.action == "list": emit([w.__dict__ for w in list_workspaces()]); return 0
        if args.action == "remove": emit({"removed": remove_workspace(args.workspace)}); return 0
        if args.action == "info": emit(get_workspace(args.workspace).__dict__); return 0
    if args.cmd == "mailbox":
        if args.action == "list": emit(list_results(args.workspace, args.task, args.all)); return 0
        if args.action == "ack": emit({"acked": ack(args.result_id)}); return 0
    if args.cmd == "tunnel":
        if args.action == "init-command":
            emit({"env": "CONTROL_PLANE_API_KEY=<runtime-key>", "command": f'tunnel-client init --sample sample_mcp_stdio_local --profile {args.profile} --tunnel-id {args.tunnel_id} --mcp-server-url http://127.0.0.1:{args.port}/mcp', "then": f"tunnel-client doctor --profile {args.profile} --explain"}); return 0
        if args.action == "run-command": emit({"command": f"tunnel-client run --profile {args.profile}"}); return 0
    if args.cmd == "doctor":
        checks = {
            "python": sys.version.split()[0],
            "git": shutil.which("git"),
            "tunnelClient": shutil.which("tunnel-client"),
            "workspaces": len(list_workspaces()),
            "stateHome": os.environ.get("C2C_HOME", str(Path.home() / ".codex-by-gpt")),
        }
        emit(checks)
        return 0 if checks["git"] else 1
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
