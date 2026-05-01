import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creatures.execution.tools.screenshot import (
    BROWSER_HEADLESS_ENV,
    Screenshotter,
    browser_should_run_headless,
    system_has_gui,
)


class FakePage:
    def __init__(self):
        self.calls = []

    def goto(self, url, wait_until=None, timeout=None):
        self.calls.append(("goto", url, wait_until, timeout))

    def wait_for_selector(self, selector, timeout=None):
        self.calls.append(("wait_for_selector", selector, timeout))

    def wait_for_timeout(self, timeout):
        self.calls.append(("wait_for_timeout", timeout))

    def screenshot(self, path, full_page=True):
        self.calls.append(("screenshot", path, full_page))
        Path(path).write_bytes(b"png")


class FakeBrowser:
    def __init__(self):
        self.page = FakePage()
        self.closed = False

    def new_page(self, viewport=None):
        self.viewport = viewport
        return self.page

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.browser = FakeBrowser()

    def launch(self, headless=True):
        self.headless = headless
        return self.browser


class FakePlaywrightContext:
    def __init__(self):
        self.chromium = FakeChromium()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class ScreenshotTests(unittest.TestCase):
    def test_capture_returns_error_when_playwright_unavailable(self):
        screenshotter = Screenshotter(
            playwright_factory=lambda: (_ for _ in ()).throw(
                RuntimeError("playwright not available")
            )
        )

        result = screenshotter.capture("http://localhost:8096", "run", "home")

        self.assertIsNone(result["path"])
        self.assertEqual(result["error"], "playwright not available")

    def test_capture_writes_sanitized_png_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            context = FakePlaywrightContext()
            screenshotter = Screenshotter(
                artifacts_root=temp_dir,
                playwright_factory=lambda: context,
            )

            result = screenshotter.capture(
                "http://localhost:8096/web",
                "run",
                "step 1/fail",
                wait_selector="#login",
                wait_ms=50,
            )

            self.assertEqual(
                result["path"],
                str(Path(temp_dir).resolve() / "run" / "screenshots" / "step_1_fail.png"),
            )
            self.assertTrue(Path(result["path"]).exists())
            self.assertTrue(context.chromium.browser.closed)
            self.assertIn(context.chromium.headless, {True, False})
            calls = context.chromium.browser.page.calls
            self.assertEqual(calls[0][0], "goto")
            self.assertEqual(calls[1], ("wait_for_selector", "#login", 50))

    def test_browser_headless_env_override_forces_headless(self):
        with patch.dict("os.environ", {BROWSER_HEADLESS_ENV: "true"}, clear=True):
            self.assertTrue(browser_should_run_headless())

    def test_browser_headless_env_override_forces_gui(self):
        with patch.dict("os.environ", {BROWSER_HEADLESS_ENV: "false"}, clear=True):
            self.assertFalse(browser_should_run_headless())

    def test_browser_defaults_to_gui_on_non_headless_macos(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("platform.system", return_value="Darwin"),
        ):
            self.assertTrue(system_has_gui())
            self.assertFalse(browser_should_run_headless())

    def test_browser_defaults_to_headless_without_linux_display(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("platform.system", return_value="Linux"),
        ):
            self.assertFalse(system_has_gui())
            self.assertTrue(browser_should_run_headless())

    def test_capture_passes_headless_choice_to_playwright(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            context = FakePlaywrightContext()
            screenshotter = Screenshotter(
                artifacts_root=temp_dir,
                playwright_factory=lambda: context,
            )

            with patch.dict("os.environ", {BROWSER_HEADLESS_ENV: "false"}, clear=True):
                result = screenshotter.capture(
                    "http://localhost:8096/web",
                    "run",
                    "visible",
                    wait_ms=0,
                )

            self.assertFalse(result["headless"])
            self.assertFalse(context.chromium.headless)


if __name__ == "__main__":
    unittest.main()
