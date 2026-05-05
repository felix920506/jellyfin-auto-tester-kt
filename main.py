"""Pipeline fabric and CLI for the Jellyfin auto-tester."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import re
import shlex
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, TextIO
from urllib.parse import urlparse

from stage1_model_blacklist import is_stage1_model_blacklisted
from tools.async_compat import run_sync_away_from_loop
from tools.reproduction_plan_markdown import (
    parse_reproduction_plan_markdown,
    ReproductionPlanMarkdownError,
)


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_RECIPE_PATH = REPO_ROOT / "terrarium.yaml"
DEFAULT_DOTENV_PATH = REPO_ROOT / ".env"
DEFAULT_ANALYSIS_CONFIG_PATH = REPO_ROOT / "creatures" / "analysis" / "config.yaml"
TERMINAL_CHANNELS = ("final_report", "human_review_queue")
ANALYSIS_PLAN_CHANNELS = ("plan_ready", "web_client_plan_ready")
EXECUTION_PLAN_CHANNELS = ("plan_ready", "verification_request")
WEB_CLIENT_PLAN_CHANNELS = (
    "web_client_plan_ready",
    "web_client_verification_request",
)
WEB_CLIENT_TASK_CHANNEL = "web_client_task"
WEB_CLIENT_DONE_CHANNEL = "web_client_done"
EXECUTION_DONE_CHANNEL = "execution_done"
REPORT_VERIFICATION_CHANNELS = (
    "verification_request",
    "web_client_verification_request",
)
STAGE_CHOICES = ("analysis", "execution", "web-client", "report")
STAGE_CHANNEL_GRACE_S = 2.0
ANALYSIS_TRANSCRIPT_FILE = "transcript.json"
INSUFFICIENT_INFORMATION_SUMMARY_FILE = "insufficient_information.md"
LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_STDERR_CHOICES = ("auto", "on", "off")
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_STDERR = "auto"
ANALYSIS_PLAN_MAX_ATTEMPTS = 3
STAGE_HANDOFF_FILES = {
    "analysis": ("plan.md",),
    "execution": ("result.json", "execution_result.json"),
    "web-client": ("result.json", "execution_result.json"),
    "report": ("result.json", "execution_result.json"),
}
PROVIDER_AUTH_ENV_VARS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "MIMO_API_KEY",
    }
)
PIPELINE_PAYLOAD_KEYS = {
    "report_path",
    "run_id",
    "verification_run_id",
    "overall_result",
    "verified",
    "issue_url",
    "reason",
}
MINIMUM_REPRODUCTION_PLAN_KEYS = {
    "issue_url",
    "reproduction_steps",
}
DEMO_SERVER_URLS = {
    "stable": "https://demo.jellyfin.org/stable",
    "unstable": "https://demo.jellyfin.org/unstable",
}
LOGGER_NAME = "kohakuterrarium.jellyfin_auto_tester"
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())


@dataclass(slots=True)
class PipelineResult:
    """Terminal result emitted by the pipeline fabric."""

    channel: str
    message: Any
    report_path: str | None = None
    run_id: str | None = None
    verification_run_id: str | None = None
    overall_result: str | None = None
    verified: bool | None = None
    issue_url: str | None = None
    analysis_output: str = ""

    @property
    def status(self) -> str:
        if self.channel == "final_report":
            return "complete"
        if self.channel == "human_review_queue":
            return "human_review"
        if self.channel == "analysis":
            return "insufficient_information"
        return self.channel


class PipelineTimeoutError(TimeoutError):
    """Raised when the pipeline does not reach a terminal channel in time."""


class AnalysisAgentProtocolError(AssertionError):
    """Raised when Stage 1 finishes without following the output protocol."""


class AnalysisAgentEmptyResponseError(AnalysisAgentProtocolError):
    """Raised when Stage 1 returns no text and no ReproductionPlan."""


class BlockedStage1ModelError(RuntimeError):
    """Raised when Stage 1 is configured with a blacklisted model."""


@dataclass(slots=True)
class StageDebugResult:
    """Disk handoff result for a single-stage debug run."""

    stage: str
    status: str
    output_dir: str
    output_file: str | None = None
    metadata: dict[str, Any] | None = None


class _NullOutput:
    """Output sink used when the pipeline fabric owns stdout streaming."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def write(self, _content: str) -> None:
        pass

    async def write_stream(self, _chunk: str) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def on_processing_start(self) -> None:
        pass

    async def on_processing_end(self) -> None:
        pass

    def on_activity(self, _activity_type: str, _detail: str) -> None:
        pass

    def reset(self) -> None:
        pass


class _TranscriptOutput:
    """Output sink that captures visible agent text for transcript files."""

    def __init__(self, on_write: Callable[[str], None] | None = None) -> None:
        self._parts: list[str] = []
        self._on_write = on_write

    @property
    def text(self) -> str:
        return "".join(self._parts)

    @property
    def has_text(self) -> bool:
        return bool(self._parts)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        await self.flush()

    async def write(self, content: str) -> None:
        self._write(content)

    async def write_stream(self, chunk: str) -> None:
        self._write(chunk)

    async def flush(self) -> None:
        pass

    async def on_processing_start(self) -> None:
        pass

    async def on_processing_end(self) -> None:
        pass

    def on_activity(self, _activity_type: str, _detail: str) -> None:
        pass

    def reset(self) -> None:
        pass

    def _write(self, value: Any) -> None:
        text = _chunk_text(value)
        if not text or not text.strip():
            return
        self._parts.append(text)
        if self._on_write is not None:
            self._on_write(text)


class _AnalysisTranscriptWriter:
    """Keep a stage transcript.json valid while provider traffic is in flight."""

    def __init__(
        self,
        path: str | Path,
        *,
        issue_url: str | None,
        container_version: str | None,
        prompt: str,
        issue_thread: dict[str, Any] | None,
        system_prompt: str | None,
        stage: str = "analysis",
        input_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.stage = stage
        self.issue_url = issue_url
        self.container_version = container_version
        self.prompt = prompt
        self.issue_thread = issue_thread
        self.system_prompt = system_prompt
        self.input_metadata = dict(input_metadata or {})
        self._fallback_messages: list[dict[str, Any]] = []
        self._conversation_messages: list[dict[str, Any]] = []
        self._provider_messages: list[dict[str, Any]] = []
        self._provider_requests: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._assistant_parts: list[str] = []
        self._current_assistant_index: int | None = None
        self._provider_active = False
        self._provider_request_count = 0
        if system_prompt:
            self._fallback_messages.append(
                {"role": "system", "content": system_prompt}
            )

    @property
    def assistant_text(self) -> str:
        return "".join(self._assistant_parts)

    @property
    def has_provider_traffic(self) -> bool:
        return self._provider_request_count > 0

    def initialize(self) -> None:
        self.write_snapshot(status="initialized")

    def record_prompt(self, prompt: str, *, attempt: int) -> None:
        self._fallback_messages.append(
            {
                "role": "user",
                "content": prompt,
                "source": "provider_send",
                "attempt": attempt,
            }
        )
        self._fallback_messages.append(
            {
                "role": "assistant",
                "content": "",
                "source": "provider_receive",
                "attempt": attempt,
            }
        )
        self._current_assistant_index = len(self._fallback_messages) - 1
        self._events.append(
            {
                "type": "fallback_prompt",
                "attempt": attempt,
                "message_count": len(self._fallback_messages),
            }
        )
        self.write_snapshot(status="running")

    def record_assistant_text(self, text: str, *, source: str) -> None:
        if not _has_transcript_text(text):
            return
        self._assistant_parts.append(text)
        if self._current_assistant_index is None:
            self._fallback_messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "source": source,
                }
            )
            self._current_assistant_index = len(self._fallback_messages) - 1
        self._fallback_messages[self._current_assistant_index]["content"] += text
        self._fallback_messages[self._current_assistant_index]["source"] = source
        self.write_snapshot(status="running")

    def record_provider_request(self, messages: Any) -> None:
        provider_messages = _normalize_transcript_messages(messages)
        if not provider_messages:
            return
        self._provider_request_count += 1
        self._provider_active = True
        self._provider_messages = provider_messages
        self._assistant_parts = []
        self._provider_requests.append(
            {
                "request_index": self._provider_request_count,
                "message_count": len(provider_messages),
                "messages": provider_messages,
            }
        )
        self._events.append(
            {
                "type": "provider_request",
                "request_index": self._provider_request_count,
                "message_count": len(provider_messages),
            }
        )
        self.write_snapshot(status="running")

    def record_provider_chunk(self, text: str) -> None:
        if not _has_transcript_text(text):
            return
        self._assistant_parts.append(text)
        self.write_snapshot(status="running")

    def record_conversation(self, conversation: Any, *, source: str) -> None:
        messages = _conversation_to_transcript_messages(conversation)
        if not messages:
            return
        self._conversation_messages = messages
        last_role = messages[-1].get("role")
        if self._provider_active and last_role == "assistant":
            self._provider_active = False
            self._assistant_parts = []
        self._events.append(
            {
                "type": source,
                "message_count": len(messages),
            }
        )
        self.write_snapshot(status="running")

    def write_snapshot(
        self,
        *,
        assistant_output: str | None = None,
        output_sources: list[dict[str, str]] | None = None,
        status: str,
    ) -> None:
        assistant = self.assistant_text if assistant_output is None else assistant_output
        _write_json_file(
            self.path,
            _analysis_transcript_payload(
                stage=self.stage,
                issue_url=self.issue_url,
                container_version=self.container_version,
                prompt=self.prompt,
                issue_thread=self.issue_thread,
                system_prompt=self.system_prompt,
                input_metadata=self.input_metadata,
                assistant_output=assistant,
                output_sources=(
                    output_sources
                    if output_sources is not None
                    else _transcript_sources(("incremental", self.assistant_text))
                ),
                messages=self._messages_for_output(assistant),
                status=status,
                provider_requests=self._provider_requests,
                events=self._events,
            ),
        )

    def _messages_for_output(self, assistant_output: str) -> list[dict[str, Any]]:
        messages = self._current_messages()
        if (
            self._provider_active
            or self._conversation_messages
            or self._provider_messages
        ):
            return messages
        assistant_indexes = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "assistant"
        ]
        if not assistant_indexes:
            messages.append({"role": "assistant", "content": assistant_output})
        else:
            messages[assistant_indexes[0]]["content"] = assistant_output
        return messages

    def _current_messages(self) -> list[dict[str, Any]]:
        if self._provider_active and self._provider_messages:
            messages = [dict(message) for message in self._provider_messages]
            draft = self.assistant_text
            if draft:
                messages.append(
                    {
                        "role": "assistant",
                        "content": draft,
                        "source": "provider_stream",
                    }
                )
            return messages
        if self._conversation_messages:
            return [dict(message) for message in self._conversation_messages]
        if self._provider_messages:
            return [dict(message) for message in self._provider_messages]
        return [dict(message) for message in self._fallback_messages]


def load_env_file(path: str | Path = DEFAULT_DOTENV_PATH) -> bool:
    """Load environment variables from ``.env`` if it exists.

    Values already present in the process environment win over values in the
    file. Blank provider auth values are ignored so KohakuTerrarium can keep
    using its default saved-provider login store.
    """

    dotenv_path = Path(path).expanduser()
    if not dotenv_path.exists():
        return False

    try:
        from dotenv import dotenv_values
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "python-dotenv is required to load .env files. "
            "Install dependencies with: .venv/bin/python -m pip install -r requirements.txt"
        ) from exc

    loaded = False
    for key, value in dotenv_values(dotenv_path).items():
        if not key or value is None or key in os.environ:
            continue
        if key in PROVIDER_AUTH_ENV_VARS and not value.strip():
            continue
        os.environ[key] = value
        loaded = True
    return loaded


def configure_runtime_logging(
    log_level: str | None = None,
    log_stderr: str | None = None,
) -> None:
    """Configure KohakuTerrarium and local pipeline logs for this CLI process."""

    load_env_file()
    level = _normalize_log_level(log_level or _default_log_level())
    stderr_mode = _normalize_log_stderr(log_stderr or _default_log_stderr())

    try:
        from kohakuterrarium.utils.logging import (
            configure_utf8_stdio,
            disable_stderr_logging,
            enable_stderr_logging,
            get_logger,
            set_level,
        )
    except Exception as exc:
        _configure_stdlib_logging(level, stderr_mode)
        _logger().debug("KohakuTerrarium logging setup unavailable: %s", exc)
        return

    configure_utf8_stdio(log=False)
    kt_log_stderr = os.environ.pop("KT_LOG_STDERR", None) if stderr_mode == "off" else None
    try:
        get_logger(LOGGER_NAME, logging.NOTSET)
    finally:
        if kt_log_stderr is not None:
            os.environ["KT_LOG_STDERR"] = kt_log_stderr
    set_level(level)
    _inherit_kohakuterrarium_logger_levels()
    if stderr_mode in {"auto", "on"}:
        enable_stderr_logging(level)
    else:
        disable_stderr_logging()


def _configure_stdlib_logging(level: str, stderr_mode: str) -> None:
    root_logger = logging.getLogger()
    if stderr_mode in {"auto", "on"} and not root_logger.handlers:
        logging.basicConfig(
            level=getattr(logging, level),
            format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    root_logger.setLevel(getattr(logging, level))
    logging.getLogger(LOGGER_NAME).setLevel(logging.NOTSET)


def _inherit_kohakuterrarium_logger_levels() -> None:
    """Let the configured KohakuTerrarium root level control child loggers."""

    for name, logger in logging.Logger.manager.loggerDict.items():
        if not name.startswith("kohakuterrarium."):
            continue
        if isinstance(logger, logging.Logger):
            logger.setLevel(logging.NOTSET)


def _logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def _default_log_level() -> str:
    return _normalize_log_level(
        os.environ.get("JF_AUTO_TESTER_LOG_LEVEL")
        or os.environ.get("KT_LOG_LEVEL")
        or DEFAULT_LOG_LEVEL
    )


def _default_log_stderr() -> str:
    return _normalize_log_stderr(
        os.environ.get("JF_AUTO_TESTER_LOG_STDERR") or DEFAULT_LOG_STDERR
    )


def _normalize_log_level(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in LOG_LEVEL_CHOICES:
        choices = ", ".join(LOG_LEVEL_CHOICES)
        raise ValueError(f"log level must be one of: {choices}")
    return normalized


def _normalize_log_stderr(value: str) -> str:
    normalized = value.strip().lower()
    aliases = {
        "1": "on",
        "true": "on",
        "yes": "on",
        "0": "off",
        "false": "off",
        "no": "off",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in LOG_STDERR_CHOICES:
        choices = ", ".join(LOG_STDERR_CHOICES)
        raise ValueError(f"log stderr must be one of: {choices}")
    return normalized


def _parse_log_level(value: str) -> str:
    try:
        return _normalize_log_level(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_log_stderr(value: str) -> str:
    try:
        return _normalize_log_stderr(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def execution_turn_budget(step_count: int) -> tuple[int, int]:
    """Return Stage 2 ``(max_iterations, max_turns)`` for a plan size."""

    max_iterations = max(60, 16 + 4 * max(0, step_count))
    return max_iterations, max_iterations + 10


async def run_issue(
    issue_url: str,
    container_version: str,
    *,
    recipe_path: str | Path = DEFAULT_RECIPE_PATH,
    timeout_s: float | None = 60 * 60,
    stream: TextIO | None = sys.stdout,
    engine_factory: Callable[[str], Any] | None = None,
    issue_fetcher: Callable[..., Any] | None = None,
) -> PipelineResult:
    """Run the full Stage 1 -> Stage 2 -> Stage 3 pipeline.

    The Terrarium channel topology owns stage-to-stage delivery. This fabric
    starts terminal-channel listeners before prompting Stage 1 so broadcast
    final reports are not missed, then returns the first terminal route:
    ``final_report`` or ``human_review_queue``.
    """

    load_env_file()
    logger = _logger()
    logger.info("Starting full pipeline for %s on Jellyfin %s", issue_url, container_version)
    if engine_factory is None:
        _assert_stage1_model_allowed_for_recipe(recipe_path)
    logger.info("Prefetching GitHub issue thread")
    issue_thread = await _prefetch_issue_thread(issue_url, issue_fetcher)
    logger.info("Loading Terrarium recipe from %s", recipe_path)
    engine = await _load_engine(recipe_path, engine_factory=engine_factory)
    _apply_default_execution_budget(engine)

    terminal_task = asyncio.create_task(_wait_for_terminal_message(engine))
    plan_observer_task = _create_first_channel_observer_task(
        engine,
        ANALYSIS_PLAN_CHANNELS,
    )
    if plan_observer_task is not None:
        await asyncio.sleep(0)
    analysis_output: list[str] = []
    chat_task: asyncio.Task[None] | None = None
    try:
        prompt = _analysis_prompt(issue_url, container_version, issue_thread)
        analysis_agent = _get_agent(engine, "analysis_agent")
        _suppress_default_output(analysis_agent)
        current_prompt = prompt
        plan_ready_observed = False
        observer_failed = False
        for attempt in range(1, ANALYSIS_PLAN_MAX_ATTEMPTS + 1):
            logger.info(
                "Starting Stage 1 analysis attempt %s/%s",
                attempt,
                ANALYSIS_PLAN_MAX_ATTEMPTS,
            )
            chat_task = asyncio.create_task(
                _collect_analysis_chat(analysis_agent, current_prompt, analysis_output)
            )

            if plan_observer_task is None or observer_failed:
                await chat_task
                break

            done, _pending = await asyncio.wait(
                {chat_task, plan_observer_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if plan_observer_task in done:
                try:
                    plan_observer_task.result()
                except Exception as exc:
                    _logger().debug("analysis plan observer failed: %s", exc)
                    observer_failed = True
                    if not chat_task.done():
                        await chat_task
                    break

                route_channel, _route_message = plan_observer_task.result()
                _logger().info(
                    "Observed %s; stopping Stage 1 analysis",
                    route_channel,
                )
                await _stop_analysis_agent(analysis_agent)
                if not chat_task.done():
                    chat_task.cancel()
                    await _cancel_task(chat_task)
                chat_task = None
                plan_ready_observed = True
                break

            await chat_task
            chat_task = None
            if (
                plan_observer_task is not None
                and not observer_failed
                and not plan_observer_task.done()
            ):
                await asyncio.sleep(0)
            if (
                plan_observer_task is not None
                and not observer_failed
                and plan_observer_task.done()
            ):
                try:
                    plan_observer_task.result()
                except Exception as exc:
                    _logger().debug("analysis plan observer failed: %s", exc)
                    observer_failed = True
                else:
                    plan_ready_observed = True
                    break
            transcript = "".join(analysis_output)
            if _is_insufficient_information(transcript):
                logger.info("Stage 1 stopped with insufficient information")
                terminal_task.cancel()
                await _cancel_task(terminal_task)
                if plan_observer_task is not None and not plan_observer_task.done():
                    plan_observer_task.cancel()
                    await _cancel_task(plan_observer_task)
                return PipelineResult(
                    channel="analysis",
                    message=transcript.strip(),
                    issue_url=issue_url,
                    analysis_output=transcript,
                )

            if attempt >= ANALYSIS_PLAN_MAX_ATTEMPTS:
                break

            logger.warning(
                "Rejecting Stage 1 attempt %s/%s: no plan_ready channel message observed",
                attempt,
                ANALYSIS_PLAN_MAX_ATTEMPTS,
            )
            current_prompt = _analysis_plan_retry_prompt(
                attempt + 1,
                ANALYSIS_PLAN_MAX_ATTEMPTS,
            )

        transcript = "".join(analysis_output)
        if _is_insufficient_information(transcript):
            logger.info("Stage 1 stopped with insufficient information")
            terminal_task.cancel()
            await _cancel_task(terminal_task)
            if plan_observer_task is not None and not plan_observer_task.done():
                plan_observer_task.cancel()
                await _cancel_task(plan_observer_task)
            return PipelineResult(
                channel="analysis",
                message=transcript.strip(),
                issue_url=issue_url,
                analysis_output=transcript,
            )
        if (
            plan_observer_task is not None
            and not observer_failed
            and not plan_ready_observed
        ):
            terminal_task.cancel()
            await _cancel_task(terminal_task)
            _raise_analysis_plan_protocol_error(
                transcript,
                attempts=ANALYSIS_PLAN_MAX_ATTEMPTS,
            )

        try:
            logger.info("Waiting for terminal pipeline channel")
            channel, message = await asyncio.wait_for(terminal_task, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise PipelineTimeoutError(
                f"timed out waiting for {', '.join(TERMINAL_CHANNELS)}"
            ) from exc

        result = _pipeline_result(channel, message, transcript)
        logger.info("Pipeline finished with status %s", result.status)
        if stream is not None:
            _print_terminal_summary(result, stream)
        return result
    except BaseException:
        if chat_task is not None and not chat_task.done():
            chat_task.cancel()
            await _cancel_task(chat_task)
        if plan_observer_task is not None and not plan_observer_task.done():
            plan_observer_task.cancel()
            await _cancel_task(plan_observer_task)
        terminal_task.cancel()
        await _cancel_task(terminal_task)
        raise


async def run_analysis_stage(
    issue_url: str,
    container_version: str,
    out_dir: str | Path,
    *,
    timeout_s: float | None = 10 * 60,
    stream: TextIO | None = sys.stdout,
    engine_factory: Callable[[str], Any] | None = None,
    issue_fetcher: Callable[..., Any] | None = None,
) -> StageDebugResult:
    """Run Stage 1 only and write a disk handoff folder.

    Output files:
    - ``input.json``: issue URL and target version used for the run.
    - ``transcript.json``: machine-readable prompt and assistant response.
    - ``plan.md``: ReproductionPlan Markdown payload for Stage 2 when available.
    - ``insufficient_information.md``: readable early-stop summary when no
      executable plan can be produced.
    - ``stage_result.json``: machine-readable metadata for the debug run.
    """

    load_env_file()
    logger = _logger()
    logger.info(
        "Starting Stage 1 debug run for %s on Jellyfin %s",
        issue_url,
        container_version,
    )
    if engine_factory is None:
        _assert_stage1_model_config_allowed(DEFAULT_ANALYSIS_CONFIG_PATH)
    issue_thread = await _prefetch_issue_thread(issue_url, issue_fetcher)
    output_dir = _prepare_output_dir(out_dir)
    _write_json_file(
        output_dir / "input.json",
        {
            "stage": "analysis",
            "issue_url": issue_url,
            "container_version": container_version,
            "prefetched_issue_thread": issue_thread,
        },
    )

    engine = await _load_stage_engine("analysis", engine_factory=engine_factory)
    plan_task = _create_first_channel_receive_task(engine, ANALYSIS_PLAN_CHANNELS)
    await asyncio.sleep(0)
    transcript_parts: list[str] = []
    chat_task: asyncio.Task[None] | None = None
    transcript_message_hooks: list[tuple[Any, str, Any]] = []
    try:
        analysis_agent = _get_agent(engine, "analysis_agent")
        prompt = _analysis_prompt(issue_url, container_version, issue_thread)
        system_prompt = _agent_system_prompt_text(analysis_agent)
        transcript_writer = _AnalysisTranscriptWriter(
            output_dir / ANALYSIS_TRANSCRIPT_FILE,
            issue_url=issue_url,
            container_version=container_version,
            prompt=prompt,
            issue_thread=issue_thread,
            system_prompt=system_prompt,
        )
        transcript_writer.initialize()
        transcript_message_hooks = _install_transcript_message_hooks(
            analysis_agent,
            transcript_writer,
        )
        transcript_capture_state = _install_transcript_output(analysis_agent)
        transcript_capture = (
            transcript_capture_state[0] if transcript_capture_state is not None else None
        )
        channel_plan_markdown: str | None = None
        plan_markdown: str | None = None
        plan_source: str | None = None
        plan_channel: str | None = None
        transcript = ""
        transcript_sources: list[dict[str, str]] = []
        current_prompt = prompt
        try:
            for attempt in range(1, ANALYSIS_PLAN_MAX_ATTEMPTS + 1):
                logger.info(
                    "Capturing Stage 1 analysis transcript attempt %s/%s",
                    attempt,
                    ANALYSIS_PLAN_MAX_ATTEMPTS,
                )
                transcript_writer.record_prompt(current_prompt, attempt=attempt)
                chat_task = asyncio.create_task(
                    _collect_analysis_chat(
                        analysis_agent,
                        current_prompt,
                        transcript_parts,
                        transcript_capture=transcript_capture,
                        transcript_writer=transcript_writer,
                    )
                )
                done, _pending = await asyncio.wait(
                    {chat_task, plan_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if plan_task in done:
                    (
                        channel_plan_markdown,
                        _channel_error,
                        channel_name,
                    ) = _completed_channel_plan_markdown_with_channel(
                        plan_task,
                    )
                    if channel_plan_markdown is not None:
                        plan_channel = channel_name
                        await _stop_analysis_agent(analysis_agent)
                        if not chat_task.done():
                            chat_task.cancel()
                            await _cancel_task(chat_task)
                    elif not chat_task.done():
                        await chat_task
                else:
                    await chat_task
                chat_task = None

                if transcript_capture is not None:
                    await transcript_capture.flush()

                captured_transcript = (
                    transcript_capture.text if transcript_capture is not None else ""
                )
                conversation_transcript = _assistant_conversation_text(analysis_agent)
                transcript_sources = _transcript_sources(
                    ("conversation", conversation_transcript),
                    ("output_router", captured_transcript),
                    ("chat_chunks", "".join(transcript_parts)),
                )
                transcript = _merge_transcript_sources(
                    *(source["content"] for source in transcript_sources)
                )
                transcript_writer.write_snapshot(
                    assistant_output=transcript,
                    output_sources=transcript_sources,
                    status="attempt_complete",
                )

                if _is_insufficient_information(transcript):
                    logger.info("Stage 1 debug run stopped with insufficient information")
                    summary_text = _extract_insufficient_information_summary(transcript)
                    summary_file = _write_insufficient_information_summary(
                        output_dir,
                        summary_text=summary_text,
                        issue_url=issue_url,
                        container_version=container_version,
                        issue_thread=issue_thread,
                    )
                    transcript_writer.write_snapshot(
                        assistant_output=transcript,
                        output_sources=transcript_sources,
                        status="insufficient_information",
                    )
                    plan_task.cancel()
                    await _cancel_task(plan_task)
                    return _write_stage_result(
                        StageDebugResult(
                            stage="analysis",
                            status="insufficient_information",
                            output_dir=str(output_dir),
                            output_file=summary_file.name,
                            metadata={
                                "message": summary_text,
                                "summary": summary_file.name,
                                "transcript": ANALYSIS_TRANSCRIPT_FILE,
                            },
                        )
                    )

                plan_markdown = channel_plan_markdown
                plan_source = "channel" if channel_plan_markdown is not None else None
                if plan_markdown is None:
                    (
                        plan_markdown,
                        plan_source,
                        resolved_channel,
                    ) = await _resolve_analysis_stage_plan(
                        plan_task,
                        transcript,
                        timeout_s=timeout_s,
                    )
                    if plan_markdown is not None:
                        plan_channel = resolved_channel
                if plan_markdown is not None:
                    break

                if attempt >= ANALYSIS_PLAN_MAX_ATTEMPTS:
                    break

                logger.warning(
                    "Rejecting Stage 1 attempt %s/%s: no plan_ready channel message received",
                    attempt,
                    ANALYSIS_PLAN_MAX_ATTEMPTS,
                )
                if plan_task.done():
                    (
                        channel_plan_markdown,
                        _channel_error,
                        channel_name,
                    ) = _completed_channel_plan_markdown_with_channel(
                        plan_task,
                    )
                    if channel_plan_markdown is not None:
                        plan_markdown = channel_plan_markdown
                        plan_source = "channel"
                        plan_channel = channel_name
                        break
                    plan_task = _create_first_channel_receive_task(
                        engine,
                        ANALYSIS_PLAN_CHANNELS,
                    )
                    await asyncio.sleep(0)
                current_prompt = _analysis_plan_retry_prompt(
                    attempt + 1,
                    ANALYSIS_PLAN_MAX_ATTEMPTS,
                )
        finally:
            if chat_task is not None and not chat_task.done():
                chat_task.cancel()
                await _cancel_task(chat_task)
            _restore_transcript_output(transcript_capture_state)
            _restore_transcript_message_hooks(transcript_message_hooks)
        if plan_markdown is None:
            transcript_writer.write_snapshot(
                assistant_output=transcript,
                output_sources=transcript_sources,
                status="protocol_error",
            )
            plan_task.cancel()
            await _cancel_task(plan_task)
            _raise_analysis_plan_protocol_error(
                transcript,
                attempts=ANALYSIS_PLAN_MAX_ATTEMPTS,
            )

        transcript_writer.write_snapshot(
            assistant_output=transcript,
            output_sources=transcript_sources,
            status=plan_channel or "plan_ready",
        )
        _write_text_file(output_dir / "plan.md", plan_markdown)
        logger.info(
            "Stage 1 debug run wrote plan.md from %s",
            plan_channel or plan_source,
        )
        return _write_stage_result(
            StageDebugResult(
                stage="analysis",
                status=plan_channel or "plan_ready",
                output_dir=str(output_dir),
                output_file="plan.md",
                metadata={
                    "transcript": ANALYSIS_TRANSCRIPT_FILE,
                    "source": plan_source,
                    "channel": plan_channel or "plan_ready",
                },
            )
        )
    except BaseException:
        plan_task.cancel()
        await _cancel_task(plan_task)
        raise


def run_execution_stage(
    input_path: str | Path,
    out_dir: str | Path,
    *,
    run_id: str | None = None,
    timeout_s: float | None = 10 * 60,
    engine_factory: Callable[[str], Any] | None = None,
) -> StageDebugResult:
    """Run the standard Stage 2 KT peer from a ReproductionPlan handoff."""

    return run_sync_away_from_loop(
        _run_execution_stage_sync,
        input_path,
        out_dir,
        run_id=run_id,
        timeout_s=timeout_s,
        engine_factory=engine_factory,
    )


def _run_execution_stage_sync(
    input_path: str | Path,
    out_dir: str | Path,
    *,
    run_id: str | None = None,
    timeout_s: float | None = 10 * 60,
    engine_factory: Callable[[str], Any] | None = None,
) -> StageDebugResult:
    return asyncio.run(
        _run_execution_stage_impl(
            input_path,
            out_dir,
            run_id=run_id,
            timeout_s=timeout_s,
            engine_factory=engine_factory,
        )
    )


async def _run_execution_stage_impl(
    input_path: str | Path,
    out_dir: str | Path,
    *,
    run_id: str | None = None,
    timeout_s: float | None = 10 * 60,
    engine_factory: Callable[[str], Any] | None = None,
) -> StageDebugResult:
    load_env_file()
    logger = _logger()
    logger.info("Starting Stage 2 debug run from %s", input_path)
    output_dir = _prepare_output_dir(out_dir)
    plan_path = _resolve_stage_input_file(input_path, "analysis")
    plan_markdown = _read_text_file(plan_path)
    plan = parse_reproduction_plan_markdown(plan_markdown)
    output_plan_path = _write_text_file(output_dir / "plan.md", plan_markdown)
    logger.debug(
        "Loaded execution Stage 2 plan execution_target=%s steps=%s",
        plan.get("execution_target"),
        len(plan.get("reproduction_steps", []))
        if isinstance(plan.get("reproduction_steps"), list)
        else 0,
    )

    engine = await _load_stage_engine("execution", engine_factory=engine_factory)
    route_channel = _execution_stage_plan_channel(plan)
    max_iterations, max_turns = apply_execution_turn_budget(engine, plan)
    metadata: dict[str, Any] = {
        "artifacts_root": str(output_dir),
        "plan_markdown_path": str(output_plan_path),
    }
    if run_id:
        metadata["run_id"] = run_id

    logger.debug(
        "Routing execution Stage 2 plan through KT agent channel=%s run_id=%s "
        "max_iterations=%s max_turns=%s",
        route_channel,
        run_id,
        max_iterations,
        max_turns,
    )
    result = await _run_stage_agent_handoff(
        engine,
        route_channel=route_channel,
        message=plan_markdown,
        metadata=metadata,
        timeout_s=timeout_s,
        sender="execution_stage_debug",
        stage_label="execution",
    )
    _write_json_file(output_dir / "execution_result.json", result)
    _write_json_file(output_dir / "result.json", result)
    logger.debug(
        "Execution Stage 2 agent finished run_id=%s overall_result=%s artifacts_dir=%s",
        result.get("run_id"),
        result.get("overall_result"),
        result.get("artifacts_dir"),
    )
    logger.info("Stage 2 debug run wrote execution_result.json")
    return _write_stage_result(
        StageDebugResult(
            stage="execution",
            status=str(result.get("overall_result", "complete")),
            output_dir=str(output_dir),
            output_file="execution_result.json",
            metadata={
                "input": str(plan_path),
                "run_id": result.get("run_id"),
                "artifacts_dir": result.get("artifacts_dir"),
            },
        )
    )


def run_web_client_stage(
    input_path: str | Path,
    out_dir: str | Path,
    *,
    run_id: str | None = None,
    timeout_s: float | None = 10 * 60,
    engine_factory: Callable[[str], Any] | None = None,
) -> StageDebugResult:
    """Run the web-client Stage 2 KT peer from a ReproductionPlan handoff."""

    return run_sync_away_from_loop(
        _run_web_client_stage_sync,
        input_path,
        out_dir,
        run_id=run_id,
        timeout_s=timeout_s,
        engine_factory=engine_factory,
    )


def _run_web_client_stage_sync(
    input_path: str | Path,
    out_dir: str | Path,
    *,
    run_id: str | None = None,
    timeout_s: float | None = 10 * 60,
    engine_factory: Callable[[str], Any] | None = None,
) -> StageDebugResult:
    return asyncio.run(
        _run_web_client_stage_impl(
            input_path,
            out_dir,
            run_id=run_id,
            timeout_s=timeout_s,
            engine_factory=engine_factory,
        )
    )


async def _run_web_client_stage_impl(
    input_path: str | Path,
    out_dir: str | Path,
    *,
    run_id: str | None = None,
    timeout_s: float | None = 10 * 60,
    engine_factory: Callable[[str], Any] | None = None,
) -> StageDebugResult:
    load_env_file()
    logger = _logger()
    logger.info("Starting web-client Stage 2 debug run from %s", input_path)
    output_dir = _prepare_output_dir(out_dir)
    plan_path = _resolve_stage_input_file(input_path, "analysis")
    plan_markdown = _read_text_file(plan_path)
    plan = parse_reproduction_plan_markdown(plan_markdown)
    output_plan_path = _write_text_file(output_dir / "plan.md", plan_markdown)
    logger.debug(
        "Resolved web-client Stage 2 handoff input=%s output_dir=%s",
        plan_path,
        output_dir,
    )
    logger.debug(
        "Loaded web-client Stage 2 plan execution_target=%s server_mode=%s steps=%s",
        plan.get("execution_target"),
        (plan.get("server_target") or {}).get("mode"),
        len(plan.get("reproduction_steps", []))
        if isinstance(plan.get("reproduction_steps"), list)
        else 0,
    )

    engine = await _load_stage_engine("web-client", engine_factory=engine_factory)
    route_channel = _web_client_stage_plan_channel(plan)
    run_id_value = run_id or f"web-client-{uuid.uuid4()}"
    metadata: dict[str, Any] = {
        "plan_markdown_path": str(output_plan_path),
        "artifacts_root": str(output_dir),
        "run_id": run_id_value,
    }
    prompt = _web_client_stage_prompt(route_channel, plan_markdown, metadata)
    web_client_agent = _optional_agent(engine, "web_client_agent")
    system_prompt = (
        _agent_system_prompt_text(web_client_agent)
        if web_client_agent is not None
        else None
    )
    transcript_writer = _AnalysisTranscriptWriter(
        output_dir / ANALYSIS_TRANSCRIPT_FILE,
        issue_url=_optional_text(plan.get("issue_url")),
        container_version=_optional_text(
            plan.get("target_version") or plan.get("jellyfin_version")
        ),
        prompt=prompt,
        issue_thread=None,
        system_prompt=system_prompt,
        stage="web-client",
        input_metadata={
            "channel": route_channel,
            "input_path": str(plan_path),
            "plan_markdown": plan_markdown,
            "run_id": run_id_value,
            "plan_markdown_path": str(output_plan_path),
            "artifacts_root": str(output_dir),
        },
    )
    transcript_writer.initialize()
    transcript_writer.record_prompt(prompt, attempt=1)
    transcript_message_hooks: list[tuple[Any, str, Any]] = []
    transcript_capture_state: tuple[_TranscriptOutput, Any, Any] | None = None
    if web_client_agent is not None:
        transcript_message_hooks = _install_transcript_message_hooks(
            web_client_agent,
            transcript_writer,
        )

        def record_visible_output(text: str) -> None:
            if not transcript_writer.has_provider_traffic:
                transcript_writer.record_assistant_text(text, source="output_router")

        transcript_capture_state = _install_transcript_output(
            web_client_agent,
            on_write=record_visible_output,
        )

    logger.debug(
        "Routing web-client Stage 2 plan through KT agent channel=%s run_id=%s",
        route_channel,
        run_id,
    )
    try:
        result = await _run_web_client_agent_handoff(
            engine,
            route_channel=route_channel,
            message=plan_markdown,
            metadata=metadata,
            timeout_s=timeout_s,
        )
    finally:
        _restore_transcript_output(transcript_capture_state)
        _restore_transcript_message_hooks(transcript_message_hooks)
    _write_json_file(output_dir / "execution_result.json", result)
    _write_json_file(output_dir / "result.json", result)
    conversation_output = _assistant_conversation_text(web_client_agent)
    assistant_output = conversation_output or transcript_writer.assistant_text
    output_sources = _transcript_sources(
        ("conversation", conversation_output),
        ("incremental", transcript_writer.assistant_text),
    )
    transcript_writer.write_snapshot(
        assistant_output=assistant_output,
        output_sources=output_sources,
        status=EXECUTION_DONE_CHANNEL,
    )
    logger.debug(
        "Web-client Stage 2 agent finished run_id=%s overall_result=%s artifacts_dir=%s",
        result.get("run_id"),
        result.get("overall_result"),
        result.get("artifacts_dir"),
    )
    logger.info("Web-client Stage 2 debug run wrote execution_result.json")
    return _write_stage_result(
        StageDebugResult(
            stage="web-client",
            status=str(result.get("overall_result", "complete")),
            output_dir=str(output_dir),
            output_file="execution_result.json",
            metadata={
                "input": str(plan_path),
                "run_id": result.get("run_id"),
                "artifacts_dir": result.get("artifacts_dir"),
                "transcript": ANALYSIS_TRANSCRIPT_FILE,
            },
        )
    )


async def _run_web_client_agent_handoff(
    engine: Any,
    *,
    route_channel: str,
    message: Any,
    metadata: dict[str, Any] | None = None,
    timeout_s: float | None,
) -> dict[str, Any]:
    return await _run_stage_agent_handoff(
        engine,
        route_channel=route_channel,
        message=message,
        metadata=metadata,
        timeout_s=timeout_s,
        sender="web_client_stage_debug",
        stage_label="web-client",
    )


async def _run_stage_agent_handoff(
    engine: Any,
    *,
    route_channel: str,
    message: Any,
    metadata: dict[str, Any] | None = None,
    timeout_s: float | None,
    sender: str,
    stage_label: str,
) -> dict[str, Any]:
    logger = _logger()
    engine_task = asyncio.create_task(_run_stage_engine(engine))
    result_task: asyncio.Task[tuple[str, Any]] | None = None
    try:
        await _wait_for_engine_channel(engine, route_channel, engine_task)
        await _wait_for_engine_channel(engine, EXECUTION_DONE_CHANNEL, engine_task)
        result_task = asyncio.create_task(
            _receive_channel_message(engine, EXECUTION_DONE_CHANNEL)
        )
        await _send_channel_message(
            engine,
            route_channel,
            (
                message
                if isinstance(message, str)
                else json.dumps(message, sort_keys=True, default=str)
            ),
            sender=sender,
            metadata=metadata,
        )
        logger.debug("Sent %s Stage 2 plan to %s", stage_label, route_channel)

        done, pending = await asyncio.wait(
            {result_task, engine_task},
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if result_task in done:
            _channel, payload = result_task.result()
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"{stage_label} agent emitted non-JSON execution result"
                )
            return payload
        if engine_task in done:
            engine_task.result()
            try:
                _channel, payload = await asyncio.wait_for(
                    result_task,
                    timeout=STAGE_CHANNEL_GRACE_S,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"{stage_label} stage engine stopped before emitting "
                    f"{EXECUTION_DONE_CHANNEL}"
                ) from exc
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"{stage_label} agent emitted non-JSON execution result"
                )
            return payload
        for task in pending:
            task.cancel()
        timeout_text = (
            "without a timeout"
            if timeout_s is None
            else f"within {timeout_s:g} seconds"
        )
        raise PipelineTimeoutError(
            f"timed out waiting for {EXECUTION_DONE_CHANNEL} {timeout_text}"
        )
    finally:
        if result_task is not None and not result_task.done():
            result_task.cancel()
            await _cancel_task(result_task)
        await _stop_stage_engine(engine)
        if not engine_task.done():
            engine_task.cancel()
            await _cancel_task(engine_task)
        elif engine_task.cancelled():
            pass
        else:
            try:
                engine_task.result()
            except Exception:
                if result_task is None or not result_task.done():
                    raise


async def _run_stage_engine(engine: Any) -> None:
    run = getattr(engine, "run", None)
    if callable(run) and _callable_accepts_no_arguments(run):
        await _maybe_await(run())
        return
    start = getattr(engine, "start", None)
    if callable(start) and _callable_accepts_no_arguments(start):
        await _maybe_await(start())
        return

    await asyncio.Event().wait()


async def _stop_stage_engine(engine: Any) -> None:
    for method_name in ("shutdown", "stop"):
        stop = getattr(engine, method_name, None)
        if not callable(stop) or not _callable_accepts_no_arguments(stop):
            continue
        try:
            await _maybe_await(stop())
        except Exception as exc:
            _logger().debug("Failed to stop stage engine: %s", exc)
        return


def _callable_accepts_no_arguments(callback: Callable[..., Any]) -> bool:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return True

    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ) and parameter.default is inspect.Parameter.empty:
            return False
    return True


async def _wait_for_engine_channel(
    engine: Any,
    channel_name: str,
    engine_task: asyncio.Task[Any],
) -> None:
    while _get_channel(engine, channel_name) is None:
        if engine_task.done():
            engine_task.result()
            raise RuntimeError(
                f"stage engine stopped before creating {channel_name!r} channel"
            )
        await asyncio.sleep(0.01)


async def _send_channel_message(
    engine: Any,
    channel_name: str,
    content: str,
    *,
    sender: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    channel = _get_channel(engine, channel_name)
    if channel is None:
        raise RuntimeError(f"Terrarium engine does not expose {channel_name!r} channel")

    send = getattr(channel, "send", None)
    if not callable(send):
        raise RuntimeError(f"{channel_name!r} channel is not sendable")

    message = SimpleNamespace(
        sender=sender,
        content=content,
        metadata=dict(metadata or {}),
        timestamp=None,
        message_id=f"stage_debug_{id(content)}",
        reply_to=None,
        channel=None,
    )
    await _maybe_await(send(message))


def _web_client_stage_plan_channel(plan: dict[str, Any]) -> str:
    if plan.get("is_verification"):
        return "web_client_verification_request"
    return "web_client_plan_ready"


def _web_client_stage_prompt(
    route_channel: str,
    plan_markdown: str,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    metadata_payload = json.dumps(
        dict(metadata or {}),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )
    return (
        f"Channel: {route_channel}\n\n"
        "Web-client Stage 2 handoff Markdown:\n"
        "```markdown\n"
        f"{plan_markdown.rstrip()}\n"
        "```\n\n"
        "Debug metadata:\n"
        "```json\n"
        f"{metadata_payload}\n"
        "```"
    )


def _execution_stage_plan_channel(plan: dict[str, Any]) -> str:
    if plan.get("is_verification"):
        return "verification_request"
    return "plan_ready"


def run_report_stage(
    input_path: str | Path,
    out_dir: str | Path,
    *,
    verification_result_path: str | Path | None = None,
    timeout_s: float | None = 10 * 60,
    engine_factory: Callable[[str], Any] | None = None,
) -> StageDebugResult:
    """Run the Stage 3 KT peer from an ExecutionResult handoff.

    By default, the report agent receives the first ExecutionResult through
    ``execution_done``, requests verification, and the isolated execution agent
    handles that second Stage 2 run. With ``verification_result_path``, the
    supplied result is injected only after the report agent emits
    ``verification_request``.
    """

    return run_sync_away_from_loop(
        _run_report_stage_sync,
        input_path,
        out_dir,
        verification_result_path=verification_result_path,
        timeout_s=timeout_s,
        engine_factory=engine_factory,
    )


def _run_report_stage_sync(
    input_path: str | Path,
    out_dir: str | Path,
    *,
    verification_result_path: str | Path | None = None,
    timeout_s: float | None = 10 * 60,
    engine_factory: Callable[[str], Any] | None = None,
) -> StageDebugResult:
    return asyncio.run(
        _run_report_stage_impl(
            input_path,
            out_dir,
            verification_result_path=verification_result_path,
            timeout_s=timeout_s,
            engine_factory=engine_factory,
        )
    )


async def _run_report_stage_impl(
    input_path: str | Path,
    out_dir: str | Path,
    *,
    verification_result_path: str | Path | None = None,
    timeout_s: float | None = 10 * 60,
    engine_factory: Callable[[str], Any] | None = None,
) -> StageDebugResult:
    load_env_file()
    logger = _logger()
    logger.info("Starting Stage 3 debug run from %s", input_path)
    output_dir = _prepare_output_dir(out_dir)
    result_path = _resolve_stage_input_file(input_path, "execution")
    execution_result = _read_json_file(result_path)
    verification_result = (
        _read_json_file(_resolve_stage_input_file(verification_result_path, "execution"))
        if verification_result_path is not None
        else None
    )

    if not isinstance(execution_result, dict):
        raise TypeError("Stage 3 input must be an ExecutionResult JSON object")
    if verification_result is not None and not isinstance(verification_result, dict):
        raise TypeError("verification result must be an ExecutionResult JSON object")

    _write_json_file(output_dir / "execution_result.json", execution_result)
    _ensure_execution_result_artifact_file(execution_result)
    if verification_result is not None:
        _write_json_file(output_dir / "verification_result.json", verification_result)

    engine = await _load_stage_engine(
        "report",
        engine_factory=engine_factory,
        include_verification_agents=verification_result is None,
    )
    logger.debug(
        "Routing Stage 3 execution result through KT report agent run_id=%s "
        "verification_result=%s",
        execution_result.get("run_id"),
        verification_result is not None,
    )
    route, route_payload, verification_injected = await _run_report_agent_handoff(
        engine,
        execution_result,
        verification_result,
        timeout_s=timeout_s,
    )

    route_payload = _normalize_route_payload(route_payload)
    report_file = _mirror_report_from_route(route_payload, output_dir, execution_result)
    if report_file is not None and isinstance(route_payload, dict):
        route_payload["path"] = str(report_file)
        if "report_path" in route_payload:
            route_payload["report_path"] = str(report_file)

    route_file = output_dir / f"{route}.json"
    _write_json_file(route_file, route_payload)
    _write_json_file(output_dir / "result.json", route_payload)
    _write_json_file(output_dir / "report_metadata.json", route_payload)
    logger.info("Stage 3 debug run wrote %s", route_file.name)

    return _write_stage_result(
        StageDebugResult(
            stage="report",
            status=route,
            output_dir=str(output_dir),
            output_file=route_file.name,
            metadata={
                "input": str(result_path),
                "report": "report.md" if report_file is not None else None,
                "verified": (
                    route_payload.get("verified")
                    if isinstance(route_payload, dict)
                    else None
                ),
                "verification_injected": verification_injected,
                "verification_result": (
                    "verification_result.json"
                    if verification_result is not None
                    else None
                ),
            },
        )
    )


async def _run_report_agent_handoff(
    engine: Any,
    execution_result: dict[str, Any],
    verification_result: dict[str, Any] | None,
    *,
    timeout_s: float | None,
) -> tuple[str, Any, bool]:
    logger = _logger()
    engine_task = asyncio.create_task(_run_stage_engine(engine))
    terminal_task: asyncio.Task[tuple[str, Any]] | None = None
    verification_task: asyncio.Task[tuple[str, Any]] | None = None
    loop = asyncio.get_running_loop()
    deadline = None if timeout_s is None else loop.time() + timeout_s
    verification_injected = False
    try:
        await _wait_for_engine_channel(engine, EXECUTION_DONE_CHANNEL, engine_task)
        for channel in TERMINAL_CHANNELS:
            await _wait_for_engine_channel(engine, channel, engine_task)
        if verification_result is not None:
            verification_task = _create_first_channel_observer_task(
                engine,
                REPORT_VERIFICATION_CHANNELS,
            )
            if verification_task is None:
                verification_channels = _available_channels(
                    engine,
                    REPORT_VERIFICATION_CHANNELS,
                )
                if not verification_channels:
                    verification_channels = ("verification_request",)
                verification_task = _create_first_channel_receive_task(
                    engine,
                    verification_channels,
                )

        terminal_task = asyncio.create_task(_wait_for_terminal_message(engine))
        await _send_channel_message(
            engine,
            EXECUTION_DONE_CHANNEL,
            json.dumps(execution_result, sort_keys=True, default=str),
            sender="report_stage_debug",
        )
        logger.debug("Sent first-run ExecutionResult to report agent")

        while True:
            tasks: set[asyncio.Task[Any]] = {terminal_task, engine_task}
            if verification_task is not None and not verification_injected:
                tasks.add(verification_task)
            done, _pending = await asyncio.wait(
                tasks,
                timeout=_timeout_remaining(deadline),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if terminal_task in done:
                channel, payload = terminal_task.result()
                return channel, payload, verification_injected

            if (
                verification_task is not None
                and verification_task in done
                and not verification_injected
            ):
                route_channel, request_payload = verification_task.result()
                logger.debug(
                    "Observed %s from report agent; injecting supplied "
                    "verification result run_id=%s",
                    route_channel,
                    verification_result.get("run_id"),
                )
                if isinstance(request_payload, dict):
                    _apply_execution_turn_budget_if_available(engine, request_payload)
                await _send_channel_message(
                    engine,
                    EXECUTION_DONE_CHANNEL,
                    json.dumps(verification_result, sort_keys=True, default=str),
                    sender="report_stage_debug_verification",
                )
                verification_injected = True
                continue

            if engine_task in done:
                engine_task.result()
                try:
                    channel, payload = await asyncio.wait_for(
                        terminal_task,
                        timeout=STAGE_CHANNEL_GRACE_S,
                    )
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        "report stage engine stopped before emitting a terminal "
                        "report channel"
                    ) from exc
                return channel, payload, verification_injected

            timeout_text = (
                "without a timeout"
                if timeout_s is None
                else f"within {timeout_s:g} seconds"
            )
            raise PipelineTimeoutError(
                "timed out waiting for Stage 3 report handoff "
                f"{timeout_text}"
            )
    finally:
        if verification_task is not None and not verification_task.done():
            verification_task.cancel()
            await _cancel_task(verification_task)
        if terminal_task is not None and not terminal_task.done():
            terminal_task.cancel()
            await _cancel_task(terminal_task)
        await _stop_stage_engine(engine)
        if not engine_task.done():
            engine_task.cancel()
            await _cancel_task(engine_task)
        elif engine_task.cancelled():
            pass
        else:
            try:
                engine_task.result()
            except Exception:
                if terminal_task is None or not terminal_task.done():
                    raise


def _timeout_remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - asyncio.get_running_loop().time())


def _apply_execution_turn_budget_if_available(
    engine: Any,
    plan: dict[str, Any],
) -> None:
    try:
        apply_execution_turn_budget(engine, plan)
    except Exception as exc:
        _logger().debug("Could not apply verification execution budget: %s", exc)


def _available_channels(
    engine: Any,
    channel_names: Iterable[str],
) -> tuple[str, ...]:
    return tuple(
        channel_name
        for channel_name in channel_names
        if _get_channel(engine, channel_name) is not None
    )


def _ensure_execution_result_artifact_file(execution_result: dict[str, Any]) -> None:
    artifacts_dir = execution_result.get("artifacts_dir")
    if not artifacts_dir:
        return
    target = Path(str(artifacts_dir)).expanduser() / "result.json"
    if target.is_file():
        return
    try:
        _write_json_file(target, execution_result)
    except Exception as exc:
        _logger().debug("Could not mirror execution result into artifacts: %s", exc)


def _normalize_route_payload(payload: Any) -> Any:
    decoded = _decode_jsonish(payload)
    if isinstance(decoded, dict):
        return dict(decoded)
    return decoded


def _mirror_report_from_route(
    route_payload: Any,
    output_dir: Path,
    execution_result: dict[str, Any],
) -> Path | None:
    source: Any = None
    if isinstance(route_payload, dict):
        source = route_payload.get("path") or route_payload.get("report_path")
    if not source:
        artifacts_dir = execution_result.get("artifacts_dir")
        if artifacts_dir:
            source = Path(str(artifacts_dir)).expanduser() / "report.md"
    if not source:
        return None

    source_path = Path(str(source)).expanduser()
    if not source_path.is_file():
        _logger().debug("Report route referenced missing report path: %s", source_path)
        return None
    return _mirror_report(source_path, output_dir)


async def _load_engine(
    recipe_path: str | Path,
    *,
    engine_factory: Callable[[str], Any] | None,
) -> Any:
    recipe_path = Path(recipe_path).expanduser()
    recipe = str(recipe_path)
    if engine_factory is not None:
        return await _maybe_await(engine_factory(recipe))

    _assert_stage1_model_allowed_for_recipe(recipe_path)

    try:
        from kohakuterrarium import Terrarium
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on runtime env
        raise RuntimeError(
            "kohakuterrarium is required to run the full pipeline. "
            "Install it in this environment, then rerun main.py."
        ) from exc

    return await _maybe_await(Terrarium.from_recipe(recipe))


async def _load_stage_engine(
    stage: str,
    *,
    engine_factory: Callable[[str], Any] | None,
    include_verification_agents: bool = True,
) -> Any:
    if engine_factory is not None:
        return await _maybe_await(engine_factory(stage))
    if stage not in {"analysis", "execution", "web-client", "report"}:
        raise ValueError(f"no isolated Terrarium recipe is defined for stage {stage!r}")
    if stage == "analysis":
        _assert_stage1_model_config_allowed(DEFAULT_ANALYSIS_CONFIG_PATH)

    try:
        from kohakuterrarium import Terrarium
        from kohakuterrarium.terrarium.config import (
            ChannelConfig,
            CreatureConfig,
            TerrariumConfig,
        )
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on runtime env
        raise RuntimeError(
            f"kohakuterrarium is required to run Stage {stage!r}. "
            "Install it in this environment, then rerun main.py."
        ) from exc

    if stage == "analysis":
        recipe = TerrariumConfig(
            name="analysis_debug",
            creatures=[
                CreatureConfig(
                    name="analysis_agent",
                    config_data={
                        "name": "analysis_agent",
                        "base_config": "creatures/analysis",
                    },
                    base_dir=REPO_ROOT,
                    send_channels=list(ANALYSIS_PLAN_CHANNELS),
                )
            ],
            channels=[
                ChannelConfig(name=channel, channel_type="queue")
                for channel in ANALYSIS_PLAN_CHANNELS
            ],
        )
    elif stage == "web-client":
        recipe = TerrariumConfig(
            name="web_client_debug",
            creatures=[
                CreatureConfig(
                    name="web_client_agent",
                    config_data={
                        "name": "web_client_agent",
                        "base_config": "creatures/web_client",
                    },
                    base_dir=REPO_ROOT,
                    listen_channels=[
                        *WEB_CLIENT_PLAN_CHANNELS,
                        WEB_CLIENT_TASK_CHANNEL,
                    ],
                    send_channels=[EXECUTION_DONE_CHANNEL, WEB_CLIENT_DONE_CHANNEL],
                )
            ],
            channels=[
                *[
                    ChannelConfig(name=channel, channel_type="queue")
                    for channel in WEB_CLIENT_PLAN_CHANNELS
                ],
                ChannelConfig(name=WEB_CLIENT_TASK_CHANNEL, channel_type="queue"),
                ChannelConfig(name=WEB_CLIENT_DONE_CHANNEL, channel_type="queue"),
                ChannelConfig(name=EXECUTION_DONE_CHANNEL, channel_type="queue"),
            ],
        )
    elif stage == "execution":
        recipe = TerrariumConfig(
            name="execution_debug",
            creatures=[
                CreatureConfig(
                    name="execution_agent",
                    config_data={
                        "name": "execution_agent",
                        "base_config": "creatures/execution",
                    },
                    base_dir=REPO_ROOT,
                    listen_channels=list(EXECUTION_PLAN_CHANNELS),
                    send_channels=[EXECUTION_DONE_CHANNEL],
                )
            ],
            channels=[
                *[
                    ChannelConfig(name=channel, channel_type="queue")
                    for channel in EXECUTION_PLAN_CHANNELS
                ],
                ChannelConfig(name=EXECUTION_DONE_CHANNEL, channel_type="queue"),
            ],
        )
    else:
        creatures = [
            CreatureConfig(
                name="report_agent",
                config_data={
                    "name": "report_agent",
                    "base_config": "creatures/report",
                },
                base_dir=REPO_ROOT,
                listen_channels=[EXECUTION_DONE_CHANNEL],
                send_channels=[
                    "verification_request",
                    "web_client_verification_request",
                    "final_report",
                    "human_review_queue",
                ],
            )
        ]
        if include_verification_agents:
            creatures.extend(
                [
                    CreatureConfig(
                        name="execution_agent",
                        config_data={
                            "name": "execution_agent",
                            "base_config": "creatures/execution",
                        },
                        base_dir=REPO_ROOT,
                        listen_channels=["verification_request"],
                        send_channels=[EXECUTION_DONE_CHANNEL],
                    ),
                    CreatureConfig(
                        name="web_client_agent",
                        config_data={
                            "name": "web_client_agent",
                            "base_config": "creatures/web_client",
                        },
                        base_dir=REPO_ROOT,
                        listen_channels=[
                            "web_client_verification_request",
                            WEB_CLIENT_TASK_CHANNEL,
                        ],
                        send_channels=[EXECUTION_DONE_CHANNEL, WEB_CLIENT_DONE_CHANNEL],
                    ),
                ]
            )
        recipe = TerrariumConfig(
            name="report_debug",
            creatures=creatures,
            channels=[
                ChannelConfig(name=EXECUTION_DONE_CHANNEL, channel_type="queue"),
                ChannelConfig(name="verification_request", channel_type="queue"),
                ChannelConfig(
                    name="web_client_verification_request",
                    channel_type="queue",
                ),
                ChannelConfig(name=WEB_CLIENT_TASK_CHANNEL, channel_type="queue"),
                ChannelConfig(name=WEB_CLIENT_DONE_CHANNEL, channel_type="queue"),
                ChannelConfig(name="final_report", channel_type="broadcast"),
                ChannelConfig(name="human_review_queue", channel_type="queue"),
            ],
        )
    return await _maybe_await(Terrarium.from_recipe(recipe))


def _assert_stage1_model_allowed_for_recipe(recipe_path: str | Path) -> None:
    config_path = _stage1_config_path_from_recipe(recipe_path)
    _assert_stage1_model_config_allowed(config_path)


def _assert_stage1_model_config_allowed(config_path: str | Path) -> None:
    model = _stage1_model_identifier_from_config(config_path)
    if not model or not is_stage1_model_blacklisted(model):
        return

    relative_path = _display_path(config_path)
    raise BlockedStage1ModelError(
        "Stage 1 is configured with a model that is currently blocked. "
        f"Update {relative_path} to use a different Stage 1 model, or edit "
        "stage1_model_blacklist.py if the blocklist should change."
    )


def _stage1_config_path_from_recipe(recipe_path: str | Path) -> Path:
    recipe_path = _absolute_path(recipe_path)
    recipe = _load_yaml_mapping(recipe_path)
    creatures = recipe.get("creatures")
    if not isinstance(creatures, list):
        return DEFAULT_ANALYSIS_CONFIG_PATH

    for creature in creatures:
        if not isinstance(creature, dict):
            continue
        if creature.get("name") != "analysis_agent":
            continue
        config_value = creature.get("base_config") or creature.get("config")
        if not isinstance(config_value, str) or not config_value.strip():
            return DEFAULT_ANALYSIS_CONFIG_PATH
        config_path = Path(config_value).expanduser()
        if not config_path.is_absolute():
            config_path = recipe_path.parent / config_path
        return _creature_config_file(config_path)

    return DEFAULT_ANALYSIS_CONFIG_PATH


def _stage1_model_identifier_from_config(config_path: str | Path) -> str | None:
    config = _load_yaml_mapping(_creature_config_file(Path(config_path).expanduser()))
    controller = config.get("controller")
    sources = [controller, config] if isinstance(controller, dict) else [config]
    for source in sources:
        if not isinstance(source, dict):
            continue
        model = _nonempty_string(source.get("model"))
        provider = _nonempty_string(source.get("provider"))
        if model:
            if provider and "/" not in model:
                return f"{provider}/{model}"
            return model

        llm = _nonempty_string(source.get("llm"))
        if llm:
            return llm

    return None


def _load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency install issue
        raise RuntimeError(
            "PyYAML is required to inspect Stage 1 model configuration. "
            "Install dependencies with `.venv/bin/python -m pip install -r "
            "requirements.txt`."
        ) from exc

    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"expected YAML mapping in {_display_path(path)}")
    return data


def _creature_config_file(path: Path) -> Path:
    if path.suffix.lower() in {".yaml", ".yml"}:
        return path
    return path / "config.yaml"


def _absolute_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved
    return (Path.cwd() / resolved).resolve()


def _display_path(path: str | Path) -> str:
    path = Path(path)
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


async def _prefetch_issue_thread(
    issue_url: str,
    issue_fetcher: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Fetch the target GitHub issue before starting the analysis agent."""

    fetcher = issue_fetcher or _default_issue_fetcher()
    fetcher_kwargs = _github_fetcher_kwargs(fetcher, issue_url)
    if issue_fetcher is None:
        issue_thread = await asyncio.to_thread(
            fetcher,
            **fetcher_kwargs,
        )
    else:
        issue_thread = fetcher(**fetcher_kwargs)
        issue_thread = await _maybe_await(issue_thread)

    if not isinstance(issue_thread, dict):
        raise TypeError("issue prefetcher must return a dictionary")
    return issue_thread


def _github_fetcher_kwargs(
    fetcher: Callable[..., Any],
    issue_url: str,
) -> dict[str, Any]:
    url_key = "url"
    try:
        signature = inspect.signature(fetcher)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        parameters = signature.parameters.values()
        accepts_url = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            or parameter.name == "url"
            for parameter in parameters
        )
        if not accepts_url:
            url_key = "issue_url"
    return {
        url_key: issue_url,
        "include_comments": True,
        "include_linked": True,
    }


def _default_issue_fetcher() -> Callable[..., Any]:
    from tools.github_fetcher import github_fetcher

    return github_fetcher


def _analysis_prompt(
    issue_url: str,
    container_version: str,
    issue_thread: dict[str, Any] | None = None,
) -> str:
    prompt = f"Issue: {issue_url}\nTarget version: {container_version}"
    if issue_thread is None:
        return prompt

    issue_json = json.dumps(
        issue_thread,
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )
    return (
        f"{prompt}\n\n"
        "Prefetched GitHub issue thread JSON:\n"
        "```json\n"
        f"{issue_json}\n"
        "```\n\n"
        "Use the prefetched issue thread as the primary source for this target "
        "issue. Do not call github_fetcher for the same target issue unless "
        "the supplied JSON is missing required fields or appears stale. Use "
        "tools only for linked issues, external resources, documentation, and "
        "additional supporting context."
    )


def _analysis_plan_retry_prompt(attempt: int, max_attempts: int) -> str:
    return (
        "REJECTED: Your previous Stage 1 response did not deliver a valid "
        "`ReproductionPlan Markdown v1` document to either `plan_ready` or "
        "`web_client_plan_ready`. "
        "You may have claimed "
        "that the plan was sent, but the runner did not observe a real "
        "`send_message` delivery to an allowed plan channel with the Markdown payload.\n\n"
        f"This is attempt {attempt} of {max_attempts}. Send no prose and no "
        "summary. Your entire response must be exactly one `send_message` "
        "tool-call block in this KohakuTerrarium bracket format:\n\n"
        "[/send_message]\n"
        "@@channel=plan_ready\n"
        "# ReproductionPlan Markdown v1\n"
        "## Goal\n"
        "...\n"
        "[send_message/]\n\n"
        "Use `@@channel=web_client_plan_ready` instead when the plan is a pure "
        "Jellyfin Web client bug and has `execution_target: \"web_client\"`.\n\n"
        "The closing tag is `[send_message/]`, not `[/send_message]`. The block "
        "body must be the raw Markdown document, not JSON, not a named output "
        "block, not Python-call syntax, and not a description. "
        "Do not say the plan has been sent. If an executable plan cannot be produced, reply with "
        "`INSUFFICIENT_INFORMATION` and the missing details instead."
    )


async def _resolve_analysis_stage_plan(
    plan_task: asyncio.Task[tuple[str, Any]],
    _transcript: str,
    *,
    timeout_s: float | None,
) -> tuple[str | None, str | None, str | None]:
    channel_error: BaseException | None = None
    grace_s = _analysis_channel_grace(timeout_s)

    if plan_task.done():
        plan_markdown, channel_error = _completed_channel_plan_markdown(plan_task)
        if plan_markdown is not None:
            channel_name = None
            try:
                channel_name, _message = plan_task.result()
            except Exception:
                pass
            return plan_markdown, "channel", channel_name

    if not plan_task.done():
        try:
            channel_name, message = await asyncio.wait_for(
                asyncio.shield(plan_task),
                timeout=grace_s,
            )
            plan_markdown = _coerce_plan_markdown_handoff(message)
            if plan_markdown is not None:
                return plan_markdown, "channel", channel_name
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            channel_error = exc

    if channel_error is not None and not plan_task.done():
        plan_task.cancel()
        await _cancel_task(plan_task)
    return None, None, None


def _analysis_channel_grace(timeout_s: float | None) -> float:
    if timeout_s is None:
        return STAGE_CHANNEL_GRACE_S
    return max(0.0, min(float(timeout_s), STAGE_CHANNEL_GRACE_S))


def _raise_analysis_plan_protocol_error(
    transcript: str,
    *,
    attempts: int = ANALYSIS_PLAN_MAX_ATTEMPTS,
) -> None:
    if not transcript.strip():
        raise AnalysisAgentEmptyResponseError(
            "analysis agent returned an empty response without sending plan_ready "
            f"after {attempts} attempts"
        )
    raise AnalysisAgentProtocolError(
        f"analysis agent failed to send plan_ready after {attempts} attempts; "
        "final Stage 1 output must use a send_message tool-call block targeting "
        "`plan_ready` or `web_client_plan_ready` with ReproductionPlan Markdown: "
        "[/send_message]@@channel=plan_ready... [send_message/]"
    )


def _completed_channel_plan_markdown(
    plan_task: asyncio.Task[tuple[str, Any]],
) -> tuple[str | None, BaseException | None]:
    plan_markdown, error, _channel = _completed_channel_plan_markdown_with_channel(
        plan_task,
    )
    return plan_markdown, error


def _completed_channel_plan_markdown_with_channel(
    plan_task: asyncio.Task[tuple[str, Any]],
) -> tuple[str | None, BaseException | None, str | None]:
    try:
        channel_name, message = plan_task.result()
    except Exception as exc:
        return None, exc, None
    return _coerce_plan_markdown_handoff(message), None, channel_name


def _coerce_plan_markdown_handoff(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        return None
    plan_markdown = value.strip()
    if not plan_markdown:
        return None
    try:
        parse_reproduction_plan_markdown(plan_markdown)
    except ReproductionPlanMarkdownError as exc:
        _logger().debug("invalid Stage 1 Markdown handoff: %s", exc)
        return None
    return plan_markdown.rstrip() + "\n"


def _extract_reproduction_plan(
    transcript: str,
    *,
    issue_url: str | None = None,
    container_version: str | None = None,
    issue_thread: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    for fenced in _json_fenced_blocks(transcript):
        plan = _decode_reproduction_plan(
            fenced,
            issue_url=issue_url,
            container_version=container_version,
            issue_thread=issue_thread,
        )
        if plan is not None:
            return plan

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", transcript):
        try:
            value, _ = decoder.raw_decode(transcript[match.start() :])
        except json.JSONDecodeError:
            continue
        plan = _coerce_reproduction_plan(
            value,
            issue_url=issue_url,
            container_version=container_version,
            issue_thread=issue_thread,
        )
        if plan is not None:
            return plan
    return None


def _json_fenced_blocks(text: str) -> Iterable[str]:
    pattern = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
    for match in pattern.finditer(text):
        yield match.group(1).strip()


def _decode_reproduction_plan(
    value: str,
    *,
    issue_url: str | None = None,
    container_version: str | None = None,
    issue_thread: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return _coerce_reproduction_plan(
        decoded,
        issue_url=issue_url,
        container_version=container_version,
        issue_thread=issue_thread,
    )


def _coerce_reproduction_plan(
    value: Any,
    *,
    issue_url: str | None = None,
    container_version: str | None = None,
    issue_thread: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    value = _decode_jsonish(value)
    plan = _normalize_reproduction_plan(
        value,
        issue_url=issue_url,
        container_version=container_version,
        issue_thread=issue_thread,
    )
    if plan is not None:
        return plan
    if isinstance(value, dict):
        for key in ("plan", "reproduction_plan", "content", "payload", "data", "message"):
            if key not in value:
                continue
            plan = _coerce_reproduction_plan(
                value[key],
                issue_url=issue_url,
                container_version=container_version,
                issue_thread=issue_thread,
            )
            if plan is not None:
                return plan
    return None


def _normalize_reproduction_plan(
    value: Any,
    *,
    issue_url: str | None = None,
    container_version: str | None = None,
    issue_thread: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not _looks_like_reproduction_plan(
        value,
        allow_context=bool(issue_url and container_version),
    ):
        return None

    plan = dict(value)
    target_version = (
        plan.get("target_version")
        or plan.get("jellyfin_version")
        or _target_version_from_docker_image(plan.get("docker_image"))
        or container_version
    )
    if target_version is not None:
        plan["target_version"] = str(target_version)

    plan.setdefault("issue_url", issue_url)
    raw_server_target = plan.get("server_target")
    server_target = _normalize_plan_server_target(
        plan.get("server_target"),
        target_version=plan.get("target_version"),
    )
    if raw_server_target is not None or server_target.get("mode") == "demo":
        plan["server_target"] = server_target
    else:
        plan.pop("server_target", None)
    if (
        plan.get("target_version")
        and not plan.get("docker_image")
        and server_target.get("mode") != "demo"
    ):
        plan["docker_image"] = f"jellyfin/jellyfin:{plan['target_version']}"
    if server_target.get("mode") == "demo":
        plan.pop("docker_image", None)
        plan["execution_target"] = "web_client"
    plan.setdefault("issue_title", _issue_title(plan.get("issue_url"), issue_thread))
    plan.setdefault("prerequisites", [])
    plan.setdefault("failure_indicators", [])
    plan.setdefault("confidence", "medium")
    plan.setdefault("ambiguities", [])
    if plan.get("execution_target") not in {"standard", "web_client"}:
        plan["execution_target"] = "standard"
    plan.setdefault("is_verification", False)
    plan.setdefault("original_run_id", None)
    plan["environment"] = _normalize_plan_environment(plan.get("environment"))
    plan["reproduction_steps"] = _normalize_plan_steps(plan.get("reproduction_steps"))
    return plan


def _looks_like_reproduction_plan(value: Any, *, allow_context: bool = False) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("reproduction_steps"), list):
        return False
    has_version = (
        "target_version" in value
        or "jellyfin_version" in value
        or _target_version_from_docker_image(value.get("docker_image")) is not None
    )
    if allow_context:
        return has_version or any(
            key in value
            for key in ("issue_url", "docker_image", "reproduction_goal", "environment")
        )
    if not MINIMUM_REPRODUCTION_PLAN_KEYS.issubset(value):
        return False
    if not has_version:
        return False
    return True


def _target_version_from_docker_image(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    prefix = "jellyfin/jellyfin:"
    if not value.startswith(prefix):
        return None
    tag = value[len(prefix):].strip()
    return tag or None


def _normalize_plan_server_target(
    value: Any,
    *,
    target_version: Any = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"mode": "docker"}

    server_target = dict(value)
    mode = str(server_target.get("mode") or "docker").strip().lower()
    if mode != "demo":
        server_target["mode"] = "docker"
        return server_target

    release_track = _demo_release_track(
        server_target.get("release_track") or target_version
    )
    server_target["mode"] = "demo"
    server_target["release_track"] = release_track
    server_target["base_url"] = str(
        server_target.get("base_url") or DEMO_SERVER_URLS[release_track]
    )
    server_target.setdefault("username", "demo")
    server_target.setdefault("password", "")
    server_target.setdefault("requires_admin", False)
    return server_target


def _demo_release_track(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"unstable", "latest-unstable", "master"}:
        return "unstable"
    return "stable"


def _issue_title(issue_url: Any, issue_thread: dict[str, Any] | None = None) -> str:
    title = issue_thread.get("title") if isinstance(issue_thread, dict) else None
    if title:
        return str(title)
    return _issue_title_from_url(issue_url)


def _issue_title_from_url(issue_url: Any) -> str:
    text = str(issue_url or "").rstrip("/")
    issue_id = text.rsplit("/", 1)[-1] if text else ""
    return f"Issue {issue_id}" if issue_id else "Jellyfin issue"


def _normalize_plan_environment(value: Any) -> dict[str, Any]:
    environment = dict(value) if isinstance(value, dict) else {}
    environment["ports"] = _normalize_plan_ports(environment.get("ports"))
    environment["volumes"] = _normalize_plan_volumes(environment.get("volumes"))
    env_vars = environment.get("env_vars")
    environment["env_vars"] = dict(env_vars) if isinstance(env_vars, dict) else {}
    return environment


def _normalize_plan_ports(value: Any) -> dict[str, int]:
    if isinstance(value, dict):
        if "host" in value or "container" in value:
            return {
                "host": int(value.get("host", 8096)),
                "container": int(value.get("container", 8096)),
            }
        if value:
            container, host = next(iter(value.items()))
            return {
                "host": int(host),
                "container": int(str(container).split("/")[0]),
            }
    return {"host": 8096, "container": 8096}


def _normalize_plan_volumes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        volumes = []
        for host, spec in value.items():
            if not isinstance(spec, dict):
                continue
            container = spec.get("container") or spec.get("bind")
            if container:
                volumes.append(
                    {
                        "host": str(host),
                        "container": str(container),
                        "mode": str(spec.get("mode", "rw")),
                    }
                )
        return volumes
    return []


def _normalize_plan_steps(value: Any) -> list[dict[str, Any]]:
    steps = [dict(step) for step in value] if isinstance(value, list) else []
    trigger_index = next(
        (
            index
            for index, step in enumerate(steps)
            if step.get("role") == "trigger"
        ),
        None,
    )

    for index, step in enumerate(steps):
        step.setdefault("step_id", step.get("step", index + 1))
        step.setdefault("action", step.get("description") or f"Run step {index + 1}")
        if step.get("role") == "verification":
            step["role"] = "verify"
        if "role" not in step:
            step["role"] = _default_step_role(index, trigger_index)
        step.setdefault("expected_outcome", step.get("action", "Step completes"))
        if "input" in step:
            step["input"] = _normalize_step_input(step["input"], step.get("tool"))
        if isinstance(step.get("capture"), dict):
            step["capture"] = _normalize_step_capture(step["capture"])
        if isinstance(step.get("success_criteria"), dict):
            step["success_criteria"] = _normalize_success_criteria(
                step["success_criteria"]
            )
        else:
            step["success_criteria"] = _default_success_criteria(step.get("tool"))
        step.pop("step", None)
        step.pop("description", None)
    return steps


def _default_step_role(index: int, trigger_index: int | None) -> str:
    if trigger_index is None:
        return "trigger" if index == 0 else "verify"
    return "setup" if index < trigger_index else "verify"


def _normalize_step_input(value: Any, tool: Any) -> dict[str, Any]:
    if isinstance(value, str):
        if tool in {"bash", "docker_exec"}:
            step_input = {"command": value}
        elif tool == "http_request":
            step_input = {"path": value}
        elif tool == "screenshot":
            step_input = {"url": value}
        else:
            step_input = {"value": value}
    elif isinstance(value, dict):
        step_input = dict(value)
    else:
        step_input = {}
    if tool == "http_request" and "path" not in step_input and step_input.get("url"):
        parsed = urlparse(str(step_input["url"]))
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        step_input["path"] = path
        step_input.pop("url", None)
    if tool == "http_request" and isinstance(step_input.get("query"), dict):
        if "params" not in step_input:
            step_input["params"] = _normalize_query_params(step_input["query"])
        step_input.pop("query", None)
    if tool == "http_request":
        step_input.pop("url", None)
        step_input.setdefault("method", "GET")
        step_input.setdefault("auth", "auto")
    if tool in {"bash", "docker_exec"} and isinstance(step_input.get("command"), list):
        step_input["command"] = shlex.join(str(part) for part in step_input["command"])
    return step_input


def _normalize_query_params(value: dict[str, Any]) -> dict[str, Any]:
    return {key: _normalize_query_value(item) for key, item in value.items()}


def _normalize_query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return [_normalize_query_value(item) for item in value]
    return value


def _normalize_step_capture(value: dict[str, Any]) -> dict[str, Any]:
    capture = {}
    for key, expression in value.items():
        if isinstance(expression, str):
            capture[key] = {
                "from": "body_json_path",
                "path": _normalize_json_path(expression),
            }
        else:
            capture[key] = expression
    return capture


def _normalize_success_criteria(value: dict[str, Any]) -> dict[str, Any]:
    criteria = dict(value)
    for operator in ("all_of", "any_of"):
        assertions = criteria.get(operator)
        if isinstance(assertions, list):
            criteria[operator] = [
                _normalize_criteria_assertion(assertion)
                for assertion in assertions
            ]
    return criteria


def _default_success_criteria(tool: Any) -> dict[str, Any]:
    if tool == "http_request":
        return {"all_of": [{"type": "status_code", "in": [200, 204]}]}
    if tool == "screenshot":
        return {"all_of": [{"type": "screenshot_present", "label": "screenshot"}]}
    if tool == "browser":
        return {"all_of": [{"type": "browser_action_run"}]}
    return {"all_of": [{"type": "exit_code", "equals": 0}]}


def _normalize_criteria_assertion(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        legacy = _normalize_legacy_criteria_assertion(value)
        if legacy is not None:
            return legacy
    assertion = dict(value)
    operator = assertion.pop("operator", None)
    if assertion.get("type") == "bash_exit_code":
        assertion["type"] = "exit_code"
    if assertion.get("type") == "json_match":
        assertion["type"] = "body_json_path"
        assertion["path"] = _normalize_json_path(assertion.get("path"))
        if operator is None and "value" in assertion and "equals" not in assertion:
            assertion["equals"] = assertion.pop("value")
    if assertion.get("type") == "json_field":
        if operator == "exists":
            return {
                "type": "body_matches",
                "pattern": _json_path_existence_pattern(assertion.get("path")),
            }
        assertion["type"] = "body_json_path"
    if operator == "eq" and "value" in assertion:
        assertion["equals"] = assertion.pop("value")
    elif operator == "in" and "value" in assertion:
        assertion["in"] = assertion.pop("value")
    elif assertion.get("type") in {"status_code", "exit_code"} and "value" in assertion:
        assertion["equals"] = assertion.pop("value")
    return assertion


def _normalize_json_path(path: Any) -> str:
    text = str(path or "").strip()
    if not text:
        return "$"
    if text.startswith("$"):
        return text
    if text.startswith("["):
        return f"${text}"
    return f"$.{text}"


def _normalize_legacy_criteria_assertion(value: dict[str, Any]) -> dict[str, Any] | None:
    key, payload = next(iter(value.items()))
    if key in {
        "browser_action_run",
        "browser_element",
        "browser_text_contains",
        "browser_url_matches",
        "browser_media_state",
        "browser_console_matches",
    }:
        return _normalize_legacy_browser_assertion(key, payload)
    if key == "http_status":
        assertion_type = "status_code"
    elif key in {"exit_code", "body_contains", "stdout_contains", "stderr_contains"}:
        assertion_type = key
    else:
        return None

    if key in {"body_contains", "stdout_contains", "stderr_contains"}:
        return {"type": assertion_type, "value": str(payload)}
    if isinstance(payload, dict):
        if "==" in payload:
            return {"type": assertion_type, "equals": payload["=="]}
        if "in" in payload:
            return {"type": assertion_type, "in": payload["in"]}
    return {"type": assertion_type, "equals": payload}


def _normalize_legacy_browser_assertion(key: str, payload: Any) -> dict[str, Any]:
    if key == "browser_action_run":
        return {"type": "browser_action_run"}
    assertion = {"type": key}
    if isinstance(payload, dict):
        assertion.update(payload)
    elif key in {"browser_text_contains", "browser_media_state"}:
        field = "value" if key == "browser_text_contains" else "state"
        assertion[field] = str(payload)
    elif key == "browser_url_matches":
        assertion["pattern"] = str(payload)
    elif key == "browser_console_matches":
        assertion["pattern"] = str(payload)
    elif key == "browser_element":
        assertion["selector"] = str(payload)

    if (
        key == "browser_text_contains"
        and "value" not in assertion
        and "text" in assertion
    ):
        assertion["value"] = assertion.pop("text")
    if key == "browser_element":
        exists = assertion.pop("exists", None)
        visible = assertion.pop("visible", None)
        hidden = assertion.pop("hidden", None)
        if "state" not in assertion:
            if exists is True:
                assertion["state"] = "exists"
            elif visible is True:
                assertion["state"] = "visible"
            elif hidden is True:
                assertion["state"] = "hidden"
            elif exists is False:
                assertion["state"] = "detached"
            else:
                assertion["state"] = "visible"
    return assertion


def _json_path_existence_pattern(path: Any) -> str:
    field = str(path or "").rsplit(".", 1)[-1].strip("[]'\"")
    if not field or field == "$":
        return ".+"
    return rf'"{re.escape(field)}"\s*:'


def _apply_default_execution_budget(engine: Any) -> None:
    """Set the Stage 2 floor budget from the master plan when possible."""

    max_iterations, max_turns = execution_turn_budget(0)
    try:
        execution_agent = _get_agent(engine, "execution_agent")
    except Exception:
        return

    _set_if_present(execution_agent, "max_iterations", max_iterations)
    termination = getattr(execution_agent, "termination", None)
    if isinstance(termination, dict):
        termination["max_turns"] = max(termination.get("max_turns", 0), max_turns)
    else:
        _set_if_present(termination, "max_turns", max_turns)


def apply_execution_turn_budget(engine: Any, plan: dict[str, Any]) -> tuple[int, int]:
    """Apply the master-plan Stage 2 budget for a concrete ReproductionPlan.

    This is exposed for Terrarium hook integrations that can observe
    ``plan_ready`` without consuming the queue message.
    """

    steps = plan.get("reproduction_steps") if isinstance(plan, dict) else None
    max_iterations, max_turns = execution_turn_budget(
        len(steps) if isinstance(steps, list) else 0
    )
    execution_agent = _get_agent(engine, "execution_agent")
    _set_or_configure(execution_agent, "max_iterations", max_iterations)

    termination = getattr(execution_agent, "termination", None)
    if isinstance(termination, dict):
        termination["max_turns"] = max_turns
    else:
        _set_or_configure(termination, "max_turns", max_turns)
    return max_iterations, max_turns


async def _wait_for_terminal_message(engine: Any) -> tuple[str, Any]:
    pending = {
        asyncio.create_task(_receive_channel_message(engine, channel))
        for channel in TERMINAL_CHANNELS
    }
    errors: list[BaseException] = []
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            result: tuple[str, Any] | None = None
            for task in done:
                try:
                    result = task.result()
                except BaseException as exc:
                    errors.append(exc)
            if result is not None:
                return result
        messages = "; ".join(str(error) for error in errors) or "no channel listeners"
        raise RuntimeError(f"no terminal channel emitted a message: {messages}")
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def _receive_channel_message(engine: Any, channel_name: str) -> tuple[str, Any]:
    source = _channel_receive_source(engine, channel_name)
    message = await _first_message(source)
    return channel_name, _unwrap_message(message)


def _create_channel_observer_task(
    engine: Any,
    channel_name: str,
) -> asyncio.Task[tuple[str, Any]] | None:
    try:
        source = _channel_observe_source(engine, channel_name)
    except RuntimeError:
        _logger().debug("Channel %s cannot be observed without consuming it", channel_name)
        return None
    return asyncio.create_task(_first_observed_channel_message(channel_name, source))


def _create_first_channel_observer_task(
    engine: Any,
    channel_names: Iterable[str],
) -> asyncio.Task[tuple[str, Any]] | None:
    tasks = [
        task
        for channel_name in channel_names
        if (task := _create_channel_observer_task(engine, channel_name)) is not None
    ]
    if not tasks:
        return None
    if len(tasks) == 1:
        return tasks[0]
    return asyncio.create_task(_first_completed_channel_message(tasks))


def _create_first_channel_receive_task(
    engine: Any,
    channel_names: Iterable[str],
) -> asyncio.Task[tuple[str, Any]]:
    tasks = [
        asyncio.create_task(_receive_channel_message(engine, channel_name))
        for channel_name in channel_names
    ]
    return asyncio.create_task(_first_completed_channel_message(tasks))


async def _first_completed_channel_message(
    tasks: list[asyncio.Task[tuple[str, Any]]],
) -> tuple[str, Any]:
    pending = set(tasks)
    errors: list[BaseException] = []
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            result: tuple[str, Any] | None = None
            for task in done:
                try:
                    result = task.result()
                except BaseException as exc:
                    errors.append(exc)
            if result is not None:
                return result
        messages = "; ".join(str(error) for error in errors) or "no channel listeners"
        raise RuntimeError(f"no analysis plan channel emitted a message: {messages}")
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _first_observed_channel_message(
    channel_name: str,
    source: Any,
) -> tuple[str, Any]:
    message = await _first_message(source)
    return channel_name, _unwrap_message(message)


async def _observe_channel_send(channel: Any) -> Any:
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
    loop = asyncio.get_running_loop()

    def callback(_channel_name: str, message: Any = None) -> None:
        if message is None:
            message = _channel_name

        def enqueue() -> None:
            if queue.empty():
                queue.put_nowait(message)

        loop.call_soon_threadsafe(enqueue)

    unsubscribe = channel.on_send(callback)
    try:
        history = getattr(channel, "history", None)
        if history:
            return history[-1]
        return await queue.get()
    finally:
        if callable(unsubscribe):
            unsubscribe()
        else:
            remove = getattr(channel, "remove_on_send", None)
            if callable(remove):
                remove(callback)


def _channel_observe_source(engine: Any, channel_name: str) -> Any:
    channel = _get_channel(engine, channel_name)
    if channel is not None and callable(getattr(channel, "on_send", None)):
        return _observe_channel_send(channel)
    raise RuntimeError(f"{channel_name!r} channel cannot be observed")


def _channel_receive_source(engine: Any, channel_name: str) -> Any:
    channel = _get_channel(engine, channel_name)
    if channel is not None:
        if isinstance(channel, (str, bytes, dict)):
            return channel
        if callable(getattr(channel, "on_send", None)):
            return _observe_channel_send(channel)
        for method_name in ("receive", "recv", "get", "next"):
            method = getattr(channel, method_name, None)
            if callable(method):
                return method()
        for method_name in ("listen", "subscribe", "stream"):
            method = getattr(channel, method_name, None)
            if callable(method):
                return method()
        if _is_message_source(channel):
            return channel

    for method_name in ("receive", "recv", "get", "listen", "subscribe", "stream"):
        method = getattr(engine, method_name, None)
        if callable(method):
            try:
                return method(channel_name)
            except TypeError:
                continue

    raise RuntimeError(
        f"Terrarium engine does not expose a readable {channel_name!r} channel"
    )


def _get_channel(engine: Any, channel_name: str) -> Any | None:
    for method_name in ("channel", "get_channel"):
        method = getattr(engine, method_name, None)
        if callable(method):
            try:
                channel = method(channel_name)
            except (KeyError, AttributeError, TypeError):
                continue
            if channel is not None:
                return channel

    channels = getattr(engine, "channels", None)
    if callable(channels):
        try:
            channels = channels()
        except TypeError:
            channels = None
    if isinstance(channels, dict):
        return channels.get(channel_name)
    if channels is not None:
        try:
            return channels[channel_name]
        except (KeyError, TypeError, AttributeError):
            pass

    graph_channel = _get_graph_environment_channel(engine, channel_name)
    if graph_channel is not None:
        return graph_channel
    return None


def _get_graph_environment_channel(engine: Any, channel_name: str) -> Any | None:
    list_graphs = getattr(engine, "list_graphs", None)
    environments = getattr(engine, "_environments", None)
    if not callable(list_graphs) or not isinstance(environments, dict):
        return None

    try:
        graphs = list_graphs()
    except Exception:
        return None

    for graph in graphs:
        graph_id = getattr(graph, "graph_id", graph)
        environment = environments.get(graph_id)
        registry = getattr(environment, "shared_channels", None)
        if registry is None:
            continue

        get_channel = getattr(registry, "get", None)
        if callable(get_channel):
            channel = get_channel(channel_name)
            if channel is not None:
                return channel

        registry_channels = getattr(registry, "_channels", None)
        if isinstance(registry_channels, dict) and channel_name in registry_channels:
            return registry_channels[channel_name]
    return None


def _get_agent(engine: Any, name: str) -> Any:
    try:
        return engine[name]
    except (KeyError, TypeError):
        pass

    method = getattr(engine, "agent", None)
    if callable(method):
        return method(name)

    method = getattr(engine, "get_agent", None)
    if callable(method):
        return method(name)

    agents = getattr(engine, "agents", None)
    if isinstance(agents, dict) and name in agents:
        return agents[name]

    raise KeyError(f"Terrarium engine does not expose agent {name!r}")


def _optional_agent(engine: Any, name: str) -> Any | None:
    try:
        return _get_agent(engine, name)
    except KeyError:
        return None


def _suppress_default_output(creature_or_agent: Any) -> None:
    """Prevent KohakuTerrarium's default stdout output from duplicating chat.

    ``Creature.chat()`` streams text by adding a secondary output callback, but
    leaves the default output active. In this pipeline, ``run_issue`` also
    writes every yielded chat chunk to its own stream, so the default output must
    be muted to keep the transcript single-sourced.
    """

    agent = getattr(creature_or_agent, "agent", creature_or_agent)
    output_router = getattr(agent, "output_router", None)
    if output_router is None or not hasattr(output_router, "default_output"):
        return

    default_output = getattr(output_router, "default_output")
    if isinstance(default_output, _NullOutput):
        return

    setattr(output_router, "default_output", _NullOutput())


def _install_transcript_output(
    creature_or_agent: Any,
    *,
    on_write: Callable[[str], None] | None = None,
) -> tuple[_TranscriptOutput, Any, Any] | None:
    """Route visible default output into a transcript for Stage 1 debug runs."""

    agent = getattr(creature_or_agent, "agent", creature_or_agent)
    output_router = getattr(agent, "output_router", None)
    if output_router is None or not hasattr(output_router, "default_output"):
        return None

    default_output = getattr(output_router, "default_output")
    capture = _TranscriptOutput(on_write=on_write)
    setattr(output_router, "default_output", capture)
    return capture, output_router, default_output


def _restore_transcript_output(
    state: tuple[_TranscriptOutput, Any, Any] | None,
) -> None:
    if state is None:
        return
    _, output_router, default_output = state
    setattr(output_router, "default_output", default_output)


def _install_transcript_message_hooks(
    creature_or_agent: Any,
    transcript_writer: _AnalysisTranscriptWriter,
) -> list[tuple[Any, str, Any]]:
    states: list[tuple[Any, str, Any]] = []
    conversation = _agent_conversation(creature_or_agent)
    if conversation is not None:
        transcript_writer.record_conversation(
            conversation,
            source="conversation_initial",
        )
        for method_name in ("append", "append_message", "clear", "truncate_from"):
            original = getattr(conversation, method_name, None)
            if callable(original):
                setattr(
                    conversation,
                    method_name,
                    _conversation_hook(
                        original,
                        conversation,
                        transcript_writer,
                        method_name,
                    ),
                )
                states.append((conversation, method_name, original))

    llm = _agent_llm(creature_or_agent)
    chat = getattr(llm, "chat", None) if llm is not None else None
    if callable(chat):
        setattr(llm, "chat", _provider_chat_hook(chat, transcript_writer))
        states.append((llm, "chat", chat))

    return states


def _restore_transcript_message_hooks(states: list[tuple[Any, str, Any]]) -> None:
    for target, name, original in reversed(states):
        setattr(target, name, original)


def _conversation_hook(
    original: Callable[..., Any],
    conversation: Any,
    transcript_writer: _AnalysisTranscriptWriter,
    method_name: str,
) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        transcript_writer.record_conversation(
            conversation,
            source=f"conversation_{method_name}",
        )
        return result

    return wrapper


def _provider_chat_hook(
    original_chat: Callable[..., Any],
    transcript_writer: _AnalysisTranscriptWriter,
) -> Callable[..., Any]:
    async def wrapper(messages: Any, *args: Any, **kwargs: Any):
        transcript_writer.record_provider_request(messages)
        async for chunk in original_chat(messages, *args, **kwargs):
            transcript_writer.record_provider_chunk(_chunk_text(chunk))
            yield chunk

    return wrapper


def _assistant_conversation_text(creature_or_agent: Any) -> str:
    """Read the final assistant message from Kohaku's conversation model."""

    conversation = _agent_conversation(creature_or_agent)
    if conversation is None:
        return ""

    get_last = getattr(conversation, "get_last_assistant_message", None)
    message = get_last() if callable(get_last) else None
    if message is None:
        return ""
    return _message_content_text(getattr(message, "content", ""))


def _agent_system_prompt_text(creature_or_agent: Any) -> str | None:
    conversation = _agent_conversation(creature_or_agent)
    if conversation is None:
        return None

    get_system = getattr(conversation, "get_system_message", None)
    message = get_system() if callable(get_system) else None
    if message is None:
        return None
    text = _message_content_text(getattr(message, "content", ""))
    return text or None


def _agent_conversation(creature_or_agent: Any) -> Any | None:
    agent = getattr(creature_or_agent, "agent", creature_or_agent)
    controller = getattr(agent, "controller", None)
    return getattr(controller, "conversation", None)


def _agent_llm(creature_or_agent: Any) -> Any | None:
    agent = getattr(creature_or_agent, "agent", creature_or_agent)
    llm = getattr(agent, "llm", None)
    if llm is not None:
        return llm
    controller = getattr(agent, "controller", None)
    return getattr(controller, "llm", None)


def _message_content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return _chunk_text(content.get("text") or content.get("content") or "")
    if isinstance(content, Iterable) and not isinstance(content, (bytes, str)):
        parts = []
        for part in content:
            text = getattr(part, "text", None)
            if text is None and isinstance(part, dict):
                text = part.get("text")
            if text:
                parts.append(str(text))
        return "".join(parts)
    return _chunk_text(content)


def _has_transcript_text(text: Any) -> bool:
    return bool(_chunk_text(text).strip())


def _conversation_to_transcript_messages(conversation: Any) -> list[dict[str, Any]]:
    to_messages = getattr(conversation, "to_messages", None)
    if callable(to_messages):
        try:
            messages = _normalize_transcript_messages(to_messages())
        except Exception as exc:
            _logger().debug("Failed to serialize conversation messages: %s", exc)
        else:
            if messages:
                return messages

    get_messages = getattr(conversation, "get_messages", None)
    if callable(get_messages):
        try:
            return _normalize_transcript_messages(get_messages())
        except Exception as exc:
            _logger().debug("Failed to serialize raw conversation messages: %s", exc)
    return []


def _normalize_transcript_messages(messages: Any) -> list[dict[str, Any]]:
    if messages is None or isinstance(messages, (str, bytes, dict)):
        return []
    if not isinstance(messages, Iterable):
        return []

    result: list[dict[str, Any]] = []
    for item in messages:
        if isinstance(item, dict):
            message = dict(item)
        else:
            to_dict = getattr(item, "to_dict", None)
            if not callable(to_dict):
                continue
            message = dict(to_dict())
        role = message.get("role")
        if not role:
            continue
        message["role"] = str(role)
        if "content" not in message:
            message["content"] = ""
        result.append(_json_safe_value(_sanitize_transcript_message(message)))
    return result


_TRANSCRIPT_NOISY_KEYS = {
    "reasoning",
    "reasoning_details",
    "reasoning_content",
    "reasoning_metadata",
    "reasoning_summary",
    "reasoning_text",
}


def _sanitize_transcript_message(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _TRANSCRIPT_NOISY_KEYS or key_text.startswith("reasoning_"):
                continue
            sanitized[key] = _sanitize_transcript_message(item)
        return sanitized
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                item_type = str(item.get("type") or "")
                if item_type.startswith("reasoning"):
                    continue
            result.append(_sanitize_transcript_message(item))
        return result
    return value


def _json_safe_value(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _transcript_sources(*sources: tuple[str, str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for name, content in sources:
        if not content or not content.strip():
            continue
        if any(content == item["content"] for item in result):
            continue
        result.append({"source": name, "content": content})
    return result


def _merge_transcript_sources(*sources: str) -> str:
    merged: list[str] = []
    for source in sources:
        if not source:
            continue
        if any(source in existing for existing in merged):
            continue
        merged = [existing for existing in merged if existing not in source]
        merged.append(source)
    return "".join(merged)


def _analysis_transcript_payload(
    *,
    issue_url: str | None,
    container_version: str | None,
    prompt: str,
    issue_thread: dict[str, Any] | None,
    system_prompt: str | None,
    assistant_output: str,
    output_sources: list[dict[str, str]],
    stage: str = "analysis",
    input_metadata: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    status: str | None = None,
    provider_requests: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload_messages = messages
    if payload_messages is None:
        payload_messages = []
        if system_prompt:
            payload_messages.append(
                {
                    "role": "system",
                    "content": system_prompt,
                }
            )
        payload_messages.extend(
            [
                {
                    "role": "user",
                    "content": prompt,
                },
                {
                    "role": "assistant",
                    "content": assistant_output,
                },
            ]
        )
    input_payload: dict[str, Any] = {
        "system_prompt": system_prompt,
        "prompt": prompt,
        "issue_url": issue_url,
        "container_version": container_version,
        "prefetched_issue_thread": issue_thread,
    }
    if input_metadata:
        input_payload.update(_json_safe_value(input_metadata))

    payload = {
        "stage": stage,
        "schema_version": 1,
        "issue_url": issue_url,
        "container_version": container_version,
        "input": input_payload,
        "output": {
            "assistant": assistant_output,
            "sources": output_sources,
        },
        "messages": payload_messages,
    }
    if status is not None:
        payload["status"] = status
    if provider_requests is not None:
        payload["provider_requests"] = provider_requests
    if events is not None:
        payload["events"] = events
    return payload


async def _collect_analysis_chat(
    agent: Any,
    prompt: str,
    transcript_parts: list[str],
    *,
    transcript_capture: "_TranscriptOutput | None" = None,
    transcript_writer: "_AnalysisTranscriptWriter | None" = None,
) -> None:
    async for chunk in _chat(agent, prompt):
        text = _chunk_text(chunk)
        if transcript_capture is None or not transcript_capture.has_text:
            transcript_parts.append(text)
            if transcript_writer is not None and not transcript_writer.has_provider_traffic:
                transcript_writer.record_assistant_text(text, source="chat_chunks")


async def _wait_for_analysis_completion_signal(
    *,
    chat_task: asyncio.Task[None],
    plan_observer_task: asyncio.Task[tuple[str, Any]] | None,
    analysis_agent: Any,
) -> None:
    if plan_observer_task is None:
        await chat_task
        return

    done, _pending = await asyncio.wait(
        {chat_task, plan_observer_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if plan_observer_task in done:
        try:
            plan_observer_task.result()
        except Exception as exc:
            _logger().debug("plan_ready observer failed: %s", exc)
            if not chat_task.done():
                await chat_task
            return

        _logger().info("Observed plan_ready; stopping Stage 1 analysis")
        await _stop_analysis_agent(analysis_agent)
        if not chat_task.done():
            chat_task.cancel()
            await _cancel_task(chat_task)
        return

    await chat_task
    if not plan_observer_task.done():
        plan_observer_task.cancel()
        await _cancel_task(plan_observer_task)


async def _stop_analysis_agent(creature_or_agent: Any) -> None:
    for target in (creature_or_agent, getattr(creature_or_agent, "agent", None)):
        if target is None:
            continue
        stop = getattr(target, "stop", None)
        if callable(stop):
            try:
                await _maybe_await(stop())
            except Exception as exc:
                _logger().debug("Failed to stop analysis agent: %s", exc)
            return


async def _chat(agent: Any, prompt: str):
    chat = getattr(agent, "chat", None)
    if not callable(chat):
        raise TypeError("analysis_agent does not expose chat(prompt)")

    source = chat(prompt)
    if inspect.isawaitable(source) and not _is_message_source(source):
        source = await source
    async for item in _iterate_messages(source):
        yield item


async def _first_message(source: Any) -> Any:
    if inspect.isawaitable(source) and not _is_message_source(source):
        source = await source
    async for item in _iterate_messages(source):
        return item
    raise RuntimeError("channel closed without emitting a message")


async def _iterate_messages(source: Any):
    if hasattr(source, "__aiter__"):
        async for item in source:
            yield item
        return
    if inspect.isawaitable(source):
        yield await source
        return
    if isinstance(source, (str, bytes, dict)):
        yield source
        return
    if isinstance(source, Iterable):
        for item in source:
            yield item
        return
    yield source


def _unwrap_message(message: Any) -> Any:
    for attr in ("content", "payload", "data", "message"):
        if hasattr(message, attr):
            return _unwrap_message(getattr(message, attr))

    if isinstance(message, dict):
        if PIPELINE_PAYLOAD_KEYS.intersection(message):
            return message
        for key in ("content", "payload", "data", "message"):
            if key in message:
                return _decode_jsonish(message[key])
    return _decode_jsonish(message)


def _decode_jsonish(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _pipeline_result(
    channel: str,
    message: Any,
    analysis_output: str,
) -> PipelineResult:
    payload = message if isinstance(message, dict) else {}
    return PipelineResult(
        channel=channel,
        message=message,
        report_path=_optional_text(payload.get("report_path")),
        run_id=_optional_text(payload.get("run_id")),
        verification_run_id=_optional_text(payload.get("verification_run_id")),
        overall_result=_optional_text(payload.get("overall_result")),
        verified=payload.get("verified") if isinstance(payload.get("verified"), bool) else None,
        issue_url=_optional_text(payload.get("issue_url")),
        analysis_output=analysis_output,
    )


def _print_terminal_summary(result: PipelineResult, stream: TextIO) -> None:
    if result.channel == "final_report":
        stream.write(f"Final report: {result.report_path or result.message}\n")
    else:
        stream.write(f"Queued for human review: {result.report_path or result.message}\n")
    stream.flush()


def _is_insufficient_information(transcript: str) -> bool:
    return "INSUFFICIENT_INFORMATION" in transcript and "REPRODUCTION_PLAN_COMPLETE" not in transcript


def _extract_insufficient_information_summary(transcript: str) -> str:
    text = transcript.replace("\r\n", "\n").strip()
    marker_index = text.rfind("INSUFFICIENT_INFORMATION")
    if marker_index < 0:
        return text
    return text[marker_index:].strip()


def _write_insufficient_information_summary(
    output_dir: Path,
    *,
    summary_text: str,
    issue_url: str,
    container_version: str,
    issue_thread: dict[str, Any] | None,
) -> Path:
    path = output_dir / INSUFFICIENT_INFORMATION_SUMMARY_FILE
    path.write_text(
        _render_insufficient_information_summary(
            summary_text,
            issue_url=issue_url,
            container_version=container_version,
            issue_thread=issue_thread,
        ),
        encoding="utf-8",
    )
    return path


def _render_insufficient_information_summary(
    summary_text: str,
    *,
    issue_url: str,
    container_version: str,
    issue_thread: dict[str, Any] | None,
) -> str:
    lines = [
        "# Insufficient Information",
        "",
        f"- Issue: {issue_url}",
        f"- Target Jellyfin version: {container_version}",
    ]
    if isinstance(issue_thread, dict) and issue_thread.get("title"):
        lines.append(f"- Issue title: {issue_thread['title']}")
    lines.extend(
        [
            "",
            "## Summary",
            "",
            summary_text.strip() or "Stage 1 stopped before producing a reproduction plan.",
            "",
            "Full Stage 1 transcript: `transcript.json`",
            "",
        ]
    )
    return "\n".join(lines)


def _chunk_text(chunk: Any) -> str:
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, dict):
        for key in ("text", "content", "delta"):
            if key in chunk:
                return str(chunk[key])
    return str(chunk)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        return


def _is_message_source(value: Any) -> bool:
    return hasattr(value, "__aiter__") or (
        hasattr(value, "__iter__") and not isinstance(value, (str, bytes, dict))
    )


def _set_if_present(target: Any, name: str, value: Any) -> None:
    if target is not None and hasattr(target, name):
        current = getattr(target, name)
        try:
            value = max(current, value)
        except TypeError:
            pass
        setattr(target, name, value)


def _set_or_configure(target: Any, name: str, value: Any) -> None:
    if target is None:
        return
    if isinstance(target, dict):
        target[name] = value
        return
    if hasattr(target, name):
        setattr(target, name, value)
        return
    config = getattr(target, "config", None)
    if isinstance(config, dict):
        config[name] = value


def _prepare_output_dir(path: str | Path) -> Path:
    output_dir = Path(path).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _resolve_stage_input_file(path: str | Path, producer_stage: str) -> Path:
    input_path = Path(path).expanduser()
    candidates = STAGE_HANDOFF_FILES[producer_stage]
    if input_path.is_file():
        return input_path.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"stage input does not exist: {input_path}")
    if not input_path.is_dir():
        raise ValueError(f"stage input must be a file or directory: {input_path}")
    for candidate in candidates:
        file_path = input_path / candidate
        if file_path.is_file():
            return file_path.resolve()
    expected = ", ".join(candidates)
    raise FileNotFoundError(f"{input_path} does not contain one of: {expected}")


def _read_json_file(path: str | Path) -> Any:
    with Path(path).expanduser().open(encoding="utf-8") as handle:
        return json.load(handle)


def _read_text_file(path: str | Path) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8")


def _write_json_file(path: str | Path, value: Any) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.tmp")
    temp_path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(target)
    return target


def _write_text_file(path: str | Path, value: str) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.tmp")
    temp_path.write_text(value.rstrip() + "\n", encoding="utf-8")
    temp_path.replace(target)
    return target


def _write_stage_result(result: StageDebugResult) -> StageDebugResult:
    _write_json_file(
        Path(result.output_dir) / "stage_result.json",
        asdict(result),
    )
    return result


def _mirror_report(source: Any, output_dir: Path) -> Path:
    if not source:
        raise ValueError("report_writer did not return a report path")
    source_path = Path(str(source)).expanduser()
    target = output_dir / "report.md"
    if source_path.resolve() != target.resolve():
        target.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Jellyfin issue reproduction pipeline."
    )
    parser.add_argument(
        "--stage",
        choices=STAGE_CHOICES,
        help="Run a single stage with disk handoff files instead of the full pipeline",
    )
    parser.add_argument("issue_url", nargs="?", help="GitHub issue URL to reproduce")
    parser.add_argument(
        "container_version",
        nargs="?",
        help="Jellyfin container tag, e.g. 10.9.7",
    )
    parser.add_argument(
        "--recipe",
        default=str(DEFAULT_RECIPE_PATH),
        help="Path to terrarium.yaml",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=60 * 60,
        help="Seconds to wait for final_report or human_review_queue",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not stream analysis-agent output",
    )
    _add_logging_args(parser)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the terminal PipelineResult as JSON",
    )
    return parser


def _normalize_stage_argv(argv: list[str]) -> list[str] | None:
    for arg in argv:
        if arg == "--stage" or arg.startswith("--stage="):
            return argv
    return None


def _parse_stage_choice(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--stage", choices=STAGE_CHOICES, required=True)
    args, _ = parser.parse_known_args(argv)
    return args.stage


def _build_stage_parser(stage: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"main.py --stage {stage}",
        description="Run one pipeline stage with disk handoff files.",
    )
    parser.add_argument("--stage", choices=STAGE_CHOICES, required=True)
    if stage == "analysis":
        parser.add_argument("issue_url", help="GitHub issue URL to analyze")
        parser.add_argument("container_version", help="Jellyfin container tag")
        parser.add_argument("--out", required=True, help="Output handoff directory")
        parser.add_argument(
            "--timeout-s",
            type=float,
            default=10 * 60,
            help="Seconds to wait for plan_ready",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Do not stream analysis-agent output",
        )
    elif stage in {"execution", "web-client"}:
        parser.add_argument(
            "--input",
            required=True,
            help="Stage 1 handoff folder or ReproductionPlan Markdown file",
        )
        parser.add_argument("--out", required=True, help="Output handoff directory")
        parser.add_argument("--run-id", help="Optional deterministic run id")
        parser.add_argument(
            "--timeout-s",
            type=float,
            default=10 * 60,
            help="Seconds to wait for execution_done",
        )
    elif stage == "report":
        parser.add_argument(
            "--input",
            required=True,
            help="Stage 2 handoff folder or ExecutionResult JSON file",
        )
        parser.add_argument("--out", required=True, help="Output handoff directory")
        parser.add_argument(
            "--verification-result",
            help="Optional Stage 2 verification handoff folder or result JSON file",
        )
    else:  # pragma: no cover - guarded by _parse_stage_choice
        raise ValueError(f"unknown stage: {stage}")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print StageDebugResult as JSON",
    )
    _add_logging_args(parser)
    return parser


def _add_logging_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-level",
        type=_parse_log_level,
        default=_default_log_level(),
        help=(
            "Log level for KohakuTerrarium and pipeline logs "
            f"(default: ${'JF_AUTO_TESTER_LOG_LEVEL'} or {DEFAULT_LOG_LEVEL})"
        ),
    )
    parser.add_argument(
        "--log-stderr",
        type=_parse_log_stderr,
        default=_default_log_stderr(),
        help=(
            "Mirror logs to stderr: auto, on, or off "
            f"(default: ${'JF_AUTO_TESTER_LOG_STDERR'} or {DEFAULT_LOG_STDERR})"
        ),
    )


async def _async_main(argv: list[str] | None = None) -> int:
    load_env_file()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "stage":
        parser = _build_parser()
        parser.error("stage mode is selected with --stage, e.g. --stage analysis")

    stage_argv = _normalize_stage_argv(raw_argv)
    if stage_argv is not None:
        return await _async_stage_main(stage_argv)

    parser = _build_parser()
    args = parser.parse_args(raw_argv)
    if not args.issue_url or not args.container_version:
        parser.error("issue_url and container_version are required unless --stage is used")
    configure_runtime_logging(args.log_level, args.log_stderr)
    result = await run_issue(
        args.issue_url,
        args.container_version,
        recipe_path=args.recipe,
        timeout_s=args.timeout_s,
        stream=None if args.quiet or args.json else sys.stdout,
    )
    if args.json:
        print(json.dumps(asdict(result), indent=2, sort_keys=True, default=str))
    return 0 if result.status in {"complete", "human_review", "insufficient_information"} else 1


async def _async_stage_main(argv: list[str]) -> int:
    stage = _parse_stage_choice(argv)
    args = _build_stage_parser(stage).parse_args(argv)
    configure_runtime_logging(args.log_level, args.log_stderr)
    if args.stage == "analysis":
        result = await run_analysis_stage(
            args.issue_url,
            args.container_version,
            args.out,
            timeout_s=args.timeout_s,
            stream=None if args.quiet or args.json else sys.stdout,
        )
    elif args.stage == "execution":
        result = run_execution_stage(
            args.input,
            args.out,
            run_id=args.run_id,
            timeout_s=args.timeout_s,
        )
    elif args.stage == "web-client":
        result = run_web_client_stage(
            args.input,
            args.out,
            run_id=args.run_id,
            timeout_s=args.timeout_s,
        )
    elif args.stage == "report":
        result = run_report_stage(
            args.input,
            args.out,
            verification_result_path=args.verification_result,
        )
    else:  # pragma: no cover - argparse enforces choices
        raise ValueError(f"unknown stage: {args.stage}")

    if args.json:
        print(json.dumps(asdict(result), indent=2, sort_keys=True, default=str))
    else:
        output = f" -> {result.output_file}" if result.output_file else ""
        print(f"{result.stage}: {result.status}{output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
