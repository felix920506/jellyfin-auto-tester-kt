import json
import tempfile
import unittest

from tools.criteria import (
    CaptureError,
    UnboundVariableError,
    evaluate_criteria,
    extract_captures,
    extract_json_path,
    resolve_references,
)


class ExecutionCriteriaTests(unittest.TestCase):
    def test_evaluate_all_of_http_criteria(self):
        result = evaluate_criteria(
            {
                "all_of": [
                    {"type": "status_code", "equals": 200},
                    {"type": "body_json_path", "path": "$.Items[0].Id", "equals": "abc"},
                ]
            },
            {
                "tool": "http_request",
                "http": {
                    "status_code": 200,
                    "body": json.dumps({"Items": [{"Id": "abc"}]}),
                },
            },
        )

        self.assertTrue(result["passed"])
        self.assertTrue(all(item["passed"] for item in result["assertions"]))

    def test_evaluate_any_of_log_criteria(self):
        result = evaluate_criteria(
            {
                "any_of": [
                    {"type": "status_code", "equals": 500},
                    {"type": "log_matches", "pattern": "HEVC decode error"},
                ]
            },
            {
                "tool": "http_request",
                "http": {"status_code": 200, "body": "{}"},
                "logs_since_step_start": "HEVC decode error at frame 42",
            },
        )

        self.assertTrue(result["passed"])
        self.assertFalse(result["assertions"][0]["passed"])
        self.assertTrue(result["assertions"][1]["passed"])

    def test_inapplicable_criteria_fail_with_diagnostic(self):
        result = evaluate_criteria(
            {"all_of": [{"type": "status_code", "equals": 204}]},
            {"tool": "bash", "stdout": "ok", "exit_code": 0},
        )

        self.assertFalse(result["passed"])
        self.assertIn("criterion not applicable", result["assertions"][0]["message"])

    def test_screenshot_present_checks_file_path(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            result = evaluate_criteria(
                {"all_of": [{"type": "screenshot_present", "label": "step_1"}]},
                {"screenshots": {"step_1": image.name}},
            )

        self.assertTrue(result["passed"])

    def test_extract_json_path_supports_dots_indexes_and_quoted_keys(self):
        payload = {"Items": [{"Id": "abc", "media.path": "/media/test.mkv"}]}

        self.assertEqual(extract_json_path(payload, "$.Items[0].Id"), "abc")
        self.assertEqual(
            extract_json_path(payload, "$.Items[0]['media.path']"),
            "/media/test.mkv",
        )

    def test_resolve_references_recurses_over_json_values(self):
        resolved = resolve_references(
            {
                "path": "/Items/${item_id}/PlaybackInfo",
                "body": {"ids": ["${item_id}"]},
            },
            {"item_id": "abc"},
        )

        self.assertEqual(resolved["path"], "/Items/abc/PlaybackInfo")
        self.assertEqual(resolved["body"]["ids"], ["abc"])

    def test_resolve_references_rejects_missing_variable(self):
        with self.assertRaises(UnboundVariableError) as raised:
            resolve_references("/Items/${missing}", {})

        self.assertEqual(raised.exception.name, "missing")

    def test_extract_captures_from_http_and_stdout(self):
        captures = extract_captures(
            {
                "item_id": {"from": "body_json_path", "path": "$.Items[0].Id"},
                "etag": {"from": "header", "name": "etag"},
                "trimmed": {"from": "stdout_trimmed"},
                "movie_id": {"from": "stdout_regex", "pattern": r"id=(\d+)"},
                "code": {"from": "exit_code"},
            },
            {
                "http": {
                    "body": json.dumps({"Items": [{"Id": "abc"}]}),
                    "headers": {"ETag": "v1"},
                },
                "stdout": " id=42 \n",
                "exit_code": 0,
            },
        )

        self.assertEqual(
            captures,
            {
                "item_id": "abc",
                "etag": "v1",
                "trimmed": "id=42",
                "movie_id": "42",
                "code": 0,
            },
        )

    def test_evaluate_browser_criteria(self):
        context = {
            "tool": "browser",
            "browser": {
                "status": "pass",
                "actions": [{"type": "click", "status": "pass"}],
                "final_url": "http://localhost:8096/web/index.html#!/details",
                "page_text": "Playback failed unexpectedly",
                "media_state": {"state": "errored"},
                "console": [{"type": "error", "text": "React playback exception"}],
            },
            "browser_elements": {
                ".toast": {"attached": True, "visible": True},
                ".spinner": {"attached": False, "visible": False},
            },
        }

        result = evaluate_criteria(
            {
                "all_of": [
                    {"type": "browser_action_run"},
                    {
                        "type": "browser_element",
                        "selector": ".toast",
                        "state": "visible",
                    },
                    {
                        "type": "browser_element",
                        "selector": ".spinner",
                        "state": "detached",
                    },
                    {
                        "type": "browser_text_contains",
                        "value": "Playback failed",
                    },
                    {
                        "type": "browser_url_matches",
                        "pattern": r"/web/index\.html",
                    },
                    {"type": "browser_media_state", "state": "errored"},
                    {
                        "type": "browser_console_matches",
                        "pattern": "playback exception",
                    },
                ]
            },
            context,
        )

        self.assertTrue(result["passed"])

    def test_evaluate_legacy_browser_criteria_shape(self):
        result = evaluate_criteria(
            {
                "all_of": [
                    {"browser_text_contains": {"text": "Songs"}},
                    {
                        "browser_element": {
                            "selector": "[role='row']",
                            "exists": True,
                        }
                    },
                    {"browser_media_state": {"state": "stopped"}},
                ]
            },
            {
                "tool": "browser",
                "browser": {
                    "status": "pass",
                    "actions": [],
                    "page_text": "Home Songs",
                    "media_state": {"state": "paused"},
                },
                "browser_elements": {
                    "[role='row']": {"attached": True, "visible": True},
                },
            },
        )

        self.assertTrue(result["passed"])

    def test_browser_action_run_fails_when_an_action_failed(self):
        result = evaluate_criteria(
            {"all_of": [{"type": "browser_action_run"}]},
            {
                "tool": "browser",
                "browser": {
                    "status": "fail",
                    "actions": [{"type": "click", "status": "fail"}],
                },
            },
        )

        self.assertFalse(result["passed"])

    def test_extract_captures_from_browser_context(self):
        captures = extract_captures(
            {
                "text": {"from": "browser_text"},
                "url": {"from": "browser_url"},
                "attribute": {
                    "from": "browser_attribute",
                    "selector": ".poster",
                    "name": "data-id",
                },
                "evaluated": {"from": "browser_eval", "script": "() => 1"},
            },
            {
                "browser": {
                    "page_text": "Home screen",
                    "final_url": "http://localhost:8096/web",
                },
                "browser_attributes": {
                    ".poster": {"data-id": "movie-1"},
                },
                "browser_capture_values": {
                    "evaluated": 1,
                },
            },
        )

        self.assertEqual(
            captures,
            {
                "text": "Home screen",
                "url": "http://localhost:8096/web",
                "attribute": "movie-1",
                "evaluated": 1,
            },
        )

    def test_extract_captures_reports_variable_name_on_failure(self):
        with self.assertRaises(CaptureError) as raised:
            extract_captures(
                {"missing": {"from": "body_regex", "pattern": r"id=(\d+)"}},
                {"http": {"body": "no id here"}},
            )

        self.assertEqual(raised.exception.variable, "missing")
        self.assertIn("regex did not match", raised.exception.reason)


if __name__ == "__main__":
    unittest.main()
