import tempfile
import unittest
from pathlib import Path

from tools.execution_runner import ExecutionRunner


class FakeDocker:
    def __init__(self):
        self.pulled = []
        self.started = []
        self.stopped = []
        self.logs_calls = []
        self.running = True

    def pull(self, image, run_id=None):
        self.pulled.append((image, run_id))
        return {"image": image}

    def start(self, image, ports, volumes, env_vars, run_id):
        self.started.append(
            {
                "image": image,
                "ports": ports,
                "volumes": volumes,
                "env_vars": env_vars,
                "run_id": run_id,
            }
        )
        return {"container_id": "container-1", "base_url": "http://localhost:8097"}

    def exec(self, container_id, command, timeout_s=120, run_id=None):
        return {"stdout": "inside", "stderr": "", "exit_code": 0}

    def logs(self, container_id, tail=500, since=None, run_id=None):
        self.logs_calls.append({"tail": tail, "since": since, "run_id": run_id})
        return {"logs": "server log\nHEVC decode error\n"}

    def inspect(self, container_id):
        return {"State": {"Running": self.running, "Status": "running"}}

    def stop(self, container_id, run_id=None):
        self.stopped.append((container_id, run_id))
        return {"status": "removed"}


class FakeAPI:
    def __init__(self, responses=None, auth_success=True, healthy=True):
        self.responses = list(responses or [])
        self.auth_success = auth_success
        self.healthy = healthy
        self.requests = []
        self.base_url = "http://localhost:8096"

    def configure(self, base_url=None, run_id=None):
        if base_url:
            self.base_url = base_url
        self.run_id = run_id

    def wait_healthy(self, timeout_s=60):
        return {"healthy": self.healthy}

    def complete_startup_wizard(self):
        self.completed_wizard = True
        return {"provisioned": True}

    def authenticate(self):
        return {"success": self.auth_success, "token": "token"}

    def request(self, method, path, **kwargs):
        self.requests.append({"method": method, "path": path, **kwargs})
        return self.responses.pop(0)


class FakeScreenshotter:
    def __init__(self, artifacts_root):
        self.artifacts_root = Path(artifacts_root)
        self.captures = []

    def capture(self, url, run_id, label, wait_selector=None, wait_ms=2000):
        self.captures.append({"url": url, "run_id": run_id, "label": label})
        path = self.artifacts_root / run_id / "screenshots" / f"{label}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return {"path": str(path), "url": url, "label": label}


class FakeBrowserDriver:
    def __init__(self, artifacts_root, results=None):
        self.artifacts_root = Path(artifacts_root)
        self.results = list(results or [])
        self.configures = []
        self.runs = []
        self.inspected_selectors = []
        self.capture_maps = []
        self.closed = False

    def configure(self, base_url=None, run_id=None):
        self.configures.append({"base_url": base_url, "run_id": run_id})

    def run(self, browser_input, run_id, step_id=None):
        self.runs.append(
            {"browser_input": browser_input, "run_id": run_id, "step_id": step_id}
        )
        if self.results:
            return self.results.pop(0)
        label = str(browser_input.get("label") or f"step_{step_id}")
        path = self.artifacts_root / run_id / "screenshots" / f"{label}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return {
            "status": "pass",
            "actions": [
                {
                    "type": "screenshot",
                    "status": "pass",
                    "label": label,
                    "screenshot_path": str(path),
                }
            ],
            "screenshot_paths": [str(path)],
            "final_url": "http://localhost:8097/web",
            "title": "Jellyfin",
            "console": [],
            "failed_network": [],
            "dom_summary": "title='Jellyfin'",
            "dom_path": None,
            "page_text": "Jellyfin Home",
            "media_state": {"state": "none", "elements": []},
            "error": None,
        }

    def inspect_selectors(self, selectors):
        self.inspected_selectors.append(list(selectors))
        return {
            selector: {"attached": True, "visible": selector != ".hidden"}
            for selector in selectors
        }

    def capture_values(self, capture_map):
        self.capture_maps.append(capture_map)
        values = {}
        for variable, expression in (capture_map or {}).items():
            if expression.get("from") == "browser_eval":
                values[variable] = "eval-value"
            elif expression.get("from") == "browser_attribute":
                values[variable] = "attr-value"
        return values

    def close(self):
        self.closed = True


def base_plan(steps):
    return {
        "issue_url": "https://github.com/jellyfin/jellyfin/issues/1",
        "issue_title": "Bug",
        "target_version": "10.9.7",
        "docker_image": "jellyfin/jellyfin:10.9.7",
        "prerequisites": [],
        "environment": {
            "ports": {"host": 8096, "container": 8096},
            "volumes": [],
            "env_vars": {},
        },
        "reproduction_steps": steps,
        "reproduction_goal": "Observe bug",
        "failure_indicators": ["bug"],
        "confidence": "high",
        "ambiguities": [],
        "is_verification": False,
        "original_run_id": None,
    }


class ExecutionRunnerTests(unittest.TestCase):
    def test_execute_plan_resolves_capture_and_marks_reproduced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            api = FakeAPI(
                responses=[
                    {"status_code": 500, "body": "bug happened", "headers": {}},
                ]
            )
            docker = FakeDocker()
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=docker,
                api=api,
                screenshotter=FakeScreenshotter(temp_dir),
                command_runner=lambda command, cwd=None, timeout_s=120: {
                    "stdout": "item-1\n",
                    "stderr": "",
                    "exit_code": 0,
                },
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "setup",
                        "action": "Capture item id",
                        "tool": "bash",
                        "input": {"command": "echo item-1"},
                        "capture": {"item_id": {"from": "stdout_trimmed"}},
                        "expected_outcome": "item id printed",
                        "success_criteria": {
                            "all_of": [{"type": "exit_code", "equals": 0}]
                        },
                    },
                    {
                        "step_id": 2,
                        "role": "trigger",
                        "action": "Trigger bug",
                        "tool": "http_request",
                        "input": {"method": "GET", "path": "/Items/${item_id}"},
                        "expected_outcome": "bug observed",
                        "success_criteria": {
                            "all_of": [
                                {"type": "status_code", "equals": 500},
                                {"type": "body_contains", "value": "bug"},
                            ]
                        },
                    },
                ]
            )

            result = runner.execute_plan(plan, run_id="run-1")

            self.assertEqual(result["overall_result"], "reproduced")
            self.assertEqual(api.requests[0]["path"], "/Items/item-1")
            self.assertEqual([entry["outcome"] for entry in result["execution_log"]], ["pass", "pass"])
            self.assertEqual(docker.started[0]["volumes"][0]["container"], "/media")
            self.assertTrue(Path(temp_dir, "run-1", "plan.json").exists())
            self.assertTrue(Path(temp_dir, "run-1", "result.json").exists())
            self.assertTrue(Path(temp_dir, "run-1", "jellyfin_server.log").exists())

    def test_trigger_fail_marks_not_reproduced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            api = FakeAPI(
                responses=[
                    {"status_code": 200, "body": "ok", "headers": {}},
                ]
            )
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=api,
                screenshotter=FakeScreenshotter(temp_dir),
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "trigger",
                        "action": "Trigger bug",
                        "tool": "http_request",
                        "input": {"method": "GET", "path": "/bug"},
                        "expected_outcome": "HTTP 500",
                        "success_criteria": {
                            "all_of": [{"type": "status_code", "equals": 500}]
                        },
                    }
                ]
            )

            result = runner.execute_plan(plan, run_id="run-2")

            self.assertEqual(result["overall_result"], "not_reproduced")
            self.assertEqual(result["execution_log"][0]["outcome"], "fail")

    def test_http_request_forwards_raw_transport_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            api = FakeAPI(
                responses=[
                    {"status_code": 400, "body": "bad json", "headers": {}},
                ]
            )
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=api,
                screenshotter=FakeScreenshotter(temp_dir),
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "trigger",
                        "action": "Send malformed JSON",
                        "tool": "http_request",
                        "input": {
                            "method": "POST",
                            "path": "/Users",
                            "params": {"bad": "true"},
                            "headers": {"Content-Type": "application/json"},
                            "auth": "none",
                            "body_text": '{"Name":',
                            "timeout_s": 5,
                            "follow_redirects": True,
                        },
                        "expected_outcome": "HTTP 400",
                        "success_criteria": {
                            "all_of": [{"type": "status_code", "equals": 400}]
                        },
                    }
                ]
            )

            result = runner.execute_plan(plan, run_id="run-raw")

            self.assertEqual(result["overall_result"], "reproduced")
            self.assertEqual(
                api.requests[0],
                {
                    "method": "POST",
                    "path": "/Users",
                    "params": {"bad": "true"},
                    "headers": {"Content-Type": "application/json"},
                    "auth": "none",
                    "token": None,
                    "body_json": None,
                    "body_text": '{"Name":',
                    "body_base64": None,
                    "timeout_s": 5,
                    "follow_redirects": True,
                    "allow_absolute_url": False,
                },
            )

    def test_http_request_rejects_legacy_body_field(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            api = FakeAPI(responses=[])
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=api,
                screenshotter=FakeScreenshotter(temp_dir),
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "trigger",
                        "action": "Use unsupported body field",
                        "tool": "http_request",
                        "input": {"method": "POST", "path": "/Users", "body": {}},
                        "expected_outcome": "Rejected",
                        "success_criteria": {
                            "all_of": [{"type": "status_code", "equals": 400}]
                        },
                    }
                ]
            )

            result = runner.execute_plan(plan, run_id="run-legacy-body")

            self.assertEqual(api.requests, [])
            self.assertEqual(result["execution_log"][0]["outcome"], "fail")
            self.assertIn("body_json", result["execution_log"][0]["reason"])

    def test_auth_failure_skips_steps_and_marks_inconclusive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            docker = FakeDocker()
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=docker,
                api=FakeAPI(auth_success=False),
                screenshotter=FakeScreenshotter(temp_dir),
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "trigger",
                        "action": "Trigger bug",
                        "tool": "http_request",
                        "input": {"method": "GET", "path": "/bug"},
                        "expected_outcome": "HTTP 500",
                        "success_criteria": {
                            "all_of": [{"type": "status_code", "equals": 500}]
                        },
                    }
                ]
            )

            result = runner.execute_plan(plan, run_id="run-3")

            self.assertEqual(result["overall_result"], "inconclusive")
            self.assertEqual(result["execution_log"][0]["outcome"], "skip")
            self.assertEqual(result["error_summary"], "authentication failed")
            self.assertEqual(docker.stopped, [("container-1", "run-3")])

    def test_forbidden_docker_lifecycle_step_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "trigger",
                        "action": "Start another container",
                        "tool": "bash",
                        "input": {"command": "docker run jellyfin/jellyfin:10.9.7"},
                        "expected_outcome": "Skipped",
                        "success_criteria": {
                            "all_of": [{"type": "exit_code", "equals": 0}]
                        },
                    }
                ]
            )

            result = runner.execute_plan(plan, run_id="run-4")
            docker_log = Path(temp_dir, "run-4", "docker_ops.log").read_text(
                encoding="utf-8"
            )

            self.assertEqual(result["overall_result"], "inconclusive")
            self.assertEqual(result["execution_log"][0]["outcome"], "skip")
            self.assertIn("forbidden_docker_step_skipped", docker_log)

    def test_browser_step_runs_driver_and_binds_screenshot_criteria(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "trigger",
                        "action": "Use Jellyfin Web",
                        "tool": "browser",
                        "input": {
                            "path": "/web",
                            "label": "web_home",
                            "actions": [{"type": "screenshot", "label": "web_home"}],
                        },
                        "expected_outcome": "Web UI is visible",
                        "success_criteria": {
                            "all_of": [
                                {"type": "screenshot_present", "label": "web_home"}
                            ]
                        },
                    }
                ]
            )

            result = runner.execute_plan(plan, run_id="run-browser")

            entry = result["execution_log"][0]
            self.assertEqual(result["overall_result"], "reproduced")
            self.assertEqual(entry["outcome"], "pass")
            self.assertEqual(entry["browser"]["status"], "pass")
            self.assertTrue(Path(entry["screenshot_path"]).exists())
            self.assertEqual(browser_driver.runs[0]["step_id"], 1)
            self.assertEqual(
                browser_driver.configures,
                [{"base_url": "http://localhost:8097", "run_id": "run-browser"}],
            )
            self.assertTrue(browser_driver.closed)

    def test_browser_session_is_reused_across_browser_steps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "setup",
                        "action": "Open dashboard",
                        "tool": "browser",
                        "input": {
                            "label": "dashboard",
                            "actions": [{"type": "screenshot", "label": "dashboard"}],
                        },
                        "expected_outcome": "Dashboard appears",
                        "success_criteria": {
                            "all_of": [
                                {"type": "screenshot_present", "label": "dashboard"}
                            ]
                        },
                    },
                    {
                        "step_id": 2,
                        "role": "trigger",
                        "action": "Open playback",
                        "tool": "browser",
                        "input": {
                            "label": "playback",
                            "actions": [{"type": "screenshot", "label": "playback"}],
                        },
                        "expected_outcome": "Playback bug appears",
                        "success_criteria": {
                            "all_of": [
                                {"type": "screenshot_present", "label": "playback"}
                            ]
                        },
                    },
                ]
            )

            result = runner.execute_plan(plan, run_id="run-browser-reuse")

            self.assertEqual([entry["outcome"] for entry in result["execution_log"]], ["pass", "pass"])
            self.assertEqual([call["step_id"] for call in browser_driver.runs], [1, 2])
            self.assertTrue(browser_driver.closed)

    def test_browser_failure_records_failure_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(
                temp_dir,
                results=[
                    {
                        "status": "fail",
                        "actions": [
                            {
                                "type": "click",
                                "status": "fail",
                                "selector": "#missing",
                                "error": "selector not found",
                            }
                        ],
                        "screenshot_paths": [],
                        "final_url": "http://localhost:8097/web",
                        "title": "Jellyfin",
                        "console": [],
                        "failed_network": [],
                        "dom_summary": "missing button",
                        "dom_path": None,
                        "media_state": {"state": "none", "elements": []},
                        "error": "selector not found",
                    }
                ],
            )
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "trigger",
                        "action": "Click missing control",
                        "tool": "browser",
                        "input": {
                            "path": "/web",
                            "label": "missing",
                            "actions": [{"type": "click", "selector": "#missing"}],
                        },
                        "expected_outcome": "Browser action succeeds",
                        "success_criteria": {
                            "all_of": [
                                {"type": "screenshot_present", "label": "missing"}
                            ]
                        },
                    }
                ]
            )

            result = runner.execute_plan(plan, run_id="run-browser-fail")
            entry = result["execution_log"][0]

            self.assertEqual(entry["outcome"], "fail")
            self.assertEqual(entry["browser"]["status"], "fail")
            self.assertTrue(Path(entry["failure_logs_path"]).exists())
            self.assertTrue(Path(entry["failure_screenshot_path"]).exists())

    def test_browser_criteria_and_captures_feed_later_steps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            commands = []
            browser_driver = FakeBrowserDriver(temp_dir)
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
                command_runner=lambda command, cwd=None, timeout_s=120: commands.append(command)
                or {"stdout": "ok", "stderr": "", "exit_code": 0},
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "setup",
                        "action": "Read UI state",
                        "tool": "browser",
                        "input": {
                            "label": "ui",
                            "actions": [{"type": "wait_for", "selector": ".poster"}],
                        },
                        "capture": {
                            "item_id": {
                                "from": "browser_attribute",
                                "selector": ".poster",
                                "name": "data-id",
                            },
                            "eval_id": {"from": "browser_eval", "script": "() => 1"},
                        },
                        "expected_outcome": "UI state is available",
                        "success_criteria": {
                            "all_of": [
                                {
                                    "type": "browser_element",
                                    "selector": ".poster",
                                    "state": "visible",
                                },
                                {
                                    "type": "browser_text_contains",
                                    "value": "Jellyfin",
                                },
                            ]
                        },
                    },
                    {
                        "step_id": 2,
                        "role": "trigger",
                        "action": "Use captured UI values",
                        "tool": "bash",
                        "input": {"command": "echo ${item_id}:${eval_id}"},
                        "expected_outcome": "Values are resolved",
                        "success_criteria": {
                            "all_of": [{"type": "exit_code", "equals": 0}]
                        },
                    },
                ]
            )

            result = runner.execute_plan(plan, run_id="run-browser-captures")

            self.assertEqual([entry["outcome"] for entry in result["execution_log"]], ["pass", "pass"])
            self.assertEqual(commands[-1], "echo attr-value:eval-value")
            self.assertEqual(browser_driver.inspected_selectors, [[".poster"]])
            self.assertEqual(list(browser_driver.capture_maps[0]), ["item_id", "eval_id"])

    def test_browser_repair_request_retry_and_final_result_include_attempts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repaired_path = Path(temp_dir) / "repair-shot.png"
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
                                "error": "strict mode violation",
                            }
                        ],
                        "screenshot_paths": [],
                        "final_url": "http://localhost:8097/web",
                        "title": "Jellyfin",
                        "console": [{"type": "error", "text": "old selector failed"}],
                        "failed_network": [{"url": "http://localhost:8097/api", "status": 500}],
                        "dom_summary": "button changed",
                        "dom_path": None,
                        "page_text": "button changed",
                        "media_state": {"state": "none", "elements": []},
                        "error": "strict mode violation",
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
                        "final_url": "http://localhost:8097/web",
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
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "trigger",
                        "action": "Click old selector",
                        "tool": "browser",
                        "input": {
                            "path": "/web",
                            "label": "old",
                            "actions": [{"type": "click", "selector": "#old"}],
                        },
                        "expected_outcome": "Browser action succeeds",
                        "success_criteria": {"all_of": [{"type": "browser_action_run"}]},
                    }
                ]
            )

            repair = runner.start_plan(plan, run_id="run-repair")

            self.assertEqual(repair["status"], "needs_browser_repair")
            self.assertEqual(repair["step_id"], 1)
            self.assertEqual(
                repair["repair_context"]["failed_action"]["selector"],
                "#old",
            )
            self.assertEqual(
                repair["repair_context"]["playwright_error"],
                "strict mode violation",
            )
            self.assertIn("refresh", repair["repair_context"]["note"])

            result = runner.retry_browser_step(
                1,
                {
                    "label": "fixed",
                    "actions": [
                        {"type": "refresh"},
                        {"type": "click", "selector": "#new"},
                        {"type": "screenshot", "label": "fixed"},
                    ],
                },
            )

            self.assertEqual(result["overall_result"], "reproduced")
            self.assertEqual(
                [entry["attempt"] for entry in result["execution_log"]],
                [1, 2],
            )
            self.assertEqual(
                [entry["browser"]["status"] for entry in result["execution_log"]],
                ["fail", "pass"],
            )
            self.assertEqual(browser_driver.runs[1]["browser_input"]["label"], "fixed")

            rejected = runner.retry_browser_step(1, {"actions": []})
            self.assertEqual(rejected["status"], "repair_rejected")

    def test_browser_repair_rejects_forbidden_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_driver = FakeBrowserDriver(
                temp_dir,
                results=[
                    {
                        "status": "fail",
                        "actions": [{"type": "click", "status": "fail", "error": "bad"}],
                        "screenshot_paths": [],
                        "final_url": "http://localhost:8097/web",
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
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "trigger",
                        "action": "Click old selector",
                        "tool": "browser",
                        "input": {
                            "path": "/web",
                            "actions": [{"type": "click", "selector": "#old"}],
                        },
                        "expected_outcome": "Browser action succeeds",
                        "success_criteria": {"all_of": [{"type": "browser_action_run"}]},
                    }
                ]
            )

            repair = runner.start_plan(plan, run_id="run-repair-reject")
            rejected = runner.retry_browser_step(
                repair["step_id"],
                {
                    "actions": [{"type": "click", "selector": "#new"}],
                    "success_criteria": {"all_of": [{"type": "browser_action_run"}]},
                },
            )
            result = runner.finalize_plan()

            self.assertEqual(rejected["status"], "repair_rejected")
            self.assertIn("forbidden", rejected["reason"])
            self.assertEqual(result["overall_result"], "not_reproduced")
            self.assertEqual(len(result["execution_log"]), 1)

    def test_synthetic_browser_reproduction_records_media_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            shot = Path(temp_dir) / "media-playing.png"
            shot.write_bytes(b"png")
            browser_driver = FakeBrowserDriver(
                temp_dir,
                results=[
                    {
                        "status": "pass",
                        "actions": [
                            {"type": "goto", "status": "pass"},
                            {"type": "wait_for_media", "status": "pass", "state": "playing"},
                            {
                                "type": "screenshot",
                                "status": "pass",
                                "label": "media_playing",
                                "screenshot_path": str(shot),
                            },
                        ],
                        "screenshot_paths": [str(shot)],
                        "final_url": "http://localhost:8097/web/index.html#!/video",
                        "title": "Jellyfin",
                        "console": [{"type": "warning", "text": "media warning"}],
                        "failed_network": [],
                        "dom_summary": "video player",
                        "dom_path": None,
                        "page_text": "Now Playing",
                        "media_state": {
                            "state": "playing",
                            "elements": [{"tag": "video", "paused": False}],
                        },
                        "error": None,
                    }
                ],
            )
            runner = ExecutionRunner(
                artifacts_root=temp_dir,
                docker=FakeDocker(),
                api=FakeAPI(),
                screenshotter=FakeScreenshotter(temp_dir),
                browser_driver=browser_driver,
            )
            plan = base_plan(
                [
                    {
                        "step_id": 1,
                        "role": "trigger",
                        "action": "Play media in Jellyfin Web",
                        "tool": "browser",
                        "input": {
                            "path": "/web/index.html",
                            "auth": "auto",
                            "label": "media_playing",
                            "actions": [
                                {"type": "goto"},
                                {"type": "wait_for_media", "state": "playing"},
                                {"type": "screenshot", "label": "media_playing"},
                            ],
                        },
                        "expected_outcome": "Media is playing in the web client.",
                        "success_criteria": {
                            "all_of": [
                                {"type": "browser_action_run"},
                                {"type": "browser_media_state", "state": "playing"},
                                {
                                    "type": "browser_console_matches",
                                    "pattern": "media warning",
                                },
                            ]
                        },
                    }
                ]
            )

            result = runner.execute_plan(plan, run_id="run-synthetic-browser")

            self.assertEqual(result["overall_result"], "reproduced")
            self.assertEqual(
                result["execution_log"][0]["browser"]["media_state"]["state"],
                "playing",
            )
            self.assertEqual(browser_driver.runs[0]["browser_input"]["auth"], "auto")


if __name__ == "__main__":
    unittest.main()
