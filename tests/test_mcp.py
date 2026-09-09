from __future__ import annotations
import importlib
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

class McpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["C2C_HOME"] = str(Path(self.tmp.name) / "state")
        import codex_by_gpt.config as config
        import codex_by_gpt.mailbox as mailbox
        import codex_by_gpt.mcp as mcp
        importlib.reload(config); importlib.reload(mailbox); importlib.reload(mcp)
        self.config, self.mcp = config, mcp
        root = Path(self.tmp.name) / "repo"; root.mkdir()
        (root / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        self.ws = config.add_workspace(str(root), "repo")

    def tearDown(self): self.tmp.cleanup()

    def test_tools_require_registered_workspace_id(self):
        ok = self.mcp.call_tool("read_file", {"workspace_id": self.ws.id, "path": "hello.txt"})
        self.assertNotIn("isError", ok)
        bad = self.mcp.call_tool("read_file", {"workspace_id": "ws_missing", "path": "hello.txt"})
        self.assertTrue(bad["isError"])

    def test_tool_surface_has_no_workspace_write_or_shell(self):
        names = {tool["name"] for tool in self.mcp.TOOLS}
        self.assertIn("browser_target_select", names)
        self.assertIn("submit_result", names)
        self.assertFalse(names & {"write_file", "delete_file", "shell", "exec", "git_commit", "git_push"})
        cancel = next(tool for tool in self.mcp.TOOLS if tool["name"] == "cancel_result")
        self.assertFalse(cancel["annotations"]["readOnlyHint"])

    def test_initialize_and_tools_list(self):
        init = self.mcp.handle_rpc({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "codex-by-gpt-gateway")
        self.assertEqual(init["result"]["serverInfo"]["version"], "0.2.3")
        self.assertIn("HARD POLICY", init["result"]["instructions"])
        self.assertIn("never use ChatGPT Work mode", init["result"]["instructions"])
        self.assertIn("preflight", init["result"]["instructions"])
        self.assertIn("BLOCKED", init["result"]["instructions"])
        tools = self.mcp.handle_rpc({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
        self.assertGreaterEqual(len(tools["result"]["tools"]), 8)

    def test_tool_catalog_digest_is_stable_and_contract_sensitive(self):
        import copy
        digest = self.mcp.tool_catalog_digest()
        self.assertEqual(digest, self.mcp.tool_catalog_digest(copy.deepcopy(self.mcp.TOOLS)))
        changed = copy.deepcopy(self.mcp.TOOLS)
        changed[0]["inputSchema"]["properties"]["extra"] = {"type": "string"}
        self.assertNotEqual(digest, self.mcp.tool_catalog_digest(changed))

    def test_healthz_exposes_catalog_digest(self):
        captured = {}
        handler = object.__new__(self.mcp.McpHandler)
        handler.path = "/healthz"
        handler._json = lambda status, payload: captured.update(status=status, payload=payload)
        with mock.patch.object(self.mcp, "list_workspaces", return_value=[]):
            handler.do_GET()
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["payload"]["mcpCatalogDigest"], self.mcp.MCP_CATALOG_DIGEST)

    def test_cancel_result_action_returns_terminal_receipt(self):
        source = self.mcp.call_tool("submit_result", {"workspace_id": self.ws.id, "task_id": "task-cancel", "iteration": 1, "kind": "PLAN", "payload": "old"})
        result_id = json.loads(source["content"][0]["text"])["resultId"]
        cancelled = self.mcp.call_tool("cancel_result", {"result_id": result_id, "reason": "quota exhausted"})
        self.assertIn('"state": "CANCELLED"', cancelled["content"][0]["text"])

    def test_wait_execution_is_read_only_and_returns_evidence(self):
        wait_tool = next(tool for tool in self.mcp.TOOLS if tool["name"] == "wait_execution")
        self.assertTrue(wait_tool["annotations"]["readOnlyHint"])
        pending = self.mcp.call_tool("wait_execution", {"workspace_id": self.ws.id, "task_id": "task-1", "iteration": 1})
        self.assertIn('"state": "PENDING"', pending["content"][0]["text"])
        import codex_by_gpt.mailbox as mailbox
        source = mailbox.submit(self.ws.id, "task-1", 1, "PLAN", "evidence")
        mailbox.record_execution(self.ws.id, "task-1", 1, source.id, 0, "evidence", {"status": "passed", "command": "tests", "summary": "ok"})
        executed = self.mcp.call_tool("wait_execution", {"workspace_id": self.ws.id, "task_id": "task-1", "iteration": 1})
        self.assertIn('"state": "EXECUTED"', executed["content"][0]["text"])
        self.assertIn('"execution_output": "evidence"', executed["content"][0]["text"])

    def test_browser_target_select_is_normal_only_and_read_only(self):
        tool = next(tool for tool in self.mcp.TOOLS if tool["name"] == "browser_target_select")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        normal = self.mcp.call_tool("browser_target_select", {"tabs": [{"id": "normal", "providerTabId": "p-normal", "title": "Chat", "url": "https://chatgpt.com/c/normal", "mode": "NORMAL"}]})
        self.assertIn('"providerTabId": "p-normal"', normal["content"][0]["text"])
        blocked = self.mcp.call_tool("browser_target_select", {"tabs": [{"id": "work", "title": "ChatGPT Work", "url": "https://chatgpt.com/c/work", "mode": "WORK"}]})
        self.assertTrue(blocked["isError"])
        self.assertIn("NO_SAFE_NORMAL_CHATGPT_TAB", blocked["content"][0]["text"])

    def test_chatgpt_cannot_submit_executed(self):
        result = self.mcp.call_tool("submit_result", {"workspace_id": self.ws.id, "task_id": "task-1", "iteration": 1, "kind": "EXECUTED", "payload": "fake"})
        self.assertTrue(result["isError"])

    def test_submit_result_is_idempotent_and_rejects_conflicts(self):
        args = {"workspace_id": self.ws.id, "task_id": "task-retry", "iteration": 1, "kind": "PLAN", "payload": "same"}
        first = self.mcp.call_tool("submit_result", args)
        retry = self.mcp.call_tool("submit_result", args)
        first_data = json.loads(first["content"][0]["text"])
        retry_data = json.loads(retry["content"][0]["text"])
        self.assertEqual(first_data["resultId"], retry_data["resultId"])
        conflict = self.mcp.call_tool("submit_result", {**args, "payload": "changed"})
        self.assertTrue(conflict["isError"])
        self.assertIn("new iteration", conflict["content"][0]["text"])
