import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools import report_writer
from tools.execution_result_handoff import compact_execution_result


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
                    "auth": "auto",
                    "body_json": {"StartTimeTicks": 0},
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


def demo_result(artifacts_root, run_id="demo-run"):
    result = sample_result(artifacts_root, run_id=run_id)
    result["plan"].pop("docker_image", None)
    result["plan"]["target_version"] = "stable"
    result["plan"]["execution_target"] = "web_client"
    result["plan"]["server_target"] = {
        "mode": "demo",
        "release_track": "stable",
        "base_url": "https://demo.jellyfin.org/stable",
        "username": "demo",
        "password": "",
        "requires_admin": False,
    }
    result["plan"]["prerequisites"] = []
    result["plan"]["environment"] = {"ports": {}, "volumes": [], "env_vars": {}}
    result["container_id"] = None
    result["jellyfin_logs"] = ""
    return result


class ReportWriterTests(unittest.TestCase):
    def test_summarize_execution_result_uses_program_step_summaries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = sample_result(temp_dir)
            result["step_summaries"] = [
                {
                    "step_id": 1,
                    "role": "setup",
                    "planned_action": "Create the media item",
                    "status": "pass",
                    "reason": None,
                    "criteria_evaluation": {"passed": True},
                    "decisive_attempt_id": "attempt-1",
                    "evidence_refs": [],
                },
                {
                    "step_id": 2,
                    "role": "trigger",
                    "planned_action": "Request playback info for the HEVC item",
                    "status": "pass",
                    "reason": None,
                    "criteria_evaluation": {"passed": True},
                    "decisive_attempt_id": "attempt-2",
                    "evidence_refs": [{"type": "http", "status_code": 500}],
                },
            ]
            result["trigger_summary"] = {
                "step_id": 2,
                "status": "pass",
                "decisive_attempt_id": "attempt-2",
                "reason": None,
            }

            summary = report_writer.summarize_execution_result(result)

            self.assertEqual(summary["source"], "step_summaries")
            self.assertEqual(summary["trigger"]["source"], "trigger_summary")
            self.assertEqual(summary["trigger"]["status"], "pass")
            self.assertEqual(summary["trigger"]["decisive_attempt_id"], "attempt-2")
            self.assertEqual(len(summary["steps"]), 2)

    def test_summarize_execution_result_falls_back_to_execution_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = sample_result(temp_dir)
            result["execution_log"][1]["outcome"] = "fail"
            result["execution_log"][1]["reason"] = "status_code expected 500 got 200"
            result["execution_log"][1]["criteria_evaluation"] = {
                "passed": False,
                "assertions": [],
            }

            summary = report_writer.summarize_execution_result(result)

            self.assertEqual(summary["source"], "execution_log")
            self.assertEqual(summary["trigger"]["status"], "fail")
            self.assertEqual(
                summary["trigger"]["reason"],
                "status_code expected 500 got 200",
            )

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

    def test_collect_report_evidence_returns_structured_report_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = sample_result(temp_dir)

            evidence = report_writer.collect_report_evidence(
                result,
                output_dir=Path(result["artifacts_dir"]),
                artifacts_root=temp_dir,
            )

            self.assertEqual(
                evidence["logs"]["lines"],
                ["WARN playback pipeline warning", "ERROR Transcoding failed in FFmpeg"],
            )
            self.assertEqual(evidence["http_responses"][0]["method"], "POST")
            self.assertEqual(evidence["http_responses"][0]["status_code"], 500)
            self.assertIn("Transcoding failed", evidence["http_responses"][0]["body"])
            self.assertEqual(
                evidence["screenshots"][0]["relative_path"],
                "screenshots/playback_error.png",
            )

    def test_select_report_steps_uses_deterministic_minimal_set(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = sample_result(temp_dir)
            late_setup = {
                "step_id": 4,
                "role": "setup",
                "action": "Create unrelated state after trigger",
                "tool": "bash",
                "input": {"command": "true"},
                "expected_outcome": "The command exits successfully.",
                "success_criteria": {"all_of": [{"type": "exit_code", "equals": 0}]},
            }
            late_verify = {
                "step_id": 5,
                "role": "verify",
                "action": "Check the same failure again",
                "tool": "http_request",
                "input": {"method": "GET", "path": "/System/Info"},
                "expected_outcome": "The server remains reachable.",
                "success_criteria": {"all_of": [{"type": "status_code", "equals": 200}]},
            }
            result["plan"]["reproduction_steps"].extend([late_setup, late_verify])

            steps = report_writer.select_report_steps(result)

            self.assertEqual([step["step_id"] for step in steps], [1, 2, 3, 5])
            self.assertEqual(sum(1 for step in steps if step["role"] == "trigger"), 1)
            self.assertIsNot(steps[0], result["plan"]["reproduction_steps"][0])
            self.assertNotIn(late_setup, steps)

    def test_generate_hydrates_compact_execution_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = sample_result(temp_dir)
            result_path = Path(result["artifacts_dir"]) / "result.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            compact = compact_execution_result(result)

            metadata = report_writer.generate(compact, artifacts_base=temp_dir)

            report = Path(metadata["path"]).read_text(encoding="utf-8")
            self.assertIn("# Reproduction Report: PlaybackInfo returns 500", report)
            self.assertIn("**Result:** Reproduced", report)

    def test_generate_includes_browser_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = sample_result(temp_dir)
            browser_path = Path(temp_dir) / "run-1" / "screenshots" / "web_flow.png"
            browser_path.write_bytes(b"png")
            result["plan"]["reproduction_steps"].append(
                {
                    "step_id": 4,
                    "role": "verify",
                    "action": "Drive the Web playback screen",
                    "tool": "browser",
                    "input": {
                        "path": "/web/index.html",
                        "label": "web_flow",
                        "actions": [
                            {"type": "goto"},
                            {"type": "click", "selector": ".play-button"},
                            {"type": "screenshot", "label": "web_flow"},
                        ],
                    },
                    "expected_outcome": "The playback screen shows the failure.",
                    "success_criteria": {
                        "all_of": [{"type": "browser_media_state", "state": "errored"}]
                    },
                }
            )
            result["execution_log"].append(
                {
                    "step_id": 4,
                    "role": "verify",
                    "action": "Drive the Web playback screen",
                    "tool": "browser",
                    "stdout": "",
                    "stderr": "",
                    "exit_code": None,
                    "http": None,
                    "browser": {
                        "status": "pass",
                        "actions": [
                            {"type": "goto", "status": "pass"},
                            {
                                "type": "click",
                                "status": "pass",
                                "selector": ".play-button",
                            },
                            {
                                "type": "screenshot",
                                "status": "pass",
                                "label": "web_flow",
                                "screenshot_path": str(browser_path),
                            },
                        ],
                        "screenshot_paths": [str(browser_path)],
                        "final_url": "http://localhost:8096/web/index.html",
                        "console": [{"type": "error", "text": "Playback exploded"}],
                        "failed_network": [
                            {
                                "url": "http://localhost:8096/Videos/1/stream",
                                "status": 500,
                            }
                        ],
                        "media_state": {"state": "errored"},
                        "dom_summary": "text='Playback error'",
                    },
                    "screenshot_path": str(browser_path),
                    "outcome": "pass",
                    "reason": None,
                    "criteria_evaluation": {"passed": True, "assertions": []},
                    "duration_ms": 40,
                }
            )

            metadata = report_writer.generate(result, artifacts_base=temp_dir)

            report = Path(metadata["path"]).read_text(encoding="utf-8")
            self.assertIn("Run browser flow at `/web/index.html`", report)
            self.assertIn("### Browser Evidence", report)
            self.assertIn("click .play-button", report)
            self.assertIn("Media state: `errored`", report)
            self.assertIn("Playback exploded", report)
            self.assertIn("stream", report)
            self.assertIn("![Step 4 screenshot](screenshots/web_flow.png)", report)

    def test_generate_describes_demo_server_without_log_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = demo_result(temp_dir)

            metadata = report_writer.generate(result, artifacts_base=temp_dir)

            report = Path(metadata["path"]).read_text(encoding="utf-8")
            self.assertIn("Public demo server", report)
            self.assertIn("https://demo.jellyfin.org/stable", report)
            self.assertIn("Login as `demo` with blank password", report)
            self.assertIn("does not collect Jellyfin server logs", report)
            self.assertNotIn("Docker Image", report)
            self.assertNotIn("docker run", report)

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
            result_path = Path(original["artifacts_dir"]) / "result.json"
            result_path.write_text(json.dumps(original), encoding="utf-8")
            compact = compact_execution_result(original)
            original_plan = copy.deepcopy(original["plan"])
            written_steps = [copy.deepcopy(original["plan"]["reproduction_steps"][1])]

            verification_plan = report_writer.build_verification_plan(
                compact,
                written_steps,
            )

            self.assertTrue(verification_plan["is_verification"])
            self.assertEqual(verification_plan["original_run_id"], "run-1")
            self.assertEqual(verification_plan["environment"], original_plan["environment"])
            self.assertEqual(verification_plan["prerequisites"], original_plan["prerequisites"])
            self.assertEqual(verification_plan["reproduction_steps"], written_steps)
            self.assertFalse(original["plan"]["is_verification"])
            self.assertIsNone(original["plan"]["original_run_id"])

    def test_build_verification_plan_preserves_demo_target_without_docker_image(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = demo_result(temp_dir)
            written_steps = [copy.deepcopy(original["plan"]["reproduction_steps"][1])]

            verification_plan = report_writer.build_verification_plan(
                original,
                written_steps,
            )

            self.assertNotIn("docker_image", verification_plan)
            self.assertEqual(
                verification_plan["server_target"],
                original["plan"]["server_target"],
            )
            self.assertEqual(verification_plan["execution_target"], "web_client")

    def test_build_verification_plan_requires_exactly_one_trigger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = sample_result(temp_dir, run_id="run-1")
            setup_only = [copy.deepcopy(original["plan"]["reproduction_steps"][0])]

            with self.assertRaises(ValueError):
                report_writer.build_verification_plan(original, setup_only)

    def test_load_original_context_hydrates_compact_verification_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = sample_result(temp_dir, run_id="run-1")
            original_path = Path(original["artifacts_dir"]) / "result.json"
            original_path.write_text(json.dumps(original), encoding="utf-8")
            report_writer.generate(original, artifacts_base=temp_dir)

            verification = sample_result(temp_dir, run_id="run-2")
            verification["is_verification"] = True
            verification["original_run_id"] = "run-1"
            verification["plan"]["is_verification"] = True
            verification["plan"]["original_run_id"] = "run-1"
            verification_path = Path(verification["artifacts_dir"]) / "result.json"
            verification_path.write_text(json.dumps(verification), encoding="utf-8")

            context = report_writer.load_original_context(
                compact_execution_result(verification),
                artifacts_base=temp_dir,
            )

            self.assertEqual(context["original_result"]["run_id"], "run-1")
            self.assertEqual(
                Path(context["report_path"]).resolve(),
                (Path(temp_dir) / "run-1" / "report.md").resolve(),
            )
            self.assertIn(
                "# Reproduction Report: PlaybackInfo returns 500",
                context["report_markdown"],
            )

    def test_load_original_context_requires_original_run_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            verification = sample_result(temp_dir, run_id="run-2")
            verification["is_verification"] = True
            verification["original_run_id"] = None

            with self.assertRaisesRegex(ValueError, "original_run_id is required"):
                report_writer.load_original_context(
                    verification,
                    artifacts_base=temp_dir,
                )

    def test_load_original_context_requires_original_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            verification = sample_result(temp_dir, run_id="run-2")
            verification["is_verification"] = True
            verification["original_run_id"] = "missing-run"

            with self.assertRaisesRegex(FileNotFoundError, "original result not found"):
                report_writer.load_original_context(
                    verification,
                    artifacts_base=temp_dir,
                )

    def test_load_original_context_requires_original_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = sample_result(temp_dir, run_id="run-1")
            result_path = Path(original["artifacts_dir"]) / "result.json"
            result_path.write_text(json.dumps(original), encoding="utf-8")
            verification = sample_result(temp_dir, run_id="run-2")
            verification["is_verification"] = True
            verification["original_run_id"] = "run-1"

            with self.assertRaisesRegex(FileNotFoundError, "original report not found"):
                report_writer.load_original_context(
                    verification,
                    artifacts_base=temp_dir,
                )


if __name__ == "__main__":
    unittest.main()
