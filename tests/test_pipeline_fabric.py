import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import (
    apply_execution_turn_budget,
    execution_turn_budget,
    load_env_file,
    run_issue,
)


class FakeAnalysisAgent:
    def __init__(self, chunks):
        self.chunks = chunks
        self.prompts = []

    async def chat(self, prompt):
        self.prompts.append(prompt)
        for chunk in self.chunks:
            yield chunk


class FakeExecutionAgent:
    def __init__(self):
        self.max_iterations = 1
        self.termination = {"max_turns": 1}


class FakeChannel:
    def __init__(self, message):
        self.message = message

    async def receive(self):
        return self.message


class FakeEngine:
    def __init__(self, analysis_chunks, channels=None):
        self.analysis_agent = FakeAnalysisAgent(analysis_chunks)
        self.execution_agent = FakeExecutionAgent()
        self.channels = channels or {}

    def __getitem__(self, name):
        if name == "analysis_agent":
            return self.analysis_agent
        if name == "execution_agent":
            return self.execution_agent
        raise KeyError(name)


class PipelineFabricTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_issue_returns_final_report_payload(self):
        payload = {
            "report_path": "/tmp/artifacts/run-1/report.md",
            "run_id": "run-1",
            "verification_run_id": "run-2",
            "overall_result": "reproduced",
            "verified": True,
            "issue_url": "https://github.com/jellyfin/jellyfin/issues/1",
        }
        engine = FakeEngine(
            ["analysis started\n", "REPRODUCTION_PLAN_COMPLETE\n"],
            channels={"final_report": FakeChannel({"content": payload})},
        )
        stream = io.StringIO()

        result = await run_issue(
            "https://github.com/jellyfin/jellyfin/issues/1",
            "10.9.7",
            stream=stream,
            engine_factory=lambda recipe: engine,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.report_path, payload["report_path"])
        self.assertEqual(result.verification_run_id, "run-2")
        self.assertIn(
            "Issue: https://github.com/jellyfin/jellyfin/issues/1",
            engine.analysis_agent.prompts[0],
        )
        self.assertIn("Target version: 10.9.7", engine.analysis_agent.prompts[0])
        self.assertIn(
            "Final report: /tmp/artifacts/run-1/report.md",
            stream.getvalue(),
        )

    async def test_run_issue_stops_on_insufficient_information(self):
        engine = FakeEngine(["INSUFFICIENT_INFORMATION\nmissing steps\n"])

        result = await run_issue(
            "https://github.com/jellyfin/jellyfin/issues/2",
            "10.9.7",
            stream=None,
            engine_factory=lambda recipe: engine,
        )

        self.assertEqual(result.status, "insufficient_information")
        self.assertIn("missing steps", result.message)

    def test_execution_turn_budget_matches_master_plan(self):
        self.assertEqual(execution_turn_budget(0), (60, 70))
        self.assertEqual(execution_turn_budget(20), (96, 106))

    def test_apply_execution_turn_budget_updates_execution_agent(self):
        engine = FakeEngine([])
        plan = {"reproduction_steps": [{} for _ in range(20)]}

        budget = apply_execution_turn_budget(engine, plan)

        self.assertEqual(budget, (96, 106))
        self.assertEqual(engine.execution_agent.max_iterations, 96)
        self.assertEqual(engine.execution_agent.termination["max_turns"], 106)

    def test_load_env_file_loads_values_without_overriding_existing_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text(
                "GITHUB_TOKEN=from-file\n"
                "JF_AUTO_TESTER_BROWSER_HEADLESS=true\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"GITHUB_TOKEN": "from-env"}, clear=True):
                self.assertTrue(load_env_file(dotenv_path))

                self.assertEqual(os.environ["GITHUB_TOKEN"], "from-env")
                self.assertEqual(os.environ["JF_AUTO_TESTER_BROWSER_HEADLESS"], "true")

    def test_load_env_file_missing_file_is_noop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {}, clear=True):
                self.assertFalse(load_env_file(Path(temp_dir) / ".env"))
                self.assertEqual(dict(os.environ), {})


if __name__ == "__main__":
    unittest.main()
