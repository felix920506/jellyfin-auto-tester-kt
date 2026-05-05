import unittest

from tools.reproduction_plan_markdown import (
    ReproductionPlanMarkdownError,
    parse_reproduction_plan_markdown,
    render_reproduction_plan_markdown,
)


def standard_plan():
    return {
        "issue_url": "https://github.com/jellyfin/jellyfin/issues/1",
        "issue_title": "Debug issue",
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
                "role": "setup",
                "action": "Create test media",
                "tool": "bash",
                "input": {"command": "printf test > /tmp/jellyfin-test.txt"},
                "expected_outcome": "The fixture is created.",
                "success_criteria": {
                    "all_of": [{"type": "exit_code", "equals": 0}]
                },
            },
            {
                "step_id": 2,
                "role": "trigger",
                "action": "Call the health endpoint",
                "tool": "http_request",
                "input": {"method": "GET", "path": "/health", "auth": "none"},
                "expected_outcome": "The endpoint exposes the reported failure.",
                "success_criteria": {
                    "all_of": [{"type": "status_code", "equals": 500}]
                },
            },
        ],
        "reproduction_goal": "Observe the reported server behavior.",
        "failure_indicators": ["Unexpected HTTP status"],
        "execution_target": "standard",
        "confidence": "high",
        "ambiguities": [],
        "is_verification": False,
        "original_run_id": None,
    }


def web_client_docker_plan():
    plan = standard_plan()
    plan["execution_target"] = "web_client"
    plan["reproduction_steps"] = [
        {
            "step_id": 1,
            "role": "trigger",
            "action": "Capture Jellyfin Web",
            "tool": "browser",
            "input": {
                "path": "/web",
                "auth": "auto",
                "label": "web_home",
                "actions": [{"type": "screenshot", "label": "web_home"}],
            },
            "expected_outcome": "The web UI shows the reported symptom.",
            "success_criteria": {
                "all_of": [{"type": "browser_action_run"}]
            },
        }
    ]
    return plan


def web_client_demo_plan():
    plan = web_client_docker_plan()
    plan.pop("docker_image", None)
    plan["target_version"] = "stable"
    plan["server_target"] = {
        "mode": "demo",
        "release_track": "stable",
        "base_url": "https://demo.jellyfin.org/stable",
        "username": "demo",
        "password": "",
        "requires_admin": False,
    }
    return plan


class ReproductionPlanMarkdownTests(unittest.TestCase):
    def test_standard_plan_round_trips(self):
        plan = standard_plan()
        markdown = render_reproduction_plan_markdown(plan)

        parsed = parse_reproduction_plan_markdown(markdown)

        self.assertEqual(parsed, plan)
        self.assertIn("# ReproductionPlan Markdown v1", markdown)
        self.assertIn("## Steps", markdown)

    def test_web_client_docker_plan_round_trips(self):
        plan = web_client_docker_plan()

        parsed = parse_reproduction_plan_markdown(
            render_reproduction_plan_markdown(plan)
        )

        self.assertEqual(parsed, plan)
        self.assertEqual(parsed["execution_target"], "web_client")
        self.assertEqual(parsed["reproduction_steps"][0]["tool"], "browser")

    def test_web_client_demo_plan_round_trips(self):
        plan = web_client_demo_plan()

        parsed = parse_reproduction_plan_markdown(
            render_reproduction_plan_markdown(plan)
        )

        self.assertEqual(parsed, plan)
        self.assertNotIn("docker_image", parsed)
        self.assertEqual(parsed["server_target"]["mode"], "demo")

    def test_rejects_multiple_trigger_steps(self):
        plan = standard_plan()
        plan["reproduction_steps"][0]["role"] = "trigger"

        with self.assertRaisesRegex(
            ReproductionPlanMarkdownError,
            "exactly one trigger",
        ):
            render_reproduction_plan_markdown(plan)

    def test_rejects_unstructured_success_criteria(self):
        plan = standard_plan()
        plan["reproduction_steps"][0]["success_criteria"] = {
            "text": "command exits successfully"
        }

        with self.assertRaisesRegex(
            ReproductionPlanMarkdownError,
            "all_of or any_of",
        ):
            render_reproduction_plan_markdown(plan)


if __name__ == "__main__":
    unittest.main()
