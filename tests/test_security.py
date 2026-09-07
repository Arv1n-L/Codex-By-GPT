from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from codex_by_gpt.security import SecurityError, assert_readable, resolve_under

class SecurityTest(unittest.TestCase):
    def test_path_escape_is_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SecurityError): resolve_under(Path(td), "../secret")
    def test_secret_suffix_is_blocked(self):
        with self.assertRaises(SecurityError): assert_readable(Path("server.key"))
