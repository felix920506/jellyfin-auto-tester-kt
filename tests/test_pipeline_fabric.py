import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import (
    ANALYSIS_TRANSCRIPT_FILE,
    _build_parser,
    _build_stage_parser,
    _default_log_level,
    _default_log_stderr,
    _normalize_stage_argv,
    _parse_stage_choice,
    _receive_channel_message,
    apply_execution_turn_budget,
    execution_turn_budget,
    load_env_file,
    run_analysis_stage,
    run_execution_stage,
    run_issue,
    run_report_stage,
)


class FakeAnalysisAgent:
    def __init__(self, chunks):
        self.chunks = chunks
        self.prompts = []

    async def chat(self, prompt):
        self.prompts.append(prompt)
        for chunk in self.chunks:
            yield chunk


class FakeDefaultOutput:
    def __init__(self):
        self.streamed = []

    async def write_stream(self, chunk):
        self.streamed.append(chunk)


class FakeOutputRouter:
    def __init__(self):
        self.default_output = FakeDefaultOutput()


class FakeInnerAgent:
    def __init__(self):
        self.output_router = FakeOutputRouter()


class DefaultWritingAnalysisAgent:
    def __init__(self, chunks):
        self.chunks = chunks
        self.prompts = []
        self.agent = FakeInnerAgent()
        self.original_default_output = self.agent.output_router.default_output

    async def chat(self, prompt):
        self.prompts.append(prompt)
        for chunk in self.chunks:
            await self.agent.output_router.default_output.write_stream(chunk)
            yield chunk


class DefaultOnlyAnalysisAgent:
    def __init__(self, chunks):
        self.chunks = chunks
        self.prompts = []
        self.agent = FakeInnerAgent()

    async def chat(self, prompt):
        self.prompts.append(prompt)
        for chunk in self.chunks:
            await self.agent.output_router.default_output.write_stream(chunk)
        if False:
            yield ""


class ConversationOnlyAnalysisAgent:
    def __init__(self, chunks):
        self.chunks = chunks
        self.prompts = []
        self.agent = FakeInnerAgent()
        self.agent.controller = FakeController(FakeConversation())

    async def chat(self, prompt):
        self.prompts.append(prompt)
        for chunk in self.chunks:
            self.agent.controller.conversation.message = FakeMessage(chunk)
        if False:
            yield ""


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeConversation:
    def __init__(self):
        self.message = None

    def get_last_assistant_message(self):
        return self.message


class FakeController:
    def __init__(self, conversation):
        self.conversation = conversation


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
    def __init__(self, analysis_chunks, channels=None, analysis_agent=None):
        self.analysis_agent = analysis_agent or FakeAnalysisAgent(analysis_chunks)
        self.execution_agent = FakeExecutionAgent()
        self.channels = channels or {}

    def __getitem__(self, name):
        if name == "analysis_agent":
            return self.analysis_agent
        if name == "execution_agent":
            return self.execution_agent
        raise KeyError(name)


class FakeGraph:
    def __init__(self, graph_id):
        self.graph_id = graph_id


class FakeRegistry:
    def __init__(self, channels):
        self.channels = channels

    def get(self, channel_name):
        return self.channels.get(channel_name)


class FakeEnvironment:
    def __init__(self, channels):
        self.shared_channels = FakeRegistry(channels)


class FakeGraphEngine:
    def __init__(self, channels):
        self._environments = {"graph-1": FakeEnvironment(channels)}

    def list_graphs(self):
        return [FakeGraph("graph-1")]


class FakeOnSendChannel:
    def __init__(self):
        self.callbacks = []

    def on_send(self, callback):
        self.callbacks.append(callback)

    def remove_on_send(self, callback):
        self.callbacks.remove(callback)

    def send(self, message):
        for callback in list(self.callbacks):
            callback("final_report", {"content": message})


class FakeStage2Runner:
    def __init__(self, artifacts_root):
        self.artifacts_root = Path(artifacts_root)
        self.plans = []

    def execute_plan(self, plan, run_id=None):
        self.plans.append(plan)
        run_id = run_id or "debug-run"
        artifacts_dir = self.artifacts_root / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        return {
            "plan": plan,
            "run_id": run_id,
            "is_verification": bool(plan.get("is_verification", False)),
            "original_run_id": plan.get("original_run_id"),
            "container_id": "container-1",
            "execution_log": [_sample_execution_entry(plan)],
            "overall_result": "reproduced",
            "artifacts_dir": str(artifacts_dir),
            "jellyfin_logs": "server log\n",
            "error_summary": None,
        }


def _sample_plan():
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
                "role": "trigger",
                "action": "Call health endpoint",
                "tool": "http_request",
                "input": {"method": "GET", "path": "/health"},
                "expected_outcome": "The endpoint returns Healthy.",
                "success_criteria": {"all_of": [{"type": "status_code", "equals": 200}]},
            }
        ],
        "reproduction_goal": "Observe the reported behavior.",
        "failure_indicators": ["Health endpoint response"],
        "confidence": "high",
        "ambiguities": [],
        "is_verification": False,
        "original_run_id": None,
    }


def _sample_legacy_printed_plan():
    return {
        "issue_url": "https://github.com/jellyfin/jellyfin/issues/14267",
        "jellyfin_version": "10.11.8",
        "docker_image": "jellyfin/jellyfin:10.11.8",
        "reproduction_goal": "Confirm repository validation behavior.",
        "confidence": "high",
        "ambiguities": [],
        "environment": {
            "ports": {"8096": 8096},
            "volumes": {},
        },
        "reproduction_steps": [
            {
                "step": 1,
                "description": "Create the initial admin user.",
                "tool": "http_request",
                "input": {
                    "method": "POST",
                    "url": "http://localhost:8096/Startup/User",
                    "body": {"Name": "testadmin", "Password": "TestPassword1!"},
                },
                "success_criteria": {
                    "all_of": [
                        {"type": "status_code", "operator": "in", "value": [200, 204]},
                        {"type": "json_field", "path": "$.AccessToken", "operator": "exists"},
                    ]
                },
            },
            {
                "step": 2,
                "role": "trigger",
                "description": "POST /Repositories with an invalid manifest URL.",
                "tool": "http_request",
                "input": {
                    "method": "POST",
                    "url": "http://localhost:8096/Repositories",
                },
                "success_criteria": {
                    "all_of": [
                        {"type": "status_code", "operator": "eq", "value": 204}
                    ]
                },
            },
            {
                "step": 3,
                "description": "Search logs for the manifest error.",
                "tool": "docker_exec",
                "input": {"command": ["sh", "-c", "grep -rl manifest /config/log/"]},
                "success_criteria": {
                    "all_of": [
                        {"type": "exit_code", "operator": "eq", "value": 0}
                    ]
                },
            },
        ],
    }


def _sample_gemini_partial_plan():
    return {
        "reproduction_goal": "Confirm invalid plugin repository behavior.",
        "target_version": "10.11.8",
        "reproduction_steps": [
            {
                "name": "setup_admin",
                "description": "Create the initial admin user.",
                "tool": "http_request",
                "input": {
                    "method": "POST",
                    "url": "http://localhost:8096/Startup/User",
                },
            },
            {
                "name": "add_invalid_repo",
                "role": "trigger",
                "description": "Add an invalid plugin repository.",
                "tool": "http_request",
                "input": {
                    "method": "POST",
                    "url": "http://localhost:8096/Repositories",
                },
                "success_criteria": {
                    "all_of": [
                        {"type": "status_code", "value": 204},
                    ]
                },
            },
            {
                "name": "verify_repo_added",
                "role": "verification",
                "description": "Verify the invalid repository is listed.",
                "tool": "http_request",
                "input": {
                    "method": "GET",
                    "url": "http://localhost:8096/Repositories",
                },
                "success_criteria": {
                    "all_of": [
                        {"body_contains": "Invalid Bug Repo"},
                    ]
                },
            },
        ],
    }


def _sample_issue_thread():
    return {
        "title": "Debug issue",
        "body": "Steps to reproduce the debug issue.",
        "labels": ["bug"],
        "state": "open",
        "created_at": "2026-01-01T00:00:00Z",
        "author": "reporter",
        "comments": [
            {
                "author": "maintainer",
                "body": "Confirmed on the target version.",
                "created_at": "2026-01-02T00:00:00Z",
            }
        ],
        "linked_issues": [],
        "linked_prs": [],
    }


def _sample_issue_fetcher(**_kwargs):
    return _sample_issue_thread()


def _read_stage_transcript(path):
    return json.loads(
        (Path(path) / ANALYSIS_TRANSCRIPT_FILE).read_text(encoding="utf-8")
    )


def _sample_execution_entry(plan):
    step = plan["reproduction_steps"][0]
    return {
        "step_id": step["step_id"],
        "role": step["role"],
        "action": step["action"],
        "tool": step["tool"],
        "stdout": "",
        "stderr": "",
        "exit_code": None,
        "http": {"status_code": 200, "body": "Healthy", "headers": {}},
        "screenshot_path": None,
        "outcome": "pass",
        "reason": None,
        "criteria_evaluation": {
            "passed": True,
            "assertions": [
                {
                    "type": "status_code",
                    "passed": True,
                    "actual": 200,
                    "expected": 200,
                    "message": "status matched",
                }
            ],
        },
        "duration_ms": 10,
    }


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
            issue_fetcher=_sample_issue_fetcher,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.report_path, payload["report_path"])
        self.assertEqual(result.verification_run_id, "run-2")
        self.assertIn(
            "Issue: https://github.com/jellyfin/jellyfin/issues/1",
            engine.analysis_agent.prompts[0],
        )
        self.assertIn("Target version: 10.9.7", engine.analysis_agent.prompts[0])
        self.assertIn("Prefetched GitHub issue thread JSON", engine.analysis_agent.prompts[0])
        self.assertIn("Debug issue", engine.analysis_agent.prompts[0])
        self.assertIn(
            "Final report: /tmp/artifacts/run-1/report.md",
            stream.getvalue(),
        )

    async def test_run_issue_does_not_stream_analysis_output(self):
        payload = {"report_path": "/tmp/artifacts/run-1/report.md"}
        analysis_agent = DefaultWritingAnalysisAgent(
            ["analysis started\n", "REPRODUCTION_PLAN_COMPLETE\n"]
        )
        engine = FakeEngine(
            [],
            channels={"final_report": FakeChannel({"content": payload})},
            analysis_agent=analysis_agent,
        )
        stream = io.StringIO()

        await run_issue(
            "https://github.com/jellyfin/jellyfin/issues/1",
            "10.9.7",
            stream=stream,
            engine_factory=lambda recipe: engine,
            issue_fetcher=_sample_issue_fetcher,
        )

        self.assertEqual(analysis_agent.original_default_output.streamed, [])
        self.assertNotIn("analysis started", stream.getvalue())
        self.assertNotIn("REPRODUCTION_PLAN_COMPLETE", stream.getvalue())
        self.assertIn("Final report: /tmp/artifacts/run-1/report.md", stream.getvalue())

    async def test_run_issue_prefetches_issue_before_loading_engine(self):
        events = []
        payload = {"report_path": "/tmp/artifacts/run-1/report.md"}
        engine = FakeEngine(
            ["analysis started\n", "REPRODUCTION_PLAN_COMPLETE\n"],
            channels={"final_report": FakeChannel({"content": payload})},
        )

        def issue_fetcher(**_kwargs):
            events.append("issue_fetch")
            return _sample_issue_thread()

        def engine_factory(_recipe):
            events.append("engine_load")
            return engine

        await run_issue(
            "https://github.com/jellyfin/jellyfin/issues/1",
            "10.9.7",
            stream=None,
            engine_factory=engine_factory,
            issue_fetcher=issue_fetcher,
        )

        self.assertEqual(events[:2], ["issue_fetch", "engine_load"])

    async def test_run_issue_stops_on_insufficient_information(self):
        engine = FakeEngine(["INSUFFICIENT_INFORMATION\nmissing steps\n"])

        result = await run_issue(
            "https://github.com/jellyfin/jellyfin/issues/2",
            "10.9.7",
            stream=None,
            engine_factory=lambda recipe: engine,
            issue_fetcher=_sample_issue_fetcher,
        )

        self.assertEqual(result.status, "insufficient_information")
        self.assertIn("missing steps", result.message)

    async def test_channel_listener_observes_kohaku_graph_environment_channel(self):
        channel = FakeOnSendChannel()
        engine = FakeGraphEngine({"final_report": channel})
        payload = {"report_path": "/tmp/artifacts/run-1/report.md"}

        listener = asyncio.create_task(_receive_channel_message(engine, "final_report"))
        await asyncio.sleep(0)
        channel.send(payload)

        channel_name, message = await asyncio.wait_for(listener, timeout=1)

        self.assertEqual(channel_name, "final_report")
        self.assertEqual(message, payload)
        self.assertEqual(channel.callbacks, [])

    async def test_run_analysis_stage_writes_plan_handoff_folder(self):
        plan = _sample_plan()
        engine = FakeEngine(
            ["analysis started\n", "REPRODUCTION_PLAN_COMPLETE\n"],
            channels={"plan_ready": FakeChannel({"content": plan})},
        )
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = await run_analysis_stage(
                "https://github.com/jellyfin/jellyfin/issues/1",
                "10.9.7",
                temp_dir,
                stream=stream,
                engine_factory=lambda stage: engine,
                issue_fetcher=_sample_issue_fetcher,
            )

            self.assertEqual(result.status, "plan_ready")
            self.assertEqual(result.output_file, "plan.json")
            self.assertEqual(json.loads((Path(temp_dir) / "plan.json").read_text()), plan)
            input_payload = json.loads((Path(temp_dir) / "input.json").read_text())
            self.assertEqual(input_payload["prefetched_issue_thread"]["title"], "Debug issue")
            transcript_payload = _read_stage_transcript(temp_dir)
            self.assertEqual(transcript_payload["schema_version"], 1)
            self.assertEqual(transcript_payload["stage"], "analysis")
            self.assertIn(
                "Issue: https://github.com/jellyfin/jellyfin/issues/1",
                transcript_payload["input"]["prompt"],
            )
            self.assertIn("system_prompt", transcript_payload["input"])
            self.assertEqual(transcript_payload["messages"][0]["role"], "user")
            self.assertEqual(transcript_payload["messages"][1]["role"], "assistant")
            self.assertIn("analysis started", transcript_payload["output"]["assistant"])
            self.assertEqual(
                transcript_payload["input"]["prefetched_issue_thread"]["title"],
                "Debug issue",
            )
            self.assertEqual(stream.getvalue(), "")

    async def test_run_analysis_stage_extracts_printed_plan_without_channel(self):
        plan = _sample_plan()
        transcript = (
            "analysis started\n"
            "```json\n"
            f"{json.dumps(plan)}\n"
            "```\n"
            "REPRODUCTION_PLAN_COMPLETE\n"
        )
        engine = FakeEngine([transcript])
        with tempfile.TemporaryDirectory() as temp_dir:
            result = await asyncio.wait_for(
                run_analysis_stage(
                    "https://github.com/jellyfin/jellyfin/issues/1",
                    "10.9.7",
                    temp_dir,
                    timeout_s=60,
                    stream=None,
                    engine_factory=lambda stage: engine,
                    issue_fetcher=_sample_issue_fetcher,
                ),
                timeout=1,
            )

            self.assertEqual(result.status, "plan_ready")
            self.assertEqual(result.metadata["source"], "transcript")
            self.assertEqual(result.metadata["transcript"], ANALYSIS_TRANSCRIPT_FILE)
            self.assertEqual(json.loads((Path(temp_dir) / "plan.json").read_text()), plan)

    async def test_run_analysis_stage_captures_default_output_plan(self):
        plan = _sample_legacy_printed_plan()
        transcript = (
            "Now I have enough context to write the reproduction plan.\n"
            "```json\n"
            f"{json.dumps(plan)}\n"
            "```\n"
            "`REPRODUCTION_PLAN_COMPLETE`"
        )
        analysis_agent = DefaultOnlyAnalysisAgent([transcript])
        engine = FakeEngine([], analysis_agent=analysis_agent)

        with tempfile.TemporaryDirectory() as temp_dir:
            result = await asyncio.wait_for(
                run_analysis_stage(
                    "https://github.com/jellyfin/jellyfin/issues/14267",
                    "10.11.8",
                    temp_dir,
                    timeout_s=60,
                    stream=None,
                    engine_factory=lambda stage: engine,
                    issue_fetcher=_sample_issue_fetcher,
                ),
                timeout=1,
            )

            written_plan = json.loads((Path(temp_dir) / "plan.json").read_text())
            written_transcript = _read_stage_transcript(temp_dir)

            self.assertEqual(result.status, "plan_ready")
            self.assertEqual(result.metadata["source"], "transcript")
            self.assertIn("jellyfin_version", written_transcript["output"]["assistant"])
            self.assertEqual(written_transcript["messages"][0]["role"], "user")
            self.assertEqual(written_transcript["messages"][1]["role"], "assistant")
            self.assertEqual(
                written_transcript["messages"][1]["content"],
                written_transcript["output"]["assistant"],
            )
            self.assertEqual(written_plan["target_version"], "10.11.8")
            self.assertEqual(written_plan["issue_title"], "Debug issue")
            self.assertFalse(written_plan["is_verification"])
            self.assertIsNone(written_plan["original_run_id"])
            self.assertEqual(
                written_plan["environment"]["ports"],
                {"host": 8096, "container": 8096},
            )
            self.assertEqual(written_plan["environment"]["volumes"], [])
            self.assertEqual(written_plan["reproduction_steps"][0]["step_id"], 1)
            self.assertEqual(written_plan["reproduction_steps"][0]["role"], "setup")
            self.assertEqual(
                written_plan["reproduction_steps"][0]["input"]["path"],
                "/Startup/User",
            )
            self.assertEqual(
                written_plan["reproduction_steps"][0]["success_criteria"]["all_of"][0],
                {"type": "status_code", "in": [200, 204]},
            )
            self.assertEqual(
                written_plan["reproduction_steps"][0]["success_criteria"]["all_of"][1],
                {"type": "body_matches", "pattern": r'"AccessToken"\s*:'},
            )
            self.assertEqual(written_plan["reproduction_steps"][2]["role"], "verify")
            self.assertEqual(
                written_plan["reproduction_steps"][2]["input"]["command"],
                "sh -c 'grep -rl manifest /config/log/'",
            )

    async def test_run_analysis_stage_recovers_plan_from_conversation(self):
        plan = _sample_legacy_printed_plan()
        transcript = (
            "Now I have enough context to write the reproduction plan.\n"
            "```json\n"
            f"{json.dumps(plan)}\n"
            "```\n"
            "REPRODUCTION_PLAN_COMPLETE\n"
        )
        analysis_agent = ConversationOnlyAnalysisAgent([transcript])
        engine = FakeEngine([], analysis_agent=analysis_agent)
        stream = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            result = await asyncio.wait_for(
                run_analysis_stage(
                    "https://github.com/jellyfin/jellyfin/issues/14267",
                    "10.11.8",
                    temp_dir,
                    timeout_s=60,
                    stream=stream,
                    engine_factory=lambda stage: engine,
                    issue_fetcher=_sample_issue_fetcher,
                ),
                timeout=1,
            )

            written_plan = json.loads((Path(temp_dir) / "plan.json").read_text())
            written_transcript = _read_stage_transcript(temp_dir)

            self.assertEqual(result.status, "plan_ready")
            self.assertEqual(stream.getvalue(), "")
            self.assertIn("jellyfin_version", written_transcript["output"]["assistant"])
            self.assertEqual(written_transcript["output"]["sources"][0]["source"], "conversation")
            self.assertEqual(written_plan["target_version"], "10.11.8")

    async def test_run_analysis_stage_recovers_gemini_same_turn_partial_plan(self):
        partial_plan = _sample_gemini_partial_plan()
        transcript = (
            "[/web_search]\n"
            "@@queries=[\"Jellyfin plugin repositories\"]\n"
            "[web_search/]"
            "[/send_message]\n"
            "@@channel=plan_ready\n"
            f"{json.dumps(partial_plan)}\n"
            "[send_message/]"
            "REPRODUCTION_PLAN_COMPLETE"
            "[web_fetch]\n"
            "@@url=\"https://api.jellyfin.org/#tag/Plugins\"\n"
            "[web_fetch/]"
            "REPRODUCTION_PLAN_COMPLETE"
        )
        engine = FakeEngine([transcript])

        with tempfile.TemporaryDirectory() as temp_dir:
            result = await asyncio.wait_for(
                run_analysis_stage(
                    "https://github.com/jellyfin/jellyfin/issues/14267",
                    "10.11.8",
                    temp_dir,
                    timeout_s=0.01,
                    stream=None,
                    engine_factory=lambda stage: engine,
                    issue_fetcher=_sample_issue_fetcher,
                ),
                timeout=1,
            )

            written_plan = json.loads((Path(temp_dir) / "plan.json").read_text())

        self.assertEqual(result.status, "plan_ready")
        self.assertEqual(result.metadata["source"], "transcript")
        self.assertEqual(
            written_plan["issue_url"],
            "https://github.com/jellyfin/jellyfin/issues/14267",
        )
        self.assertEqual(written_plan["docker_image"], "jellyfin/jellyfin:10.11.8")
        self.assertEqual(written_plan["issue_title"], "Debug issue")
        self.assertEqual(written_plan["reproduction_steps"][0]["role"], "setup")
        self.assertEqual(
            written_plan["reproduction_steps"][0]["success_criteria"],
            {"all_of": [{"type": "status_code", "in": [200, 204]}]},
        )
        self.assertEqual(
            written_plan["reproduction_steps"][1]["success_criteria"],
            {"all_of": [{"type": "status_code", "equals": 204}]},
        )
        self.assertEqual(written_plan["reproduction_steps"][2]["role"], "verify")
        self.assertEqual(
            written_plan["reproduction_steps"][2]["success_criteria"],
            {"all_of": [{"type": "body_contains", "value": "Invalid Bug Repo"}]},
        )

    async def test_run_analysis_stage_returns_no_plan_instead_of_hanging(self):
        engine = FakeEngine(["analysis started\nREPRODUCTION_PLAN_COMPLETE\n"])
        with tempfile.TemporaryDirectory() as temp_dir:
            result = await asyncio.wait_for(
                run_analysis_stage(
                    "https://github.com/jellyfin/jellyfin/issues/1",
                    "10.9.7",
                    temp_dir,
                    timeout_s=0.01,
                    stream=None,
                    engine_factory=lambda stage: engine,
                    issue_fetcher=_sample_issue_fetcher,
                ),
                timeout=1,
            )

            self.assertEqual(result.status, "no_plan")
            self.assertEqual(result.output_file, ANALYSIS_TRANSCRIPT_FILE)
            self.assertFalse((Path(temp_dir) / "plan.json").exists())

    def test_run_execution_stage_reads_plan_and_writes_result_handoff(self):
        plan = _sample_plan()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "analysis"
            input_dir.mkdir()
            (input_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

            result = run_execution_stage(
                input_dir,
                temp_path / "execution",
                run_id="run-1",
                runner_factory=lambda artifacts_root: FakeStage2Runner(artifacts_root),
            )

            result_path = temp_path / "execution" / "result.json"
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result.status, "reproduced")
            self.assertEqual(result.output_file, "result.json")
            self.assertEqual(payload["run_id"], "run-1")
            self.assertEqual(payload["plan"], plan)

    def test_run_report_stage_writes_report_and_verification_plan(self):
        plan = _sample_plan()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            execution_dir = temp_path / "execution"
            artifacts_dir = execution_dir / "run-1"
            artifacts_dir.mkdir(parents=True)
            execution_result = {
                "plan": plan,
                "run_id": "run-1",
                "is_verification": False,
                "original_run_id": None,
                "container_id": "container-1",
                "execution_log": [_sample_execution_entry(plan)],
                "overall_result": "reproduced",
                "artifacts_dir": str(artifacts_dir),
                "jellyfin_logs": "server log\n",
                "error_summary": None,
            }
            (execution_dir / "result.json").write_text(
                json.dumps(execution_result),
                encoding="utf-8",
            )

            result = run_report_stage(execution_dir, temp_path / "report")

            verification_plan = json.loads(
                (temp_path / "report" / "verification_plan.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(result.status, "verification_ready")
            self.assertTrue((temp_path / "report" / "report.md").is_file())
            self.assertTrue(verification_plan["is_verification"])
            self.assertEqual(verification_plan["original_run_id"], "run-1")

    def test_execution_turn_budget_matches_master_plan(self):
        self.assertEqual(execution_turn_budget(0), (60, 70))
        self.assertEqual(execution_turn_budget(20), (96, 106))

    def test_stage_mode_is_selected_by_flag_only(self):
        self.assertIsNone(_normalize_stage_argv(["stage", "analysis"]))
        self.assertEqual(
            _normalize_stage_argv(["--stage", "analysis"]),
            ["--stage", "analysis"],
        )
        self.assertEqual(
            _normalize_stage_argv(["--stage=execution"]),
            ["--stage=execution"],
        )
        self.assertEqual(
            _parse_stage_choice(["--stage=report", "--input", "x", "--out", "y"]),
            "report",
        )

    def test_cli_parsers_accept_logging_controls(self):
        parser = _build_parser()
        args = parser.parse_args(
            [
                "--log-level",
                "debug",
                "--log-stderr",
                "off",
                "https://github.com/jellyfin/jellyfin/issues/1",
                "10.9.7",
            ]
        )

        self.assertEqual(args.log_level, "DEBUG")
        self.assertEqual(args.log_stderr, "off")

        stage_args = _build_stage_parser("analysis").parse_args(
            [
                "--stage",
                "analysis",
                "--log-level",
                "WARNING",
                "--log-stderr",
                "on",
                "https://github.com/jellyfin/jellyfin/issues/1",
                "10.9.7",
                "--out",
                "debug/stage1",
            ]
        )

        self.assertEqual(stage_args.log_level, "WARNING")
        self.assertEqual(stage_args.log_stderr, "on")

    def test_logging_defaults_read_environment(self):
        with patch.dict(
            os.environ,
            {
                "JF_AUTO_TESTER_LOG_LEVEL": "debug",
                "JF_AUTO_TESTER_LOG_STDERR": "false",
            },
            clear=True,
        ):
            self.assertEqual(_default_log_level(), "DEBUG")
            self.assertEqual(_default_log_stderr(), "off")

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

    def test_load_env_file_ignores_blank_provider_auth_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text(
                "OPENROUTER_API_KEY=\n"
                "ANTHROPIC_API_KEY=\n"
                "JF_AUTO_TESTER_BROWSER_HEADLESS=false\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                self.assertTrue(load_env_file(dotenv_path))

                self.assertNotIn("OPENROUTER_API_KEY", os.environ)
                self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
                self.assertEqual(os.environ["JF_AUTO_TESTER_BROWSER_HEADLESS"], "false")

    def test_load_env_file_loads_non_empty_provider_auth_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text("OPENROUTER_API_KEY=sk-or-test\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                self.assertTrue(load_env_file(dotenv_path))

                self.assertEqual(os.environ["OPENROUTER_API_KEY"], "sk-or-test")

    def test_load_env_file_missing_file_is_noop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {}, clear=True):
                self.assertFalse(load_env_file(Path(temp_dir) / ".env"))
                self.assertEqual(dict(os.environ), {})


if __name__ == "__main__":
    unittest.main()
