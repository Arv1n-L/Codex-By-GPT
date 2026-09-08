from __future__ import annotations

import argparse
import json
import sys

from .config import add_workspace, get_workspace, list_workspaces, remove_workspace
from .mailbox import ack, list_results
from .mcp import serve
from .runtime import ListenerAlreadyActiveError, collect_status, doctor_report
from .service import (
    configure_service,
    load_service_config,
    read_logs,
    restart_service,
    run_service,
    service_status,
    start_service,
    stop_service,
)
from .worker import listen


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
    codex = sub.add_parser("codex")
    codexs = codex.add_subparsers(dest="action", required=True)
    listen_p = codexs.add_parser("listen")
    listen_p.add_argument("--workspace", required=True, action="append")
    listen_p.add_argument("--poll-interval", type=float, default=1.0)
    listen_p.add_argument("--once", action="store_true")
    tun = sub.add_parser("tunnel")
    tuns = tun.add_subparsers(dest="action", required=True)
    init = tuns.add_parser("init-command"); init.add_argument("--tunnel-id", required=True); init.add_argument("--profile", default="codex-by-gpt"); init.add_argument("--port", type=int, default=8765)
    run = tuns.add_parser("run-command"); run.add_argument("--profile", default="codex-by-gpt")
    status_p = sub.add_parser("status")
    status_p.add_argument("--host", default="127.0.0.1")
    status_p.add_argument("--port", type=int, default=8765)
    status_p.add_argument("--profile", default="codex-by-gpt")
    doctor_p = sub.add_parser("doctor")
    doctor_p.add_argument("--host", default="127.0.0.1")
    doctor_p.add_argument("--port", type=int, default=8765)
    doctor_p.add_argument("--profile", default="codex-by-gpt")
    service = sub.add_parser("service")
    services = service.add_subparsers(dest="action", required=True)
    configure = services.add_parser("configure")
    configure.add_argument("--tunnel-id", required=True)
    configure.add_argument("--workspace", required=True, action="append")
    configure.add_argument("--api-key-env", default="chatgpt-apikey")
    configure.add_argument("--tunnel-client")
    configure.add_argument("--profile", default="codex-by-gpt")
    configure.add_argument("--gateway-host", default="127.0.0.1")
    configure.add_argument("--gateway-port", type=int, default=8765)
    configure.add_argument("--tunnel-health-url", default="http://127.0.0.1:8080")
    configure.add_argument("--poll-interval", type=float, default=1.0)
    services.add_parser("start")
    services.add_parser("stop")
    services.add_parser("restart")
    services.add_parser("status")
    logs = services.add_parser("logs")
    logs.add_argument("--component", choices=["supervisor", "gateway", "tunnel", "listener"], default="supervisor")
    logs.add_argument("--lines", type=int, default=100)
    logs.add_argument("--follow", action="store_true")
    services.add_parser("run")
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
    if args.cmd == "codex" and args.action == "listen":
        configs = [get_workspace(workspace) for workspace in args.workspace]
        try:
            listen(configs, args.poll_interval, args.once)
        except ListenerAlreadyActiveError as exc:
            print(f"ERROR:\n{exc}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "tunnel":
        if args.action == "init-command":
            emit({"env": "CONTROL_PLANE_API_KEY=<runtime-key>", "command": f'tunnel-client init --sample sample_mcp_stdio_local --profile {args.profile} --tunnel-id {args.tunnel_id} --mcp-server-url http://127.0.0.1:{args.port}/mcp', "then": f"tunnel-client doctor --profile {args.profile} --explain"}); return 0
        if args.action == "run-command": emit({"command": f"tunnel-client run --profile {args.profile}"}); return 0
    if args.cmd == "status":
        emit(collect_status(args.host, args.port, args.profile))
        return 0
    if args.cmd == "doctor":
        report = doctor_report(args.host, args.port, args.profile)
        emit(report)
        return 0 if report["ok"] else 1
    if args.cmd == "service":
        try:
            if args.action == "configure":
                emit(configure_service(args.tunnel_id, args.workspace, args.api_key_env, args.tunnel_client, args.profile, args.gateway_host, args.gateway_port, args.tunnel_health_url, args.poll_interval).to_dict())
                return 0
            if args.action == "start":
                emit(start_service()); return 0
            if args.action == "stop":
                emit(stop_service()); return 0
            if args.action == "restart":
                emit(restart_service()); return 0
            if args.action == "status":
                emit(service_status()); return 0
            if args.action == "logs":
                if args.follow:
                    for line in read_logs(args.component, args.lines, True):
                        print(line, end="")
                else:
                    print(read_logs(args.component, args.lines, False), end="")
                return 0
            if args.action == "run":
                return run_service()
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
