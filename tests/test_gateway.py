from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["C2C_HOME"] = str(Path(self.tmp.name) / "state")
        # reload modules so config picks up test home
        import importlib, codex_by_gpt.config as config
        import codex_by_gpt.mailbox as mailbox
        importlib.reload(config); importlib.reload(mailbox)
        self.config = config; self.mailbox = mailbox

    def tearDown(self):
        self.tmp.cleanup()

    def test_multiple_workspaces_have_distinct_ids(self):
        a = Path(self.tmp.name) / "a"; b = Path(self.tmp.name) / "b"; a.mkdir(); b.mkdir()
        wa = self.config.add_workspace(str(a)); wb = self.config.add_workspace(str(b))
        self.assertNotEqual(wa.id, wb.id)
        self.assertEqual(len(self.config.list_workspaces()), 2)

    def test_mailbox_is_bounded_write_surface(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        result = self.mailbox.submit(ws.id, "task-1", 2, "REVIEW", "Looks good")
        rows = self.mailbox.list_results(ws.id, "task-1")
        self.assertEqual(rows[0]["id"], result.id)
        self.assertTrue(self.mailbox.ack(result.id))
        self.assertEqual(self.mailbox.list_results(ws.id, "task-1"), [])

    def test_execution_record_is_separate_bounded_and_idempotent(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        source = self.mailbox.submit(ws.id, "task-2", 1, "PLAN", "change it")
        first = self.mailbox.record_execution(
            ws.id,
            "task-2",
            1,
            source.id,
            0,
            "x" * (self.mailbox.MAX_EXECUTION_OUTPUT_CHARS + 10),
            {"status": "passed", "command": "python -m unittest", "summary": "ok"},
        )
        second = self.mailbox.record_execution(ws.id, "task-2", 1, source.id, 9, "different", None)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(first.execution_output), self.mailbox.MAX_EXECUTION_OUTPUT_CHARS)
        self.assertEqual(first.test_status["status"], "passed")
        self.assertEqual(self.mailbox.list_results(ws.id, "task-2")[0]["id"], source.id)
        failed_source = self.mailbox.submit(ws.id, "task-failed", 2, "PLAN", "change it")
        failed = self.mailbox.record_execution(
            ws.id,
            "task-failed",
            2,
            failed_source.id,
            1,
            "failed",
            {"status": "passed", "command": "tests", "summary": "claimed pass"},
        )
        self.assertEqual(failed.test_status["status"], "unknown")

    def test_mailbox_submit_and_ack_are_serialized(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        source = self.mailbox.submit(ws.id, "task-seed", 1, "PLAN", "seed")
        save_entered = threading.Event()
        release_save = threading.Event()
        submit_done = threading.Event()
        ack_done = threading.Event()
        original_save = self.mailbox._save
        slow_once = True

        def slow_save(rows):
            nonlocal slow_once
            if slow_once:
                slow_once = False
                save_entered.set()
                self.assertTrue(release_save.wait(2))
            original_save(rows)

        with mock.patch.object(self.mailbox, "_save", side_effect=slow_save):
            submit_thread = threading.Thread(
                target=lambda: (self.mailbox.submit(ws.id, "task-new", 2, "REVIEW", "new"), submit_done.set())
            )
            ack_thread = threading.Thread(target=lambda: (self.mailbox.ack(source.id), ack_done.set()))
            submit_thread.start()
            self.assertTrue(save_entered.wait(2))
            ack_thread.start()
            self.assertFalse(ack_done.wait(0.1))
            release_save.set()
            submit_thread.join(2)
            ack_thread.join(2)

        self.assertTrue(submit_done.is_set())
        self.assertTrue(ack_done.is_set())
        rows = self.mailbox.list_results(ws.id, include_acked=True)
        self.assertEqual({row["task_id"] for row in rows}, {"task-seed", "task-new"})
        self.assertTrue(next(row for row in rows if row["id"] == source.id)["acked"])

    def test_execution_output_redacts_secrets_before_persistence(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        record = self.mailbox.record_execution(
            ws.id,
            "task-secret",
            1,
            "source-secret",
            0,
            "API_KEY=top-secret\nAuthorization: Bearer abcdefghijklmnop\nsk-example123456789\nordinary output",
            {
                "status": "not_run",
                "command": "tool --api-key=command-secret",
                "summary": "Authorization: Bearer summarysecret123",
            },
        )
        self.assertNotIn("top-secret", record.execution_output)
        self.assertNotIn("abcdefghijklmnop", record.execution_output)
        self.assertNotIn("sk-example123456789", record.execution_output)
        self.assertIn("ordinary output", record.execution_output)
        self.assertNotIn("command-secret", record.test_status["command"])
        self.assertNotIn("summarysecret123", record.test_status["summary"])
        persisted = self.config.EXECUTIONS_FILE.read_text(encoding="utf-8")
        self.assertNotIn("top-secret", persisted)
        self.assertNotIn("command-secret", persisted)
        self.assertNotIn("summarysecret123", persisted)
        visible = self.mailbox.wait_for_execution(ws.id, "task-secret", 1)["record"]
        self.assertNotIn("command-secret", visible["test_status"]["command"])
        self.assertNotIn("summarysecret123", visible["test_status"]["summary"])

    def test_wait_for_execution_returns_pending_then_executed(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        self.assertEqual(self.mailbox.wait_for_execution(ws.id, "task-3", 1)["state"], "PENDING")
        self.mailbox.record_execution(ws.id, "task-3", 1, "source-3", 1, "failed", {"status": "invalid"})
        result = self.mailbox.wait_for_execution(ws.id, "task-3", 1)
        self.assertEqual(result["state"], "EXECUTED")
        self.assertEqual(result["record"]["test_status"]["status"], "unknown")
