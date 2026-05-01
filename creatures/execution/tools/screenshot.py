"""Headless Jellyfin web UI screenshot helper for the execution stage."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACTS_ROOT = Path(
    os.environ.get("JF_AUTO_TESTER_ARTIFACTS_ROOT", REPO_ROOT / "artifacts")
).resolve()


class Screenshotter:
    """Capture screenshots with Playwright when it is available."""

    def __init__(
        self,
        artifacts_root: str | Path | None = None,
        playwright_factory: Any | None = None,
    ) -> None:
        self.artifacts_root = Path(artifacts_root or DEFAULT_ARTIFACTS_ROOT).resolve()
        self._playwright_factory = playwright_factory

    def capture(
        self,
        url: str,
        run_id: str,
        label: str,
        wait_selector: str | None = None,
        wait_ms: int = 2000,
    ) -> dict[str, Any]:
        """Take a PNG screenshot and return a serializable artifact record."""

        timestamp = datetime.now(timezone.utc).isoformat()
        path = self._path(run_id, label)
        try:
            factory = self._playwright_factory or _load_sync_playwright()
        except RuntimeError as exc:
            return {
                "path": None,
                "url": url,
                "label": label,
                "timestamp": timestamp,
                "error": str(exc),
            }

        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with factory() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 720})
                    page.goto(url, wait_until="networkidle", timeout=max(wait_ms, 1) + 30000)
                    if wait_selector:
                        page.wait_for_selector(wait_selector, timeout=max(wait_ms, 1))
                    elif wait_ms > 0:
                        page.wait_for_timeout(wait_ms)
                    page.screenshot(path=str(path), full_page=True)
                finally:
                    browser.close()
        except Exception as exc:
            return {
                "path": None,
                "url": url,
                "label": label,
                "timestamp": timestamp,
                "error": str(exc),
            }

        return {
            "path": str(path),
            "url": url,
            "label": label,
            "timestamp": timestamp,
        }

    def _path(self, run_id: str, label: str) -> Path:
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "screenshot"
        return self.artifacts_root / run_id / "screenshots" / f"{safe_label}.png"


def capture(
    url: str,
    run_id: str,
    label: str,
    wait_selector: str | None = None,
    wait_ms: int = 2000,
) -> dict[str, Any]:
    return Screenshotter().capture(
        url=url,
        run_id=run_id,
        label=label,
        wait_selector=wait_selector,
        wait_ms=wait_ms,
    )


def _load_sync_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("playwright not available") from exc
    return sync_playwright
