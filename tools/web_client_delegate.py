"""Helpers for delegating browser work to the web-client Stage 2 peer."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from typing import Any, Callable


async def delegate_browser_task(
    task: dict[str, Any],
    *,
    send_message: Callable[..., Any],
    receive_message: Callable[..., Any],
    timeout_s: float = 300,
) -> dict[str, Any]:
    """Send a web_client_task and wait for the matching web_client_done result."""

    payload = dict(task)
    payload.setdefault("request_id", str(uuid.uuid4()))
    request_id = str(payload["request_id"])

    await _maybe_await(
        _call_channel_function(
            send_message,
            "web_client_task",
            json.dumps(payload, sort_keys=True, default=str),
        )
    )

    async def wait_for_match() -> dict[str, Any]:
        while True:
            message = await _maybe_await(
                _call_channel_function(receive_message, "web_client_done")
            )
            result = _decode_message(message)
            if isinstance(result, dict) and str(result.get("request_id")) == request_id:
                return result

    return await asyncio.wait_for(wait_for_match(), timeout=timeout_s)


def _call_channel_function(function: Callable[..., Any], channel: str, message: Any = None) -> Any:
    try:
        if message is None:
            return function(channel)
        return function(channel, message)
    except TypeError:
        if message is None:
            return function(channel_name=channel)
        return function(channel=channel, message=message)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _decode_message(message: Any) -> Any:
    for attr in ("content", "payload", "data", "message"):
        if hasattr(message, attr):
            return _decode_message(getattr(message, attr))
    if isinstance(message, dict):
        for key in ("content", "payload", "data", "message"):
            if key in message:
                return _decode_message(message[key])
        return message
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    if isinstance(message, str):
        stripped = message.strip()
        if stripped.startswith("{"):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return message
    return message
