from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["C2C_HOME"] = str(Path(self.tmp.name) / "state")
        import codex_by_gpt.config as config
        import codex_by_gpt.mailbox as mailbox
        import codex_by_gpt.worker as worker
        importlib.reload(config)
        importlib.reload(mailbox)
        importlib.reload(worker)
        self.config, self.mailbox, self.worker = config, mailbox, worker
        root = Path(self.tmp.name) / "repo"; root.mkdir()
        self.ws = config.add_workspace(str(root), "repo")

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_runs_once_records_then_acks(self):
        source = self.mailbox.submit(self.ws.id, "task-1", 1, "PLAN", "do it")
        calls = []

        def fake_runner(cfg, result):
            calls.append((cfg.root, result["id"]))
            return self.worker.ExecutionOutcome(0, "jsonl evidence", {"status": "passed", "command": "tests", "summary": "ok"})

        self.assertTrue(self.worker.process_next(self.ws, fake_runner))
        self.assertEqual(calls, [(self.ws.root, source.id)])
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-1"), [])
        record = self.mailbox.get_execution(self.ws.id, "task-1", 1)
        self.assertEqual(record["source_result_id"], source.id)
        self.assertEqual(record["codex_exit_code"], 0)
        self.assertFalse(self.worker.process_next(self.ws, fake_runner))
        self.assertEqual(len(calls), 1)

    def test_non_actionable_result_is_not_run(self):
        self.mailbox.submit(self.ws.id, "task-2", 1, "DONE", "finished")
        runner = mock.Mock()
        self.assertFalse(self.worker.process_next(self.ws, runner))
        runner.assert_not_called()
        self.assertEqual(len(self.mailbox.list_results(self.ws.id, "task-2")), 1)

    def test_execution_write_failure_does_not_ack(self):
        source = self.mailbox.submit(self.ws.id, "task-3", 1, "REVIEW", "fix")
        outcome = self.worker.ExecutionOutcome(1, "failed", {"status": "unknown", "command": None, "summary": "failed"})
        with mock.patch.object(self.worker, "record_execution", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.worker.process_next(self.ws, lambda cfg, result: outcome)
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-3")[0]["id"], source.id)

    def test_existing_execution_is_acked_without_rerun(self):
        source = self.mailbox.submit(self.ws.id, "task-4", 2, "PLAN", "do it")
        self.mailbox.record_execution(self.ws.id, "task-4", 2, source.id, 0, "done", {"status": "not_run"})
        runner = mock.Mock()
        self.assertTrue(self.worker.process_next(self.ws, runner))
        runner.assert_not_called()
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-4"), [])

    def test_run_codex_builds_fixed_workspace_command_and_reads_final(self):
        result = {"task_id": "task-5", "iteration": 1, "kind": "PLAN", "payload": "do it"}

        def fake_run(command, **kwargs):
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text(
                '{"summary":"done","test_status":{"status":"passed","command":"tests","summary":"ok"}}',
                encoding="utf-8",
            )
            self.assertEqual(kwargs["cwd"], self.ws.root)
            self.assertEqual(command[command.index("--cd") + 1], self.ws.root)
            return self.worker.subprocess.CompletedProcess(command, 0, stdout="jsonl", stderr="")

        with mock.patch.object(self.worker.shutil, "which", return_value="codex"), mock.patch.object(
            self.worker.subprocess, "run", side_effect=fake_run
        ):
            outcome = self.worker.run_codex(self.ws, result)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.test_status["status"], "passed")

    def test_run_codex_handles_missing_binary_timeout_and_malformed_final(self):
        result = {"task_id": "task-6", "iteration": 1, "kind": "REVIEW", "payload": "fix"}
        with mock.patch.object(self.worker.shutil, "which", return_value=None):
            self.assertIsNone(self.worker.run_codex(self.ws, result).exit_code)

        with mock.patch.object(self.worker.shutil, "which", return_value="codex"), mock.patch.object(
            self.worker.subprocess, "run", side_effect=self.worker.subprocess.TimeoutExpired("codex", 1, output="partial")
        ):
            timed_out = self.worker.run_codex(self.ws, result)
        self.assertIsNone(timed_out.exit_code)
        self.assertEqual(timed_out.test_status["status"], "unknown")

        def malformed_run(command, **kwargs):
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text("not-json", encoding="utf-8")
            return self.worker.subprocess.CompletedProcess(command, 3, stdout="bad", stderr="error")

        with mock.patch.object(self.worker.shutil, "which", return_value="codex"), mock.patch.object(
            self.worker.subprocess, "run", side_effect=malformed_run
        ):
            malformed = self.worker.run_codex(self.ws, result)
        self.assertEqual(malformed.exit_code, 3)
        self.assertEqual(malformed.test_status["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
