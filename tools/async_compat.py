"""Helpers for running blocking sync code from async-hosted entrypoints."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


def run_sync_away_from_loop(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking sync callable outside the active asyncio loop thread.

    Playwright's sync API refuses to start when it is invoked on a thread that
    already owns a running asyncio loop. The public tool APIs in this project are
    still synchronous, so async-hosted callers need a small thread boundary.
    """

    if not _running_in_asyncio_loop():
        return function(*args, **kwargs)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="jf-sync-tool") as pool:
        return pool.submit(function, *args, **kwargs).result()


def _running_in_asyncio_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
