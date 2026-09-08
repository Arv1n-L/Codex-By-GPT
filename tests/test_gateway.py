from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
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

    def test_gateway_health_and_unknown_get_routes(self):
        import importlib
        import codex_by_gpt.mcp as mcp
        importlib.reload(mcp)
        server = mcp.ThreadingHTTPServer(("127.0.0.1", 0), mcp.McpHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def get(path):
            try:
                with urllib.request.urlopen(base + path, timeout=2) as response:
                    return response.status, response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8")

        try:
            health_status, health_body = get("/healthz")
            self.assertEqual(health_status, 200)
            self.assertIn('"ok": true', health_body)
            for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource", "/unknown"):
                status, _body = get(path)
                self.assertEqual(status, 404, path)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_exact_retry_reuses_original_result_even_after_ack(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        first = self.mailbox.submit(ws.id, "task-retry", 1, "PLAN", "same")
        retry = self.mailbox.submit(ws.id, "task-retry", 1, "PLAN", "same")
        self.assertEqual(retry.id, first.id)
        self.assertEqual(retry.created_at, first.created_at)
        self.assertEqual(len(self.mailbox.list_results(ws.id, "task-retry", include_acked=True)), 1)
        self.assertTrue(self.mailbox.ack(first.id))
        acked_retry = self.mailbox.submit(ws.id, "task-retry", 1, "PLAN", "same")
        self.assertEqual(acked_retry.id, first.id)
        self.assertTrue(acked_retry.acked)

    def test_cancel_result_is_terminal_and_idempotent(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        source = self.mailbox.submit(ws.id, "task-cancel", 1, "PLAN", "old plan")
        first = self.mailbox.cancel_result(source.id, "quota exhausted")
        retry = self.mailbox.cancel_result(source.id, "different wording")
        self.assertEqual(first.id, retry.id)
        self.assertEqual(first.state, "CANCELLED")
        self.assertEqual(self.mailbox.wait_for_execution(ws.id, "task-cancel", 1)["state"], "CANCELLED")
        self.assertEqual(self.mailbox.list_results(ws.id, "task-cancel"), [])
        visible = self.mailbox.list_results(ws.id, "task-cancel", include_acked=True)[0]
        self.assertTrue(visible["acked"])
        self.assertTrue(visible["cancelled"])

    def test_cancel_does_not_rewrite_existing_execution_or_other_iteration(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        first = self.mailbox.submit(ws.id, "task-supersede", 1, "PLAN", "old")
        second = self.mailbox.submit(ws.id, "task-supersede", 2, "PLAN", "new")
        cancelled = self.mailbox.cancel_result(first.id, "SUPERSEDED by iteration 2")
        self.assertEqual(cancelled.state, "SUPERSEDED")
        self.assertEqual(self.mailbox.list_results(ws.id, "task-supersede", include_acked=True)[1]["payload"], "new")
        self.mailbox.record_execution(ws.id, "task-done", 1, "done-source", 0, "done", {"status": "passed"})
        with self.assertRaisesRegex(RuntimeError, "EXECUTED"):
            self.mailbox.cancel_result("done-source", "too late")
        self.assertEqual(self.mailbox.list_results(ws.id, "task-retry"), [])

    def test_block_result_is_terminal_and_idempotent(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        source = self.mailbox.submit(ws.id, "task-block", 1, "PLAN", "open ChatGPT Work mode")
        first = self.mailbox.block_result(source.id, "BLOCKED: client session control is forbidden")
        retry = self.mailbox.block_result(source.id, "different wording")
        self.assertEqual(first.id, retry.id)
        self.assertEqual(first.state, "BLOCKED")
        self.assertEqual(self.mailbox.wait_for_execution(ws.id, "task-block", 1)["state"], "BLOCKED")
        visible = self.mailbox.list_results(ws.id, "task-block", include_acked=True)[0]
        self.assertTrue(visible["blocked"])
        self.assertTrue(visible["acked"])

    def test_conflicting_retry_is_rejected_without_append(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        self.mailbox.submit(ws.id, "task-conflict", 1, "PLAN", "original")
        with self.assertRaisesRegex(self.mailbox.SubmissionConflictError, "new iteration"):
            self.mailbox.submit(ws.id, "task-conflict", 1, "PLAN", "changed")
        with self.assertRaisesRegex(self.mailbox.SubmissionConflictError, "new iteration"):
            self.mailbox.submit(ws.id, "task-conflict", 1, "REVIEW", "original")
        self.assertEqual(len(self.mailbox.list_results(ws.id, "task-conflict", include_acked=True)), 1)

    def test_concurrent_identical_submissions_create_one_row(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        start = threading.Barrier(3)
        ids = []
        errors = []

        def submit_once():
            try:
                start.wait()
                ids.append(self.mailbox.submit(ws.id, "task-concurrent", 1, "PLAN", "same").id)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(2)

        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.mailbox.list_results(ws.id, "task-concurrent", include_acked=True)), 1)

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

    def test_execution_claim_is_durable_idempotent_and_clearable(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        first = self.mailbox.claim_execution(ws.id, "task-claim", 1, "source-1")
        retry = self.mailbox.claim_execution(ws.id, "task-claim", 1, "source-1")
        self.assertEqual(retry.created_at, first.created_at)
        self.assertEqual(
            self.mailbox.get_execution_claim(ws.id, "task-claim", 1)["source_result_id"],
            "source-1",
        )
        with self.assertRaisesRegex(RuntimeError, "different mailbox result"):
            self.mailbox.claim_execution(ws.id, "task-claim", 1, "source-2")
        self.assertTrue(self.mailbox.clear_execution_claim(ws.id, "task-claim", 1))
        self.assertIsNone(self.mailbox.get_execution_claim(ws.id, "task-claim", 1))
        self.assertFalse(self.mailbox.clear_execution_claim(ws.id, "task-claim", 1))

    def test_execution_record_deduplicates_by_logical_iteration(self):
        root = Path(self.tmp.name) / "a"; root.mkdir()
        ws = self.config.add_workspace(str(root))
        first = self.mailbox.record_execution(
            ws.id, "task-logical", 4, "source-1", 0, "first", {"status": "passed"}
        )
        duplicate = self.mailbox.record_execution(
            ws.id, "task-logical", 4, "source-2", 1, "second", {"status": "failed"}
        )
        self.assertEqual(duplicate.id, first.id)
        self.assertEqual(duplicate.source_result_id, "source-1")
        self.assertEqual(len(self.mailbox._load_executions()), 1)

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
