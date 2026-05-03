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

        errors = sorted(validator.iter_errors(minimal_plan()), key=lambda error: error.path)

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
        execution_prompt = (
            REPO_ROOT / "creatures" / "execution" / "prompts" / "system.md"
        ).read_text(encoding="utf-8")

        self.assertIn("`browser`", analysis_prompt)
        self.assertIn("`browser`", execution_prompt)

    def test_plan_normalization_defaults_browser_criteria_to_action_run(self):
        plan = minimal_plan()
        plan["reproduction_steps"][0].pop("success_criteria")

        normalized = _normalize_reproduction_plan(plan)

        self.assertEqual(
            normalized["reproduction_steps"][0]["success_criteria"],
            {"all_of": [{"type": "browser_action_run"}]},
        )


if __name__ == "__main__":
    unittest.main()
