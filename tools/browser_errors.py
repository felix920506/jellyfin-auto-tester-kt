"""Classification helpers for browser infrastructure failures."""

from __future__ import annotations

from typing import Any, Mapping


BROWSER_INFRASTRUCTURE_ERROR_MARKERS = (
    "playwright sync api",
    "please use the async api",
    "playwright not available",
    "executable doesn't exist",
    "browser executable",
    "browser_type.launch",
    "failed to launch",
    "target page, context or browser has been closed",
)


def browser_infrastructure_error(entry: Mapping[str, Any]) -> str | None:
    """Return an infrastructure error from a browser step, if one is present."""

    if entry.get("tool") != "browser":
        return None
    candidates = [entry.get("reason")]
    browser = entry.get("browser")
    if isinstance(browser, Mapping):
        candidates.append(browser.get("error"))
        actions = browser.get("actions")
        if isinstance(actions, list):
            candidates.extend(
                action.get("error")
                for action in actions
                if isinstance(action, Mapping)
            )
    for candidate in candidates:
        text = str(candidate or "")
        if _is_browser_infrastructure_text(text):
            return text
    return None


def _is_browser_infrastructure_text(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in BROWSER_INFRASTRUCTURE_ERROR_MARKERS)
