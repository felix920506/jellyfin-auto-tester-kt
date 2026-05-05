import copy
import json
import unittest
from pathlib import Path

import yaml
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

    def test_reproduction_plan_schema_rejects_multi_action_browser_step(self):
        validator = Draft202012Validator(load_schema("reproduction_plan.json"))
        plan = minimal_plan()
        plan["execution_target"] = "web_client"
        plan["reproduction_steps"][0]["input"]["actions"] = [
            {"type": "goto"},
            {"type": "screenshot", "label": "web_home"},
        ]

        errors = sorted(validator.iter_errors(plan), key=lambda error: error.path)

        self.assertTrue(errors)

    def test_reproduction_plan_schema_requires_typed_click_targets(self):
        validator = Draft202012Validator(load_schema("reproduction_plan.json"))
        plan = minimal_plan()
        plan["execution_target"] = "web_client"
        plan["reproduction_steps"][0]["input"]["actions"] = [
            {
                "type": "click",
                "target": {
                    "kind": "control",
                    "name": "Add to favorites",
                    "scope": "player",
                },
            }
        ]
        legacy = copy.deepcopy(plan)
        legacy["reproduction_steps"][0]["input"]["actions"] = [
            {"type": "click", "selector": ".btnFavorite"}
        ]

        self.assertEqual(list(validator.iter_errors(plan)), [])
        self.assertTrue(list(validator.iter_errors(legacy)))

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

    def test_web_client_task_schema_accepts_session_commands(self):
        start_task = {
            "command": "start",
            "request_id": "request-1",
            "run_id": "run-1",
            "base_url": "http://localhost:8096",
            "artifacts_root": "/tmp/artifacts",
            "step_id": 1,
            "browser_input": {
                "path": "/web",
                "auth": {"mode": "auto", "username": "demo", "password": ""},
                "locale": "fr-FR",
            },
        }
        action_task = {
            "command": "action",
            "request_id": "request-2",
            "action": {"type": "goto"},
            "selector_assertions": [{"selector": "body", "state": "visible"}],
            "capture": {"url": {"from": "browser_url"}},
        }
        finalize_task = {
            "command": "finalize",
            "request_id": "request-3",
        }
        validator = Draft202012Validator(load_schema("web_client_task.json"))

        errors = []
        for task in (start_task, action_task, finalize_task):
            errors.extend(validator.iter_errors(task))

        self.assertEqual(sorted(errors, key=lambda error: error.path), [])

    def test_web_client_task_schema_rejects_multi_action_payloads(self):
        validator = Draft202012Validator(load_schema("web_client_task.json"))
        legacy_task = {
            "command": "start",
            "request_id": "request-1",
            "run_id": "run-1",
            "base_url": "http://localhost:8096",
            "artifacts_root": "/tmp/artifacts",
            "browser_input": {
                "path": "/web",
                "actions": [{"type": "goto"}],
            },
        }
        top_level_multi_action = {
            "command": "action",
            "request_id": "request-2",
            "action": [{"type": "goto"}, {"type": "screenshot", "label": "home"}],
        }
        top_level_actions = {
            "command": "action",
            "request_id": "request-3",
            "actions": [{"type": "goto"}],
        }

        self.assertTrue(list(validator.iter_errors(legacy_task)))
        self.assertTrue(list(validator.iter_errors(top_level_multi_action)))
        self.assertTrue(list(validator.iter_errors(top_level_actions)))

    def test_web_client_session_schema_accepts_session_commands(self):
        start_plan_request = {
            "command": "start",
            "request_id": "request-1",
            "plan_markdown": "# ReproductionPlan Markdown v1\n\n...",
        }
        start_task_request = {
            "command": "start",
            "request_id": "request-2",
            "base_url": "http://localhost:8096",
        }
        action_request = {
            "command": "action",
            "request_id": "request-3",
            "action": {"type": "screenshot", "label": "home"},
            "step_id": 1,
            "role": "trigger",
            "action_label": "Capture home",
            "selector_assertions": [{"selector": "body", "state": "visible"}],
        }
        finalize_request = {
            "command": "finalize",
            "request_id": "request-4",
            "overall_result": "inconclusive",
        }
        validator = Draft202012Validator(load_schema("web_client_session.json"))

        errors = []
        for request in (
            start_plan_request,
            start_task_request,
            action_request,
            finalize_request,
        ):
            errors.extend(validator.iter_errors({"request": request}))

        self.assertEqual(sorted(errors, key=lambda error: error.path), [])

    def test_web_client_session_schema_rejects_multi_action_payloads(self):
        validator = Draft202012Validator(load_schema("web_client_session.json"))
        browser_input_actions = {
            "command": "action",
            "request_id": "request-1",
            "browser_input": {
                "path": "/web",
                "actions": [{"type": "goto"}],
            },
            "action": {"type": "screenshot", "label": "home"},
        }
        top_level_actions = {
            "command": "action",
            "request_id": "request-2",
            "action": {"type": "goto"},
            "actions": [{"type": "goto"}],
        }
        action_array = {
            "command": "action",
            "request_id": "request-3",
            "action": [{"type": "goto"}],
        }

        raw_command = {
            "command": "start",
            "request_id": "request-4",
            "plan_markdown": "# ReproductionPlan Markdown v1\n\n...",
        }

        self.assertTrue(list(validator.iter_errors({"request": browser_input_actions})))
        self.assertTrue(list(validator.iter_errors({"request": top_level_actions})))
        self.assertTrue(list(validator.iter_errors({"request": action_array})))
        self.assertTrue(list(validator.iter_errors(raw_command)))

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
        self.assertIn("ReproductionPlan Markdown v1", analysis_prompt)
        self.assertNotIn("valid ReproductionPlan JSON", analysis_prompt)
        self.assertNotIn("raw JSON body", analysis_context)
        self.assertNotIn("#### Input", analysis_prompt)
        self.assertNotIn("#### Success Criteria", analysis_prompt)
        self.assertIn("do not put routine plan fields", analysis_prompt)
        self.assertIn("#### Exact Request Payload", analysis_prompt)
        self.assertIn("demo.jellyfin.org/stable", analysis_prompt)
        self.assertIn("demo.jellyfin.org/unstable", analysis_prompt)
        self.assertIn("blank password", analysis_prompt)
        self.assertIn("specific old version", analysis_prompt)

    def test_stage1_prompts_describe_plan_focused_baseline_docker_server_state(self):
        prompts = {
            "system": (
                REPO_ROOT / "creatures" / "analysis" / "prompts" / "system.md"
            ).read_text(encoding="utf-8"),
            "context": (
                REPO_ROOT / "creatures" / "analysis" / "prompts" / "context.md"
            ).read_text(encoding="utf-8"),
        }
        normalized_system = " ".join(prompts["system"].split())
        normalized_context = " ".join(prompts["context"].split())

        self.assertIn("test execution handoff", normalized_system)
        self.assertIn("Jellyfin test plan", normalized_context)

        for name, prompt in prompts.items():
            with self.subTest(prompt=name):
                normalized = " ".join(prompt.split())
                self.assertNotIn("Stage 2", normalized)
                self.assertIn("already configured Jellyfin server", normalized)
                self.assertIn("playable video", normalized)
                self.assertIn("playable audio/music", normalized)
                self.assertIn("baseline environment", normalized)
                self.assertIn("first-run setup", normalized)
                self.assertIn(
                    "unless the issue specifically requires",
                    normalized,
                )

    def test_web_client_and_report_prompts_describe_demo_verification_routing(self):
        web_client_prompt = (
            REPO_ROOT / "creatures" / "web_client" / "prompts" / "system.md"
        ).read_text(encoding="utf-8")
        report_prompt = (
            REPO_ROOT / "creatures" / "report" / "prompts" / "system.md"
        ).read_text(encoding="utf-8")

        self.assertIn("web_client_verification_request", web_client_prompt)
        self.assertIn("server_target.mode: \"demo\"", web_client_prompt)
        self.assertIn("does not own\nserver lifecycle", web_client_prompt)
        self.assertIn("web_client_verification_request", report_prompt)
        self.assertIn('execution_target: "web_client"', report_prompt)

    def test_web_client_prompt_describes_one_action_session_protocol(self):
        web_client_prompt = (
            REPO_ROOT / "creatures" / "web_client" / "prompts" / "system.md"
        ).read_text(encoding="utf-8")

        self.assertIn("web_client_session", web_client_prompt)
        self.assertIn('command: "action"', web_client_prompt)
        self.assertIn("exactly one top-level `action` object", web_client_prompt)
        self.assertIn("plan_markdown", web_client_prompt)
        self.assertIn("Do not use local filesystem paths", web_client_prompt)
        self.assertIn('command: "finalize"', web_client_prompt)
        self.assertIn("There is only one active web-client session", web_client_prompt)
        self.assertNotIn("session_id", web_client_prompt)

    def test_web_client_tool_contract_uses_valid_tool_names(self):
        config = yaml.safe_load(
            (REPO_ROOT / "creatures" / "web_client" / "config.yaml").read_text(
                encoding="utf-8"
            )
        )
        package_tools = {
            tool["name"]: tool
            for tool in config.get("tools", [])
            if tool.get("type") == "package"
        }
        prompt = (
            REPO_ROOT / "creatures" / "web_client" / "prompts" / "system.md"
        ).read_text(encoding="utf-8")

        self.assertNotIn("web_client_execute_plan", package_tools)
        self.assertEqual(
            package_tools["web_client_session"]["class_name"],
            "WebClientSessionTool",
        )
        self.assertIn("web_client_session", prompt)
        self.assertNotIn("web_client_plan_session", prompt)
        self.assertNotIn("web_client_run_task", prompt)
        self.assertNotIn("web_client_execute_plan", prompt)
        self.assertNotIn("web_client_runner.execute_plan", prompt)
        self.assertNotIn("web_client_runner.run_task", prompt)
        self.assertNotIn("session_id", prompt)

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
