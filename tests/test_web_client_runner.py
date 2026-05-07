import asyncio
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import tools.web_client_runner as web_client_runner_module
from tools.async_compat import shutdown_sync_workers
from tools.web_client_runner import (
    WebClientExecutePlanTool,
    WebClientRunner,
    WebClientSessionTool,
)
from tools.reproduction_plan_markdown import render_reproduction_plan_markdown

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


def two_step_browser_plan():
    return base_plan(
        [
            {
                "step_id": 1,
                "role": "setup",
                "action": "Open Jellyfin Web",
                "tool": "browser",
                "input": {},
                "expected_outcome": "Web UI is visible",
            },
            {
                "step_id": 2,
                "role": "trigger",
                "action": "Capture home",
                "tool": "browser",
                "input": {},
                "expected_outcome": "Home is captured",
            },
        ]
    )


class ThreadTrackingBrowserDriver(FakeBrowserDriver):
    def __init__(self, artifacts_root, results=None):
        super().__init__(artifacts_root, results=results)
        self.thread_events = []

    def configure(self, base_url=None, run_id=None):
        self.thread_events.append(("configure", threading.get_ident()))
        return super().configure(base_url=base_url, run_id=run_id)

    def run(self, browser_input, run_id, step_id=None):
        self.thread_events.append(("run", threading.get_ident()))
        return super().run(browser_input, run_id=run_id, step_id=step_id)

    def close(self):
        self.thread_events.append(("close", threading.get_ident()))
        return super().close()


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

    def test_session_start_with_plan_markdown_path_prepares_without_running_browser_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            docker = FakeDocker()
            browser_driver = FakeBrowserDriver(temp_dir)
            plan_path = Path(temp_dir) / "plan.md"
            plan_path.write_text(
                render_reproduction_plan_markdown(browser_plan()),
                encoding="utf-8",
            )
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=docker,
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "plan-session-1",
            )

            start = runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "plan-run",
                    "artifacts_root": temp_dir,
                    "plan_markdown_path": str(plan_path),
                }
            )

            self.assertEqual(start["status"], "pass")
            self.assertNotIn("session_id", start)
            self.assertTrue(start["plan_loaded"])
            self.assertEqual(browser_driver.runs, [])
            self.assertEqual(docker.pulled, [("jellyfin/jellyfin:10.9.7", "plan-run")])
            self.assertEqual(len(docker.started), 1)
            self.assertTrue(Path(temp_dir, "plan-run", "plan.md").is_file())
            self.assertTrue(Path(temp_dir, "plan-run", "plan.json").is_file())

            runner.session(
                {
                    "command": "finalize",
                    "request_id": "finalize-1",
                    "overall_result": "inconclusive",
                }
            )

    def test_session_start_with_inline_plan_markdown_uses_generated_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            docker = FakeDocker()
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=docker,
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "inline-plan-run",
            )

            start = runner.session(
                {
                    "command": "start",
                    "request_id": "start-inline",
                    "plan_markdown": render_reproduction_plan_markdown(browser_plan()),
                }
            )
            final = runner.session(
                {
                    "command": "finalize",
                    "request_id": "finalize-inline",
                    "overall_result": "reproduced",
                }
            )

            self.assertEqual(start["status"], "pass")
            self.assertEqual(start["run_id"], "inline-plan-run")
            self.assertTrue(start["plan_loaded"])
            self.assertEqual(docker.pulled, [("jellyfin/jellyfin:10.9.7", "inline-plan-run")])
            self.assertTrue(Path(temp_dir, "inline-plan-run", "plan.md").is_file())
            self.assertTrue(Path(temp_dir, "inline-plan-run", "plan.json").is_file())
            self.assertEqual(final["overall_result"], "reproduced")

    def test_session_generates_request_ids_when_omitted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            start = runner.session(
                {
                    "command": "start",
                    "run_id": "session-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                }
            )
            first_action = runner.session(
                {
                    "command": "action",
                    "action": {"type": "goto"},
                }
            )
            second_action = runner.session(
                {
                    "command": "action",
                    "action": {"type": "screenshot", "label": "home"},
                }
            )
            final = runner.session({"command": "finalize"})

            self.assertEqual(start["request_id"], "start-1")
            self.assertEqual(first_action["request_id"], "action-1")
            self.assertEqual(second_action["request_id"], "action-2")
            self.assertEqual(final["request_id"], "finalize-1")
            self.assertEqual(final["status"], "pass")
            artifacts_dir = Path(temp_dir, "session-run")
            for request_id in ("start-1", "action-1", "action-2", "finalize-1"):
                self.assertTrue(
                    Path(
                        artifacts_dir,
                        f"web_client_session_{request_id}.json",
                    ).is_file()
                )
                self.assertTrue(
                    Path(
                        artifacts_dir,
                        f"web_client_result_{request_id}.json",
                    ).is_file()
                )
            start_payload = json.loads(
                Path(artifacts_dir, "web_client_session_start-1.json").read_text(
                    encoding="utf-8",
                )
            )
            self.assertEqual(start_payload["request_id"], "start-1")

    def test_session_preserves_explicit_request_id_for_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=FakeBrowserDriver(temp_dir),
            )

            start = runner.session(
                {
                    "command": "start",
                    "request_id": "caller-start",
                    "run_id": "explicit-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                }
            )
            final = runner.session(
                {
                    "command": "finalize",
                    "request_id": "caller-finalize",
                }
            )

            self.assertEqual(start["request_id"], "caller-start")
            self.assertEqual(final["request_id"], "caller-finalize")
            self.assertTrue(
                Path(
                    temp_dir,
                    "explicit-run",
                    "web_client_session_caller-start.json",
                ).is_file()
            )
            self.assertTrue(
                Path(
                    temp_dir,
                    "explicit-run",
                    "web_client_result_caller-finalize.json",
                ).is_file()
            )

    def test_session_rejects_explicit_empty_request_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            result = runner.session(
                {
                    "command": "start",
                    "request_id": "",
                    "run_id": "empty-id-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                }
            )

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error_code"], "schema_error")
            self.assertEqual(result["schema_path"], "$.request_id")
            self.assertEqual(browser_driver.configures, [])

    def test_session_action_runs_exactly_one_plan_backed_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            plan_path = Path(temp_dir) / "plan.json"
            plan_path.write_text(json.dumps(browser_plan()), encoding="utf-8")
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "plan-session-1",
            )
            start = runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "plan-run",
                    "artifacts_root": temp_dir,
                    "plan_path": str(plan_path),
                }
            )

            action = runner.session(
                {
                    "command": "action",
                    "request_id": "next-1",
                    "action": {"type": "screenshot", "label": "web_home"},
                }
            )
            advance = runner.session(
                {
                    "command": "advance_step",
                    "request_id": "advance-1",
                }
            )

            self.assertEqual(action["status"], "pass")
            self.assertEqual(action["step_tracker"]["current_step"]["step_id"], 1)
            self.assertEqual(action["tracked_action"]["step_id"], 1)
            self.assertEqual(advance["execution_entry"]["step_id"], 1)
            self.assertEqual(advance["execution_entry"]["role"], "trigger")
            self.assertEqual(advance["execution_entry"]["action"], "Open Jellyfin Web")
            self.assertEqual(len(browser_driver.runs), 1)
            self.assertEqual(
                browser_driver.runs[0]["browser_input"]["actions"],
                [{"type": "screenshot", "label": "web_home"}],
            )

            runner.session(
                {
                    "command": "finalize",
                    "request_id": "finalize-1",
                    "overall_result": "reproduced",
                }
            )

    def test_session_requires_llm_to_send_each_plan_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "plan-session-1",
            )
            plan = browser_plan()
            plan_path = Path(temp_dir) / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            start = runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "legacy-run",
                    "artifacts_root": temp_dir,
                    "plan_path": str(plan_path),
                }
            )

            first = runner.session(
                {
                    "command": "action",
                    "request_id": "next-1",
                    "action": {"type": "goto"},
                }
            )
            second = runner.session(
                {
                    "command": "action",
                    "request_id": "next-2",
                    "action": {"type": "wait_for", "selector": "body"},
                }
            )
            third = runner.session(
                {
                    "command": "action",
                    "request_id": "next-3",
                    "action": {"type": "screenshot", "label": "web_home"},
                }
            )

            self.assertEqual(first["status"], "pass")
            self.assertEqual(second["status"], "pass")
            self.assertEqual(third["status"], "pass")
            self.assertEqual(third["step_tracker"]["actions_in_current_step"], 3)
            self.assertEqual(
                [
                    run["browser_input"]["actions"]
                    for run in browser_driver.runs
                ],
                [
                    [{"type": "goto"}],
                    [{"type": "wait_for", "selector": "body"}],
                    [{"type": "screenshot", "label": "web_home"}],
                ],
            )

            final = runner.session(
                {
                    "command": "finalize",
                    "request_id": "finalize-1",
                    "overall_result": "reproduced",
                }
            )
            self.assertEqual(len(final["execution_log"]), 1)
            self.assertEqual(
                len(final["execution_log"][0]["browser"]["actions"]),
                3,
            )

    def test_session_finalize_returns_execution_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            docker = FakeDocker()
            browser_driver = FakeBrowserDriver(temp_dir)
            plan_path = Path(temp_dir) / "plan.json"
            plan_path.write_text(json.dumps(browser_plan()), encoding="utf-8")
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=docker,
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "plan-session-1",
            )
            start = runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "plan-run",
                    "artifacts_root": temp_dir,
                    "plan_path": str(plan_path),
                }
            )
            runner.session(
                {
                    "command": "action",
                    "request_id": "next-1",
                    "action": {"type": "screenshot", "label": "web_home"},
                }
            )

            final = runner.session(
                {
                    "command": "finalize",
                    "request_id": "finalize-1",
                    "overall_result": "reproduced",
                }
            )

            self.assertEqual(final["overall_result"], "reproduced")
            self.assertEqual(final["run_id"], "plan-run")
            self.assertEqual(len(final["execution_log"]), 1)
            self.assertNotIn("plan", final)
            self.assertEqual(
                Path(final["result_path"]).resolve(),
                Path(temp_dir, "plan-run", "result.json").resolve(),
            )
            full_result = json.loads(
                Path(temp_dir, "plan-run", "result.json").read_text(encoding="utf-8")
            )
            self.assertIn("plan", full_result)
            self.assertTrue(browser_driver.closed)
            self.assertEqual(docker.stopped, [("container-1", "plan-run")])

    def test_session_advance_step_moves_runner_owned_cursor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            plan_path = Path(temp_dir) / "plan.json"
            plan_path.write_text(json.dumps(two_step_browser_plan()), encoding="utf-8")
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            start = runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "cursor-run",
                    "artifacts_root": temp_dir,
                    "plan_path": str(plan_path),
                }
            )
            runner.session(
                {
                    "command": "action",
                    "request_id": "step-1-action",
                    "action": {"type": "goto"},
                }
            )
            advanced = runner.session(
                {
                    "command": "advance_step",
                    "request_id": "advance-1",
                }
            )
            runner.session(
                {
                    "command": "action",
                    "request_id": "step-2-action",
                    "action": {"type": "screenshot", "label": "home"},
                }
            )
            final = runner.session(
                {
                    "command": "finalize",
                    "request_id": "finalize-1",
                    "overall_result": "reproduced",
                }
            )

            self.assertEqual(start["step_tracker"]["current_step"]["step_id"], 1)
            self.assertEqual(advanced["execution_entry"]["step_id"], 1)
            self.assertEqual(advanced["step_tracker"]["current_step"]["step_id"], 2)
            self.assertEqual(len(final["execution_log"]), 2)
            self.assertEqual([entry["step_id"] for entry in final["execution_log"]], [1, 2])

    def test_session_retry_actions_are_aggregated_under_current_step(self):
        failed_browser = {
            "status": "fail",
            "actions": [
                {
                    "type": "click",
                    "status": "fail",
                    "error": "target unavailable",
                }
            ],
            "screenshot_paths": [],
            "final_url": "http://localhost:8097/web",
            "title": "Jellyfin",
            "console": [],
            "failed_network": [],
            "dom_summary": "title='Jellyfin'",
            "dom_path": None,
            "page_text": "Jellyfin Home",
            "visible_controls": [],
            "media_state": {"state": "none", "elements": []},
            "error": "target unavailable",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir, results=[failed_browser])
            plan_path = Path(temp_dir) / "plan.json"
            plan_path.write_text(json.dumps(browser_plan()), encoding="utf-8")
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "retry-run",
                    "artifacts_root": temp_dir,
                    "plan_path": str(plan_path),
                }
            )
            first = runner.session(
                {
                    "command": "action",
                    "request_id": "failed-click",
                    "action": {
                        "type": "click",
                        "target": {"kind": "text", "name": "Missing"},
                    },
                }
            )
            second = runner.session(
                {
                    "command": "action",
                    "request_id": "successful-shot",
                    "action": {"type": "screenshot", "label": "home"},
                }
            )
            advanced = runner.session(
                {
                    "command": "advance_step",
                    "request_id": "advance-1",
                }
            )

            self.assertEqual(first["status"], "fail")
            self.assertEqual(second["status"], "pass")
            self.assertEqual(advanced["execution_entry"]["outcome"], "pass")
            action_statuses = [
                action["status"]
                for action in advanced["execution_entry"]["browser"]["actions"]
            ]
            self.assertEqual(action_statuses, ["fail", "pass"])
            self.assertTrue(
                Path(temp_dir, "retry-run", "browser_action_history.json").is_file()
            )

    def test_session_finalize_auto_closes_active_step_and_skips_remaining(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            plan_path = Path(temp_dir) / "plan.json"
            plan_path.write_text(json.dumps(two_step_browser_plan()), encoding="utf-8")
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "auto-finalize-run",
                    "artifacts_root": temp_dir,
                    "plan_path": str(plan_path),
                }
            )
            runner.session(
                {
                    "command": "action",
                    "request_id": "step-1-action",
                    "action": {"type": "goto"},
                }
            )
            final = runner.session(
                {
                    "command": "finalize",
                    "request_id": "finalize-1",
                    "overall_result": "inconclusive",
                }
            )

            self.assertEqual([entry["outcome"] for entry in final["execution_log"]], ["pass", "skip"])
            self.assertEqual([entry["step_id"] for entry in final["execution_log"]], [1, 2])

    def test_session_advance_step_requires_active_plan_session(self):
        temp_dir = tempfile.gettempdir()
        runner = WebClientRunner(
            docker=FakeDocker(),
            api=FakeAPI(),
            screenshotter=FakeScreenshotter(temp_dir),
            browser_driver=FakeBrowserDriver(temp_dir),
        )

        before_start = runner.session(
            {
                "command": "advance_step",
                "request_id": "advance-before-start",
            }
        )
        runner.session(
            {
                "command": "start",
                "request_id": "start-1",
                "run_id": "task-session",
                "base_url": "http://localhost:9000",
                "artifacts_root": temp_dir,
            }
        )
        task_mode = runner.session(
            {
                "command": "advance_step",
                "request_id": "advance-task",
            }
        )

        self.assertEqual(before_start["status"], "error")
        self.assertIn("no active web_client_session", before_start["error"])
        self.assertEqual(task_mode["status"], "error")
        self.assertIn("plan-backed", task_mode["error"])

    def test_session_rejects_multi_action_payloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "session-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                }
            )

            result = runner.session(
                {
                    "command": "action",
                    "request_id": "bad-action",
                    "action": [
                        {"type": "goto"},
                        {"type": "screenshot", "label": "home"},
                    ],
                }
            )

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error_code"], "schema_error")
            self.assertEqual(result["schema_path"], "$.action")
            self.assertEqual(browser_driver.runs, [])

    def test_session_preflight_schema_error_does_not_start_browser_or_docker(self):
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

            result = runner.session(
                {
                    "command": "start",
                    "request_id": "bad-start",
                    "run_id": "bad-run",
                    "artifacts_root": temp_dir,
                    "base_url": "http://localhost:9000",
                    "content": "{}",
                }
            )

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error_code"], "schema_error")
            self.assertEqual(result["schema_path"], "$.content")
            self.assertEqual(docker.pulled, [])
            self.assertEqual(docker.started, [])
            self.assertEqual(browser_driver.configures, [])
            self.assertEqual(browser_driver.runs, [])

    def test_session_schema_error_circuit_breaker_requires_finalize(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "schema-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                }
            )

            first = runner.session(
                {
                    "command": "action",
                    "request_id": "bad-1",
                    "action": {"type": "click", "selector": "#legacy"},
                }
            )
            second = runner.session(
                {
                    "command": "action",
                    "request_id": "bad-2",
                    "action": {"type": "click", "text": "Legacy"},
                }
            )
            blocked = runner.session(
                {
                    "command": "action",
                    "request_id": "blocked",
                    "action": {"type": "goto"},
                }
            )
            final = runner.session(
                {
                    "command": "finalize",
                    "request_id": "finalize-schema",
                }
            )

            self.assertEqual(first["error_code"], "schema_error")
            self.assertEqual(second["error_code"], "schema_error")
            self.assertTrue(second["requires_finalize"])
            self.assertEqual(blocked["error_code"], "schema_error")
            self.assertTrue(blocked["requires_finalize"])
            self.assertEqual(final["status"], "pass")
            self.assertEqual(browser_driver.runs, [])

    def test_session_writes_replay_manifest_with_actions_and_schema_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "replay-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                }
            )
            runner.session(
                {
                    "command": "action",
                    "request_id": "goto-1",
                    "action": {"type": "goto"},
                }
            )
            schema_error = runner.session(
                {
                    "command": "action",
                    "request_id": "bad-click",
                    "action": {"type": "click", "selector": "#legacy"},
                }
            )
            runner.session(
                {
                    "command": "action",
                    "request_id": "shot-1",
                    "action": {"type": "screenshot", "label": "home"},
                }
            )
            runner.session(
                {
                    "command": "finalize",
                    "request_id": "finalize-1",
                }
            )

            manifest_path = (
                Path(temp_dir)
                / "replay-run"
                / "browser_replay"
                / "replay_manifest.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            commands = manifest["commands"]
            replayable = [command for command in commands if command["replayable"]]
            schema_errors = [
                command
                for command in commands
                if command.get("schema_path") == "$.action.selector"
            ]

            self.assertEqual(schema_error["error_code"], "schema_error")
            self.assertEqual(
                [item["action"]["type"] for item in replayable],
                ["goto", "screenshot"],
            )
            self.assertEqual(len(schema_errors), 1)
            self.assertFalse(schema_errors[0]["replayable"])
            self.assertTrue(
                Path(
                    temp_dir,
                    "replay-run",
                    "browser_replay",
                    "replay_browser_session.py",
                ).is_file()
            )
            self.assertTrue(
                Path(
                    temp_dir,
                    "replay-run",
                    "browser_replay",
                    "README.md",
                ).is_file()
            )
            self.assertEqual(len(browser_driver.runs), 2)

    def test_session_no_progress_blocks_after_repeated_equivalent_failures(self):
        def failed_browser():
            return {
                "status": "fail",
                "actions": [
                    {
                        "type": "click",
                        "status": "fail",
                        "error": "no visible control",
                    }
                ],
                "screenshot_paths": [],
                "final_url": "http://localhost:8097/web",
                "title": "Jellyfin",
                "console": [],
                "failed_network": [],
                "dom_summary": "title='Jellyfin'",
                "dom_path": None,
                "page_text": "Jellyfin Home",
                "visible_controls": [
                    {"name": "Play", "scope": "player", "data_action": None}
                ],
                "media_state": {"state": "none", "elements": []},
                "error": "no visible control",
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(
                temp_dir,
                results=[failed_browser(), failed_browser(), failed_browser()],
            )
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "no-progress-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                }
            )
            request = {
                "command": "action",
                "action": {
                    "type": "click",
                    "target": {
                        "kind": "control",
                        "name": "Add to favorites",
                        "scope": "player",
                    },
                },
            }

            first = runner.session({**request, "request_id": "click-1"})
            second = runner.session({**request, "request_id": "click-2"})
            third = runner.session({**request, "request_id": "click-3"})
            blocked = runner.session({**request, "request_id": "click-4"})

            self.assertEqual(first["status"], "fail")
            self.assertNotIn("error_code", first)
            self.assertEqual(second["error_code"], "no_progress")
            self.assertTrue(second["no_progress"]["requires_different_strategy"])
            self.assertEqual(third["error_code"], "no_progress")
            self.assertTrue(third["requires_finalize"])
            self.assertEqual(blocked["status"], "error")
            self.assertEqual(blocked["error_code"], "no_progress")
            self.assertEqual(len(browser_driver.runs), 3)

    def test_session_action_before_start_returns_no_active_session_error(self):
        temp_dir = tempfile.gettempdir()
        runner = WebClientRunner(
            docker=FakeDocker(),
            api=FakeAPI(),
            screenshotter=FakeScreenshotter(temp_dir),
            browser_driver=FakeBrowserDriver(temp_dir),
        )

        result = runner.session(
            {
                "command": "action",
                "request_id": "action-before-start",
                "action": {"type": "goto"},
            }
        )

        self.assertEqual(result["status"], "error")
        self.assertIn("no active web_client_session", result["error"])

    def test_session_finalize_before_start_returns_no_active_session_error(self):
        temp_dir = tempfile.gettempdir()
        runner = WebClientRunner(
            docker=FakeDocker(),
            api=FakeAPI(),
            screenshotter=FakeScreenshotter(temp_dir),
            browser_driver=FakeBrowserDriver(temp_dir),
        )

        result = runner.session(
            {
                "command": "finalize",
                "request_id": "finalize-before-start",
            }
        )

        self.assertEqual(result["status"], "error")
        self.assertIn("no active web_client_session", result["error"])

    def test_session_rejects_second_start_without_replacing_active_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            first = runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "task-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                }
            )

            second = runner.session(
                {
                    "command": "start",
                    "request_id": "start-2",
                    "run_id": "other-run",
                    "base_url": "http://localhost:9001",
                    "artifacts_root": temp_dir,
                }
            )
            action = runner.session(
                {
                    "command": "action",
                    "request_id": "action-1",
                    "action": {"type": "goto"},
                }
            )

            self.assertEqual(first["status"], "pass")
            self.assertEqual(second["status"], "error")
            self.assertIn("already has an active session", second["error"])
            self.assertFalse(browser_driver.closed)
            self.assertEqual(action["status"], "pass")
            self.assertEqual(action["run_id"], "task-run")

    def test_session_rejects_deprecated_session_id_without_browser_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            runner.session(
                {
                    "command": "start",
                    "request_id": "start-1",
                    "run_id": "task-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                }
            )

            result = runner.session(
                {
                    "command": "action",
                    "request_id": "action-1",
                    "session_id": "stale-session",
                    "action": {"type": "goto"},
                }
            )

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error_code"], "schema_error")
            self.assertEqual(result["schema_path"], "$.session_id")
            self.assertEqual(browser_driver.runs, [])

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

    def test_module_level_session_preserves_task_sessions_across_calls(self):
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
            previous = web_client_runner_module._DEFAULT_SESSION_RUNNER
            web_client_runner_module._DEFAULT_SESSION_RUNNER = runner
            try:
                start = web_client_runner_module.session(
                    {
                        "command": "start",
                        "request_id": "module-start",
                        "run_id": "module-run",
                        "base_url": "http://localhost:9000",
                        "artifacts_root": temp_dir,
                        "browser_input": {"path": "/web"},
                    }
                )
                action = web_client_runner_module.session(
                    {
                        "command": "action",
                        "request_id": "module-action",
                        "action": {"type": "goto"},
                    }
                )
            finally:
                web_client_runner_module._DEFAULT_SESSION_RUNNER = previous

            self.assertNotIn("session_id", start)
            self.assertEqual(action["status"], "pass")
            self.assertNotIn("session_id", action)
            self.assertEqual(len(browser_driver.runs), 1)

    def test_module_level_session_uses_hidden_start_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "fallback-run",
            )
            previous_runner = web_client_runner_module._DEFAULT_SESSION_RUNNER
            previous_defaults = web_client_runner_module.configure_session_defaults(
                artifacts_root=temp_dir,
                run_id="ambient-run",
            )
            web_client_runner_module._DEFAULT_SESSION_RUNNER = runner
            try:
                start = web_client_runner_module.session(
                    {
                        "command": "start",
                        "request_id": "ambient-start",
                        "base_url": "http://localhost:9000",
                    }
                )
                final = web_client_runner_module.session(
                    {
                        "command": "finalize",
                        "request_id": "ambient-finalize",
                    }
                )
            finally:
                web_client_runner_module._DEFAULT_SESSION_RUNNER = previous_runner
                web_client_runner_module.restore_session_defaults(previous_defaults)

            self.assertEqual(start["status"], "pass")
            self.assertEqual(start["run_id"], "ambient-run")
            self.assertTrue(Path(temp_dir, "ambient-run").is_dir())
            self.assertEqual(final["status"], "pass")

    def test_module_level_session_task_mode_uses_same_worker_thread_inside_event_loop(self):
        main_thread = threading.get_ident()
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = ThreadTrackingBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "module-session",
            )
            previous = web_client_runner_module._DEFAULT_SESSION_RUNNER
            web_client_runner_module._DEFAULT_SESSION_RUNNER = runner

            async def exercise():
                start = web_client_runner_module.session(
                    {
                        "command": "start",
                        "request_id": "worker-start",
                        "run_id": "worker-task-run",
                        "base_url": "http://localhost:9000",
                        "artifacts_root": temp_dir,
                        "browser_input": {"path": "/web"},
                    }
                )
                action = web_client_runner_module.session(
                    {
                        "command": "action",
                        "request_id": "worker-action",
                        "action": {"type": "goto"},
                    }
                )
                final = web_client_runner_module.session(
                    {
                        "command": "finalize",
                        "request_id": "worker-finalize",
                    }
                )
                return start, action, final

            try:
                start, action, final = asyncio.run(exercise())
            finally:
                web_client_runner_module._DEFAULT_SESSION_RUNNER = previous
                shutdown_sync_workers()

            thread_ids = {thread_id for _event, thread_id in browser_driver.thread_events}
            self.assertNotIn("session_id", start)
            self.assertEqual(action["status"], "pass")
            self.assertNotIn("session_id", action)
            self.assertEqual(final["status"], "pass")
            self.assertNotIn("session_id", final)
            self.assertEqual(len(thread_ids), 1)
            self.assertNotEqual(next(iter(thread_ids)), main_thread)

    def test_module_level_session_uses_same_worker_thread_inside_event_loop(self):
        main_thread = threading.get_ident()
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = ThreadTrackingBrowserDriver(temp_dir)
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                uuid_factory=lambda: "module-plan-session",
            )
            plan_path = Path(temp_dir) / "plan.json"
            plan_path.write_text(json.dumps(demo_browser_plan()), encoding="utf-8")
            previous = web_client_runner_module._DEFAULT_SESSION_RUNNER
            web_client_runner_module._DEFAULT_SESSION_RUNNER = runner

            async def exercise():
                start = web_client_runner_module.session(
                    {
                        "command": "start",
                        "request_id": "worker-start",
                        "run_id": "worker-plan-run",
                        "artifacts_root": temp_dir,
                        "plan_path": str(plan_path),
                    }
                )
                action = web_client_runner_module.session(
                    {
                        "command": "action",
                        "request_id": "worker-next",
                        "action": {"type": "screenshot", "label": "home"},
                    }
                )
                final = web_client_runner_module.session(
                    {
                        "command": "finalize",
                        "request_id": "worker-finalize",
                        "overall_result": "reproduced",
                    }
                )
                return start, action, final

            try:
                start, action, final = asyncio.run(exercise())
            finally:
                web_client_runner_module._DEFAULT_SESSION_RUNNER = previous
                shutdown_sync_workers()

            thread_ids = {thread_id for _event, thread_id in browser_driver.thread_events}
            self.assertNotIn("session_id", start)
            self.assertEqual(action["status"], "pass")
            self.assertNotIn("session_id", action)
            self.assertEqual(final["overall_result"], "reproduced")
            self.assertEqual(len(thread_ids), 1)
            self.assertNotEqual(next(iter(thread_ids)), main_thread)


class WebClientRunnerToolTests(unittest.TestCase):
    def test_session_tool_accepts_canonical_request_payload(self):
        request = {
            "command": "start",
            "request_id": "start-1",
            "run_id": "tool-run",
            "artifacts_root": "/tmp/artifacts",
            "plan_markdown_path": "/tmp/artifacts/plan.md",
        }
        expected = {
            "request_id": "start-1",
            "status": "pass",
        }

        with patch.object(
            web_client_runner_module,
            "session",
            return_value=expected,
        ) as session_tool:
            wrapped = asyncio.run(
                WebClientSessionTool()._execute({"request": request})
            )
            raw = asyncio.run(WebClientSessionTool()._execute(request))

        self.assertIsNone(wrapped.error)
        self.assertEqual(json.loads(wrapped.output), expected)
        self.assertIsNotNone(raw.error)
        self.assertIn("schema_error", raw.error)
        session_tool.assert_called_once_with(request=request)

    def test_session_tool_accepts_body_style_json(self):
        request = {
            "command": "start",
            "request_id": "start-1",
            "run_id": "tool-run",
            "artifacts_root": "/tmp/artifacts",
            "plan_markdown_path": "/tmp/artifacts/plan.md",
        }
        expected = {"request_id": "start-1", "status": "pass"}

        with patch.object(
            web_client_runner_module,
            "session",
            return_value=expected,
        ) as session_tool:
            raw_body = asyncio.run(
                WebClientSessionTool()._execute(
                    {"content": json.dumps(request)}
                )
            )
            wrapped_body = asyncio.run(
                WebClientSessionTool()._execute(
                    {"content": json.dumps({"request": request})}
                )
            )

        self.assertIsNone(raw_body.error)
        self.assertEqual(json.loads(raw_body.output), expected)
        self.assertIsNone(wrapped_body.error)
        self.assertEqual(json.loads(wrapped_body.output), expected)
        self.assertEqual(session_tool.call_count, 2)
        for call in session_tool.call_args_list:
            self.assertEqual(call.kwargs["request"], request)

    def test_session_tool_accepts_multiline_at_request_arg(self):
        request = {
            "command": "start",
            "request_id": "start-1",
            "plan_markdown": "# Plan\\n\\nLine 2",
        }
        expected = {"request_id": "start-1", "status": "pass"}

        full_json = json.dumps(request)
        # The KT bracket parser splits multi-line ``@@request={`` calls into
        # ``request="{"`` and the JSON tail in ``content``.
        split_request = "{"
        split_content = full_json[1:]

        with patch.object(
            web_client_runner_module,
            "session",
            return_value=expected,
        ) as session_tool:
            result = asyncio.run(
                WebClientSessionTool()._execute(
                    {"request": split_request, "content": split_content}
                )
            )

        self.assertIsNone(result.error)
        self.assertEqual(json.loads(result.output), expected)
        session_tool.assert_called_once_with(request=request)

    def test_session_tool_rejects_unknown_top_level_field(self):
        result = asyncio.run(
            WebClientSessionTool()._execute(
                {"request": {"command": "start"}, "extra": "nope"}
            )
        )
        self.assertIsNotNone(result.error)
        self.assertIn("$.extra", result.error)

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


class FinalizeResultTrimTests(unittest.TestCase):
    def test_trim_strips_verbose_browser_evidence(self):
        result = {
            "plan": {"reproduction_steps": []},
            "run_id": "run-1",
            "execution_log": [
                {
                    "step_id": 1,
                    "browser": {
                        "status": "pass",
                        "final_url": "http://localhost/web",
                        "actions": [{"type": "click", "status": "pass"}],
                        "dom_summary": "x" * 5000,
                        "page_text": "y" * 5000,
                        "visible_controls": [{"name": str(i)} for i in range(50)],
                        "visible_links": [{"name": str(i)} for i in range(50)],
                        "player_controls": [{"name": str(i)} for i in range(20)],
                        "console": [{"text": "z"}],
                        "failed_network": [],
                        "media_state": {"state": "none", "elements": []},
                        "dom_path": "/tmp/dom.html",
                    },
                }
            ],
        }

        trimmed = web_client_runner_module._trim_finalize_result(result)
        self.assertNotIn("plan", trimmed)
        browser = trimmed["execution_log"][0]["browser"]
        self.assertEqual(browser["status"], "pass")
        self.assertEqual(browser["final_url"], "http://localhost/web")
        self.assertEqual(browser["actions"], [{"type": "click", "status": "pass"}])
        for stripped in (
            "dom_summary",
            "page_text",
            "visible_controls",
            "visible_links",
            "player_controls",
            "console",
            "failed_network",
            "media_state",
            "dom_path",
        ):
            self.assertNotIn(stripped, browser)
        self.assertIsNot(trimmed, result)
        self.assertIn("dom_summary", result["execution_log"][0]["browser"])

    def test_trim_caps_target_diagnostics_candidates(self):
        many = [{"name": f"candidate-{i}"} for i in range(20)]
        result = {
            "execution_log": [
                {
                    "browser": {
                        "actions": [
                            {
                                "type": "click",
                                "status": "fail",
                                "target_diagnostics": {
                                    "candidates": many,
                                    "match_count": len(many),
                                },
                            }
                        ],
                    },
                }
            ],
        }

        trimmed = web_client_runner_module._trim_finalize_result(result)
        action = trimmed["execution_log"][0]["browser"]["actions"][0]
        diag = action["target_diagnostics"]
        self.assertEqual(len(diag["candidates"]), 5)
        self.assertEqual(diag["candidates_truncated"], 20)


if __name__ == "__main__":
    unittest.main()
