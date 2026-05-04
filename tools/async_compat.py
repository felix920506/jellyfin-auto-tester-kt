"""Helpers for running blocking sync code from async-hosted entrypoints."""

from __future__ import annotations

import atexit
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from typing import Any, Callable


_PERSISTENT_EXECUTORS: dict[str, ThreadPoolExecutor] = {}
_PERSISTENT_LOCK = threading.Lock()
_WORKER_STATE = threading.local()
_ATEXIT_REGISTERED = False


def run_sync_away_from_loop(
    function: Callable[..., Any],
    *args: Any,
    worker_key: str | None = None,
    **kwargs: Any,
) -> Any:
    """Run a blocking sync callable outside the active asyncio loop thread.

    Playwright's sync API refuses to start when it is invoked on a thread that
    already owns a running asyncio loop. The public tool APIs in this project are
    still synchronous, so async-hosted callers need a small thread boundary.
    """

    if worker_key:
        key = str(worker_key)
        if getattr(_WORKER_STATE, "key", None) == key:
            return function(*args, **kwargs)
        pool = _persistent_executor(key)
        return pool.submit(_call_with_worker_key, key, function, args, kwargs).result()

    if not _running_in_asyncio_loop():
        return function(*args, **kwargs)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="jf-sync-tool") as pool:
        return pool.submit(function, *args, **kwargs).result()


def shutdown_sync_workers() -> None:
    """Stop persistent sync workers used by stateful tool sessions."""

    with _PERSISTENT_LOCK:
        executors = list(_PERSISTENT_EXECUTORS.values())
        _PERSISTENT_EXECUTORS.clear()
    for executor in executors:
        executor.shutdown(wait=True, cancel_futures=True)


def _running_in_asyncio_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _persistent_executor(key: str) -> ThreadPoolExecutor:
    global _ATEXIT_REGISTERED
    with _PERSISTENT_LOCK:
        executor = _PERSISTENT_EXECUTORS.get(key)
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"jf-sync-tool-{_safe_thread_label(key)}",
            )
            _PERSISTENT_EXECUTORS[key] = executor
        if not _ATEXIT_REGISTERED:
            atexit.register(shutdown_sync_workers)
            _ATEXIT_REGISTERED = True
        return executor


def _call_with_worker_key(
    key: str,
    function: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    previous = getattr(_WORKER_STATE, "key", None)
    _WORKER_STATE.key = key
    try:
        return function(*args, **kwargs)
    finally:
        if previous is None:
            try:
                del _WORKER_STATE.key
            except AttributeError:
                pass
        else:
            _WORKER_STATE.key = previous


def _safe_thread_label(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value) or "worker"
