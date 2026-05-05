import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.browser import (
    BrowserDriver,
    CONTROL_INVENTORY_SCRIPT,
    DOM_SUMMARY_SCRIPT,
    MEDIA_STATE_SCRIPT,
    MEDIA_WAIT_SCRIPT,
)
from tools.screenshot import BROWSER_HEADLESS_ENV


class FakeConsoleMessage:
    def __init__(self, message_type, text):
        self.type = message_type
        self.text = text
        self.location = {"url": "app.js", "lineNumber": 7}


class FakeResponse:
    def __init__(self, url, status, status_text=""):
        self.url = url
        self.status = status
        self.status_text = status_text


class FakeRequest:
    def __init__(self, url, error_text):
        self.url = url
        self._error_text = error_text

    def failure(self):
        return {"errorText": self._error_text}


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.calls.append(("keyboard.press", key))


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def click(self, timeout=None):
        self.page.calls.append(("click", self.selector, timeout))

    def nth(self, index):
        self.page.calls.append(("nth", self.selector, index))
        return FakeLocator(self.page, f"{self.selector} >> nth={index}")

    def fill(self, value, timeout=None):
        self.page.calls.append(("fill", self.selector, value, timeout))

    def press(self, key, timeout=None):
        self.page.calls.append(("press", self.selector, key, timeout))

    def select_option(self, value, timeout=None):
        self.page.calls.append(("select_option", self.selector, value, timeout))

    def check(self, timeout=None):
        self.page.calls.append(("check", self.selector, timeout))

    def uncheck(self, timeout=None):
        self.page.calls.append(("uncheck", self.selector, timeout))

    def wait_for(self, state="visible", timeout=None):
        self.page.calls.append(("wait_for", self.selector, state, timeout))
        if self.page.failed_request_on_wait:
            self.page.emit("requestfailed", self.page.failed_request_on_wait)

    def text_content(self, timeout=None):
        self.page.calls.append(("text_content", self.selector, timeout))
        return self.page.locator_text.get(self.selector, "")

    def count(self):
        return self.page.locator_counts.get(self.selector, 0)

    def is_visible(self):
        return self.page.locator_visible.get(self.selector, False)

    def get_attribute(self, name):
        return self.page.locator_attributes.get((self.selector, name))


class FakePage:
    def __init__(self):
        self.calls = []
        self.locator_instances = []
        self.handlers = {}
        self.url = "about:blank"
        self.keyboard = FakeKeyboard(self)
        self.locator_text = {}
        self.locator_counts = {}
        self.locator_visible = {}
        self.locator_attributes = {}
        self.failed_request_on_wait = None
        self.media = [
            {
                "tag": "video",
                "paused": False,
                "ended": False,
                "error": None,
            }
        ]
        self.dom_summary = {
            "title": "Jellyfin",
            "url": "http://localhost:8096/web",
            "text": "Home Latest Media",
            "buttons": ["Play"],
            "inputs": [],
            "media_count": 1,
        }
        self.control_inventory = {
            "visible_controls": [
                {
                    "name": "Play",
                    "title": "Play",
                    "aria_label": None,
                    "classes": ["mediaButton"],
                    "href": None,
                    "data_action": None,
                    "data_id": None,
                    "data_isfavorite": None,
                    "scope": "player",
                    "selector": "[data-jfat-target-id=\"control-play\"]",
                    "target": {"kind": "control", "name": "Play", "scope": "player"},
                }
            ],
            "visible_links": [
                {
                    "name": "Music",
                    "title": None,
                    "aria_label": None,
                    "classes": ["navMenuOption"],
                    "href": "#/music",
                    "data_action": None,
                    "data_id": "music",
                    "data_isfavorite": None,
                    "scope": "drawer",
                    "selector": "[data-jfat-target-id=\"link-music\"]",
                    "target": {"kind": "link", "name": "Music"},
                }
            ],
            "player_controls": [
                {
                    "name": "Play",
                    "title": "Play",
                    "aria_label": None,
                    "classes": ["mediaButton"],
                    "href": None,
                    "data_action": None,
                    "data_id": None,
                    "data_isfavorite": None,
                    "scope": "player",
                    "selector": "[data-jfat-target-id=\"control-play\"]",
                    "target": {"kind": "control", "name": "Play", "scope": "player"},
                }
            ],
        }

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, payload):
        for handler in self.handlers.get(event, []):
            handler(payload)

    def locator(self, selector):
        self.calls.append(("locator", selector))
        locator = FakeLocator(self, selector)
        self.locator_instances.append(locator)
        return locator

    def get_by_text(self, text, exact=True):
        self.calls.append(("get_by_text", text, exact))
        locator = FakeLocator(self, f"text={text}")
        self.locator_instances.append(locator)
        return locator

    def goto(self, url, wait_until="networkidle", timeout=None):
        self.calls.append(("goto", url, wait_until, timeout))
        self.url = url
        self.emit("console", FakeConsoleMessage("warning", "deprecated option"))
        self.emit("response", FakeResponse(url + "/api/items", 500, "Server Error"))

    def reload(self, wait_until="networkidle", timeout=None):
        self.calls.append(("reload", wait_until, timeout))

    def wait_for_load_state(self, state, timeout=None):
        self.calls.append(("wait_for_load_state", state, timeout))

    def wait_for_timeout(self, timeout):
        self.calls.append(("wait_for_timeout", timeout))

    def wait_for_function(self, script, *, arg=None, timeout=None, polling=None):
        self.calls.append(("wait_for_function", script, arg, timeout))

    def wait_for_url(self, target, timeout=None):
        self.calls.append(("wait_for_url", target, timeout))

    def evaluate(self, script, args=None):
        self.calls.append(("evaluate", script, args))
        if script == DOM_SUMMARY_SCRIPT:
            return dict(self.dom_summary)
        if script == MEDIA_STATE_SCRIPT:
            return list(self.media)
        if script == CONTROL_INVENTORY_SCRIPT:
            return dict(self.control_inventory)
        return {"ok": True, "args": args}

    def screenshot(self, path, full_page=True):
        self.calls.append(("screenshot", path, full_page))
        Path(path).write_bytes(b"png")

    def content(self):
        self.calls.append(("content",))
        return "<html><head><title>Jellyfin</title></head><body>Home</body></html>"

    def title(self):
        self.calls.append(("title",))
        return "Jellyfin"


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.contexts = []
        self.closed = False

    def new_context(self, viewport=None, locale=None):
        context = FakeContext(self.page)
        self.contexts.append(
            {"viewport": viewport, "locale": locale, "context": context}
        )
        return context

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, page):
        self.browser = FakeBrowser(page)
        self.launches = []

    def launch(self, headless=True):
        self.launches.append({"headless": headless})
        return self.browser


class FakePlaywright:
    def __init__(self, page):
        self.chromium = FakeChromium(page)


class FakePlaywrightManager:
    def __init__(self, page):
        self.playwright = FakePlaywright(page)
        self.exited = False

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc, traceback):
        self.exited = True


class BrowserDriverTests(unittest.TestCase):
    def make_driver(self, artifacts_root, page):
        manager = FakePlaywrightManager(page)
        driver = BrowserDriver(
            artifacts_root=artifacts_root,
            base_url="http://localhost:8096",
            run_id="run-1",
            playwright_factory=lambda: manager,
        )
        return driver, manager

    def test_dispatch_requeries_locators_per_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            page.locator_counts["#name"] = 1
            driver, _ = self.make_driver(temp_dir, page)

            result = driver.run(
                {
                    "auth": "none",
                    "actions": [
                        {
                            "type": "click",
                            "target": {"kind": "css", "selector": "#name"},
                        },
                        {"type": "fill", "selector": "#name", "value": "secret"},
                    ],
                }
            )

            self.assertEqual(result["status"], "pass")
            self.assertEqual(
                [call for call in page.calls if call[0] == "locator"],
                [("locator", "#name"), ("locator", "#name")],
            )
            self.assertIsNot(page.locator_instances[0], page.locator_instances[1])
            self.assertEqual(
                result["actions"][1]["value_metadata"],
                {"type": "string", "length": 6},
            )

    def test_goto_resolves_relative_path_and_refresh_reloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            driver, _ = self.make_driver(temp_dir, page)

            result = driver.run(
                {
                    "path": "/web/index.html",
                    "actions": [
                        {"type": "goto"},
                        {"type": "refresh"},
                    ],
                }
            )

            self.assertEqual(result["status"], "pass")
            self.assertIn(
                ("goto", "http://localhost:8096/web/index.html", "domcontentloaded", 30000),
                page.calls,
            )
            self.assertIn(("reload", "domcontentloaded", 30000), page.calls)
            self.assertIn(("wait_for_load_state", "networkidle", 5000), page.calls)

    def test_run_single_action_wraps_one_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            driver, _ = self.make_driver(temp_dir, page)

            result = driver.run_single_action(
                {"path": "/web/index.html"},
                {"type": "goto"},
            )

            self.assertEqual(result["status"], "pass")
            self.assertEqual(len(result["actions"]), 1)
            self.assertEqual(result["actions"][0]["type"], "goto")
            self.assertIn(
                ("goto", "http://localhost:8096/web/index.html", "domcontentloaded", 30000),
                page.calls,
            )

    def test_click_can_target_visible_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            page.locator_counts["text=Songs"] = 1
            driver, _ = self.make_driver(temp_dir, page)

            result = driver.run(
                {
                    "actions": [
                        {"type": "click", "target": {"kind": "text", "name": "Songs"}},
                    ],
                }
            )

            self.assertEqual(result["status"], "pass")
            self.assertIn(("get_by_text", "Songs", True), page.calls)
            self.assertIn(("click", "text=Songs", 8000), page.calls)

    def test_click_can_target_inventory_control_and_link(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            page.locator_counts['[data-jfat-target-id="control-play"]'] = 1
            page.locator_counts['[data-jfat-target-id="link-music"]'] = 1
            driver, _ = self.make_driver(temp_dir, page)

            result = driver.run(
                {
                    "actions": [
                        {
                            "type": "click",
                            "target": {
                                "kind": "control",
                                "name": "Play",
                                "scope": "player",
                            },
                        },
                        {
                            "type": "click",
                            "target": {"kind": "link", "name": "Music"},
                        },
                    ],
                }
            )

            self.assertEqual(result["status"], "pass")
            self.assertIn(
                ("click", '[data-jfat-target-id="control-play"]', 8000),
                page.calls,
            )
            self.assertIn(
                ("click", '[data-jfat-target-id="link-music"]', 8000),
                page.calls,
            )
            self.assertEqual(result["player_controls"][0]["target"]["scope"], "player")
            self.assertEqual(
                result["visible_links"][0]["target"],
                {"kind": "link", "name": "Music"},
            )

    def test_click_control_target_reports_ambiguous_matches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            page.control_inventory["visible_controls"].append(
                {
                    **page.control_inventory["visible_controls"][0],
                    "selector": "[data-jfat-target-id=\"control-play-2\"]",
                }
            )
            driver, _ = self.make_driver(temp_dir, page)

            result = driver.run(
                {
                    "actions": [
                        {
                            "type": "click",
                            "target": {
                                "kind": "control",
                                "name": "Play",
                                "scope": "player",
                            },
                        },
                    ],
                }
            )

            self.assertEqual(result["status"], "fail")
            self.assertIn("multiple visible control targets", result["error"])
            self.assertEqual(
                result["actions"][0]["target_diagnostics"]["match_count"],
                2,
            )

    def test_stage2web_test6_fixture_exposes_player_favorite_and_stop_targets(self):
        fixture = (
            Path(__file__).resolve().parents[1]
            / "debug"
            / "stage2web-test6"
            / "web-client-a3b8c6d9-c9fe-49b6-aa47-10de09479cc8"
            / "browser_dom"
            / "step_2_6.html"
        )
        html = fixture.read_text(encoding="utf-8")
        self.assertIn('class="nowPlayingBar"', html)
        self.assertIn('title="Stop"', html)
        self.assertIn('title="Add to favorites"', html)

        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            player_controls = [
                {
                    "name": "Add to favorites",
                    "title": "Add to favorites",
                    "aria_label": None,
                    "classes": ["mediaButton", "emby-button"],
                    "href": None,
                    "data_action": None,
                    "data_id": "933e3a903b3ea327b4acfbeb46445b92",
                    "data_isfavorite": "false",
                    "scope": "player",
                    "selector": "[data-jfat-target-id=\"control-favorite\"]",
                    "target": {
                        "kind": "control",
                        "name": "Add to favorites",
                        "scope": "player",
                    },
                },
                {
                    "name": "Stop",
                    "title": "Stop",
                    "aria_label": None,
                    "classes": ["stopButton", "mediaButton"],
                    "href": None,
                    "data_action": None,
                    "data_id": None,
                    "data_isfavorite": None,
                    "scope": "player",
                    "selector": "[data-jfat-target-id=\"control-stop\"]",
                    "target": {"kind": "control", "name": "Stop", "scope": "player"},
                },
            ]
            page.control_inventory = {
                "visible_controls": player_controls,
                "visible_links": [],
                "player_controls": player_controls,
            }
            driver, _ = self.make_driver(temp_dir, page)

            result = driver.run(
                {"actions": [{"type": "screenshot", "label": "fixture"}]}
            )

            self.assertIn(
                {
                    "kind": "control",
                    "name": "Add to favorites",
                    "scope": "player",
                },
                [control["target"] for control in result["player_controls"]],
            )
            self.assertIn(
                {"kind": "control", "name": "Stop", "scope": "player"},
                [control["target"] for control in result["player_controls"]],
            )

    def test_wait_for_media_supports_stopped_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            page.media = [
                {
                    "tag": "audio",
                    "paused": True,
                    "ended": False,
                    "error": None,
                    "currentTime": 0,
                }
            ]
            driver, _ = self.make_driver(temp_dir, page)

            result = driver.run(
                {
                    "actions": [
                        {"type": "wait_for_media", "state": "stopped"},
                    ],
                }
            )

            self.assertEqual(result["status"], "pass")
            self.assertIn(
                ("wait_for_function", MEDIA_WAIT_SCRIPT, "stopped", 30000),
                page.calls,
            )
            self.assertEqual(result["media_state"]["state"], "stopped")

    def test_goto_auth_object_uses_supplied_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            page.locator_counts["#txtManualPassword, input[type='password']:visible"] = 1
            driver, _ = self.make_driver(temp_dir, page)

            result = driver.run(
                {
                    "path": "/web",
                    "auth": {"mode": "auto", "username": "demo", "password": ""},
                    "actions": [{"type": "goto"}],
                }
            )

            self.assertIn(
                (
                    "fill",
                    "#txtManualName, input[autocomplete='username']:visible, input[type='text']:visible",
                    "demo",
                    5000,
                ),
                page.calls,
            )
            self.assertIn(
                ("fill", "#txtManualPassword, input[type='password']:visible", "", 5000),
                page.calls,
            )
            self.assertEqual(result["auth"], {"mode": "auto"})

    def test_screenshot_action_writes_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            driver, _ = self.make_driver(temp_dir, page)

            result = driver.run(
                {
                    "label": "home screen",
                    "actions": [{"type": "screenshot", "label": "home screen"}],
                },
                step_id=3,
            )

            screenshot_path = Path(result["screenshot_paths"][0])
            self.assertTrue(screenshot_path.exists())
            self.assertEqual(screenshot_path.name, "home_screen.png")
            self.assertEqual(result["actions"][0]["screenshot_path"], str(screenshot_path))

    def test_console_network_dom_and_media_evidence_are_captured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            driver, _ = self.make_driver(temp_dir, page)

            result = driver.run(
                {
                    "path": "/web",
                    "label": "web",
                    "actions": [{"type": "goto"}],
                },
                step_id=2,
            )

            self.assertEqual(result["console"][0]["text"], "deprecated option")
            self.assertEqual(result["failed_network"][0]["status"], 500)
            self.assertIn("buttons", result["dom_summary"])
            self.assertTrue(Path(result["dom_path"]).exists())
            self.assertEqual(result["media_state"]["state"], "playing")
            self.assertEqual(result["page_text"], "Home Latest Media")

    def test_selector_and_capture_snapshots_use_current_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            page.locator_counts[".poster"] = 1
            page.locator_visible[".poster"] = True
            page.locator_attributes[(".poster", "data-id")] = "movie-1"
            driver, _ = self.make_driver(temp_dir, page)
            driver.run({"actions": [{"type": "wait_for", "selector": "body"}]})

            states = driver.inspect_selectors([".poster"])
            values = driver.capture_values(
                {
                    "item_id": {
                        "from": "browser_attribute",
                        "selector": ".poster",
                        "name": "data-id",
                    },
                    "eval_result": {"from": "browser_eval", "script": "() => 1"},
                }
            )

            self.assertEqual(states[".poster"], {"attached": True, "visible": True})
            self.assertEqual(values["item_id"], "movie-1")
            self.assertEqual(values["eval_result"], {"ok": True, "args": None})

    def test_failed_request_event_is_captured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            driver, _ = self.make_driver(temp_dir, page)

            page.failed_request_on_wait = FakeRequest(
                "http://localhost/bad",
                "net::ERR_FAILED",
            )
            result = driver.run({"actions": [{"type": "wait_for", "selector": "body"}]})

            self.assertEqual(result["failed_network"][0]["error"], "net::ERR_FAILED")

    def test_close_closes_context_browser_and_playwright(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            driver, manager = self.make_driver(temp_dir, page)

            driver.run({"actions": [{"type": "wait_for", "selector": "body"}]})
            driver.close()

            self.assertTrue(manager.playwright.chromium.browser.closed)
            self.assertTrue(manager.exited)

    def test_launch_uses_browser_visibility_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            driver, manager = self.make_driver(temp_dir, page)

            with patch.dict("os.environ", {BROWSER_HEADLESS_ENV: "gui"}, clear=True):
                driver.run({"actions": [{"type": "wait_for", "selector": "body"}]})

            self.assertEqual(
                manager.playwright.chromium.launches,
                [{"headless": False}],
            )

    def test_context_defaults_to_en_us_locale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            driver, manager = self.make_driver(temp_dir, page)

            result = driver.run({"actions": [{"type": "wait_for", "selector": "body"}]})

            self.assertEqual(result["locale"], "en-US")
            self.assertEqual(
                manager.playwright.chromium.browser.contexts[0]["locale"],
                "en-US",
            )

    def test_context_uses_explicit_locale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            page = FakePage()
            driver, manager = self.make_driver(temp_dir, page)

            result = driver.run(
                {
                    "locale": "fr-FR",
                    "actions": [{"type": "wait_for", "selector": "body"}],
                }
            )

            self.assertEqual(result["locale"], "fr-FR")
            self.assertEqual(
                manager.playwright.chromium.browser.contexts[0]["locale"],
                "fr-FR",
            )


if __name__ == "__main__":
    unittest.main()
