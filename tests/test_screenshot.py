import tempfile
import unittest
from pathlib import Path

from creatures.execution.tools.screenshot import Screenshotter


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
            calls = context.chromium.browser.page.calls
            self.assertEqual(calls[0][0], "goto")
            self.assertEqual(calls[1], ("wait_for_selector", "#login", 50))


if __name__ == "__main__":
    unittest.main()
