from __future__ import annotations

import importlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["C2C_HOME"] = str(Path(self.tmp.name) / "state")
        import codex_by_gpt.config as config
        import codex_by_gpt.mailbox as mailbox
        import codex_by_gpt.runtime as runtime
        import codex_by_gpt.worker as worker
        importlib.reload(config)
        importlib.reload(mailbox)
        importlib.reload(runtime)
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

    def test_invalid_mailbox_source_is_blocked_before_runner(self):
        source = self.mailbox.submit(self.ws.id, "task-invalid-source", 1, "PLAN", "valid")
        rows = self.mailbox._load()
        rows[0]["payload"] = ""
        self.mailbox._save(rows)
        runner = mock.Mock()
        self.assertTrue(self.worker.process_next(self.ws, runner))
        runner.assert_not_called()
        receipt = self.mailbox.wait_for_execution(self.ws.id, "task-invalid-source", 1)
        self.assertEqual(receipt["state"], "BLOCKED")
        self.assertIn("invalid mailbox execution source", receipt["record"]["execution_output"])
        self.assertEqual(receipt["record"]["source_result_id"], source.id)

    def test_runner_receives_the_source_revalidated_after_claim(self):
        source = self.mailbox.submit(self.ws.id, "task-revalidated-source", 1, "PLAN", "listed payload")
        original_claim = self.mailbox.claim_execution

        def claim_then_update(*args):
            claim = original_claim(*args)
            rows = self.mailbox._load()
            rows[0]["payload"] = "validated payload"
            self.mailbox._save(rows)
            return claim

        runner = mock.Mock(
            return_value=self.worker.ExecutionOutcome(
                0, "done", {"status": "passed", "command": "tests", "summary": "ok"}
            )
        )
        with mock.patch.object(self.worker, "claim_execution", side_effect=claim_then_update):
            self.assertTrue(self.worker.process_next(self.ws, runner))

        self.assertEqual(runner.call_args.args[1]["id"], source.id)
        self.assertEqual(runner.call_args.args[1]["payload"], "validated payload")

    def test_cancellation_during_source_revalidation_preserves_cancelled_receipt(self):
        source = self.mailbox.submit(self.ws.id, "task-revalidation-cancel", 1, "PLAN", "plan")
        original_validate = self.worker.validate_execution_source

        def cancel_then_validate(*args, **kwargs):
            self.mailbox.cancel_result(source.id, "cancel during validation")
            return original_validate(*args, **kwargs)

        runner = mock.Mock()
        with mock.patch.object(self.worker, "validate_execution_source", side_effect=cancel_then_validate):
            self.assertTrue(self.worker.process_next(self.ws, runner))

        runner.assert_not_called()
        receipt = self.mailbox.wait_for_execution(self.ws.id, "task-revalidation-cancel", 1)
        self.assertEqual(receipt["state"], "CANCELLED")
        self.assertIsNone(self.mailbox.get_execution_claim(self.ws.id, "task-revalidation-cancel", 1))

    def test_cancelled_result_is_not_run(self):
        source = self.mailbox.submit(self.ws.id, "task-cancel", 1, "PLAN", "do it")
        self.mailbox.cancel_result(source.id, "quota exhausted")
        runner = mock.Mock()
        self.assertFalse(self.worker.process_next(self.ws, runner))
        runner.assert_not_called()
        self.assertEqual(self.mailbox.wait_for_execution(self.ws.id, "task-cancel", 1)["state"], "CANCELLED")

    def test_forbidden_work_mode_intent_is_blocked_before_runner(self):
        source = self.mailbox.submit(self.ws.id, "task-work-mode", 1, "PLAN", "Please open a ChatGPT conversation in Work mode")
        runner = mock.Mock()
        self.assertTrue(self.worker.process_next(self.ws, runner))
        runner.assert_not_called()
        receipt = self.mailbox.wait_for_execution(self.ws.id, "task-work-mode", 1)
        self.assertEqual(receipt["state"], "BLOCKED")
        self.assertIn("client/session control", receipt["record"]["execution_output"])
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-work-mode"), [])
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-work-mode", include_acked=True)[0]["id"], source.id)

    def test_forbidden_chatgpt_client_synonyms_are_blocked(self):
        payloads = [
            "Send a message to ChatGPT",
            "Ask ChatGPT about this task",
            "Navigate to the ChatGPT page",
            "向 ChatGPT 发送消息",
            "打开 ChatGPT 页面",
            "连接 ChatGPT 对话窗口",
        ]
        runner = mock.Mock()
        for index, payload in enumerate(payloads, start=1):
            with self.subTest(payload=payload):
                self.mailbox.submit(self.ws.id, f"task-client-action-{index}", 1, "REVIEW", payload)
                self.assertTrue(self.worker.process_next(self.ws, runner))
                self.assertEqual(
                    self.mailbox.wait_for_execution(self.ws.id, f"task-client-action-{index}", 1)["state"],
                    "BLOCKED",
                )
        runner.assert_not_called()

    def test_chatgpt_reference_without_client_action_is_not_blocked(self):
        source = self.mailbox.submit(self.ws.id, "task-chatgpt-reference", 1, "PLAN", "Review the ChatGPT quota error in the local logs")
        runner = mock.Mock(return_value=self.worker.ExecutionOutcome(0, "done", {"status": "not_run", "command": None, "summary": "ok"}))
        self.assertTrue(self.worker.process_next(self.ws, runner))
        runner.assert_called_once()
        self.assertEqual(self.mailbox.get_execution(self.ws.id, "task-chatgpt-reference", 1)["state"], "EXECUTED")

    def test_cancellation_after_claim_suppresses_execution_record(self):
        source = self.mailbox.submit(self.ws.id, "task-race", 1, "PLAN", "do it")

        def fake_runner(cfg, result):
            self.mailbox.cancel_result(result["id"], "quota exhausted while starting")
            return self.worker.ExecutionOutcome(0, "should not become executed", {"status": "passed", "command": "tests", "summary": "ok"})

        self.assertTrue(self.worker.process_next(self.ws, fake_runner))
        runner_record = self.mailbox.wait_for_execution(self.ws.id, "task-race", 1)
        self.assertEqual(runner_record["state"], "CANCELLED")
        self.assertNotEqual(runner_record["record"]["state"], "EXECUTED")

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

        def fake_run(command, prompt, result_id, cwd, workspace_id, task_id, iteration):
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text(
                '{"summary":"done","test_status":{"status":"passed","command":"tests","summary":"ok"}}',
                encoding="utf-8",
            )
            self.assertEqual(cwd, self.ws.root)
            self.assertEqual(command[command.index("--cd") + 1], self.ws.root)
            return self.worker.ManagedProcessResult(0, "jsonl", "")

        with mock.patch.object(self.worker.shutil, "which", return_value="codex"), mock.patch.object(
            self.worker, "_run_managed_process", side_effect=fake_run
        ):
            outcome = self.worker.run_codex(self.ws, result)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.test_status["status"], "passed")

    def test_run_codex_handles_missing_binary_timeout_and_malformed_final(self):
        result = {"task_id": "task-6", "iteration": 1, "kind": "REVIEW", "payload": "fix"}
        with mock.patch.object(self.worker.shutil, "which", return_value=None):
            self.assertIsNone(self.worker.run_codex(self.ws, result).exit_code)

        with mock.patch.object(self.worker.shutil, "which", return_value="codex"), mock.patch.object(
            self.worker, "_run_managed_process", return_value=self.worker.ManagedProcessResult(None, "partial", "", timed_out=True)
        ):
            timed_out = self.worker.run_codex(self.ws, result)
        self.assertIsNone(timed_out.exit_code)
        self.assertEqual(timed_out.test_status["status"], "unknown")

        def malformed_run(command, prompt, result_id, cwd, workspace_id, task_id, iteration):
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text("not-json", encoding="utf-8")
            return self.worker.ManagedProcessResult(3, "bad", "error")

        with mock.patch.object(self.worker.shutil, "which", return_value="codex"), mock.patch.object(
            self.worker, "_run_managed_process", side_effect=malformed_run
        ):
            malformed = self.worker.run_codex(self.ws, result)
        self.assertEqual(malformed.exit_code, 3)
        self.assertEqual(malformed.test_status["status"], "unknown")

    def test_running_codex_process_is_terminated_after_cancellation(self):
        source = self.mailbox.submit(self.ws.id, "task-running-cancel", 1, "PLAN", "do it")

        def cancel_later():
            time.sleep(0.2)
            self.mailbox.cancel_result(source.id, "quota exhausted while running")

        canceller = threading.Thread(target=cancel_later)
        canceller.start()
        managed = self.worker._run_managed_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            "",
            source.id,
            self.ws.root,
            self.ws.id,
            "task-running-cancel",
            1,
        )
        canceller.join(timeout=5)

        self.assertTrue(managed.cancelled)
        self.assertIsNotNone(managed.exit_code)
        self.assertEqual(self.worker._ACTIVE_PROCESSES, {})

    def test_process_next_records_cancelled_after_real_process_termination(self):
        source = self.mailbox.submit(self.ws.id, "task-e2e-cancel", 1, "PLAN", "do it")

        def runner(cfg, result):
            def cancel_later():
                time.sleep(0.2)
                self.mailbox.cancel_result(result["id"], "quota exhausted in end-to-end run")

            canceller = threading.Thread(target=cancel_later)
            canceller.start()
            managed = self.worker._run_managed_process(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                "",
                result["id"],
                cfg.root,
                cfg.id,
                result["task_id"],
                int(result["iteration"]),
            )
            canceller.join(timeout=5)
            self.assertTrue(managed.cancelled)
            self.assertIsNotNone(managed.exit_code)
            return self.worker.ExecutionOutcome(managed.exit_code, "cancelled child", {"status": "unknown", "command": None, "summary": "cancelled"})

        self.assertTrue(self.worker.process_next(self.ws, runner))
        self.assertEqual(self.mailbox.wait_for_execution(self.ws.id, "task-e2e-cancel", 1)["state"], "CANCELLED")
        self.assertIsNone(self.mailbox.get_execution_claim(self.ws.id, "task-e2e-cancel", 1))
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-e2e-cancel"), [])

    def test_multi_workspace_listener_dispatches_one_explicit_workspace(self):
        other_root = Path(self.tmp.name) / "other"
        other_root.mkdir()
        other = self.config.add_workspace(str(other_root), "other")
        source = self.mailbox.submit(other.id, "task-multi", 1, "PLAN", "do it")
        calls = []

        def fake_runner(cfg, result):
            calls.append((cfg.id, result["id"]))
            return self.worker.ExecutionOutcome(
                0, "done", {"status": "passed", "command": "tests", "summary": "ok"}
            )

        self.worker.listen([self.ws, other], poll_interval=0.01, once=True, runner=fake_runner)

        self.assertEqual(calls, [(other.id, source.id)])
        self.assertEqual(self.mailbox.list_results(other.id, "task-multi"), [])

    def test_listener_ownership_blocks_runner_for_duplicate_workspace(self):
        import codex_by_gpt.runtime as runtime

        source = self.mailbox.submit(self.ws.id, "task-locked", 1, "PLAN", "do it")
        runner = mock.Mock()
        with runtime.ListenerLock(self.ws):
            with self.assertRaises(runtime.ListenerAlreadyActiveError):
                self.worker.listen(self.ws, poll_interval=0.01, once=True, runner=runner)
        runner.assert_not_called()
        self.assertEqual(self.mailbox.list_results(self.ws.id, "task-locked")[0]["id"], source.id)

    def test_multi_workspace_listener_rejects_duplicate_selection(self):
        runner = mock.Mock()
        with self.assertRaisesRegex(ValueError, "only once"):
            self.worker.listen([self.ws, self.ws], poll_interval=0.01, once=True, runner=runner)
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
