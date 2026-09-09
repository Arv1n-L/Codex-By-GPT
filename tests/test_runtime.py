from __future__ import annotations

import importlib
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_home = Path(self.tmp.name) / "state"
        os.environ["C2C_HOME"] = str(self.state_home)
        import codex_by_gpt.config as config
        import codex_by_gpt.mailbox as mailbox
        import codex_by_gpt.runtime as runtime

        importlib.reload(config)
        importlib.reload(mailbox)
        importlib.reload(runtime)
        self.config, self.mailbox, self.runtime = config, mailbox, runtime
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.ws = config.add_workspace(str(self.root), "repo")

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_workspace_lock_fails_fast_and_releases(self):
        first = self.runtime.ListenerLock(self.ws).acquire()
        try:
            with self.assertRaisesRegex(self.runtime.ListenerAlreadyActiveError, "already active"):
                self.runtime.ListenerLock(self.ws).acquire()
            status = self.runtime.listener_status(self.ws)
            self.assertEqual(status["status"], "RUNNING")
            self.assertEqual(status["pid"], os.getpid())
        finally:
            first.release()
        self.assertEqual(self.runtime.listener_status(self.ws)["status"], "STOPPED")

    def test_stopped_listener_probe_does_not_create_runtime_files(self):
        self.assertFalse(self.runtime.LISTENER_DIR.exists())
        self.assertEqual(self.runtime.listener_status(self.ws)["status"], "STOPPED")
        self.assertFalse(self.runtime.LISTENER_DIR.exists())

    def test_process_termination_releases_os_lock(self):
        script = (
            "from codex_by_gpt.config import get_workspace; "
            "from codex_by_gpt.runtime import ListenerLock; "
            "import time; "
            "lock=ListenerLock(get_workspace('repo')).acquire(); "
            "print('READY', flush=True); time.sleep(30)"
        )
        env = {**os.environ, "C2C_HOME": str(self.state_home)}
        child = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "READY")
            with self.assertRaises(self.runtime.ListenerAlreadyActiveError):
                self.runtime.ListenerLock(self.ws).acquire()
        finally:
            child.terminate()
            child.communicate(timeout=5)
        deadline = __import__("time").monotonic() + 2
        while True:
            try:
                released = self.runtime.ListenerLock(self.ws).acquire()
                break
            except self.runtime.ListenerAlreadyActiveError:
                if __import__("time").monotonic() >= deadline:
                    raise
                __import__("time").sleep(0.05)
        try:
            self.assertEqual(self.runtime.listener_status(self.ws)["status"], "RUNNING")
        finally:
            released.release()

    def test_different_workspace_locks_can_coexist(self):
        other_root = Path(self.tmp.name) / "other"
        other_root.mkdir()
        other = self.config.add_workspace(str(other_root), "other")
        with self.runtime.ListenerLock(self.ws), self.runtime.ListenerLock(other):
            self.assertEqual(self.runtime.listener_status(self.ws)["status"], "RUNNING")
            self.assertEqual(self.runtime.listener_status(other)["status"], "RUNNING")

    def test_lock_setup_failure_releases_os_lock(self):
        with mock.patch.object(self.runtime.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.runtime.ListenerLock(self.ws).acquire()
        with self.runtime.ListenerLock(self.ws):
            self.assertEqual(self.runtime.listener_status(self.ws)["status"], "RUNNING")

    def test_status_and_doctor_report_runtime_failures(self):
        source = self.mailbox.submit(self.ws.id, "task-1", 1, "PLAN", "do it")
        self.mailbox.claim_execution(self.ws.id, "task-1", 1, source.id)
        self.config.EXECUTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.EXECUTIONS_FILE.write_text("not-json\n", encoding="utf-8")
        self.root.rmdir()

        with mock.patch.object(
            self.runtime,
            "_gateway_status",
            return_value={"status": "STOPPED", "endpoint": "http://127.0.0.1:8765"},
        ), mock.patch.object(
            self.runtime,
            "_tunnel_status",
            return_value={"status": "UNAVAILABLE", "profile": "codex-by-gpt", "executable": None},
        ):
            status = self.runtime.collect_status()
            issues = self.runtime.diagnose(status)

        workspace = status["workspaces"][0]
        self.assertEqual(workspace["pending"], 1)
        self.assertEqual(workspace["claims"], 1)
        self.assertFalse(workspace["rootValid"])
        self.assertEqual(status["state"]["executions.jsonl"]["status"], "INVALID")
        codes = {issue["code"] for issue in issues}
        self.assertTrue(
            {"GATEWAY_UNAVAILABLE", "TUNNEL_CLIENT_NOT_FOUND", "WORKSPACE_ROOT_INVALID", "STATE_INVALID", "ABNORMAL_CLAIM"}
            <= codes
        )

    def test_tunnel_status_distinguishes_process_and_readiness(self):
        with mock.patch.object(self.runtime, "_tunnel_executable", return_value=None), mock.patch.object(
            self.runtime, "_tunnel_process_running", return_value=False
        ):
            self.assertEqual(self.runtime._tunnel_status("codex-by-gpt")["status"], "UNAVAILABLE")

        with mock.patch.object(self.runtime, "_tunnel_executable", return_value="C:/tunnel-client.exe"), mock.patch.object(
            self.runtime, "_tunnel_process_running", return_value=False
        ):
            self.assertEqual(self.runtime._tunnel_status("codex-by-gpt")["status"], "STOPPED")

        with mock.patch.object(self.runtime, "_tunnel_executable", return_value="C:/tunnel-client.exe"), mock.patch.object(
            self.runtime, "_tunnel_process_running", return_value=True
        ), mock.patch.object(self.runtime, "_probe_tunnel_endpoint", side_effect=[(200, None), (503, None)]) as probe:
            status = self.runtime._tunnel_status("codex-by-gpt", "http://127.0.0.1:9999/")
        self.assertEqual(status["status"], "NOT_READY")
        self.assertEqual(status["healthUrl"], "http://127.0.0.1:9999")
        self.assertEqual(status["healthz"], 200)
        self.assertEqual(status["readyz"], 503)
        self.assertEqual(probe.call_args_list[0].args[0], "http://127.0.0.1:9999/healthz")
        self.assertEqual(probe.call_args_list[1].args[0], "http://127.0.0.1:9999/readyz")

        with mock.patch.object(self.runtime, "_tunnel_executable", return_value="C:/tunnel-client.exe"), mock.patch.object(
            self.runtime, "_tunnel_process_running", return_value=True
        ), mock.patch.object(self.runtime, "_probe_tunnel_endpoint", side_effect=[(200, None), (200, None)]):
            self.assertEqual(self.runtime._tunnel_status("codex-by-gpt")["status"], "READY")

    def test_mcp_preflight_exercises_protocol_and_workspace_list(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        tools = copy.deepcopy(list(self.runtime.EXPECTED_MCP_TOOLS.values()))
        responses = [
            Response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "codex-by-gpt-gateway", "version": "0.2.3"}}}),
            Response({"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}),
            Response({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({"workspaces": [{"workspaceId": self.ws.id}]})} ]}}),
        ]
        with mock.patch.object(self.runtime.urllib.request, "urlopen", side_effect=responses) as urlopen:
            result = self.runtime._mcp_preflight()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["workspaceCount"], 1)
        self.assertEqual(result["mcpCatalogDigest"], self.runtime.MCP_CATALOG_DIGEST)
        self.assertEqual(result["expectedCatalogDigest"], self.runtime.MCP_CATALOG_DIGEST)
        self.assertEqual(urlopen.call_count, 3)

    def test_mcp_preflight_rejects_catalog_schema_drift(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(self.payload).encode("utf-8")

        tools = copy.deepcopy(list(self.runtime.EXPECTED_MCP_TOOLS.values()))
        tools[0]["annotations"] = {"readOnlyHint": False}
        responses = [
            Response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "codex-by-gpt-gateway", "version": "0.2.3"}}}),
            Response({"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}),
        ]
        with mock.patch.object(self.runtime.urllib.request, "urlopen", side_effect=responses):
            result = self.runtime._mcp_preflight()
        self.assertEqual(result["code"], "MCP_CATALOG_INVALID")
        self.assertIn("workspace_list", result["invalid"])
        self.assertEqual(result["expectedCatalogDigest"], self.runtime.MCP_CATALOG_DIGEST)
        self.assertNotEqual(result["actualCatalogDigest"], result["expectedCatalogDigest"])

    def test_mcp_preflight_rejects_digest_only_catalog_drift(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(self.payload).encode("utf-8")

        tools = copy.deepcopy(list(self.runtime.EXPECTED_MCP_TOOLS.values()))
        tools[0]["description"] = "changed public description"
        responses = [
            Response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "codex-by-gpt-gateway", "version": "0.2.3"}}}),
            Response({"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}),
        ]
        with mock.patch.object(self.runtime.urllib.request, "urlopen", side_effect=responses):
            result = self.runtime._mcp_preflight()
        self.assertEqual(result["code"], "MCP_CATALOG_MISMATCH")
        self.assertNotEqual(result["expectedCatalogDigest"], result["actualCatalogDigest"])

    def test_doctor_distinguishes_local_ready_from_client_snapshot(self):
        status = {
            "version": "0.2.3",
            "gateway": {"status": "HEALTHY", "endpoint": "http://127.0.0.1:8765", "server": {"version": "0.2.3"}},
            "tunnel": {"status": "READY", "profile": "codex-by-gpt", "healthUrl": "http://127.0.0.1:8080", "executable": "C:/tunnel-client.exe"},
            "mcpPreflight": {"status": "READY", "mcpCatalogDigest": "digest"},
            "state": {}, "workspaces": [],
        }
        with mock.patch.object(self.runtime.shutil, "which", return_value="git"):
            issues = self.runtime.diagnose(status)
        codes = {issue["code"] for issue in issues}
        self.assertIn("LOCAL_MCP_READY", codes)
        self.assertIn("CLIENT_ACTIONS_UNVERIFIED", codes)

    def test_mcp_preflight_reports_incomplete_action_set(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        responses = [
            Response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "codex-by-gpt-gateway", "version": "0.2.3"}}}),
            Response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "workspace_list"}]}}),
        ]
        with mock.patch.object(self.runtime.urllib.request, "urlopen", side_effect=responses):
            result = self.runtime._mcp_preflight()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["code"], "ACTION_SET_INCOMPLETE")
        self.assertIn("workspace_info", result["missing"])

    def test_mcp_preflight_rejects_non_loopback(self):
        result = self.runtime._mcp_preflight("192.0.2.1", 8765)
        self.assertEqual(result["code"], "MCP_PREFLIGHT_NON_LOOPBACK")

    def test_detailed_tunnel_probe_captures_bounded_redacted_body(self):
        os.environ["chatgpt-apikey"] = "probe-secret-value"

        class Response:
            status = 503

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return ("reason=probe-secret-value " + ("x" * 100)).encode()

        with mock.patch.object(self.runtime.urllib.request, "urlopen", return_value=Response()):
            status, body, error = self.runtime._probe_tunnel_endpoint_detailed("http://127.0.0.1:8080/readyz", max_body_chars=32)
        self.assertEqual(status, 503)
        self.assertIsNone(error)
        self.assertNotIn("probe-secret-value", body)
        self.assertIn("<redacted>", body)
        self.assertTrue(body.endswith("...<truncated>"))

    def test_detailed_tunnel_probe_rejects_non_loopback(self):
        status, body, error = self.runtime._probe_tunnel_endpoint_detailed("https://example.com/readyz")
        self.assertIsNone(status)
        self.assertIsNone(body)
        self.assertEqual(error, "non-loopback URL rejected")

    def test_doctor_treats_stopped_or_unready_tunnel_as_error(self):
        for tunnel_status in ("STOPPED", "STARTING", "NOT_READY"):
            status = {
                "version": "0.2.3",
                "gateway": {"status": "HEALTHY", "endpoint": "http://127.0.0.1:8765", "server": {"version": "0.2.3"}},
                "tunnel": {"status": tunnel_status, "profile": "codex-by-gpt", "healthUrl": "http://127.0.0.1:8080", "executable": "C:/tunnel-client.exe"},
                "state": {},
                "workspaces": [],
            }
            with mock.patch.object(self.runtime.shutil, "which", return_value="git"):
                issues = self.runtime.diagnose(status)
            self.assertTrue(any(issue["severity"] == "ERROR" for issue in issues), tunnel_status)


if __name__ == "__main__":
    unittest.main()
