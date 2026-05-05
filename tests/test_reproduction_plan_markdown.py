import unittest

from tools.reproduction_plan_markdown import (
    ReproductionPlanMarkdownError,
    parse_reproduction_plan_markdown,
    render_reproduction_plan_markdown,
)


DOCKER_BASELINE_ENVIRONMENT_LINE = (
    "- Docker-backed reproduction starts from a healthy, already configured "
    "Jellyfin server with admin auth and playable video/audio content."
)
DEMO_ENVIRONMENT_LINE = (
    "- Docker startup, first-run setup, and admin access are not part of this plan."
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
    def test_standard_plan_renders_human_markdown(self):
        markdown = render_reproduction_plan_markdown(standard_plan())

        parsed = parse_reproduction_plan_markdown(markdown)

        self.assertIn("# ReproductionPlan Markdown v1", markdown)
        self.assertIn("## Steps", markdown)
        self.assertIn("- Request: GET /health with `none` authentication.", markdown)
        self.assertNotIn("#### Input", markdown)
        self.assertNotIn("#### Success Criteria", markdown)
        self.assertNotIn("```json", markdown)
        self.assertNotIn("Stage 2", markdown)
        self.assertIn(DOCKER_BASELINE_ENVIRONMENT_LINE, markdown)
        self.assertEqual(parsed["execution_target"], "standard")
        self.assertEqual(parsed["docker_image"], "jellyfin/jellyfin:10.9.7")
        self.assertEqual(
            parsed["environment"],
            {
                "ports": {"host": 8096, "container": 8096},
                "volumes": [],
                "env_vars": {},
            },
        )
        self.assertEqual(parsed["reproduction_steps"][1]["tool"], "http_request")

    def test_web_client_docker_plan_renders_without_json_fragments(self):
        markdown = render_reproduction_plan_markdown(web_client_docker_plan())

        parsed = parse_reproduction_plan_markdown(markdown)

        self.assertNotIn("```json", markdown)
        self.assertEqual(parsed["execution_target"], "web_client")
        self.assertEqual(parsed["reproduction_steps"][0]["tool"], "browser")
        self.assertIn("Browser Action: capture screenshot", markdown)

    def test_web_client_demo_plan_extracts_demo_metadata(self):
        markdown = render_reproduction_plan_markdown(web_client_demo_plan())

        parsed = parse_reproduction_plan_markdown(markdown)

        self.assertNotIn("```json", markdown)
        self.assertNotIn("Docker Image", markdown)
        self.assertNotIn("Stage 2", markdown)
        self.assertIn(DEMO_ENVIRONMENT_LINE, markdown)
        self.assertEqual(parsed["server_target"]["mode"], "demo")
        self.assertEqual(parsed["server_target"]["password"], "")

    def test_demo_plan_accepts_readable_environment_and_missing_expected_outcome(self):
        markdown = """# ReproductionPlan Markdown v1

## Goal
- Issue URL: https://github.com/jellyfin/jellyfin-web/issues/7852
- Issue Title: Newly added favorite is lost if music player is stopped
- Reproduction Goal: Confirm that favoriting a playing song persists after stopping playback.

## Issue Context
The reported symptom is visible in Jellyfin Web after playback stops.

## Execution Target
- Execution Target: web_client
- Target Version: 10.11.8
- Server Mode: demo
- Demo Release Track: stable
- Demo Base URL: https://demo.jellyfin.org/stable
- Demo Username: demo
- Demo Password: ""
- Demo Requires Admin: false
- Is Verification: false
- Original Run ID: null

## Environment
- Use the public Jellyfin demo server.

## Prerequisites
- The demo catalog contains one playable song.

## Steps
### Step 1: Stop playback and observe the favorite state reset
- Step ID: 1
- Role: trigger
- Action: Stop the music player while the song is favorited.
- Tool: browser
- Browser Action: click the player stop control named Stop.
- Reproduced When: the song row changes back to Add to favorites.

## Failure Indicators
- The favorite mark disappears after stopping playback.

## Confidence
medium

## Ambiguities
- Browser engine differences may affect reproducibility.
"""

        parsed = parse_reproduction_plan_markdown(markdown)

        self.assertEqual(
            parsed["environment"],
            {"ports": {"host": 8096, "container": 8096}, "volumes": [], "env_vars": {}},
        )
        self.assertEqual(parsed["server_target"]["password"], "")
        self.assertEqual(
            parsed["reproduction_steps"][0]["expected_outcome"],
            "Stop the music player while the song is favorited.",
        )

    def test_demo_plan_accepts_observe_role_and_blank_password_label(self):
        markdown = """# ReproductionPlan Markdown v1

## Goal
- Issue URL: https://github.com/jellyfin/jellyfin-web/issues/7852
- Issue Title: Newly added favorite is lost if music player is stopped
- Reproduction Goal: Confirm favorite state after stopping playback.

## Issue Context
The reported symptom is visible in Jellyfin Web after playback stops.

## Execution Target
- Execution Target: web_client
- Target Version: stable
- Server Mode: demo
- Demo Release Track: stable
- Demo Base URL: https://demo.jellyfin.org/stable
- Demo Username: demo
- Demo Password: <blank>
- Demo Requires Admin: false
- Is Verification: false
- Original Run ID: null

## Environment
- Use the public Jellyfin demo server.

## Prerequisites
- The demo catalog contains one playable song.

## Steps
### Step 1: Stop playback
- Step ID: 1
- Role: trigger
- Action: Click the player stop control named Stop.
- Tool: browser
- Expected Outcome: The favorite indicator should remain active.
- Reproduced When: the favorite indicator turns off.

### Step 2: Revisit the Songs list
- Step ID: 2
- Role: observe
- Action: Return to the Songs list and locate the same song.
- Tool: browser
- Expected Outcome: The song is still marked favorite.
- Reproduced When: the song is no longer shown as favorited.

## Failure Indicators
- The favorite mark disappears after stopping playback.

## Confidence
medium

## Ambiguities
- The exact demo catalog contents are unknown.
"""

        parsed = parse_reproduction_plan_markdown(markdown)

        self.assertEqual(parsed["server_target"]["password"], "")
        self.assertEqual(
            [step["role"] for step in parsed["reproduction_steps"]],
            ["trigger", "observe"],
        )

    def test_rejects_routine_json_blocks(self):
        markdown = render_reproduction_plan_markdown(standard_plan()).replace(
            f"{DOCKER_BASELINE_ENVIRONMENT_LINE}\n"
            "- Host Port: 8096\n"
            "- Container Port: 8096\n"
            "- Volumes: none\n"
            "- Environment Variables: none",
            '```json\n{"ports":{"host":8096,"container":8096}}\n```',
        )

        with self.assertRaisesRegex(
            ReproductionPlanMarkdownError,
            "JSON fences are allowed only",
        ):
            parse_reproduction_plan_markdown(markdown)

    def test_allows_exact_request_payload_json(self):
        plan = standard_plan()
        plan["reproduction_steps"][1]["input"]["body_json"] = {
            "Name": "Bad payload",
            "ProviderIds": {"Tvdb": "123"},
        }
        markdown = render_reproduction_plan_markdown(plan)

        parsed = parse_reproduction_plan_markdown(markdown)

        self.assertIn("#### Exact Request Payload", markdown)
        self.assertIn("```json", markdown)
        self.assertEqual(parsed["reproduction_steps"][1]["tool"], "http_request")

    def test_renders_exact_non_json_request_body_as_text(self):
        plan = standard_plan()
        plan["reproduction_steps"][1]["input"]["body_text"] = '{"bad":'

        markdown = render_reproduction_plan_markdown(plan)
        parsed = parse_reproduction_plan_markdown(markdown)

        self.assertIn("#### Exact Request Body", markdown)
        self.assertIn("```text", markdown)
        self.assertNotIn("#### Exact Request Payload", markdown)
        self.assertNotIn("```json", markdown)
        self.assertEqual(parsed["reproduction_steps"][1]["tool"], "http_request")

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
