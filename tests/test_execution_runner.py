import tempfile
import unittest
from pathlib import Path

from creatures.execution.tools.execution_runner import ExecutionRunner


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

    def request(self, method, path, body=None, headers=None):
        self.requests.append(
            {"method": method, "path": path, "body": body, "headers": headers}
        )
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


if __name__ == "__main__":
    unittest.main()
