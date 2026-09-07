from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

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
