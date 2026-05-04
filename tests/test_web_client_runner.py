import tempfile
import unittest
import copy
from pathlib import Path

from tools.web_client_runner import WebClientRunner

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

    def test_task_mode_uses_supplied_base_url_and_never_starts_docker(self):
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

            result = runner.run_task(
                {
                    "request_id": "request-1",
                    "run_id": "task-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                    "step_id": "browser-step",
                    "browser_input": {
                        "path": "/web",
                        "label": "home",
                        "actions": [{"type": "screenshot", "label": "home"}],
                    },
                    "selector_assertions": [{"selector": "body", "state": "visible"}],
                    "capture": {"current_url": {"from": "browser_url"}},
                }
            )

            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["request_id"], "request-1")
            self.assertEqual(result["capture_values"]["current_url"], "http://localhost:8097/web")
            self.assertEqual(docker.pulled, [])
            self.assertEqual(docker.started, [])
            self.assertEqual(
                browser_driver.configures,
                [{"base_url": "http://localhost:9000", "run_id": "task-run"}],
            )

    def test_task_mode_allows_one_browser_input_repair_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repaired_path = Path(temp_dir) / "fixed.png"
            repaired_path.write_bytes(b"png")
            browser_driver = FakeBrowserDriver(
                temp_dir,
                results=[
                    {
                        "status": "fail",
                        "actions": [
                            {
                                "type": "click",
                                "status": "fail",
                                "selector": "#old",
                                "error": "selector not found",
                            }
                        ],
                        "screenshot_paths": [],
                        "final_url": "http://localhost:9000/web",
                        "title": "Jellyfin",
                        "console": [],
                        "failed_network": [],
                        "dom_summary": "old",
                        "dom_path": None,
                        "page_text": "old",
                        "media_state": {"state": "none", "elements": []},
                        "error": "selector not found",
                    },
                    {
                        "status": "pass",
                        "actions": [
                            {
                                "type": "screenshot",
                                "status": "pass",
                                "label": "fixed",
                                "screenshot_path": str(repaired_path),
                            }
                        ],
                        "screenshot_paths": [str(repaired_path)],
                        "final_url": "http://localhost:9000/web",
                        "title": "Jellyfin",
                        "console": [],
                        "failed_network": [],
                        "dom_summary": "fixed",
                        "dom_path": None,
                        "page_text": "fixed",
                        "media_state": {"state": "none", "elements": []},
                        "error": None,
                    },
                ],
            )
            runner = WebClientRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )

            result = runner.run_task(
                {
                    "request_id": "repair-1",
                    "run_id": "repair-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                    "step_id": 7,
                    "browser_input": {
                        "path": "/web",
                        "actions": [{"type": "click", "selector": "#old"}],
                    },
                    "repair_policy": {
                        "browser_input": {
                            "label": "fixed",
                            "actions": [
                                {"type": "refresh"},
                                {"type": "click", "selector": "#new"},
                                {"type": "screenshot", "label": "fixed"},
                            ],
                        }
                    },
                }
            )

            self.assertEqual(result["status"], "pass")
            self.assertTrue(result["repair_attempted"])
            self.assertEqual(len(browser_driver.runs), 2)
            self.assertEqual(browser_driver.runs[1]["browser_input"]["label"], "fixed")

    def test_task_mode_rejects_repair_fields_outside_browser_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(
                temp_dir,
                results=[
                    {
                        "status": "fail",
                        "actions": [{"type": "click", "status": "fail", "error": "bad"}],
                        "screenshot_paths": [],
                        "final_url": "http://localhost:9000/web",
                        "title": "Jellyfin",
                        "console": [],
                        "failed_network": [],
                        "dom_summary": "bad",
                        "dom_path": None,
                        "page_text": "bad",
                        "media_state": {"state": "none", "elements": []},
                        "error": "bad",
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

            result = runner.run_task(
                {
                    "request_id": "repair-reject",
                    "run_id": "repair-reject-run",
                    "base_url": "http://localhost:9000",
                    "artifacts_root": temp_dir,
                    "step_id": 7,
                    "browser_input": {
                        "path": "/web",
                        "actions": [{"type": "click", "selector": "#old"}],
                    },
                    "repair_policy": {
                        "browser_input": {
                            "actions": [{"type": "click", "selector": "#new"}],
                            "success_criteria": {
                                "all_of": [{"type": "browser_action_run"}]
                            },
                        }
                    },
                }
            )

            self.assertEqual(result["status"], "fail")
            self.assertIn("forbidden browser repair fields", result["error"])
            self.assertEqual(len(browser_driver.runs), 1)


if __name__ == "__main__":
    unittest.main()
