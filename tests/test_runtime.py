from __future__ import annotations

import importlib
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


if __name__ == "__main__":
    unittest.main()
