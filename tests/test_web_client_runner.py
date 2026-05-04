import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import tools.web_client_runner as web_client_runner_module
from tools.web_client_runner import (
    WebClientExecutePlanTool,
    WebClientRunTaskTool,
    WebClientRunner,
)

from tests.test_execution_runner import (
    FakeAPI,
    FakeBrowserDriver,
    FakeDocker,
    FakeScreenshotter,
    base_plan,
)


def browser_plan():
    return base_plan(
        [
            {
                "step_id": 1,
                "role": "trigger",
                "action": "Open Jellyfin Web",
                "tool": "browser",
                "input": {
                    "path": "/web",
                    "label": "web_home",
                    "actions": [{"type": "screenshot", "label": "web_home"}],
                },
                "expected_outcome": "Web UI is visible",
                "success_criteria": {
                    "all_of": [{"type": "screenshot_present", "label": "web_home"}]
                },
            }
        ]
    )


def demo_browser_plan(release_track="stable", include_base_url=True):
    plan = copy.deepcopy(browser_plan())
    plan.pop("docker_image", None)
    plan["target_version"] = release_track
    plan["execution_target"] = "web_client"
    plan["server_target"] = {
        "mode": "demo",
        "release_track": release_track,
        "username": "demo",
        "password": "",
        "requires_admin": False,
    }
    if include_base_url:
        plan["server_target"]["base_url"] = (
            f"https://demo.jellyfin.org/{release_track}"
        )
    return plan


class WebClientRunnerTests(unittest.TestCase):
    def test_full_web_client_plan_produces_execution_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            docker = FakeDocker()
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=docker,
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            result = runner.execute_plan(browser_plan(), run_id="web-run")

            self.assertEqual(result["overall_result"], "reproduced")
            self.assertEqual(result["run_id"], "web-run")
            self.assertEqual(result["execution_log"][0]["tool"], "browser")
            self.assertTrue(Path(temp_dir, "web-run", "result.json").is_file())
            self.assertEqual(docker.pulled, [("jellyfin/jellyfin:10.9.7", "web-run")])
            self.assertEqual(browser_driver.runs[0]["step_id"], 1)

    def test_demo_plan_uses_public_server_without_docker_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            docker = FakeDocker()
            api = FakeAPI()
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=docker,
                api=api,
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            result = runner.execute_plan(demo_browser_plan(), run_id="demo-run")

            self.assertEqual(result["overall_result"], "reproduced")
            self.assertIsNone(result["container_id"])
            self.assertEqual(result["jellyfin_logs"], "")
            self.assertEqual(docker.pulled, [])
            self.assertEqual(docker.started, [])
            self.assertEqual(docker.stopped, [])
            self.assertEqual(docker.logs_calls, [])
            self.assertFalse(hasattr(api, "completed_wizard"))
            self.assertEqual(api.requests, [])
            self.assertEqual(
                browser_driver.configures,
                [
                    {
                        "base_url": "https://demo.jellyfin.org/stable",
                        "run_id": "demo-run",
                    }
                ],
            )
            debug_log = Path(temp_dir, "demo-run", "web_client_runner.log")
            events = [
                json.loads(line)["event"]
                for line in debug_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("demo_plan_start", events)
            self.assertIn("step_start", events)
            self.assertIn("step_done", events)
            self.assertIn("demo_plan_done", events)

    def test_demo_plan_injects_demo_credentials_for_auto_auth(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            plan = demo_browser_plan()
            plan["reproduction_steps"][0]["input"]["auth"] = "auto"

            runner.execute_plan(plan, run_id="demo-auth")

            self.assertEqual(
                browser_driver.runs[0]["browser_input"]["auth"],
                {"mode": "auto", "username": "demo", "password": ""},
            )

    def test_demo_plan_injects_demo_credentials_when_auth_omitted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            plan = demo_browser_plan()
            plan["reproduction_steps"][0]["input"].pop("auth", None)

            runner.execute_plan(plan, run_id="demo-auth-omitted")

            self.assertEqual(
                browser_driver.runs[0]["browser_input"]["auth"],
                {"mode": "auto", "username": "demo", "password": ""},
            )

    def test_demo_plan_rejects_non_browser_steps_without_docker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            docker = FakeDocker()
            plan = demo_browser_plan()
            plan["reproduction_steps"].insert(
                0,
                {
                    "step_id": 1,
                    "role": "setup",
                    "action": "Call health endpoint",
                    "tool": "http_request",
                    "input": {"method": "GET", "path": "/health", "auth": "none"},
                    "expected_outcome": "Healthy",
                    "success_criteria": {
                        "all_of": [{"type": "status_code", "equals": 200}]
                    },
                },
            )
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=docker,
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=FakeBrowserDriver(temp_dir),
            )

            result = runner.execute_plan(plan, run_id="demo-unsupported")

            self.assertEqual(result["overall_result"], "inconclusive")
            self.assertIn("demo server mode only supports browser", result["error_summary"])
            self.assertEqual(docker.pulled, [])
            self.assertEqual(docker.started, [])

    def test_demo_plan_browser_action_failure_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(
                temp_dir,
                results=[
                    {
                        "status": "fail",
                        "actions": [
                            {
                                "type": "goto",
                                "status": "fail",
                                "error": "navigation failed",
                            }
                        ],
                        "screenshot_paths": [],
                        "final_url": "https://demo.jellyfin.org/stable",
                        "title": "",
                        "console": [],
                        "failed_network": [],
                        "dom_summary": "",
                        "dom_path": None,
                        "page_text": "",
                        "media_state": {"state": "none", "elements": []},
                        "error": "navigation failed",
                    }
                ],
            )
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            result = runner.execute_plan(demo_browser_plan(), run_id="demo-fail")

            self.assertEqual(result["overall_result"], "inconclusive")
            self.assertIn("demo browser flow could not complete", result["error_summary"])

    def test_demo_plan_browser_infrastructure_failure_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(
                temp_dir,
                results=[
                    {
                        "status": "fail",
                        "actions": [],
                        "screenshot_paths": [],
                        "final_url": None,
                        "title": None,
                        "console": [],
                        "failed_network": [],
                        "dom_summary": None,
                        "dom_path": None,
                        "page_text": None,
                        "media_state": {"state": "none", "elements": []},
                        "error": (
                            "It looks like you are using Playwright Sync API "
                            "inside the asyncio loop. Please use the Async API instead."
                        ),
                    }
                ],
            )
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            result = runner.execute_plan(demo_browser_plan(), run_id="demo-infra-fail")

            self.assertEqual(result["overall_result"], "inconclusive")
            self.assertIn("demo browser flow could not complete", result["error_summary"])

    def test_demo_plan_resolves_unstable_track_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            runner.execute_plan(
                demo_browser_plan(release_track="unstable", include_base_url=False),
                run_id="demo-unstable",
            )

            self.assertEqual(
                browser_driver.configures[0]["base_url"],
                "https://demo.jellyfin.org/unstable",
            )

    def test_non_browser_trigger_returns_inconclusive_without_docker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            docker = FakeDocker()
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "trigger",
                        "action": "Call health endpoint",
                        "tool": "http_request",
                        "input": {"method": "GET", "path": "/health", "auth": "none"},
                        "expected_outcome": "Healthy",
                        "success_criteria": {
                            "all_of": [{"type": "status_code", "equals": 200}]
                        },
                    }
                ]
            )
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=docker,
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=FakeBrowserDriver(temp_dir),
            )

            result = runner.execute_plan(plan, run_id="unsupported")

            self.assertEqual(result["overall_result"], "inconclusive")
            self.assertIn("trigger uses tool: browser", result["error_summary"])
            self.assertEqual(docker.pulled, [])
            self.assertEqual(docker.started, [])

    def test_task_start_returns_session_without_running_browser_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            docker = FakeDocker()
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=docker,
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "session-1",
            )

            result = runner.run_task(
                {
                    "command": "start",
                    "request_id": "request-1",
                    "run_id": "task-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                    "step_id": "browser-step",
                    "browser_input": {
                        "path": "/web",
                        "label": "home",
                    },
                }
            )

            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["request_id"], "request-1")
            self.assertEqual(result["session_id"], "session-1")
            self.assertIsNone(result["browser"])
            self.assertEqual(browser_driver.runs, [])
            self.assertEqual(docker.pulled, [])
            self.assertEqual(docker.started, [])
            self.assertEqual(
                browser_driver.configures,
                [{"base_url": "http://localhost:9000", "run_id": "task-run"}],
            )

    def test_task_action_runs_exactly_one_action_and_returns_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "session-1",
            )
            start = runner.run_task(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "task-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                    "step_id": 7,
                    "browser_input": {"path": "/web", "label": "home"},
                }
            )

            result = runner.run_task(
                {
                    "command": "action",
                    "request_id": "action-1",
                    "session_id": start["session_id"],
                    "action": {"type": "goto"},
                    "selector_assertions": [{"selector": "body", "state": "visible"}],
                    "capture": {"current_url": {"from": "browser_url"}},
                }
            )

            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["session_id"], "session-1")
            self.assertEqual(result["browser"]["dom_summary"], "title='Jellyfin'")
            self.assertEqual(
                result["capture_values"]["current_url"],
                "http://localhost:8097/web",
            )
            self.assertEqual(browser_driver.inspected_selectors, [["body"]])
            self.assertEqual(len(browser_driver.runs), 1)
            self.assertEqual(
                browser_driver.runs[0]["browser_input"]["actions"],
                [{"type": "goto"}],
            )

    def test_task_actions_reuse_session_driver_between_results(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "session-1",
            )
            start = runner.run_task(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "task-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                    "browser_input": {"path": "/web", "label": "home"},
                }
            )

            first = runner.run_task(
                {
                    "command": "action",
                    "request_id": "action-1",
                    "session_id": start["session_id"],
                    "action": {"type": "goto"},
                }
            )
            self.assertEqual(first["status"], "pass")
            self.assertEqual(len(browser_driver.runs), 1)

            second = runner.run_task(
                {
                    "command": "action",
                    "request_id": "action-2",
                    "session_id": start["session_id"],
                    "browser_input": {"locale": "fr-FR", "label": "after"},
                    "action": {"type": "screenshot", "label": "after"},
                }
            )

            self.assertEqual(second["status"], "pass")
            self.assertEqual(len(browser_driver.runs), 2)
            self.assertFalse(browser_driver.closed)
            self.assertEqual(
                browser_driver.runs[1]["browser_input"]["actions"],
                [{"type": "screenshot", "label": "after"}],
            )
            self.assertEqual(browser_driver.runs[1]["browser_input"]["path"], "/web")
            self.assertEqual(browser_driver.runs[1]["browser_input"]["locale"], "fr-FR")

    def test_task_finalize_closes_driver_and_removes_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "session-1",
            )
            start = runner.run_task(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "task-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                }
            )

            finalized = runner.run_task(
                {
                    "command": "finalize",
                    "request_id": "finalize-1",
                    "session_id": start["session_id"],
                }
            )
            after_finalize = runner.run_task(
                {
                    "command": "action",
                    "request_id": "action-after-finalize",
                    "session_id": start["session_id"],
                    "action": {"type": "screenshot", "label": "late"},
                }
            )

            self.assertEqual(finalized["status"], "pass")
            self.assertTrue(browser_driver.closed)
            self.assertEqual(after_finalize["status"], "error")
            self.assertIn("browser session not found", after_finalize["error"])

    def test_task_mode_rejects_legacy_multi_action_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            result = runner.run_task(
                {
                    "request_id": "legacy-1",
                    "run_id": "legacy-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                    "browser_input": {
                        "path": "/web",
                        "actions": [
                            {"type": "goto"},
                            {"type": "screenshot", "label": "home"},
                        ],
                    },
                }
            )

            self.assertEqual(result["status"], "error")
            self.assertIn("legacy browser_input.actions", result["error"])
            self.assertEqual(browser_driver.runs, [])

    def test_module_level_run_task_preserves_sessions_across_calls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "module-session",
            )
            previous = web_client_runner_module._DEFAULT_TASK_RUNNER
            web_client_runner_module._DEFAULT_TASK_RUNNER = runner
            try:
                start = web_client_runner_module.run_task(
                    {
                        "command": "start",
                        "request_id": "module-start",
                        "run_id": "module-run",
                        "base_url": "http://localhost:9000",
                        "artifacts_root": temp_dir,
                        "browser_input": {"path": "/web"},
                    }
                )
                action = web_client_runner_module.run_task(
                    {
                        "command": "action",
                        "request_id": "module-action",
                        "session_id": start["session_id"],
                        "action": {"type": "goto"},
                    }
                )
            finally:
                web_client_runner_module._DEFAULT_TASK_RUNNER = previous

            self.assertEqual(start["session_id"], "module-session")
            self.assertEqual(action["status"], "pass")
            self.assertEqual(len(browser_driver.runs), 1)


class WebClientRunnerToolTests(unittest.TestCase):
    def test_execute_plan_tool_accepts_wrapped_json_body(self):
        plan = browser_plan()
        payload = {"plan": plan, "run_id": "tool-run"}
        expected = {"overall_result": "reproduced", "run_id": "tool-run"}

        with patch.object(
            web_client_runner_module,
            "execute_plan",
            return_value=expected,
        ) as execute_plan:
            result = asyncio.run(
                WebClientExecutePlanTool()._execute(
                    {"content": json.dumps(payload)}
                )
            )

        self.assertIsNone(result.error)
        self.assertEqual(json.loads(result.output), expected)
        execute_plan.assert_called_once_with(plan=plan, run_id="tool-run")

    def test_execute_plan_tool_accepts_raw_plan_payload(self):
        plan = browser_plan()
        expected = {"overall_result": "reproduced", "run_id": "generated-run"}

        with patch.object(
            web_client_runner_module,
            "execute_plan",
            return_value=expected,
        ) as execute_plan:
            result = asyncio.run(WebClientExecutePlanTool()._execute(plan))

        self.assertIsNone(result.error)
        self.assertEqual(json.loads(result.output), expected)
        execute_plan.assert_called_once_with(plan=plan, run_id=None)

    def test_run_task_tool_accepts_wrapped_and_raw_payloads(self):
        task = {
            "command": "start",
            "request_id": "task-1",
            "run_id": "task-run",
            "base_url": "http://localhost:9000",
            "artifacts_root": "/tmp/artifacts",
        }
        expected = {"request_id": "task-1", "status": "pass"}

        with patch.object(
            web_client_runner_module,
            "run_task",
            return_value=expected,
        ) as run_task_tool:
            wrapped = asyncio.run(
                WebClientRunTaskTool()._execute(
                    {"content": json.dumps({"task": task})}
                )
            )
            raw = asyncio.run(WebClientRunTaskTool()._execute(task))

        self.assertIsNone(wrapped.error)
        self.assertEqual(json.loads(wrapped.output), expected)
        self.assertIsNone(raw.error)
        self.assertEqual(json.loads(raw.output), expected)
        self.assertEqual(
            run_task_tool.call_args_list,
            [
                call(task=task),
                call(task=task),
            ],
        )

    def test_tool_malformed_json_returns_error(self):
        with patch.object(
            web_client_runner_module,
            "execute_plan",
        ) as execute_plan:
            result = asyncio.run(
                WebClientExecutePlanTool()._execute({"content": "{not json"})
            )

        self.assertIsNotNone(result.error)
        self.assertIn("malformed JSON", result.error)
        execute_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
