import copy
import tempfile
import unittest
from pathlib import Path

from creatures.report.tools import report_writer


def sample_plan():
    return {
        "issue_url": "https://github.com/jellyfin/jellyfin/issues/1234",
        "issue_title": "PlaybackInfo returns 500",
        "target_version": "10.9.7",
        "docker_image": "jellyfin/jellyfin:10.9.7",
        "prerequisites": [
            {
                "type": "media_file",
                "description": "HEVC sample present in the media library",
                "source": "generate with ffmpeg: ffmpeg -f lavfi -i testsrc out.mkv",
            }
        ],
        "environment": {
            "ports": {"host": 8096, "container": 8096},
            "volumes": [{"host": "/tmp/jf-config", "container": "/config"}],
            "env_vars": {"JELLYFIN_PublishedServerUrl": "http://localhost:8096"},
        },
        "reproduction_steps": [
            {
                "step_id": 1,
                "role": "setup",
                "action": "Create the media item",
                "tool": "bash",
                "input": {"command": "printf 'item-abc\\n'"},
                "capture": {"item_id": {"from": "stdout_trimmed"}},
                "expected_outcome": "The item id is printed.",
                "success_criteria": {"all_of": [{"type": "exit_code", "equals": 0}]},
            },
            {
                "step_id": 2,
                "role": "trigger",
                "action": "Request playback info for the HEVC item",
                "tool": "http_request",
                "input": {
                    "method": "POST",
                    "path": "/Items/item-abc/PlaybackInfo",
                    "body": {"StartTimeTicks": 0},
                },
                "expected_outcome": "Jellyfin returns HTTP 500 with Transcoding failed.",
                "success_criteria": {
                    "all_of": [
                        {"type": "status_code", "equals": 500},
                        {"type": "body_contains", "value": "Transcoding failed"},
                    ]
                },
            },
            {
                "step_id": 3,
                "role": "verify",
                "action": "Capture the playback failure page",
                "tool": "screenshot",
                "input": {"path": "/web/index.html", "label": "playback_error"},
                "expected_outcome": "A screenshot of the playback error is saved.",
                "success_criteria": {
                    "all_of": [
                        {"type": "screenshot_present", "label": "playback_error"}
                    ]
                },
            },
        ],
        "reproduction_goal": "Observe PlaybackInfo returning 500 for the HEVC item.",
        "failure_indicators": ["Transcoding failed"],
        "confidence": "high",
        "ambiguities": ["The original issue did not include a sample file."],
        "is_verification": False,
        "original_run_id": None,
    }


def sample_result(artifacts_root, run_id="run-1", overall_result="reproduced"):
    artifacts_dir = Path(artifacts_root) / run_id
    screenshots_dir = artifacts_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshots_dir / "playback_error.png"
    screenshot_path.write_bytes(b"png")

    trigger_outcome = "pass" if overall_result == "reproduced" else "fail"
    trigger_reason = None if trigger_outcome == "pass" else "status_code expected 500 got 200"
    status_code = 500 if trigger_outcome == "pass" else 200
    body = '{"error":"Transcoding failed"}' if trigger_outcome == "pass" else '{"ok":true}'

    return {
        "plan": sample_plan(),
        "run_id": run_id,
        "is_verification": False,
        "original_run_id": None,
        "container_id": "container-1",
        "execution_log": [
            {
                "step_id": 1,
                "role": "setup",
                "action": "Create the media item",
                "tool": "bash",
                "stdout": "item-abc\n",
                "stderr": "",
                "exit_code": 0,
                "http": None,
                "screenshot_path": None,
                "outcome": "pass",
                "reason": None,
                "criteria_evaluation": {"passed": True, "assertions": []},
                "duration_ms": 5,
            },
            {
                "step_id": 2,
                "role": "trigger",
                "action": "Request playback info for the HEVC item",
                "tool": "http_request",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "http": {"status_code": status_code, "body": body, "headers": {}},
                "screenshot_path": None,
                "outcome": trigger_outcome,
                "reason": trigger_reason,
                "criteria_evaluation": {"passed": trigger_outcome == "pass", "assertions": []},
                "duration_ms": 12,
            },
            {
                "step_id": 3,
                "role": "verify",
                "action": "Capture the playback failure page",
                "tool": "screenshot",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "http": None,
                "screenshot_path": str(screenshot_path),
                "outcome": "pass",
                "reason": None,
                "criteria_evaluation": {"passed": True, "assertions": []},
                "duration_ms": 30,
            },
        ],
        "overall_result": overall_result,
        "artifacts_dir": str(artifacts_dir),
        "jellyfin_logs": "\n".join(
            [
                "INFO server started",
                "DEBUG noisy line that should not be excerpted",
                "WARN playback pipeline warning",
                "ERROR Transcoding failed in FFmpeg",
            ]
        ),
        "error_summary": None,
    }


class ReportWriterTests(unittest.TestCase):
    def test_generate_writes_report_with_filtered_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = sample_result(temp_dir)

            metadata = report_writer.generate(result, artifacts_base=temp_dir)

            report_path = Path(temp_dir) / "run-1" / "report.md"
            self.assertEqual(Path(metadata["path"]).resolve(), report_path.resolve())
            self.assertGreater(metadata["word_count"], 50)
            self.assertIsNone(metadata["verified"])

            report = report_path.read_text(encoding="utf-8")
            self.assertIn("# Reproduction Report: PlaybackInfo returns 500", report)
            self.assertIn("**Result:** Reproduced", report)
            self.assertIn("**Verified:** Pending", report)
            self.assertIn("`POST /Items/item-abc/PlaybackInfo` -> HTTP 500", report)
            self.assertIn("ERROR Transcoding failed in FFmpeg", report)
            self.assertIn("WARN playback pipeline warning", report)
            self.assertNotIn("DEBUG noisy line", report)
            self.assertIn("![Step 3 screenshot](screenshots/playback_error.png)", report)

    def test_generate_adds_successful_verification_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = sample_result(temp_dir, run_id="run-1")
            verification = sample_result(temp_dir, run_id="run-2")
            verification["is_verification"] = True
            verification["original_run_id"] = "run-1"
            verification["plan"]["is_verification"] = True
            verification["plan"]["original_run_id"] = "run-1"

            metadata = report_writer.generate(
                original,
                verification_result=verification,
                artifacts_base=temp_dir,
            )

            self.assertTrue(metadata["verified"])
            self.assertEqual(metadata["verification_status"], "Yes")
            report = Path(metadata["path"]).read_text(encoding="utf-8")
            self.assertIn("**Verified:** Yes", report)
            self.assertIn("**Verification Run ID:** run-2", report)
            self.assertIn("**Result:** Passed", report)

    def test_generate_records_verification_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = sample_result(temp_dir, run_id="run-1")
            verification = sample_result(
                temp_dir,
                run_id="run-2",
                overall_result="not_reproduced",
            )
            verification["is_verification"] = True
            verification["original_run_id"] = "run-1"

            metadata = report_writer.generate(
                original,
                verification_result=verification,
                artifacts_base=temp_dir,
            )

            self.assertFalse(metadata["verified"])
            report = Path(metadata["path"]).read_text(encoding="utf-8")
            self.assertIn("**Verified:** No", report)
            self.assertIn("## Verification Failure", report)
            self.assertIn("verification result `not_reproduced`", report)

    def test_build_verification_plan_preserves_environment_and_links_original_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = sample_result(temp_dir, run_id="run-1")
            original_plan = copy.deepcopy(original["plan"])
            written_steps = [copy.deepcopy(original["plan"]["reproduction_steps"][1])]

            verification_plan = report_writer.build_verification_plan(
                original,
                written_steps,
            )

            self.assertTrue(verification_plan["is_verification"])
            self.assertEqual(verification_plan["original_run_id"], "run-1")
            self.assertEqual(verification_plan["environment"], original_plan["environment"])
            self.assertEqual(verification_plan["prerequisites"], original_plan["prerequisites"])
            self.assertEqual(verification_plan["reproduction_steps"], written_steps)
            self.assertFalse(original["plan"]["is_verification"])
            self.assertIsNone(original["plan"]["original_run_id"])

    def test_build_verification_plan_requires_exactly_one_trigger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = sample_result(temp_dir, run_id="run-1")
            setup_only = [copy.deepcopy(original["plan"]["reproduction_steps"][0])]

            with self.assertRaises(ValueError):
                report_writer.build_verification_plan(original, setup_only)


if __name__ == "__main__":
    unittest.main()
