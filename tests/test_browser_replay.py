import json
import tempfile
import unittest
from pathlib import Path

from utils.browser_replay import BrowserReplayRecorder, replay_manifest


class FakeReplayDriver:
    def __init__(self, artifacts_root=None, base_url=None, run_id=None, **kwargs):
        self.artifacts_root = Path(artifacts_root)
        self.base_url = base_url
        self.run_id = run_id
        self.kwargs = kwargs
        self.configures = []
        self.browser_configs = []
        self.trace_paths = []
        self.calls = []
        self.closed = False

    def configure(self, base_url=None, run_id=None):
        self.configures.append({"base_url": base_url, "run_id": run_id})
        self.base_url = base_url
        self.run_id = run_id

    def configure_browser(self, headless=None, slow_mo_ms=None):
        self.browser_configs.append(
            {"headless": headless, "slow_mo_ms": slow_mo_ms}
        )

    def configure_tracing(self, trace_path):
        self.trace_paths.append(Path(trace_path))

    def run_single_action(self, browser_input, action, run_id=None, step_id=None):
        self.calls.append(
            {
                "browser_input": browser_input,
                "action": action,
                "run_id": run_id,
                "step_id": step_id,
            }
        )
        return {
            "status": "pass",
            "actions": [{"type": action["type"], "status": "pass"}],
            "screenshot_paths": [],
            "final_url": f"{self.base_url}/web",
            "dom_path": None,
            "error": None,
        }

    def trace_state(self):
        return {
            "enabled": True,
            "path": str(self.trace_paths[-1]) if self.trace_paths else None,
            "status": "recorded",
            "error": None,
        }

    def close(self):
        self.closed = True


class BrowserReplayTests(unittest.TestCase):
    def test_recorder_writes_manifest_script_and_readme(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = BrowserReplayRecorder(
                artifacts_dir=Path(temp_dir) / "run-1",
                run_id="run-1",
                base_url="http://localhost:9000",
                browser_input={"path": "/web", "auth": "none"},
                trace_path=(
                    Path(temp_dir)
                    / "run-1"
                    / "browser_replay"
                    / "original_trace.zip"
                ),
            )
            recorder.record_start(
                request={"command": "start", "request_id": "start-1"},
                result={"request_id": "start-1", "status": "pass"},
                base_url="http://localhost:9000",
                browser_input={"path": "/web", "auth": "none"},
            )

            manifest = json.loads(
                recorder.manifest_path.read_text(encoding="utf-8")
            )

            self.assertEqual(manifest["run_id"], "run-1")
            self.assertEqual(manifest["commands"][0]["command"], "start")
            self.assertTrue(recorder.script_path.is_file())
            self.assertTrue(recorder.readme_path.is_file())

    def test_replay_manifest_runs_replayable_actions_in_one_driver(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            replay_dir = Path(temp_dir) / "browser_replay"
            replay_dir.mkdir()
            manifest_path = replay_dir / "replay_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "kind": "web_client_session_browser_replay",
                        "run_id": "original-run",
                        "base_url": "http://original",
                        "browser_input": {"path": "/web", "auth": "none"},
                        "commands": [
                            {
                                "sequence": 1,
                                "command": "start",
                                "request_id": "start-1",
                                "replayable": False,
                                "skip_reason": "start",
                            },
                            {
                                "sequence": 2,
                                "command": "action",
                                "request_id": "goto-1",
                                "replayable": True,
                                "action_index": 1,
                                "step": {"step_id": 7},
                                "action": {"type": "goto"},
                                "browser_input": {"path": "/web"},
                            },
                            {
                                "sequence": 3,
                                "command": "action",
                                "request_id": "bad-1",
                                "replayable": False,
                                "schema_path": "$.action.selector",
                                "skip_reason": "schema error",
                            },
                            {
                                "sequence": 4,
                                "command": "action",
                                "request_id": "shot-1",
                                "replayable": True,
                                "action_index": 2,
                                "step": {"step_id": 7},
                                "action": {"type": "screenshot", "label": "home"},
                                "browser_input": {"label": "home"},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            instances = []

            def factory(**kwargs):
                driver = FakeReplayDriver(**kwargs)
                instances.append(driver)
                return driver

            summary = replay_manifest(
                manifest_path,
                base_url="http://override",
                headless=True,
                slow_mo_ms=25,
                output_dir=Path(temp_dir) / "browser_replay" / "replay-runs" / "run",
                browser_driver_factory=factory,
                printer=None,
            )

            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["action_count"], 2)
            self.assertEqual(summary["skipped_count"], 2)
            self.assertEqual(len(instances), 1)
            self.assertTrue(instances[0].closed)
            self.assertEqual(
                [call["action"]["type"] for call in instances[0].calls],
                ["goto", "screenshot"],
            )
            self.assertEqual(instances[0].calls[0]["step_id"], 7)
            self.assertEqual(instances[0].base_url, "http://override")
            self.assertEqual(
                instances[0].browser_configs,
                [{"headless": True, "slow_mo_ms": 25}],
            )
            self.assertTrue(
                Path(summary["output_dir"], "action_result_log.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
