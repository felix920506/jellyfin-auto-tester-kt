import asyncio
import io
import json
import logging
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from main import (
    ANALYSIS_TRANSCRIPT_FILE,
    INSUFFICIENT_INFORMATION_SUMMARY_FILE,
    AnalysisAgentEmptyResponseError,
    AnalysisAgentProtocolError,
    BlockedStage1ModelError,
    LOGGER_NAME,
    PipelineTimeoutError,
    _build_parser,
    _build_stage_parser,
    _assert_stage1_model_allowed_for_recipe,
    _assert_stage1_model_config_allowed,
    _analysis_plan_retry_prompt,
    _default_log_level,
    _default_log_stderr,
    _normalize_stage_argv,
    _parse_stage_choice,
    _stage1_config_path_from_recipe,
    _stage1_model_identifier_from_config,
    _receive_channel_message,
    apply_execution_turn_budget,
    configure_runtime_logging,
    execution_turn_budget,
    load_env_file,
    run_analysis_stage,
    run_execution_stage,
    run_web_client_stage,
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

    def notify_activity(self, _activity_type, *_args, **_kwargs):
        pass


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


class CrashingAnalysisAgent:
    def __init__(self, chunks):
        self.chunks = chunks
        self.prompts = []

    async def chat(self, prompt):
        self.prompts.append(prompt)
        for chunk in self.chunks:
            yield chunk
        raise RuntimeError("provider stream crashed")


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


class RetryThenPlanReadyAnalysisAgent:
    def __init__(self, plan_channel, plan):
        self.plan_channel = plan_channel
        self.plan = plan
        self.prompts = []
        self.cancelled = False

    async def chat(self, prompt):
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            yield "The reproduction plan has been sent to `plan_ready`.\n"
            return

        self.plan_channel.send(json.dumps(self.plan))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if False:
            yield ""


class SendMessagePlanReadyAnalysisAgent:
    def __init__(self, plan_channel, plan):
        self.plan_channel = plan_channel
        self.plan = plan
        self.prompts = []
        self.cancelled = False

    async def chat(self, prompt):
        self.prompts.append(prompt)
        self.plan_channel.send(json.dumps(self.plan))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
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


class FakeMessageListConversation:
    def __init__(self):
        self.messages = []

    def append(self, role, content, **kwargs):
        message = {"role": role, "content": content, **kwargs}
        self.messages.append(message)
        return FakeMessage(content)

    def to_messages(self):
        return [dict(message) for message in self.messages]

    def get_last_assistant_message(self):
        for message in reversed(self.messages):
            if message["role"] == "assistant":
                return FakeMessage(message["content"])
        return None

    def get_system_message(self):
        for message in self.messages:
            if message["role"] == "system":
                return FakeMessage(message["content"])
        return None


class FakeController:
    def __init__(self, conversation):
        self.conversation = conversation


class FakeProviderLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, messages, **_kwargs):
        self.calls.append([dict(message) for message in messages])
        response = self.responses.pop(0)
        for chunk in response:
            yield chunk


class ProviderConversationAnalysisAgent:
    def __init__(self, plan_channel, plan):
        self.prompts = []
        self.plan_channel = plan_channel
        self.plan = plan
        self.agent = FakeInnerAgent()
        conversation = FakeMessageListConversation()
        self.agent.controller = FakeController(conversation)
        self.agent.llm = FakeProviderLLM(
            [
                ["need tool\n"],
                ["plan ready\n", "REPRODUCTION_PLAN_COMPLETE\n"],
            ]
        )

    async def chat(self, prompt):
        self.prompts.append(prompt)
        conversation = self.agent.controller.conversation

        conversation.append("user", prompt)
        first_parts = []
        async for chunk in self.agent.llm.chat(conversation.to_messages(), stream=True):
            first_parts.append(chunk)
            yield chunk
        conversation.append("assistant", "".join(first_parts))

        conversation.append("user", "[Tool completed]\nresult")
        second_parts = []
        async for chunk in self.agent.llm.chat(conversation.to_messages(), stream=True):
            second_parts.append(chunk)
            yield chunk
        conversation.append("assistant", "".join(second_parts))
        self.plan_channel.send(self.plan)


class FakeExecutionAgent:
    def __init__(self):
        self.max_iterations = 1
        self.termination = {"max_turns": 1}


class FakeChannel:
    def __init__(self, message):
        self.message = message

    async def receive(self):
        return self.message


class FakeAsyncChannel:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.history = []
        self.callbacks = []

    async def send(self, message):
        self.history.append(message)
        for callback in list(self.callbacks):
            callback(getattr(message, "channel", None) or "", message)
        await self.queue.put(message)

    async def receive(self):
        return await self.queue.get()

    def on_send(self, callback):
        self.callbacks.append(callback)

    def remove_on_send(self, callback):
        self.callbacks.remove(callback)


class FakeStage2AgentEngine:
    def __init__(
        self,
        plan_channels=("plan_ready", "verification_request"),
        *,
        agent_sender="execution_agent",
        default_run_id="agent-run",
        fail_immediately=False,
        hang=False,
    ):
        self.plan_channels = tuple(plan_channels)
        self.agent_sender = agent_sender
        self.default_run_id = default_run_id
        self.fail_immediately = fail_immediately
        self.hang = hang
        self.execution_agent = FakeExecutionAgent()
        self.channels = {
            channel: FakeAsyncChannel()
            for channel in (*self.plan_channels, "execution_done")
        }
        self.received_channel = None
        self.received_payload = None
        self.run_thread = None
        self.stopped = False

    def __getitem__(self, name):
        if name == "execution_agent":
            return self.execution_agent
        raise KeyError(name)

    async def run(self):
        self.run_thread = threading.get_ident()
        if self.fail_immediately:
            raise RuntimeError("stage engine failed")
        if self.hang:
            await asyncio.Event().wait()
        pending = {
            asyncio.create_task(self.channels[channel].receive()): channel
            for channel in self.plan_channels
        }
        done, pending_tasks = await asyncio.wait(
            set(pending),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending_tasks:
            task.cancel()
        message_task = next(iter(done))
        self.received_channel = pending[message_task]
        message = message_task.result()
        self.received_payload = json.loads(message.content)
        plan = self.received_payload.get("plan", self.received_payload)
        run_id = self.received_payload.get("run_id") or self.default_run_id
        artifacts_root = self.received_payload.get("artifacts_root")
        result = {
            "plan": plan,
            "run_id": run_id,
            "is_verification": bool(plan.get("is_verification", False)),
            "original_run_id": plan.get("original_run_id"),
            "container_id": None,
            "execution_log": [_sample_execution_entry(plan)],
            "overall_result": "reproduced",
            "artifacts_dir": (
                str(Path(artifacts_root) / run_id)
                if artifacts_root
                else "agent-artifacts"
            ),
            "jellyfin_logs": "",
            "error_summary": None,
        }
        await self.channels["execution_done"].send(
            types.SimpleNamespace(
                sender=self.agent_sender,
                content=json.dumps(result),
                metadata={},
                message_id="execution-done",
                reply_to=None,
                channel="execution_done",
            )
        )

    async def stop(self):
        self.stopped = True


class FakeWebClientStageEngine(FakeStage2AgentEngine):
    def __init__(self):
        super().__init__(
            ("web_client_plan_ready", "web_client_verification_request"),
            agent_sender="web_client_agent",
            default_run_id="web-agent-run",
        )


class HangingChannel:
    async def receive(self):
        await asyncio.Event().wait()


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
    def __init__(self, channel_name="final_report"):
        self.channel_name = channel_name
        self.callbacks = []

    def on_send(self, callback):
        self.callbacks.append(callback)

    def remove_on_send(self, callback):
        self.callbacks.remove(callback)

    def send(self, message):
        if hasattr(message, "content"):
            payload = message
        else:
            payload = {"content": message}
        for callback in list(self.callbacks):
            callback(self.channel_name, payload)


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
                "input": {"method": "GET", "path": "/health", "auth": "none"},
                "expected_outcome": "The endpoint returns Healthy.",
                "success_criteria": {"all_of": [{"type": "status_code", "equals": 200}]},
            }
        ],
        "reproduction_goal": "Observe the reported behavior.",
        "failure_indicators": ["Health endpoint response"],
        "execution_target": "standard",
        "confidence": "high",
        "ambiguities": [],
        "is_verification": False,
        "original_run_id": None,
    }


def _sample_web_client_plan():
    plan = _sample_plan()
    plan["execution_target"] = "web_client"
    plan["reproduction_steps"] = [
        {
            "step_id": 1,
            "role": "trigger",
            "action": "Open Jellyfin Web",
            "tool": "browser",
            "input": {
                "path": "/web",
                "auth": "auto",
                "label": "web_home",
                "actions": [
                    {"type": "goto"},
                    {"type": "screenshot", "label": "web_home"},
                ],
            },
            "expected_outcome": "The Web UI is visible.",
            "success_criteria": {
                "all_of": [{"type": "browser_action_run"}]
            },
        }
    ]
    return plan


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
                    "auth": "none",
                    "body_json": {"Name": "testadmin", "Password": "TestPassword1!"},
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
                    "auth": "auto",
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
                    "auth": "none",
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
                    "auth": "auto",
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
                    "auth": "auto",
                },
                "success_criteria": {
                    "all_of": [
                        {"body_contains": "Invalid Bug Repo"},
                    ]
                },
            },
        ],
    }


def _sample_provider_plan_without_target_version():
    return {
        "reproduction_goal": "Confirm Dolby Vision playback profile behavior.",
        "docker_image": "jellyfin/jellyfin:10.11.8",
        "reproduction_steps": [
            {
                "name": "prepare_media",
                "tool": "bash",
                "input": "echo ok",
                "success_criteria": {
                    "all_of": [{"type": "bash_exit_code", "value": 0}]
                },
            },
            {
                "name": "check_playback_info",
                "role": "trigger",
                "tool": "http_request",
                "input": {
                    "method": "GET",
                    "url": "http://localhost:8096/Items",
                    "auth": "auto",
                    "query": {"Recursive": True},
                },
                "capture": {"item_id": "Items[0].Id"},
                "success_criteria": {
                    "all_of": [
                        {
                            "type": "json_match",
                            "path": "Items[0].Name",
                            "value": "Beautiful Planet",
                        }
                    ]
                },
            },
        ],
        "confidence": "high",
        "ambiguities": [],
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
    def test_analysis_plan_retry_prompt_uses_parseable_send_message_block(self):
        prompt = _analysis_plan_retry_prompt(2, 3)

        self.assertIn("[/send_message]", prompt)
        self.assertIn("@@channel=plan_ready", prompt)
        self.assertIn("[send_message/]", prompt)
        self.assertIn("not `[/send_message]`", prompt)
        self.assertNotIn('send_message(channel="plan_ready"', prompt)
        self.assertNotIn("output_plan_ready", prompt)

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

    async def test_run_issue_rejects_blacklisted_stage1_model_before_prefetch(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            analysis_dir = root / "creatures" / "analysis"
            analysis_dir.mkdir(parents=True)
            (analysis_dir / "config.yaml").write_text(
                "name: analysis_agent\n"
                "controller:\n"
                "  llm: openrouter/gemini-3.1-pro\n",
                encoding="utf-8",
            )
            recipe_path = root / "terrarium.yaml"
            recipe_path.write_text(
                "version: '1.0'\n"
                "creatures:\n"
                "  - name: analysis_agent\n"
                "    base_config: creatures/analysis\n",
                encoding="utf-8",
            )

            def issue_fetcher(**_kwargs):
                calls.append("issue_fetch")
                return _sample_issue_thread()

            with self.assertRaises(BlockedStage1ModelError):
                await run_issue(
                    "https://github.com/jellyfin/jellyfin/issues/1",
                    "10.9.7",
                    recipe_path=recipe_path,
                    stream=None,
                    issue_fetcher=issue_fetcher,
                )

        self.assertEqual(calls, [])

    async def test_run_issue_treats_plan_ready_as_analysis_completion(self):
        plan_channel = FakeOnSendChannel("plan_ready")
        payload = {"report_path": "/tmp/artifacts/run-1/report.md"}
        analysis_agent = SendMessagePlanReadyAnalysisAgent(
            plan_channel,
            _sample_plan(),
        )
        engine = FakeEngine(
            [],
            channels={
                "plan_ready": plan_channel,
                "final_report": FakeChannel({"content": payload}),
            },
            analysis_agent=analysis_agent,
        )

        result = await asyncio.wait_for(
            run_issue(
                "https://github.com/jellyfin/jellyfin/issues/1",
                "10.9.7",
                stream=None,
                engine_factory=lambda recipe: engine,
                issue_fetcher=_sample_issue_fetcher,
            ),
            timeout=1,
        )

        self.assertEqual(result.status, "complete")
        self.assertTrue(analysis_agent.cancelled)

    async def test_run_issue_treats_web_client_plan_ready_as_analysis_completion(self):
        plan_channel = FakeOnSendChannel("web_client_plan_ready")
        payload = {"report_path": "/tmp/artifacts/run-1/report.md"}
        analysis_agent = SendMessagePlanReadyAnalysisAgent(
            plan_channel,
            _sample_web_client_plan(),
        )
        engine = FakeEngine(
            [],
            channels={
                "web_client_plan_ready": plan_channel,
                "final_report": FakeChannel({"content": payload}),
            },
            analysis_agent=analysis_agent,
        )

        result = await asyncio.wait_for(
            run_issue(
                "https://github.com/jellyfin/jellyfin/issues/1",
                "10.9.7",
                stream=None,
                engine_factory=lambda recipe: engine,
                issue_fetcher=_sample_issue_fetcher,
            ),
            timeout=1,
        )

        self.assertEqual(result.status, "complete")
        self.assertTrue(analysis_agent.cancelled)

    async def test_run_issue_retries_after_hallucinated_plan_ready(self):
        plan_channel = FakeOnSendChannel("plan_ready")
        payload = {"report_path": "/tmp/artifacts/run-1/report.md"}
        analysis_agent = RetryThenPlanReadyAnalysisAgent(
            plan_channel,
            _sample_plan(),
        )
        engine = FakeEngine(
            [],
            channels={
                "plan_ready": plan_channel,
                "final_report": FakeChannel({"content": payload}),
            },
            analysis_agent=analysis_agent,
        )

        result = await asyncio.wait_for(
            run_issue(
                "https://github.com/jellyfin/jellyfin/issues/1",
                "10.9.7",
                stream=None,
                engine_factory=lambda recipe: engine,
                issue_fetcher=_sample_issue_fetcher,
            ),
            timeout=1,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(analysis_agent.prompts), 2)
        self.assertIn("REJECTED", analysis_agent.prompts[1])
        self.assertIn("attempt 2 of 3", analysis_agent.prompts[1])
        self.assertTrue(analysis_agent.cancelled)

    async def test_run_analysis_stage_accepts_send_message_plan_delivery(self):
        plan = _sample_plan()
        plan_channel = FakeOnSendChannel("plan_ready")
        analysis_agent = SendMessagePlanReadyAnalysisAgent(plan_channel, plan)
        engine = FakeEngine(
            [],
            channels={"plan_ready": plan_channel},
            analysis_agent=analysis_agent,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = await asyncio.wait_for(
                run_analysis_stage(
                    "https://github.com/jellyfin/jellyfin/issues/1",
                    "10.9.7",
                    temp_dir,
                    stream=None,
                    engine_factory=lambda stage: engine,
                    issue_fetcher=_sample_issue_fetcher,
                ),
                timeout=1,
            )

            written_plan = json.loads((Path(temp_dir) / "plan.json").read_text())

        self.assertEqual(result.status, "plan_ready")
        self.assertEqual(result.metadata["source"], "channel")
        self.assertEqual(written_plan, plan)
        self.assertTrue(analysis_agent.cancelled)

    async def test_run_analysis_stage_writes_routed_web_client_plan(self):
        plan = _sample_web_client_plan()
        plan_channel = FakeOnSendChannel("web_client_plan_ready")
        analysis_agent = SendMessagePlanReadyAnalysisAgent(plan_channel, plan)
        engine = FakeEngine(
            [],
            channels={"web_client_plan_ready": plan_channel},
            analysis_agent=analysis_agent,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = await asyncio.wait_for(
                run_analysis_stage(
                    "https://github.com/jellyfin/jellyfin/issues/1",
                    "10.9.7",
                    temp_dir,
                    stream=None,
                    engine_factory=lambda stage: engine,
                    issue_fetcher=_sample_issue_fetcher,
                ),
                timeout=1,
            )

            written_plan = json.loads((Path(temp_dir) / "plan.json").read_text())

        self.assertEqual(result.status, "web_client_plan_ready")
        self.assertEqual(result.metadata["source"], "channel")
        self.assertEqual(result.metadata["channel"], "web_client_plan_ready")
        self.assertEqual(written_plan["execution_target"], "web_client")
        self.assertEqual(written_plan["reproduction_steps"][0]["tool"], "browser")

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

    async def test_run_issue_cleans_plan_observers_on_insufficient_information(self):
        plan_channel = FakeOnSendChannel("plan_ready")
        web_plan_channel = FakeOnSendChannel("web_client_plan_ready")
        engine = FakeEngine(
            ["INSUFFICIENT_INFORMATION\nmissing steps\n"],
            channels={
                "plan_ready": plan_channel,
                "web_client_plan_ready": web_plan_channel,
            },
        )

        result = await run_issue(
            "https://github.com/jellyfin/jellyfin/issues/2",
            "10.9.7",
            stream=None,
            engine_factory=lambda recipe: engine,
            issue_fetcher=_sample_issue_fetcher,
        )

        self.assertEqual(result.status, "insufficient_information")
        self.assertEqual(plan_channel.callbacks, [])
        self.assertEqual(web_plan_channel.callbacks, [])

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

    async def test_run_analysis_stage_writes_insufficient_information_summary(self):
        engine = FakeEngine(
            [
                "[/info]\ntool docs\n[info/]\n\n",
                "INSUFFICIENT_INFORMATION: Missing seed data.\n\n",
                "Missing details:\n- A backup from the upgraded server\n",
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = await run_analysis_stage(
                "https://github.com/jellyfin/jellyfin/issues/1",
                "10.9.7",
                temp_dir,
                stream=None,
                engine_factory=lambda stage: engine,
                issue_fetcher=_sample_issue_fetcher,
            )

            summary_path = Path(temp_dir) / INSUFFICIENT_INFORMATION_SUMMARY_FILE
            summary = summary_path.read_text(encoding="utf-8")
            stage_result = json.loads(
                (Path(temp_dir) / "stage_result.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result.status, "insufficient_information")
        self.assertEqual(result.output_file, INSUFFICIENT_INFORMATION_SUMMARY_FILE)
        self.assertIn("# Insufficient Information", summary)
        self.assertIn("Issue title: Debug issue", summary)
        self.assertIn("INSUFFICIENT_INFORMATION: Missing seed data.", summary)
        self.assertIn("- A backup from the upgraded server", summary)
        self.assertIn("Full Stage 1 transcript: `transcript.json`", summary)
        self.assertNotIn("tool docs", summary)
        self.assertEqual(
            stage_result["output_file"],
            INSUFFICIENT_INFORMATION_SUMMARY_FILE,
        )
        self.assertEqual(
            stage_result["metadata"]["summary"],
            INSUFFICIENT_INFORMATION_SUMMARY_FILE,
        )
        self.assertEqual(stage_result["metadata"]["transcript"], "transcript.json")
        self.assertNotIn("tool docs", stage_result["metadata"]["message"])

    async def test_run_analysis_stage_flushes_transcript_while_streaming(self):
        analysis_agent = CrashingAnalysisAgent(["\n", "\n", "partial response\n"])
        engine = FakeEngine(
            [],
            channels={"plan_ready": HangingChannel()},
            analysis_agent=analysis_agent,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "provider stream crashed"):
                await run_analysis_stage(
                    "https://github.com/jellyfin/jellyfin/issues/1",
                    "10.9.7",
                    temp_dir,
                    stream=None,
                    engine_factory=lambda stage: engine,
                    issue_fetcher=_sample_issue_fetcher,
                )

            transcript_payload = _read_stage_transcript(temp_dir)

        self.assertEqual(transcript_payload["status"], "running")
        self.assertEqual(transcript_payload["messages"][0]["role"], "user")
        self.assertIn(
            "Issue: https://github.com/jellyfin/jellyfin/issues/1",
            transcript_payload["messages"][0]["content"],
        )
        self.assertEqual(transcript_payload["messages"][1]["role"], "assistant")
        self.assertEqual(
            transcript_payload["messages"][1]["content"],
            "partial response\n",
        )
        self.assertEqual(
            transcript_payload["output"]["assistant"],
            "partial response\n",
        )

    async def test_run_analysis_stage_transcript_uses_provider_messages(self):
        plan = _sample_plan()
        plan_channel = FakeOnSendChannel("plan_ready")
        analysis_agent = ProviderConversationAnalysisAgent(plan_channel, plan)
        engine = FakeEngine(
            [],
            channels={"plan_ready": plan_channel},
            analysis_agent=analysis_agent,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = await asyncio.wait_for(
                run_analysis_stage(
                    "https://github.com/jellyfin/jellyfin/issues/1",
                    "10.9.7",
                    temp_dir,
                    stream=None,
                    engine_factory=lambda stage: engine,
                    issue_fetcher=_sample_issue_fetcher,
                ),
                timeout=1,
            )

            transcript_payload = _read_stage_transcript(temp_dir)

        self.assertEqual(result.status, "plan_ready")
        self.assertEqual(len(analysis_agent.agent.llm.calls), 2)
        self.assertGreaterEqual(len(transcript_payload["messages"]), 4)
        self.assertEqual(
            [message["role"] for message in transcript_payload["messages"]],
            ["user", "assistant", "user", "assistant"],
        )
        self.assertIn(
            "[Tool completed]",
            transcript_payload["messages"][2]["content"],
        )
        self.assertEqual(len(transcript_payload["provider_requests"]), 2)
        self.assertEqual(
            transcript_payload["provider_requests"][1]["message_count"],
            3,
        )

    async def test_run_analysis_stage_accepts_context_filled_provider_plan(self):
        plan = _sample_provider_plan_without_target_version()
        engine = FakeEngine(
            ["analysis completed\n"],
            channels={"plan_ready": FakeChannel({"content": json.dumps(plan)})},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = await run_analysis_stage(
                "https://github.com/jellyfin/jellyfin/issues/14267",
                "10.11.8",
                temp_dir,
                stream=None,
                engine_factory=lambda stage: engine,
                issue_fetcher=_sample_issue_fetcher,
            )

            written_plan = json.loads((Path(temp_dir) / "plan.json").read_text())

        self.assertEqual(result.status, "plan_ready")
        self.assertEqual(result.metadata["source"], "channel")
        self.assertEqual(written_plan["target_version"], "10.11.8")
        self.assertEqual(
            written_plan["issue_url"],
            "https://github.com/jellyfin/jellyfin/issues/14267",
        )
        self.assertEqual(written_plan["issue_title"], "Debug issue")
        self.assertEqual(
            written_plan["reproduction_steps"][0]["input"]["command"],
            "echo ok",
        )
        self.assertEqual(
            written_plan["reproduction_steps"][0]["success_criteria"]["all_of"][0],
            {"type": "exit_code", "equals": 0},
        )
        self.assertEqual(
            written_plan["reproduction_steps"][1]["input"]["path"],
            "/Items",
        )
        self.assertEqual(
            written_plan["reproduction_steps"][1]["input"]["params"],
            {"Recursive": "true"},
        )
        self.assertEqual(
            written_plan["reproduction_steps"][1]["capture"]["item_id"],
            {"from": "body_json_path", "path": "$.Items[0].Id"},
        )
        self.assertEqual(
            written_plan["reproduction_steps"][1]["success_criteria"]["all_of"][0],
            {
                "type": "body_json_path",
                "path": "$.Items[0].Name",
                "equals": "Beautiful Planet",
            },
        )

    async def test_run_analysis_stage_retries_after_hallucinated_plan_ready(self):
        plan = _sample_plan()
        plan_channel = FakeOnSendChannel("plan_ready")
        analysis_agent = RetryThenPlanReadyAnalysisAgent(plan_channel, plan)
        engine = FakeEngine(
            [],
            channels={"plan_ready": plan_channel},
            analysis_agent=analysis_agent,
        )

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

            written_plan = json.loads((Path(temp_dir) / "plan.json").read_text())
            transcript_payload = _read_stage_transcript(temp_dir)

        self.assertEqual(result.status, "plan_ready")
        self.assertEqual(result.metadata["source"], "channel")
        self.assertEqual(written_plan, plan)
        self.assertEqual(len(analysis_agent.prompts), 2)
        self.assertIn("REJECTED", analysis_agent.prompts[1])
        self.assertIn("attempt 2 of 3", analysis_agent.prompts[1])
        self.assertIn(
            "has been sent to `plan_ready`",
            transcript_payload["output"]["assistant"],
        )
        self.assertTrue(analysis_agent.cancelled)

    async def test_run_analysis_stage_rejects_printed_plan_without_channel(self):
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
            with self.assertRaisesRegex(
                AnalysisAgentProtocolError,
                "send plan_ready",
            ):
                await asyncio.wait_for(
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

            transcript_payload = _read_stage_transcript(temp_dir)
            self.assertIn("analysis started", transcript_payload["output"]["assistant"])
            self.assertEqual(len(engine.analysis_agent.prompts), 3)
            self.assertIn("attempt 2 of 3", engine.analysis_agent.prompts[1])
            self.assertIn("attempt 3 of 3", engine.analysis_agent.prompts[2])
            self.assertFalse((Path(temp_dir) / "plan.json").exists())
            self.assertFalse((Path(temp_dir) / "stage_result.json").exists())

    async def test_run_analysis_stage_rejects_default_output_plan(self):
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
            with self.assertRaisesRegex(
                AnalysisAgentProtocolError,
                "send plan_ready",
            ):
                await asyncio.wait_for(
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

            written_transcript = _read_stage_transcript(temp_dir)

            self.assertIn("jellyfin_version", written_transcript["output"]["assistant"])
            self.assertEqual(written_transcript["messages"][0]["role"], "user")
            self.assertEqual(written_transcript["messages"][1]["role"], "assistant")
            self.assertEqual(
                written_transcript["messages"][1]["content"],
                written_transcript["output"]["assistant"],
            )
            self.assertFalse((Path(temp_dir) / "plan.json").exists())

    async def test_run_analysis_stage_rejects_conversation_plan_without_channel(self):
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
            with self.assertRaisesRegex(
                AnalysisAgentProtocolError,
                "send plan_ready",
            ):
                await asyncio.wait_for(
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

            written_transcript = _read_stage_transcript(temp_dir)

            self.assertEqual(stream.getvalue(), "")
            self.assertIn("jellyfin_version", written_transcript["output"]["assistant"])
            self.assertEqual(written_transcript["output"]["sources"][0]["source"], "conversation")
            self.assertFalse((Path(temp_dir) / "plan.json").exists())

    async def test_run_analysis_stage_rejects_printed_send_message_without_delivery(self):
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
            with self.assertRaisesRegex(
                AnalysisAgentProtocolError,
                "send plan_ready",
            ):
                await asyncio.wait_for(
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

            transcript_payload = _read_stage_transcript(temp_dir)
            self.assertIn("@@channel=plan_ready", transcript_payload["output"]["assistant"])
            self.assertFalse((Path(temp_dir) / "plan.json").exists())

    async def test_run_analysis_stage_raises_when_plan_ready_is_missing(self):
        engine = FakeEngine(["analysis started\nREPRODUCTION_PLAN_COMPLETE\n"])
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                AnalysisAgentProtocolError,
                "send plan_ready",
            ):
                await asyncio.wait_for(
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

            transcript_payload = _read_stage_transcript(temp_dir)
            self.assertIn("analysis started", transcript_payload["output"]["assistant"])
            self.assertFalse((Path(temp_dir) / "plan.json").exists())
            self.assertFalse((Path(temp_dir) / "stage_result.json").exists())

    async def test_run_analysis_stage_raises_on_empty_response(self):
        engine = FakeEngine([""])
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                AnalysisAgentEmptyResponseError,
                "empty response",
            ):
                await asyncio.wait_for(
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

            transcript_payload = _read_stage_transcript(temp_dir)
            self.assertEqual(transcript_payload["output"]["assistant"], "")
            self.assertEqual(len(engine.analysis_agent.prompts), 3)
            self.assertIn("attempt 2 of 3", engine.analysis_agent.prompts[1])
            self.assertIn("attempt 3 of 3", engine.analysis_agent.prompts[2])
            self.assertFalse((Path(temp_dir) / "plan.json").exists())
            self.assertFalse((Path(temp_dir) / "stage_result.json").exists())

    def test_run_execution_stage_reads_plan_and_writes_result_handoff(self):
        plan = _sample_plan()
        engine = FakeStage2AgentEngine()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "analysis"
            input_dir.mkdir()
            (input_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

            result = run_execution_stage(
                input_dir,
                temp_path / "execution",
                run_id="run-1",
                engine_factory=lambda stage: engine,
            )

            execution_result_path = temp_path / "execution" / "execution_result.json"
            result_path = temp_path / "execution" / "result.json"
            stage_result_path = temp_path / "execution" / "stage_result.json"
            payload = json.loads(execution_result_path.read_text(encoding="utf-8"))
            self.assertEqual(result.status, "reproduced")
            self.assertEqual(result.output_file, "execution_result.json")
            self.assertEqual(payload["run_id"], "run-1")
            self.assertEqual(payload["plan"], plan)
            self.assertEqual(
                json.loads(result_path.read_text(encoding="utf-8")),
                payload,
            )
            self.assertTrue(stage_result_path.is_file())
            self.assertEqual(engine.received_channel, "plan_ready")
            self.assertEqual(engine.received_payload["run_id"], "run-1")
            self.assertEqual(engine.received_payload["plan"], plan)
            self.assertEqual(
                engine.received_payload["artifacts_root"],
                str((temp_path / "execution").resolve()),
            )
            self.assertTrue(engine.stopped)

    def test_run_execution_stage_routes_verification_request_channel(self):
        plan = _sample_plan()
        plan["is_verification"] = True
        engine = FakeStage2AgentEngine()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "analysis"
            input_dir.mkdir()
            (input_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

            result = run_execution_stage(
                input_dir,
                temp_path / "execution",
                engine_factory=lambda stage: engine,
            )

            self.assertEqual(result.status, "reproduced")
            self.assertEqual(engine.received_channel, "verification_request")

    def test_run_execution_stage_stops_engine_after_timeout(self):
        plan = _sample_plan()
        engine = FakeStage2AgentEngine(hang=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "analysis"
            input_dir.mkdir()
            (input_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

            with self.assertRaises(PipelineTimeoutError):
                run_execution_stage(
                    input_dir,
                    temp_path / "execution",
                    timeout_s=0.01,
                    engine_factory=lambda stage: engine,
                )

            self.assertTrue(engine.stopped)

    def test_run_execution_stage_stops_engine_after_error(self):
        plan = _sample_plan()
        engine = FakeStage2AgentEngine(fail_immediately=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "analysis"
            input_dir.mkdir()
            (input_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "stage engine failed"):
                run_execution_stage(
                    input_dir,
                    temp_path / "execution",
                    engine_factory=lambda stage: engine,
                )

            self.assertTrue(engine.stopped)

    def test_run_web_client_stage_reads_plan_and_writes_execution_result(self):
        plan = _sample_web_client_plan()
        engine = FakeWebClientStageEngine()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "analysis"
            input_dir.mkdir()
            (input_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

            result = run_web_client_stage(
                input_dir,
                temp_path / "web-client",
                run_id="web-run-1",
                engine_factory=lambda stage: engine,
            )

            result_path = temp_path / "web-client" / "execution_result.json"
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result.stage, "web-client")
            self.assertEqual(result.status, "reproduced")
            self.assertEqual(result.output_file, "execution_result.json")
            self.assertEqual(payload["run_id"], "web-run-1")
            self.assertEqual(payload["plan"], plan)
            self.assertTrue((temp_path / "web-client" / "result.json").is_file())
            self.assertEqual(engine.received_channel, "web_client_plan_ready")
            self.assertEqual(engine.received_payload["run_id"], "web-run-1")
            self.assertEqual(engine.received_payload["plan"], plan)
            self.assertTrue(engine.stopped)

    def test_run_web_client_stage_emits_debug_logging(self):
        plan = _sample_web_client_plan()
        engine = FakeWebClientStageEngine()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "analysis"
            input_dir.mkdir()
            (input_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

            with self.assertLogs(LOGGER_NAME, level="DEBUG") as logs:
                run_web_client_stage(
                    input_dir,
                    temp_path / "web-client",
                    run_id="web-run-logs",
                    engine_factory=lambda stage: engine,
                )

            output = "\n".join(logs.output)
            self.assertIn("Loaded web-client Stage 2 plan", output)
            self.assertIn("Routing web-client Stage 2 plan through KT agent", output)
            self.assertIn("Sent web-client Stage 2 plan", output)
            self.assertIn("Web-client Stage 2 agent finished", output)

    def test_run_web_client_stage_routes_verification_request_channel(self):
        plan = _sample_web_client_plan()
        plan["is_verification"] = True
        engine = FakeWebClientStageEngine()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "analysis"
            input_dir.mkdir()
            (input_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

            result = run_web_client_stage(
                input_dir,
                temp_path / "web-client",
                engine_factory=lambda stage: engine,
            )

            self.assertEqual(result.status, "reproduced")
            self.assertEqual(
                engine.received_channel,
                "web_client_verification_request",
            )

    def test_web_client_stage_offloads_agent_engine_inside_active_event_loop(self):
        plan = _sample_web_client_plan()
        main_thread = threading.get_ident()
        engine = FakeWebClientStageEngine()

        async def call_stage(input_dir, output_dir):
            return run_web_client_stage(
                input_dir,
                output_dir,
                run_id="web-run-threaded",
                engine_factory=lambda stage: engine,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "analysis"
            input_dir.mkdir()
            (input_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

            result = asyncio.run(call_stage(input_dir, temp_path / "web-client"))

            self.assertEqual(result.status, "reproduced")
            self.assertIsNotNone(engine.run_thread)
            self.assertNotEqual(engine.run_thread, main_thread)

    def test_execution_stage_offloads_agent_engine_inside_active_event_loop(self):
        plan = _sample_plan()
        main_thread = threading.get_ident()
        engine = FakeStage2AgentEngine()

        async def call_stage(input_dir, output_dir):
            return run_execution_stage(
                input_dir,
                output_dir,
                run_id="standard-run-threaded",
                engine_factory=lambda stage: engine,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "analysis"
            input_dir.mkdir()
            (input_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

            result = asyncio.run(call_stage(input_dir, temp_path / "execution"))

            self.assertEqual(result.status, "reproduced")
            self.assertIsNotNone(engine.run_thread)
            self.assertNotEqual(engine.run_thread, main_thread)

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
        self.assertEqual(
            _parse_stage_choice(["--stage=web-client", "--input", "x", "--out", "y"]),
            "web-client",
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

        execution_stage_args = _build_stage_parser("execution").parse_args(
            [
                "--stage",
                "execution",
                "--input",
                "debug/stage1",
                "--out",
                "debug/stage2",
                "--timeout-s",
                "3.5",
            ]
        )

        self.assertEqual(execution_stage_args.timeout_s, 3.5)

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

    def test_configure_runtime_logging_leaves_debug_enabled_for_child_loggers(self):
        calls = []
        app_logger = logging.getLogger(LOGGER_NAME)
        kt_root = logging.getLogger("kohakuterrarium")
        existing_child = logging.getLogger("kohakuterrarium.some_module")
        original_levels = {
            app_logger: app_logger.level,
            kt_root: kt_root.level,
            existing_child: existing_child.level,
        }
        existing_child.setLevel(logging.INFO)

        fake_logging = types.ModuleType("kohakuterrarium.utils.logging")

        def configure_utf8_stdio(*, log=False):
            return None

        def get_logger(name, level=logging.INFO):
            calls.append((name, level))
            logger = logging.getLogger(name)
            logger.setLevel(level)
            return logger

        def set_level(level):
            kt_root.setLevel(getattr(logging, str(level)))

        fake_logging.configure_utf8_stdio = configure_utf8_stdio
        fake_logging.disable_stderr_logging = lambda: None
        fake_logging.enable_stderr_logging = lambda level: None
        fake_logging.get_logger = get_logger
        fake_logging.set_level = set_level

        try:
            with patch.dict(
                sys.modules,
                {
                    "kohakuterrarium": types.ModuleType("kohakuterrarium"),
                    "kohakuterrarium.utils": types.ModuleType("kohakuterrarium.utils"),
                    "kohakuterrarium.utils.logging": fake_logging,
                },
            ):
                configure_runtime_logging("DEBUG", "off")

            self.assertEqual(calls, [(LOGGER_NAME, logging.NOTSET)])
            self.assertEqual(kt_root.level, logging.DEBUG)
            self.assertEqual(app_logger.level, logging.NOTSET)
            self.assertEqual(app_logger.getEffectiveLevel(), logging.DEBUG)
            self.assertEqual(existing_child.level, logging.NOTSET)
        finally:
            for logger, level in original_levels.items():
                logger.setLevel(level)

    def test_apply_execution_turn_budget_updates_execution_agent(self):
        engine = FakeEngine([])
        plan = {"reproduction_steps": [{} for _ in range(20)]}

        budget = apply_execution_turn_budget(engine, plan)

        self.assertEqual(budget, (96, 106))
        self.assertEqual(engine.execution_agent.max_iterations, 96)
        self.assertEqual(engine.execution_agent.termination["max_turns"], 106)

    def test_stage1_model_config_rejects_blacklisted_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                "name: analysis_agent\n"
                "controller:\n"
                "  llm: openrouter/gemini-3.1-pro\n",
                encoding="utf-8",
            )

            with self.assertRaises(BlockedStage1ModelError) as context:
                _assert_stage1_model_config_allowed(config_path)

            error_message = str(context.exception)
            self.assertIn("currently blocked", error_message)
            self.assertIn("stage1_model_blacklist.py", error_message)
            self.assertNotIn("gemini", error_message.lower())

    def test_stage1_model_config_allows_other_gemini_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                "name: analysis_agent\n"
                "controller:\n"
                "  llm: openrouter/gemini-2.5-pro\n",
                encoding="utf-8",
            )

            _assert_stage1_model_config_allowed(config_path)

    def test_stage1_model_config_supports_provider_and_model_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                "name: analysis_agent\n"
                "controller:\n"
                "  provider: openrouter\n"
                "  model: gemini-3.1-flash-lite\n",
                encoding="utf-8",
            )

            self.assertEqual(
                _stage1_model_identifier_from_config(config_path),
                "openrouter/gemini-3.1-flash-lite",
            )
            with self.assertRaises(BlockedStage1ModelError):
                _assert_stage1_model_config_allowed(config_path)

    def test_stage1_model_recipe_resolution_checks_analysis_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            analysis_dir = root / "agents" / "analysis"
            analysis_dir.mkdir(parents=True)
            config_path = analysis_dir / "config.yaml"
            config_path.write_text(
                "name: analysis_agent\n"
                "controller:\n"
                "  llm: openrouter/gemini-3.1-pro\n",
                encoding="utf-8",
            )
            recipe_path = root / "terrarium.yaml"
            recipe_path.write_text(
                "version: '1.0'\n"
                "creatures:\n"
                "  - name: analysis_agent\n"
                "    base_config: agents/analysis\n",
                encoding="utf-8",
            )

            self.assertEqual(_stage1_config_path_from_recipe(recipe_path), config_path)
            with self.assertRaises(BlockedStage1ModelError):
                _assert_stage1_model_allowed_for_recipe(recipe_path)

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
