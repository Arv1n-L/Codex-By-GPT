from __future__ import annotations

import time
import unittest

from codex_by_gpt.browser_target import BrowserTargetError, select_preferred_target, target_dict, validate_target


def tab(tab_id: str, title: str = "Chat", *, mode: str | None = "NORMAL", active: bool = False, last_opened: float | None = None):
    value = {"id": tab_id, "providerTabId": f"provider-{tab_id}", "title": title, "url": f"https://chatgpt.com/c/{tab_id}", "active": active}
    if mode is not None:
        value["mode"] = mode
    if last_opened is not None:
        value["lastOpened"] = last_opened
    return value


class BrowserTargetTest(unittest.TestCase):
    def test_explicit_active_normal_tab_is_preferred(self):
        tabs = [tab("old", last_opened=100), tab("active", active=True, last_opened=1), tab("new", last_opened=200)]
        selected = select_preferred_target(tabs)
        self.assertEqual(selected.provider_tab_id, "provider-active")

    def test_active_tab_id_matches_provider_id(self):
        selected = select_preferred_target([tab("first"), tab("second")], "provider-second")
        self.assertEqual(selected.provider_tab_id, "provider-second")

    def test_work_and_unknown_modes_fail_closed(self):
        with self.assertRaisesRegex(BrowserTargetError, "NO_SAFE_NORMAL_CHATGPT_TAB"):
            select_preferred_target([tab("work", "验证 · 工作", mode="WORK")])
        with self.assertRaisesRegex(BrowserTargetError, "NO_SAFE_NORMAL_CHATGPT_TAB"):
            select_preferred_target([tab("stale-work", "ChatGPT Work", mode="NORMAL")])
        with self.assertRaisesRegex(BrowserTargetError, "NO_SAFE_NORMAL_CHATGPT_TAB"):
            select_preferred_target([tab("unknown", mode=None)])

    def test_target_must_still_exactly_match_inventory(self):
        inventory = [tab("keep", "Stable")]
        selected = select_preferred_target(inventory)
        self.assertEqual(validate_target(target_dict(selected), inventory).url, selected.url)
        with self.assertRaisesRegex(BrowserTargetError, "BROWSER_TARGET_CHANGED"):
            validate_target(target_dict(selected), [tab("keep", "Renamed")])

    def test_stale_target_is_rejected(self):
        inventory = [tab("keep")]
        selected = select_preferred_target(inventory)
        stale = {**target_dict(selected), "observedAt": time.time() - 301}
        with self.assertRaisesRegex(BrowserTargetError, "BROWSER_TARGET_STALE"):
            validate_target(stale, inventory)


if __name__ == "__main__":
    unittest.main()
