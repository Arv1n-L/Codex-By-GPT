from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("C2C_HOME")
        self._old_key = os.environ.get("chatgpt-apikey")
        os.environ["C2C_HOME"] = str(Path(self.tmp.name) / "state")
        import codex_by_gpt.config as config
        import codex_by_gpt.runtime as runtime
        import codex_by_gpt.service as service
        importlib.reload(config)
        importlib.reload(runtime)
        importlib.reload(service)
        self.config, self.runtime, self.service = config, runtime, service
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.ws = config.add_workspace(str(self.root), "repo")
        self.tunnel = Path(self.tmp.name) / "tunnel-client.exe"
        self.tunnel.write_text("stub", encoding="utf-8")

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("C2C_HOME", None)
        else:
            os.environ["C2C_HOME"] = self._old_home
        if self._old_key is None:
            os.environ.pop("chatgpt-apikey", None)
        else:
            os.environ["chatgpt-apikey"] = self._old_key
        self.tmp.cleanup()

    def test_configure_persists_canonical_ids_without_secret(self):
        os.environ["chatgpt-apikey"] = "sk-real-secret"
        cfg = self.service.configure_service("tunnel_x", ["repo"], tunnel_client=str(self.tunnel))
        self.assertEqual(cfg.workspace_ids, [self.ws.id])
        raw = self.config.SERVICE_FILE.read_text(encoding="utf-8")
        self.assertIn("chatgpt-apikey", raw)
        self.assertNotIn("sk-real-secret", raw)
        self.assertEqual(self.service.load_service_config().workspace_ids, [self.ws.id])

    def test_configure_rejects_duplicate_workspace(self):
        with self.assertRaisesRegex(ValueError, "only once"):
            self.service.configure_service("tunnel_x", ["repo", self.ws.id], tunnel_client=str(self.tunnel))

    def test_missing_api_key_fails_before_popen(self):
        self.service.configure_service("tunnel_x", ["repo"], tunnel_client=str(self.tunnel))
        os.environ.pop("chatgpt-apikey", None)
        with mock.patch.object(self.service, "_supervisor_lock_held", return_value=True), mock.patch.object(self.service.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "environment variable"):
                self.service.start_service()
            popen.assert_not_called()

    def test_fixed_specs_do_not_use_shell_and_use_env_reference(self):
        cfg = self.service.ServiceConfig("tunnel_x", [self.ws.id], tunnel_client=str(self.tunnel))
        specs = self.service.ServiceSupervisor(cfg)._specs([self.ws])
        self.assertIn("env:chatgpt-apikey", specs["tunnel"])
        self.assertNotIn("sk-", " ".join(specs["tunnel"]))
        self.assertEqual(specs["listener"].count("--workspace"), 1)

    def test_supervisor_lock_is_singleton(self):
        first = self.service.SupervisorLock(); self.assertTrue(first.acquire())
        try:
            second = self.service.SupervisorLock(); self.assertFalse(second.acquire()); second.release()
        finally:
            first.release()

    def test_logs_are_rotated_and_tailed(self):
        settings = {"max_bytes": 20, "backup_count": 2}
        for _ in range(5):
            self.service._append_log("tunnel", "0123456789", settings)
        self.assertTrue((self.config.LOG_DIR / "tunnel.log.1").exists())
        tail = self.service.read_logs("tunnel", lines=2, follow=False)
        self.assertLessEqual(len(tail.splitlines()), 2)

    def test_child_log_redacts_api_key_material(self):
        os.environ["chatgpt-apikey"] = "plain-secret-value"
        from codex_by_gpt.mailbox import _redact_execution_output
        self.service._append_log("tunnel", _redact_execution_output("API_KEY=plain-secret-value"), {"max_bytes": 1000, "backup_count": 1})
        self.assertNotIn("plain-secret-value", (self.config.LOG_DIR / "tunnel.log").read_text(encoding="utf-8"))

    def test_status_without_config_is_manual_safe(self):
        status = self.service.service_status()
        self.assertFalse(status["configured"])
        self.assertEqual(status["status"], "STOPPED")

    def test_pid_probe_is_non_destructive_and_reports_current_process(self):
        self.assertTrue(self.service._pid_alive(os.getpid()))
        self.assertFalse(self.service._pid_alive(4_000_000_000))

    def test_failure_budget_keeps_overall_status_failed(self):
        cfg = self.service.ServiceConfig("tunnel_x", [self.ws.id], tunnel_client=str(self.tunnel))
        supervisor = self.service.ServiceSupervisor(cfg)
        supervisor.children = {"gateway": self.service._Child("gateway", [])}
        supervisor.children["gateway"].state = "FAILED"
        self.assertEqual(supervisor._overall_status(), "FAILED")

    def test_failed_supervisor_remains_stop_controllable(self):
        state = {"status": "FAILED", "runtime": {"run_id": "run-1"}, "supervisorPid": 123}
        stopped = {"status": "STOPPED", "runtime": {"status": "STOPPED"}}
        with mock.patch.object(self.service, "service_status", side_effect=[state, stopped]), mock.patch.object(self.service, "_write_control") as write:
            self.assertEqual(self.service.stop_service(timeout=0), stopped)
            write.assert_called_once()
            self.assertEqual(write.call_args.args[0]["run_id"], "run-1")

    def test_start_is_idempotent_for_live_failed_or_starting_supervisor(self):
        runtime = {"status": "FAILED", "supervisor_pid": os.getpid(), "run_id": "run-1", "children": {}}
        self.config.save_json_atomic(self.config.SERVICE_FILE, self.service.ServiceConfig("tunnel_x", [self.ws.id], tunnel_client=str(self.tunnel)).to_dict())
        self.config.save_json_atomic(self.config.SERVICE_RUNTIME_FILE, runtime)
        with mock.patch.object(self.service, "_supervisor_lock_held", return_value=True), mock.patch.object(self.service.subprocess, "Popen") as popen:
            self.assertEqual(self.service.start_service(timeout=0)["status"], "FAILED")
            popen.assert_not_called()

    def test_non_tunnel_children_do_not_inherit_api_key(self):
        os.environ["chatgpt-apikey"] = "secret"
        cfg = self.service.ServiceConfig("tunnel_x", [self.ws.id], tunnel_client=str(self.tunnel))
        supervisor = self.service.ServiceSupervisor(cfg)

        class FakeProcess:
            pid = 123
            stdout = []
            def poll(self): return None
            def terminate(self): return None
            def wait(self, timeout=None): return 0

        with mock.patch.object(self.service.subprocess, "Popen", return_value=FakeProcess()) as popen, mock.patch.object(supervisor.job, "assign"):
            supervisor._spawn(self.service._Child("listener", ["listener"]))
            env = popen.call_args.kwargs["env"]
            self.assertNotIn("chatgpt-apikey", {key.lower() for key in env})
            supervisor._spawn(self.service._Child("tunnel", ["tunnel"]))
            tunnel_env = popen.call_args.kwargs["env"]
            key = next(key for key in tunnel_env if key.lower() == "chatgpt-apikey")
            self.assertEqual(tunnel_env[key], "secret")

    def test_job_assignment_failure_terminates_new_child(self):
        cfg = self.service.ServiceConfig("tunnel_x", [self.ws.id], tunnel_client=str(self.tunnel))
        supervisor = self.service.ServiceSupervisor(cfg)

        class FakeProcess:
            pid = 456
            stdout = []
            def __init__(self): self.terminated = False
            def poll(self): return None
            def terminate(self): self.terminated = True
            def wait(self, timeout=None): return 0

        process = FakeProcess()
        with mock.patch.object(self.service.subprocess, "Popen", return_value=process), mock.patch.object(supervisor.job, "assign", side_effect=RuntimeError("assign")):
            with self.assertRaisesRegex(RuntimeError, "assign"):
                supervisor._spawn(self.service._Child("gateway", ["gateway"]))
        self.assertTrue(process.terminated)

    def test_startup_wait_honors_matching_stop_control(self):
        cfg = self.service.ServiceConfig("tunnel_x", [self.ws.id], tunnel_client=str(self.tunnel))
        supervisor = self.service.ServiceSupervisor(cfg)
        self.service._write_control({"action": "stop", "run_id": supervisor.run_id})
        self.assertFalse(supervisor._wait_gateway(timeout=0.1))
        self.assertTrue(supervisor.stop_requested)

    def test_tunnel_readiness_timeout_logs_bounded_probe_reason(self):
        os.environ["chatgpt-apikey"] = "probe-secret-value"
        cfg = self.service.ServiceConfig("tunnel_x", [self.ws.id], tunnel_client=str(self.tunnel))
        supervisor = self.service.ServiceSupervisor(cfg)
        with mock.patch.object(
            self.service, "_probe_tunnel_endpoint_detailed",
            side_effect=[(200, "ok", None), (503, "reason=probe-secret-value", None)],
        ), mock.patch.object(self.service.time, "monotonic", side_effect=[0.0, 0.0, 2.0]), mock.patch.object(
            self.service.time, "sleep"
        ), mock.patch.object(supervisor, "_log") as log:
            self.assertFalse(supervisor._wait_tunnel(timeout=1.0))
        message = log.call_args.args[0]
        self.assertIn("healthz=200", message)
        self.assertIn("readyz=503", message)
        self.assertIn("reason=reason=<redacted>", message)
        self.assertNotIn("probe-secret-value", message)

    def test_runtime_pid_reuse_does_not_grant_supervisor_ownership(self):
        cfg = self.service.ServiceConfig("tunnel_x", [self.ws.id], tunnel_client=str(self.tunnel))
        self.service.save_service_config(cfg)
        self.config.save_json_atomic(self.config.SERVICE_RUNTIME_FILE, {"status": "RUNNING", "supervisor_pid": os.getpid(), "run_id": "stale", "children": {}})
        status = self.service.service_status()
        self.assertEqual(status["status"], "STOPPED")
        self.assertEqual(status["ownership"], "NONE")


if __name__ == "__main__":
    unittest.main()
