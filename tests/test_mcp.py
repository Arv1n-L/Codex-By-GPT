from __future__ import annotations
import importlib
import os
import tempfile
import unittest
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
        self.assertIn("submit_result", names)
        self.assertFalse(names & {"write_file", "delete_file", "shell", "exec", "git_commit", "git_push"})

    def test_initialize_and_tools_list(self):
        init = self.mcp.handle_rpc({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "codex-by-gpt-gateway")
        tools = self.mcp.handle_rpc({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
        self.assertGreaterEqual(len(tools["result"]["tools"]), 8)
