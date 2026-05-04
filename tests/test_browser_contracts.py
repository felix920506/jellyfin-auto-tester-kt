import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from main import _normalize_reproduction_plan


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_schema(name):
    return json.loads((REPO_ROOT / "schemas" / name).read_text(encoding="utf-8"))


def minimal_plan():
    return {
        "issue_url": "https://github.com/jellyfin/jellyfin/issues/1",
        "issue_title": "Web bug",
        "target_version": "10.9.7",
        "docker_image": "jellyfin/jellyfin:10.9.7",
        "prerequisites": [],
        "environment": {
            "ports": {"host": 8096, "container": 8096},
            "volumes": [],
            "env_vars": {},
        },
        "reproduction_steps": [
            {
                "step_id": 1,
                "role": "trigger",
                "action": "Open Jellyfin Web and capture the home screen",
                "tool": "browser",
                "input": {
                    "path": "/web/index.html",
                    "auth": "auto",
                    "label": "web_home",
                    "viewport": {"width": 1280, "height": 720},
                    "actions": [
                        {"type": "goto"},
                        {"type": "wait_for", "selector": "body"},
                        {"type": "screenshot", "label": "web_home"},
                    ],
                },
                "expected_outcome": "The Jellyfin Web screen is visible.",
                "success_criteria": {
                    "all_of": [{"type": "screenshot_present", "label": "web_home"}]
                },
            }
        ],
        "reproduction_goal": "Observe the Web bug.",
        "failure_indicators": ["web"],
        "confidence": "high",
        "ambiguities": [],
        "is_verification": False,
        "original_run_id": None,
    }


class BrowserContractTests(unittest.TestCase):
    def test_reproduction_plan_schema_accepts_minimal_browser_step(self):
        validator = Draft202012Validator(load_schema("reproduction_plan.json"))
        plan = minimal_plan()
        plan["execution_target"] = "web_client"

        errors = sorted(validator.iter_errors(plan), key=lambda error: error.path)

        self.assertEqual(errors, [])

    def test_reproduction_plan_schema_accepts_browser_locale_override(self):
        validator = Draft202012Validator(load_schema("reproduction_plan.json"))
        plan = minimal_plan()
        plan["execution_target"] = "web_client"
        plan["reproduction_steps"][0]["input"]["locale"] = "fr-FR"

        errors = sorted(validator.iter_errors(plan), key=lambda error: error.path)

        self.assertEqual(errors, [])

    def test_reproduction_plan_schema_accepts_demo_without_docker_image(self):
        validator = Draft202012Validator(load_schema("reproduction_plan.json"))
        plan = minimal_plan()
        plan.pop("docker_image")
        plan["target_version"] = "stable"
        plan["execution_target"] = "web_client"
        plan["server_target"] = {
            "mode": "demo",
            "release_track": "stable",
            "base_url": "https://demo.jellyfin.org/stable",
            "username": "demo",
            "password": "",
            "requires_admin": False,
        }

        errors = sorted(validator.iter_errors(plan), key=lambda error: error.path)

        self.assertEqual(errors, [])

    def test_reproduction_plan_schema_requires_docker_image_by_default(self):
        validator = Draft202012Validator(load_schema("reproduction_plan.json"))
        plan = minimal_plan()
        plan.pop("docker_image")

        errors = sorted(validator.iter_errors(plan), key=lambda error: error.path)

        self.assertTrue(errors)

    def test_plan_normalization_defaults_execution_target_to_standard(self):
        normalized = _normalize_reproduction_plan(minimal_plan())

        self.assertEqual(normalized["execution_target"], "standard")

    def test_plan_normalization_preserves_demo_server_target(self):
        plan = minimal_plan()
        plan.pop("docker_image")
        plan["target_version"] = "unstable"
        plan["execution_target"] = "web_client"
        plan["server_target"] = {"mode": "demo", "release_track": "unstable"}

        normalized = _normalize_reproduction_plan(plan)

        self.assertNotIn("docker_image", normalized)
        self.assertEqual(normalized["server_target"]["mode"], "demo")
        self.assertEqual(
            normalized["server_target"]["base_url"],
            "https://demo.jellyfin.org/unstable",
        )

    def test_web_client_task_schema_accepts_browser_request(self):
        task = {
            "request_id": "request-1",
            "run_id": "run-1",
            "base_url": "http://localhost:8096",
            "artifacts_root": "/tmp/artifacts",
            "step_id": 1,
            "browser_input": {
                "path": "/web",
                "auth": {"mode": "auto", "username": "demo", "password": ""},
                "locale": "fr-FR",
                "actions": [
                    {"type": "goto"},
                    {"type": "screenshot", "label": "home"},
                ],
            },
            "selector_assertions": [{"selector": "body", "state": "visible"}],
            "capture": {"url": {"from": "browser_url"}},
            "repair_policy": {
                "enabled": True,
                "max_attempts": 1,
                "browser_input": {"locale": "en-GB", "actions": [{"type": "refresh"}]},
            },
        }
        validator = Draft202012Validator(load_schema("web_client_task.json"))

        errors = sorted(validator.iter_errors(task), key=lambda error: error.path)

        self.assertEqual(errors, [])

    def test_web_client_result_schema_accepts_browser_result(self):
        result = {
            "request_id": "request-1",
            "status": "pass",
            "browser": {
                "status": "pass",
                "actions": [{"type": "goto", "status": "pass"}],
            },
            "screenshot_path": "/tmp/home.png",
            "browser_screenshots": {"home": "/tmp/home.png"},
            "selector_states": {"body": {"attached": True, "visible": True}},
            "capture_values": {"url": "http://localhost:8096/web"},
            "error": None,
        }
        validator = Draft202012Validator(load_schema("web_client_result.json"))

        errors = sorted(validator.iter_errors(result), key=lambda error: error.path)

        self.assertEqual(errors, [])

    def test_execution_result_schema_accepts_browser_result(self):
        result = {
            "plan": minimal_plan(),
            "run_id": "run-1",
            "is_verification": False,
            "original_run_id": None,
            "container_id": "container-1",
            "execution_log": [
                {
                    "step_id": 1,
                    "role": "trigger",
                    "action": "Open Jellyfin Web and capture the home screen",
                    "tool": "browser",
                    "stdout": "",
                    "stderr": "",
                    "exit_code": None,
                    "http": None,
                    "browser": {
                        "status": "pass",
                        "actions": [{"type": "goto", "status": "pass"}],
                        "final_url": "http://localhost:8096/web/index.html",
                        "title": "Jellyfin",
                        "screenshot_paths": ["/tmp/web_home.png"],
                    },
                    "screenshot_path": "/tmp/web_home.png",
                    "outcome": "pass",
                    "reason": None,
                    "criteria_evaluation": {"passed": True, "assertions": []},
                    "duration_ms": 10,
                }
            ],
            "overall_result": "reproduced",
            "artifacts_dir": "/tmp/run-1",
            "jellyfin_logs": "",
            "error_summary": None,
        }
        validator = Draft202012Validator(load_schema("execution_result.json"))

        errors = sorted(validator.iter_errors(result), key=lambda error: error.path)

        self.assertEqual(errors, [])

    def test_stage_prompts_advertise_browser_tool(self):
        analysis_prompt = (
            REPO_ROOT / "creatures" / "analysis" / "prompts" / "system.md"
        ).read_text(encoding="utf-8")
        analysis_context = (
            REPO_ROOT / "creatures" / "analysis" / "prompts" / "context.md"
        ).read_text(encoding="utf-8")
        execution_prompt = (
            REPO_ROOT / "creatures" / "execution" / "prompts" / "system.md"
        ).read_text(encoding="utf-8")

        self.assertIn("`browser`", analysis_prompt)
        self.assertIn("`browser`", analysis_context)
        self.assertIn("`browser`", execution_prompt)

    def test_stage1_prompt_describes_web_client_routing_path(self):
        analysis_prompt = (
            REPO_ROOT / "creatures" / "analysis" / "prompts" / "system.md"
        ).read_text(encoding="utf-8")
        analysis_context = (
            REPO_ROOT / "creatures" / "analysis" / "prompts" / "context.md"
        ).read_text(encoding="utf-8")

        self.assertIn("Choose The Execution Path", analysis_prompt)
        self.assertIn("web_client_plan_ready", analysis_prompt)
        self.assertIn('execution_target: "web_client"', analysis_prompt)
        self.assertIn("When in doubt, choose `standard`", analysis_prompt)
        self.assertIn("web_client_plan_ready", analysis_context)
        self.assertIn('"web_client"', analysis_context)
        self.assertIn("demo.jellyfin.org/stable", analysis_prompt)
        self.assertIn("demo.jellyfin.org/unstable", analysis_prompt)
        self.assertIn("blank password", analysis_prompt)
        self.assertIn("specific old version", analysis_prompt)

    def test_web_client_and_report_prompts_describe_demo_verification_routing(self):
        web_client_prompt = (
            REPO_ROOT / "creatures" / "web_client" / "prompts" / "system.md"
        ).read_text(encoding="utf-8")
        report_prompt = (
            REPO_ROOT / "creatures" / "report" / "prompts" / "system.md"
        ).read_text(encoding="utf-8")

        self.assertIn("web_client_verification_request", web_client_prompt)
        self.assertIn("server_target.mode: \"demo\"", web_client_prompt)
        self.assertIn("does not own server lifecycle", web_client_prompt)
        self.assertIn("web_client_verification_request", report_prompt)
        self.assertIn('execution_target: "web_client"', report_prompt)

    def test_plan_normalization_defaults_browser_criteria_to_action_run(self):
        plan = minimal_plan()
        plan["reproduction_steps"][0].pop("success_criteria")

        normalized = _normalize_reproduction_plan(plan)

        self.assertEqual(
            normalized["reproduction_steps"][0]["success_criteria"],
            {"all_of": [{"type": "browser_action_run"}]},
        )

    def test_plan_normalization_accepts_legacy_browser_criteria_shape(self):
        plan = minimal_plan()
        plan["reproduction_steps"][0]["input"]["actions"].append(
            {"type": "wait_for_media", "state": "stopped"}
        )
        plan["reproduction_steps"][0]["success_criteria"] = {
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
        }

        normalized = _normalize_reproduction_plan(plan)

        self.assertEqual(
            normalized["reproduction_steps"][0]["success_criteria"],
            {
                "all_of": [
                    {"type": "browser_text_contains", "value": "Songs"},
                    {
                        "type": "browser_element",
                        "selector": "[role='row']",
                        "state": "exists",
                    },
                    {"type": "browser_media_state", "state": "stopped"},
                ]
            },
        )
        validator = Draft202012Validator(load_schema("reproduction_plan.json"))
        errors = sorted(validator.iter_errors(normalized), key=lambda error: error.path)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
