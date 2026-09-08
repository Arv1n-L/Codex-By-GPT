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
            claim = self.mailbox.get_execution_claim(cfg.id, result["task_id"], int(result["iteration"]))
            self.assertIsNotNone(claim)
            self.assertEqual(claim["source_result_id"], result["id"])
            calls.append((cfg.root, result["id"]))
            return self.worker.ExecutionOutcome(0, "jsonl evidence", {"status": "passed", "command": "tests", "summary": "ok"})

        self.assertTrue(self.worker.process_next(self.ws, fake_runner))
        self.assertEqual(calls, [(self.ws.root, source.id)])
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-1"), [])
        record = self.mailbox.get_execution(self.ws.id, "task-1", 1)
        self.assertEqual(record["source_result_id"], source.id)
        self.assertEqual(record["codex_exit_code"], 0)
        self.assertIsNone(self.mailbox.get_execution_claim(self.ws.id, "task-1", 1))
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
        runner = mock.Mock(return_value=outcome)
        with mock.patch.object(self.worker, "record_execution", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.worker.process_next(self.ws, runner)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-3")[0]["id"], source.id)
        self.assertIsNotNone(self.mailbox.get_execution_claim(self.ws.id, "task-3", 1))

        self.assertTrue(self.worker.process_next(self.ws, runner))
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-3"), [])
        self.assertIsNone(self.mailbox.get_execution_claim(self.ws.id, "task-3", 1))
        recovered = self.mailbox.get_execution(self.ws.id, "task-3", 1)
        self.assertIsNone(recovered["codex_exit_code"])
        self.assertEqual(recovered["test_status"]["status"], "unknown")
        self.assertIn("Automatic rerun was suppressed", recovered["execution_output"])

    def test_claim_write_failure_does_not_start_runner(self):
        source = self.mailbox.submit(self.ws.id, "task-claim-fail", 1, "PLAN", "do it")
        runner = mock.Mock()
        with mock.patch.object(self.mailbox, "_save_execution_claims", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.worker.process_next(self.ws, runner)
        runner.assert_not_called()
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-claim-fail")[0]["id"], source.id)

    def test_preexisting_claim_recovers_without_running(self):
        source = self.mailbox.submit(self.ws.id, "task-interrupted", 2, "PLAN", "do it")
        self.mailbox.claim_execution(self.ws.id, "task-interrupted", 2, source.id)
        runner = mock.Mock()

        self.assertTrue(self.worker.process_next(self.ws, runner))

        runner.assert_not_called()
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-interrupted"), [])
        self.assertIsNone(self.mailbox.get_execution_claim(self.ws.id, "task-interrupted", 2))
        recovered = self.mailbox.wait_for_execution(self.ws.id, "task-interrupted", 2)["record"]
        self.assertIsNone(recovered["codex_exit_code"])
        self.assertEqual(recovered["test_status"]["status"], "unknown")

    def test_acked_source_with_claim_recovers_without_running(self):
        source = self.mailbox.submit(self.ws.id, "task-acked-claim", 3, "PLAN", "do it")
        self.mailbox.claim_execution(self.ws.id, "task-acked-claim", 3, source.id)
        self.assertTrue(self.mailbox.ack(source.id))
        runner = mock.Mock()

        self.assertTrue(self.worker.process_next(self.ws, runner))

        runner.assert_not_called()
        recovered = self.mailbox.wait_for_execution(self.ws.id, "task-acked-claim", 3)["record"]
        self.assertIsNone(recovered["codex_exit_code"])
        self.assertEqual(recovered["test_status"]["status"], "unknown")
        self.assertIsNone(self.mailbox.get_execution_claim(self.ws.id, "task-acked-claim", 3))

    def test_acked_source_with_terminal_and_claim_only_cleans_claim(self):
        source = self.mailbox.submit(self.ws.id, "task-acked-terminal", 4, "REVIEW", "inspect")
        self.mailbox.claim_execution(self.ws.id, "task-acked-terminal", 4, source.id)
        original = self.mailbox.record_execution(
            self.ws.id, "task-acked-terminal", 4, source.id, 0, "done", {"status": "passed"}
        )
        self.assertTrue(self.mailbox.ack(source.id))
        runner = mock.Mock()

        self.assertTrue(self.worker.process_next(self.ws, runner))

        runner.assert_not_called()
        preserved = self.mailbox.get_execution(self.ws.id, "task-acked-terminal", 4)
        self.assertEqual(preserved["id"], original.id)
        self.assertIsNone(self.mailbox.get_execution_claim(self.ws.id, "task-acked-terminal", 4))
        self.assertEqual(len(self.mailbox._load_executions()), 1)

    def test_acked_source_claim_recovery_write_failure_keeps_claim(self):
        source = self.mailbox.submit(self.ws.id, "task-recovery-fail", 5, "PLAN", "do it")
        self.mailbox.claim_execution(self.ws.id, "task-recovery-fail", 5, source.id)
        self.assertTrue(self.mailbox.ack(source.id))
        runner = mock.Mock()

        with mock.patch.object(self.worker, "record_execution", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.worker.process_next(self.ws, runner)

        runner.assert_not_called()
        self.assertIsNotNone(self.mailbox.get_execution_claim(self.ws.id, "task-recovery-fail", 5))
        self.assertEqual(self.mailbox.wait_for_execution(self.ws.id, "task-recovery-fail", 5)["state"], "PENDING")

    def test_existing_execution_is_acked_without_rerun(self):
        source = self.mailbox.submit(self.ws.id, "task-4", 2, "PLAN", "do it")
        self.mailbox.claim_execution(self.ws.id, "task-4", 2, source.id)
        self.mailbox.record_execution(self.ws.id, "task-4", 2, source.id, 0, "done", {"status": "not_run"})
        runner = mock.Mock()
        self.assertTrue(self.worker.process_next(self.ws, runner))
        runner.assert_not_called()
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-4"), [])
        self.assertIsNone(self.mailbox.get_execution_claim(self.ws.id, "task-4", 2))

    def test_legacy_duplicate_rows_execute_once(self):
        source = self.mailbox.submit(self.ws.id, "task-legacy", 3, "PLAN", "do it")
        rows = self.mailbox._load()
        duplicate = {**rows[0], "id": "legacy-duplicate", "created_at": rows[0]["created_at"] + 1}
        self.mailbox._save([*rows, duplicate])
        runner = mock.Mock(
            return_value=self.worker.ExecutionOutcome(
                0, "done", {"status": "passed", "command": "tests", "summary": "ok"}
            )
        )

        self.assertTrue(self.worker.process_next(self.ws, runner))
        self.assertTrue(self.worker.process_next(self.ws, runner))
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-legacy"), [])
        execution = self.mailbox.get_execution(self.ws.id, "task-legacy", 3)
        self.assertEqual(execution["source_result_id"], source.id)

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
