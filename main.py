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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO
from urllib.parse import urlparse
from uuid import uuid4

from stage1_model_blacklist import is_stage1_model_blacklisted


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_RECIPE_PATH = REPO_ROOT / "terrarium.yaml"
DEFAULT_DOTENV_PATH = REPO_ROOT / ".env"
DEFAULT_ANALYSIS_CONFIG_PATH = REPO_ROOT / "creatures" / "analysis" / "config.yaml"
TERMINAL_CHANNELS = ("final_report", "human_review_queue")
STAGE_CHOICES = ("analysis", "execution", "report")
STAGE_CHANNEL_GRACE_S = 2.0
ANALYSIS_TRANSCRIPT_FILE = "transcript.json"
LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_STDERR_CHOICES = ("auto", "on", "off")
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_STDERR = "auto"
ANALYSIS_PLAN_MAX_ATTEMPTS = 3
STAGE_HANDOFF_FILES = {
    "analysis": ("plan.json", "reproduction_plan.json"),
    "execution": ("result.json", "execution_result.json"),
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
    "docker_image",
    "reproduction_steps",
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


@dataclass(slots=True)
class _OutputChannelMessage:
    """Local ChannelMessage-compatible payload for Terrarium channels."""

    sender: str
    content: str | dict | list[dict]
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    message_id: str = field(default_factory=lambda: f"msg_{uuid4().hex[:12]}")
    reply_to: str | None = None
    channel: str | None = None


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

    def __init__(self) -> None:
        self._parts: list[str] = []

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
        if not text:
            return
        self._parts.append(text)


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
        get_logger(LOGGER_NAME)
    finally:
        if kt_log_stderr is not None:
            os.environ["KT_LOG_STDERR"] = kt_log_stderr
    set_level(level)
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
    _bind_channel_named_outputs(engine)
    _apply_default_execution_budget(engine)

    terminal_task = asyncio.create_task(_wait_for_terminal_message(engine))
    plan_observer_task = _create_channel_observer_task(engine, "plan_ready")
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
                    _logger().debug("plan_ready observer failed: %s", exc)
                    observer_failed = True
                    if not chat_task.done():
                        await chat_task
                    break

                _logger().info("Observed plan_ready; stopping Stage 1 analysis")
                await _stop_analysis_agent(analysis_agent)
                if not chat_task.done():
                    chat_task.cancel()
                    await _cancel_task(chat_task)
                chat_task = None
                plan_ready_observed = True
                break

            await chat_task
            chat_task = None
            transcript = "".join(analysis_output)
            if _is_insufficient_information(transcript):
                logger.info("Stage 1 stopped with insufficient information")
                terminal_task.cancel()
                await _cancel_task(terminal_task)
                return PipelineResult(
                    channel="analysis",
                    message=transcript.strip(),
                    issue_url=issue_url,
                    analysis_output=transcript,
                )

            if attempt >= ANALYSIS_PLAN_MAX_ATTEMPTS:
                break

            logger.warning(
                "Rejecting Stage 1 attempt %s/%s: no plan_ready output observed",
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
    - ``plan.json``: ReproductionPlan payload for Stage 2 when available.
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
    _bind_channel_named_outputs(engine)
    plan_task = asyncio.create_task(_receive_channel_message(engine, "plan_ready"))
    await asyncio.sleep(0)
    transcript_parts: list[str] = []
    chat_task: asyncio.Task[None] | None = None
    try:
        analysis_agent = _get_agent(engine, "analysis_agent")
        transcript_capture_state = _install_transcript_output(analysis_agent)
        transcript_capture = (
            transcript_capture_state[0] if transcript_capture_state is not None else None
        )
        prompt = _analysis_prompt(issue_url, container_version, issue_thread)
        system_prompt = _agent_system_prompt_text(analysis_agent)
        channel_plan: dict[str, Any] | None = None
        plan: dict[str, Any] | None = None
        plan_source: str | None = None
        transcript = ""
        current_prompt = prompt
        try:
            for attempt in range(1, ANALYSIS_PLAN_MAX_ATTEMPTS + 1):
                logger.info(
                    "Capturing Stage 1 analysis transcript attempt %s/%s",
                    attempt,
                    ANALYSIS_PLAN_MAX_ATTEMPTS,
                )
                chat_task = asyncio.create_task(
                    _collect_analysis_chat(
                        analysis_agent,
                        current_prompt,
                        transcript_parts,
                        transcript_capture=transcript_capture,
                    )
                )
                done, _pending = await asyncio.wait(
                    {chat_task, plan_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if plan_task in done:
                    channel_plan, _channel_error = _completed_channel_plan(
                        plan_task,
                        issue_url=issue_url,
                        container_version=container_version,
                        issue_thread=issue_thread,
                    )
                    await _stop_analysis_agent(analysis_agent)
                    if not chat_task.done():
                        chat_task.cancel()
                        await _cancel_task(chat_task)
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
                _write_json_file(
                    output_dir / ANALYSIS_TRANSCRIPT_FILE,
                    _analysis_transcript_payload(
                        issue_url=issue_url,
                        container_version=container_version,
                        prompt=prompt,
                        issue_thread=issue_thread,
                        system_prompt=system_prompt,
                        assistant_output=transcript,
                        output_sources=transcript_sources,
                    ),
                )

                if _is_insufficient_information(transcript):
                    logger.info("Stage 1 debug run stopped with insufficient information")
                    plan_task.cancel()
                    await _cancel_task(plan_task)
                    return _write_stage_result(
                        StageDebugResult(
                            stage="analysis",
                            status="insufficient_information",
                            output_dir=str(output_dir),
                            output_file=ANALYSIS_TRANSCRIPT_FILE,
                            metadata={"message": transcript.strip()},
                        )
                    )

                plan = channel_plan
                plan_source = "channel" if channel_plan is not None else None
                if plan is None:
                    plan, plan_source = await _resolve_analysis_stage_plan(
                        plan_task,
                        transcript,
                        issue_url=issue_url,
                        container_version=container_version,
                        issue_thread=issue_thread,
                        timeout_s=timeout_s,
                    )
                if plan is not None:
                    break

                if attempt >= ANALYSIS_PLAN_MAX_ATTEMPTS:
                    break

                logger.warning(
                    "Rejecting Stage 1 attempt %s/%s: no plan_ready output received",
                    attempt,
                    ANALYSIS_PLAN_MAX_ATTEMPTS,
                )
                if plan_task.done():
                    channel_plan, _channel_error = _completed_channel_plan(
                        plan_task,
                        issue_url=issue_url,
                        container_version=container_version,
                        issue_thread=issue_thread,
                    )
                    if channel_plan is not None:
                        plan = channel_plan
                        plan_source = "channel"
                        break
                    plan_task = asyncio.create_task(
                        _receive_channel_message(engine, "plan_ready")
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
        if plan is None:
            plan_task.cancel()
            await _cancel_task(plan_task)
            _raise_analysis_plan_protocol_error(
                transcript,
                attempts=ANALYSIS_PLAN_MAX_ATTEMPTS,
            )

        _write_json_file(output_dir / "plan.json", plan)
        logger.info("Stage 1 debug run wrote plan.json from %s", plan_source)
        return _write_stage_result(
            StageDebugResult(
                stage="analysis",
                status="plan_ready",
                output_dir=str(output_dir),
                output_file="plan.json",
                metadata={"transcript": ANALYSIS_TRANSCRIPT_FILE, "source": plan_source},
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
    runner_factory: Callable[[Path], Any] | None = None,
) -> StageDebugResult:
    """Run Stage 2 only from a ReproductionPlan file or folder."""

    load_env_file()
    logger = _logger()
    logger.info("Starting Stage 2 debug run from %s", input_path)
    output_dir = _prepare_output_dir(out_dir)
    plan_path = _resolve_stage_input_file(input_path, "analysis")
    plan = _read_json_file(plan_path)
    _write_json_file(output_dir / "plan.json", plan)

    if runner_factory is None:
        from creatures.execution.tools.execution_runner import ExecutionRunner

        runner = ExecutionRunner(artifacts_root=output_dir)
    else:
        runner = runner_factory(output_dir)

    result = runner.execute_plan(plan=plan, run_id=run_id)
    _write_json_file(output_dir / "result.json", result)
    logger.info("Stage 2 debug run wrote result.json")
    return _write_stage_result(
        StageDebugResult(
            stage="execution",
            status=str(result.get("overall_result", "complete")),
            output_dir=str(output_dir),
            output_file="result.json",
            metadata={
                "input": str(plan_path),
                "run_id": result.get("run_id"),
                "artifacts_dir": result.get("artifacts_dir"),
            },
        )
    )


def run_report_stage(
    input_path: str | Path,
    out_dir: str | Path,
    *,
    verification_result_path: str | Path | None = None,
) -> StageDebugResult:
    """Run Stage 3's file-based report handoff.

    With only a first-run ``result.json``, this writes a draft ``report.md`` and
    ``verification_plan.json`` that can be passed to Stage 2. With
    ``verification_result_path``, this writes the verified/final routing
    metadata as ``final_report.json`` or ``human_review_queue.json``.
    """

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

    from creatures.report.tools import report_writer

    written_steps = _debug_report_steps(execution_result)
    metadata = report_writer.generate(
        execution_result,
        verification_result,
        artifacts_base=output_dir,
        written_steps=written_steps,
    )
    report_file = _mirror_report(metadata.get("path"), output_dir)
    metadata["path"] = str(report_file)
    _write_json_file(output_dir / "report_metadata.json", metadata)
    _write_json_file(output_dir / "execution_result.json", execution_result)

    if verification_result is not None:
        _write_json_file(output_dir / "verification_result.json", verification_result)
        route = "final_report" if metadata.get("verified") is True else "human_review_queue"
        route_payload = dict(metadata)
        if route == "human_review_queue":
            route_payload.setdefault("reason", "verification failed or was inconsistent")
        route_file = output_dir / f"{route}.json"
        _write_json_file(route_file, route_payload)
        logger.info("Stage 3 debug run wrote %s", route_file.name)
        return _write_stage_result(
            StageDebugResult(
                stage="report",
                status=route,
                output_dir=str(output_dir),
                output_file=route_file.name,
                metadata={"report": "report.md", "verified": metadata.get("verified")},
            )
        )

    if execution_result.get("is_verification"):
        logger.info("Stage 3 debug run wrote report.md")
        return _write_stage_result(
            StageDebugResult(
                stage="report",
                status="report_written",
                output_dir=str(output_dir),
                output_file="report.md",
                metadata={"report_metadata": "report_metadata.json"},
            )
        )

    verification_plan = report_writer.build_verification_plan(execution_result, written_steps)
    _write_json_file(output_dir / "verification_plan.json", verification_plan)
    logger.info("Stage 3 debug run wrote report.md and verification_plan.json")
    return _write_stage_result(
        StageDebugResult(
            stage="report",
            status="verification_ready",
            output_dir=str(output_dir),
            output_file="verification_plan.json",
            metadata={"report": "report.md", "report_metadata": "report_metadata.json"},
        )
    )


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
) -> Any:
    if engine_factory is not None:
        return await _maybe_await(engine_factory(stage))
    if stage != "analysis":
        raise ValueError(f"no isolated Terrarium recipe is defined for stage {stage!r}")

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
            "kohakuterrarium is required to run Stage 1. "
            "Install it in this environment, then rerun main.py."
        ) from exc

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
                send_channels=["plan_ready"],
            )
        ],
        channels=[ChannelConfig(name="plan_ready", channel_type="queue")],
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
    if issue_fetcher is None:
        issue_thread = await asyncio.to_thread(
            fetcher,
            issue_url=issue_url,
            include_comments=True,
            include_linked=True,
        )
    else:
        issue_thread = fetcher(
            issue_url=issue_url,
            include_comments=True,
            include_linked=True,
        )
        issue_thread = await _maybe_await(issue_thread)

    if not isinstance(issue_thread, dict):
        raise TypeError("issue prefetcher must return a dictionary")
    return issue_thread


def _default_issue_fetcher() -> Callable[..., Any]:
    from creatures.analysis.tools.github_fetcher import github_fetcher

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
        "`ReproductionPlan` to the `plan_ready` named output. You may have "
        "claimed that the plan was sent, but the runner did not receive an "
        "`[/output_plan_ready]... [output_plan_ready/]` block with the actual "
        "JSON payload.\n\n"
        f"This is attempt {attempt} of {max_attempts}. Reply with no prose, no "
        "summary, and no tool calls. Your entire response must be exactly one "
        "named output block in this form:\n\n"
        "[/output_plan_ready]\n"
        "{ ... valid ReproductionPlan JSON ... }\n"
        "[output_plan_ready/]\n\n"
        "Do not say the plan has been sent. Do not use `send_message`. If an "
        "executable plan cannot be produced, reply with "
        "`INSUFFICIENT_INFORMATION` and the missing details instead."
    )


async def _resolve_analysis_stage_plan(
    plan_task: asyncio.Task[tuple[str, Any]],
    _transcript: str,
    *,
    issue_url: str | None = None,
    container_version: str | None = None,
    issue_thread: dict[str, Any] | None = None,
    timeout_s: float | None,
) -> tuple[dict[str, Any] | None, str | None]:
    channel_error: BaseException | None = None
    grace_s = _analysis_channel_grace(timeout_s)

    if plan_task.done():
        plan, channel_error = _completed_channel_plan(
            plan_task,
            issue_url=issue_url,
            container_version=container_version,
            issue_thread=issue_thread,
        )
        if plan is not None:
            return plan, "channel"

    if not plan_task.done():
        try:
            _, message = await asyncio.wait_for(
                asyncio.shield(plan_task),
                timeout=grace_s,
            )
            plan = _coerce_reproduction_plan(
                message,
                issue_url=issue_url,
                container_version=container_version,
                issue_thread=issue_thread,
            )
            if plan is not None:
                return plan, "channel"
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            channel_error = exc

    if channel_error is not None and not plan_task.done():
        plan_task.cancel()
        await _cancel_task(plan_task)
    return None, None


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
            "analysis agent returned an empty response without emitting plan_ready "
            f"after {attempts} attempts"
        )
    raise AnalysisAgentProtocolError(
        f"analysis agent failed to emit plan_ready after {attempts} attempts; "
        "final Stage 1 output must use "
        "[/output_plan_ready]...[output_plan_ready/]"
    )


def _completed_channel_plan(
    plan_task: asyncio.Task[tuple[str, Any]],
    *,
    issue_url: str | None = None,
    container_version: str | None = None,
    issue_thread: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, BaseException | None]:
    try:
        _, message = plan_task.result()
    except Exception as exc:
        return None, exc
    return (
        _coerce_reproduction_plan(
            message,
            issue_url=issue_url,
            container_version=container_version,
            issue_thread=issue_thread,
        ),
        None,
    )


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
        or container_version
    )
    if target_version is not None:
        plan["target_version"] = str(target_version)

    plan.setdefault("issue_url", issue_url)
    if plan.get("target_version") and not plan.get("docker_image"):
        plan["docker_image"] = f"jellyfin/jellyfin:{plan['target_version']}"
    plan.setdefault("issue_title", _issue_title(plan.get("issue_url"), issue_thread))
    plan.setdefault("prerequisites", [])
    plan.setdefault("failure_indicators", [])
    plan.setdefault("confidence", "medium")
    plan.setdefault("ambiguities", [])
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
    if allow_context:
        return "target_version" in value or "jellyfin_version" in value
    if not MINIMUM_REPRODUCTION_PLAN_KEYS.issubset(value):
        return False
    if "target_version" not in value and "jellyfin_version" not in value:
        return False
    return True


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
        if isinstance(step.get("input"), dict):
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


def _normalize_step_input(value: dict[str, Any], tool: Any) -> dict[str, Any]:
    step_input = dict(value)
    if tool == "http_request" and "path" not in step_input and step_input.get("url"):
        parsed = urlparse(str(step_input["url"]))
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        step_input["path"] = path
    if tool in {"bash", "docker_exec"} and isinstance(step_input.get("command"), list):
        step_input["command"] = shlex.join(str(part) for part in step_input["command"])
    return step_input


def _normalize_step_capture(value: dict[str, Any]) -> dict[str, Any]:
    capture = {}
    for key, expression in value.items():
        if isinstance(expression, str) and expression.startswith("$"):
            capture[key] = {"from": "body_json_path", "path": expression}
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


def _normalize_legacy_criteria_assertion(value: dict[str, Any]) -> dict[str, Any] | None:
    key, payload = next(iter(value.items()))
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


class _ChannelOutput:
    """Named output adapter that writes output blocks to a Terrarium channel."""

    def __init__(self, channel: Any, *, channel_name: str, sender: str) -> None:
        self._channel = channel
        self._channel_name = channel_name
        self._sender = sender

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def write(self, content: str) -> None:
        message = _OutputChannelMessage(sender=self._sender, content=content)
        await _maybe_await(self._channel.send(message))

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

    async def on_user_input(self, _text: str) -> None:
        pass


def _bind_channel_named_outputs(engine: Any) -> None:
    """Bind ``type: channel`` named outputs to Terrarium shared channels.

    KohakuTerrarium's generic output factory does not currently ship a builtin
    ``channel`` output type, so those entries otherwise fall back to stdout.
    """

    for agent_name in ("analysis_agent", "execution_agent", "report_agent"):
        try:
            agent = _get_agent(engine, agent_name)
        except Exception:
            continue

        router = getattr(agent, "output_router", None)
        named_outputs = getattr(router, "named_outputs", None)
        if not isinstance(named_outputs, dict):
            continue

        config = getattr(agent, "config", None)
        output_config = getattr(config, "output", None)
        output_items = getattr(output_config, "named_outputs", None)
        if not isinstance(output_items, dict):
            continue

        sender = _nonempty_string(getattr(config, "name", None)) or agent_name
        for output_name, output_item in output_items.items():
            if getattr(output_item, "type", None) != "channel":
                continue
            options = getattr(output_item, "options", {}) or {}
            channel_name = options.get("channel") or output_name
            channel = _get_channel(engine, channel_name)
            if channel is None:
                _logger().warning(
                    "Named output channel not found for %s.%s -> %s",
                    agent_name,
                    output_name,
                    channel_name,
                )
                continue
            named_outputs[output_name] = _ChannelOutput(
                channel,
                channel_name=channel_name,
                sender=sender,
            )


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
) -> tuple[_TranscriptOutput, Any, Any] | None:
    """Route visible default output into a transcript for Stage 1 debug runs."""

    agent = getattr(creature_or_agent, "agent", creature_or_agent)
    output_router = getattr(agent, "output_router", None)
    if output_router is None or not hasattr(output_router, "default_output"):
        return None

    default_output = getattr(output_router, "default_output")
    capture = _TranscriptOutput()
    setattr(output_router, "default_output", capture)
    return capture, output_router, default_output


def _restore_transcript_output(
    state: tuple[_TranscriptOutput, Any, Any] | None,
) -> None:
    if state is None:
        return
    _, output_router, default_output = state
    setattr(output_router, "default_output", default_output)


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


def _transcript_sources(*sources: tuple[str, str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for name, content in sources:
        if not content:
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
    issue_url: str,
    container_version: str,
    prompt: str,
    issue_thread: dict[str, Any] | None,
    system_prompt: str | None,
    assistant_output: str,
    output_sources: list[dict[str, str]],
) -> dict[str, Any]:
    messages = []
    if system_prompt:
        messages.append(
            {
                "role": "system",
                "content": system_prompt,
            }
        )
    messages.extend(
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
    return {
        "stage": "analysis",
        "schema_version": 1,
        "issue_url": issue_url,
        "container_version": container_version,
        "input": {
            "system_prompt": system_prompt,
            "prompt": prompt,
            "issue_url": issue_url,
            "container_version": container_version,
            "prefetched_issue_thread": issue_thread,
        },
        "output": {
            "assistant": assistant_output,
            "sources": output_sources,
        },
        "messages": messages,
    }


async def _collect_analysis_chat(
    agent: Any,
    prompt: str,
    transcript_parts: list[str],
    *,
    transcript_capture: "_TranscriptOutput | None" = None,
) -> None:
    async for chunk in _chat(agent, prompt):
        text = _chunk_text(chunk)
        if transcript_capture is None or not transcript_capture.has_text:
            transcript_parts.append(text)


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


def _write_json_file(path: str | Path, value: Any) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return target


def _write_stage_result(result: StageDebugResult) -> StageDebugResult:
    _write_json_file(
        Path(result.output_dir) / "stage_result.json",
        asdict(result),
    )
    return result


def _debug_report_steps(execution_result: dict[str, Any]) -> list[dict[str, Any]]:
    plan = execution_result.get("plan") if isinstance(execution_result, dict) else {}
    steps = plan.get("reproduction_steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        return []

    trigger_index = next(
        (
            index
            for index, step in enumerate(steps)
            if isinstance(step, dict) and step.get("role") == "trigger"
        ),
        None,
    )
    if trigger_index is None:
        return [dict(step) for step in steps if isinstance(step, dict)]

    selected: list[dict[str, Any]] = []
    selected.extend(
        dict(step)
        for step in steps[:trigger_index]
        if isinstance(step, dict) and step.get("role") == "setup"
    )
    selected.append(dict(steps[trigger_index]))
    selected.extend(
        dict(step)
        for step in steps[trigger_index + 1 :]
        if isinstance(step, dict) and step.get("role") == "verify"
    )
    return selected


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
    elif stage == "execution":
        parser.add_argument(
            "--input",
            required=True,
            help="Stage 1 handoff folder or ReproductionPlan JSON file",
        )
        parser.add_argument("--out", required=True, help="Output handoff directory")
        parser.add_argument("--run-id", help="Optional deterministic run id")
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
        result = run_execution_stage(args.input, args.out, run_id=args.run_id)
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
