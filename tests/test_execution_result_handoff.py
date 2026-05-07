import json
import tempfile
import unittest
from pathlib import Path

from tools.execution_result_handoff import (
    compact_execution_result,
    hydrate_execution_result,
)


class ExecutionResultHandoffTests(unittest.TestCase):
    def test_compact_execution_result_removes_plan_and_adds_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_dir = Path(temp_dir) / "run-1"
            artifacts_dir.mkdir()
            (artifacts_dir / "plan.json").write_text("{}", encoding="utf-8")
            result = _execution_result(artifacts_dir)

            compact = compact_execution_result(result)

            self.assertNotIn("plan", compact)
            self.assertEqual(compact["run_id"], "run-1")
            self.assertEqual(compact["result_path"], str(artifacts_dir / "result.json"))
            self.assertEqual(compact["plan_json_path"], str(artifacts_dir / "plan.json"))

    def test_hydrate_execution_result_loads_full_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_dir = Path(temp_dir) / "run-1"
            artifacts_dir.mkdir()
            result = _execution_result(artifacts_dir)
            result_path = artifacts_dir / "result.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            compact = compact_execution_result(result)

            hydrated = hydrate_execution_result(compact)

            self.assertEqual(hydrated["plan"], result["plan"])
            self.assertEqual(hydrated["overall_result"], "reproduced")

    def test_hydrate_execution_result_uses_fallback_plan(self):
        plan = {"reproduction_steps": []}
        payload = {
            "run_id": "run-1",
            "execution_log": [],
            "overall_result": "inconclusive",
            "artifacts_dir": "/missing/run-1",
        }

        hydrated = hydrate_execution_result(payload, fallback_plan=plan)

        self.assertEqual(hydrated["plan"], plan)


def _execution_result(artifacts_dir):
    return {
        "plan": {"reproduction_steps": []},
        "run_id": "run-1",
        "is_verification": False,
        "original_run_id": None,
        "container_id": None,
        "execution_log": [],
        "overall_result": "reproduced",
        "artifacts_dir": str(artifacts_dir),
        "jellyfin_logs": "",
        "error_summary": None,
    }


if __name__ == "__main__":
    unittest.main()
