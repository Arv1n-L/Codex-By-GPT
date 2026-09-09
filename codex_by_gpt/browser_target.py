"""Fail-closed selection of an already-open ChatGPT browser conversation.

The Codex desktop host owns browser enumeration.  This module deliberately
accepts only a host-supplied inventory; it never opens, switches, or creates a
browser tab itself.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urlparse


CHATGPT_CONVERSATION = re.compile(r"^https://(?:www\.)?chatgpt\.com/c/[^/?#]+(?:[/?#].*)?$", re.IGNORECASE)
WORK_MARKERS = re.compile(r"(?:^|[\s·|:/_-])(work|工作)(?:$|[\s·|:/_-])", re.IGNORECASE)
SAFE_MODES = {"NORMAL"}


class BrowserTargetError(ValueError):
    """A browser target cannot be safely selected or revalidated."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class BrowserTab:
    tab_id: str
    provider_tab_id: str
    title: str
    url: str
    active: bool
    last_opened: float | None
    mode: str


@dataclass(frozen=True)
class PlannerTarget:
    provider_tab_id: str
    title: str
    url: str
    mode: str
    observed_at: float


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _mode(raw: dict[str, Any], title: str) -> str:
    # A visible Work marker is always unsafe, even if a stale host field says NORMAL.
    if WORK_MARKERS.search(title):
        return "WORK"
    explicit = str(raw.get("mode", "")).strip().upper()
    if explicit in {"NORMAL", "WORK", "UNKNOWN"}:
        return explicit
    return "UNKNOWN"


def normalize_tabs(raw_tabs: Iterable[dict[str, Any]]) -> list[BrowserTab]:
    """Normalize a browser-host inventory, dropping malformed entries."""
    tabs: list[BrowserTab] = []
    for raw in raw_tabs:
        if not isinstance(raw, dict):
            continue
        tab_id = str(raw.get("id", "")).strip()
        provider_id = str(raw.get("providerTabId", "") or tab_id).strip()
        title = str(raw.get("title", "")).strip()
        url = str(raw.get("url", "")).strip()
        if not provider_id or not title or not url:
            continue
        tabs.append(
            BrowserTab(
                tab_id=tab_id,
                provider_tab_id=provider_id,
                title=title,
                url=url,
                active=raw.get("active") is True,
                last_opened=_timestamp(raw.get("lastOpened")),
                mode=_mode(raw, title),
            )
        )
    return tabs


def _is_chatgpt_conversation(tab: BrowserTab) -> bool:
    parsed = urlparse(tab.url)
    return parsed.scheme.lower() == "https" and parsed.netloc.lower() in {"chatgpt.com", "www.chatgpt.com"} and bool(
        re.match(r"^/c/[^/]+", parsed.path, re.IGNORECASE)
    )


def select_preferred_target(raw_tabs: Iterable[dict[str, Any]], active_tab_id: str | None = None) -> PlannerTarget:
    """Select an already-open, explicitly NORMAL ChatGPT conversation."""
    tabs = [tab for tab in normalize_tabs(raw_tabs) if _is_chatgpt_conversation(tab) and tab.mode in SAFE_MODES]
    if not tabs:
        raise BrowserTargetError(
            "NO_SAFE_NORMAL_CHATGPT_TAB",
            "No already-open ChatGPT conversation is explicitly marked NORMAL; Work and unknown modes are rejected.",
        )
    requested = str(active_tab_id or "").strip()

    def rank(tab: BrowserTab) -> tuple[int, int, float]:
        exact_active = int(bool(requested) and requested in {tab.tab_id, tab.provider_tab_id})
        return exact_active, int(tab.active), tab.last_opened or float("-inf")

    selected = max(tabs, key=rank)
    return PlannerTarget(selected.provider_tab_id, selected.title, selected.url, selected.mode, time.time())


def target_dict(target: PlannerTarget) -> dict[str, Any]:
    return {
        "providerTabId": target.provider_tab_id,
        "title": target.title,
        "url": target.url,
        "mode": target.mode,
        "observedAt": target.observed_at,
    }


def validate_target(target: PlannerTarget | dict[str, Any], raw_tabs: Iterable[dict[str, Any]], max_age_seconds: float = 300) -> PlannerTarget:
    """Require the selected target to still exactly match a fresh inventory."""
    if isinstance(target, PlannerTarget):
        candidate = target
    elif isinstance(target, dict):
        try:
            candidate = PlannerTarget(
                str(target["providerTabId"]),
                str(target["title"]),
                str(target["url"]),
                str(target["mode"]).upper(),
                float(target["observedAt"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BrowserTargetError("INVALID_BROWSER_TARGET", "Target token is incomplete or malformed.") from exc
    else:
        raise BrowserTargetError("INVALID_BROWSER_TARGET", "Target token is not an object.")
    if candidate.mode not in SAFE_MODES or time.time() - candidate.observed_at > max_age_seconds:
        raise BrowserTargetError("BROWSER_TARGET_STALE", "Browser target is stale or not explicitly NORMAL; reselect it.")
    for tab in normalize_tabs(raw_tabs):
        if (
            tab.provider_tab_id == candidate.provider_tab_id
            and tab.title == candidate.title
            and tab.url == candidate.url
            and tab.mode == "NORMAL"
            and _is_chatgpt_conversation(tab)
        ):
            return candidate
    raise BrowserTargetError("BROWSER_TARGET_CHANGED", "The selected browser tab changed or closed; reselect it before planning.")
