"""Pipeline fabric and CLI for the Jellyfin auto-tester."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_RECIPE_PATH = REPO_ROOT / "terrarium.yaml"
DEFAULT_DOTENV_PATH = REPO_ROOT / ".env"
TERMINAL_CHANNELS = ("final_report", "human_review_queue")
PIPELINE_PAYLOAD_KEYS = {
    "report_path",
    "run_id",
    "verification_run_id",
    "overall_result",
    "verified",
    "issue_url",
    "reason",
}


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


def load_env_file(path: str | Path = DEFAULT_DOTENV_PATH) -> bool:
    """Load environment variables from ``.env`` if it exists.

    Values already present in the process environment win over values in the
    file. Missing files are not an error.
    """

    dotenv_path = Path(path).expanduser()
    if not dotenv_path.exists():
        return False

    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "python-dotenv is required to load .env files. "
            "Install dependencies with: .venv/bin/python -m pip install -r requirements.txt"
        ) from exc

    return bool(load_dotenv(dotenv_path=dotenv_path, override=False))


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
) -> PipelineResult:
    """Run the full Stage 1 -> Stage 2 -> Stage 3 pipeline.

    The Terrarium channel topology owns stage-to-stage delivery. This fabric
    starts terminal-channel listeners before prompting Stage 1 so broadcast
    final reports are not missed, then returns the first terminal route:
    ``final_report`` or ``human_review_queue``.
    """

    load_env_file()
    engine = await _load_engine(recipe_path, engine_factory=engine_factory)
    _apply_default_execution_budget(engine)

    terminal_task = asyncio.create_task(_wait_for_terminal_message(engine))
    analysis_output: list[str] = []
    try:
        prompt = _analysis_prompt(issue_url, container_version)
        analysis_agent = _get_agent(engine, "analysis_agent")
        async for chunk in _chat(analysis_agent, prompt):
            text = _chunk_text(chunk)
            analysis_output.append(text)
            if stream is not None:
                stream.write(text)
                stream.flush()

        transcript = "".join(analysis_output)
        if _is_insufficient_information(transcript):
            terminal_task.cancel()
            await _cancel_task(terminal_task)
            return PipelineResult(
                channel="analysis",
                message=transcript.strip(),
                issue_url=issue_url,
                analysis_output=transcript,
            )

        try:
            channel, message = await asyncio.wait_for(terminal_task, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise PipelineTimeoutError(
                f"timed out waiting for {', '.join(TERMINAL_CHANNELS)}"
            ) from exc

        result = _pipeline_result(channel, message, transcript)
        if stream is not None:
            _print_terminal_summary(result, stream)
        return result
    except BaseException:
        terminal_task.cancel()
        await _cancel_task(terminal_task)
        raise


async def _load_engine(
    recipe_path: str | Path,
    *,
    engine_factory: Callable[[str], Any] | None,
) -> Any:
    recipe = str(Path(recipe_path).expanduser())
    if engine_factory is not None:
        return await _maybe_await(engine_factory(recipe))

    try:
        from kohakuterrarium import Terrarium
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on runtime env
        raise RuntimeError(
            "kohakuterrarium is required to run the full pipeline. "
            "Install it in this environment, then rerun main.py."
        ) from exc

    return await _maybe_await(Terrarium.from_recipe(recipe))


def _analysis_prompt(issue_url: str, container_version: str) -> str:
    return f"Issue: {issue_url}\nTarget version: {container_version}"


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


def _channel_receive_source(engine: Any, channel_name: str) -> Any:
    channel = _get_channel(engine, channel_name)
    if channel is not None:
        if isinstance(channel, (str, bytes, dict)):
            return channel
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
            return None
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
    prefix = "\n" if result.analysis_output and not result.analysis_output.endswith("\n") else ""
    if result.channel == "final_report":
        stream.write(f"{prefix}Final report: {result.report_path or result.message}\n")
    else:
        stream.write(f"{prefix}Queued for human review: {result.report_path or result.message}\n")
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Jellyfin issue reproduction pipeline."
    )
    parser.add_argument("issue_url", help="GitHub issue URL to reproduce")
    parser.add_argument("container_version", help="Jellyfin container tag, e.g. 10.9.7")
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
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the terminal PipelineResult as JSON",
    )
    return parser


async def _async_main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
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


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
