"""Replay artifacts for LLM-driven Jellyfin Web browser sessions."""

from __future__ import annotations

import argparse
import json
import os
import shlex
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.browser import BrowserDriver, DEFAULT_VIEWPORT, deepcopy_jsonable
from tools.screenshot import browser_locale


REPO_ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIR_NAME = "browser_replay"
MANIFEST_NAME = "replay_manifest.json"
REPLAY_SCRIPT_NAME = "replay_browser_session.py"
README_NAME = "README.md"
ORIGINAL_TRACE_NAME = "original_trace.zip"


class BrowserReplayRecorder:
    """Write a complete replay manifest for a web_client_session run."""

    def __init__(
        self,
        *,
        artifacts_dir: str | Path,
        run_id: str,
        base_url: str,
        browser_input: Mapping[str, Any] | None = None,
        trace_path: str | Path | None = None,
    ) -> None:
        self.artifacts_dir = Path(artifacts_dir).expanduser().resolve()
        self.replay_dir = self.artifacts_dir / REPLAY_DIR_NAME
        self.manifest_path = self.replay_dir / MANIFEST_NAME
        self.script_path = self.replay_dir / REPLAY_SCRIPT_NAME
        self.readme_path = self.replay_dir / README_NAME
        self.replay_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self._load_or_create_manifest(
            run_id=run_id,
            base_url=base_url,
            browser_input=browser_input or {},
            trace_path=trace_path,
        )
        self.write_support_files()
        self._write()

    @property
    def trace_path(self) -> Path:
        trace = self.manifest.get("trace")
        if isinstance(trace, Mapping) and trace.get("path"):
            return Path(str(trace["path"]))
        return self.replay_dir / ORIGINAL_TRACE_NAME

    def record_start(
        self,
        *,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
        base_url: str,
        browser_input: Mapping[str, Any],
    ) -> None:
        self.manifest["base_url"] = base_url
        self.manifest["browser_input"] = _browser_metadata(browser_input)
        self._append(
            {
                "command": "start",
                "request_id": str(
                    result.get("request_id")
                    or request.get("request_id")
                    or "unknown"
                ),
                "replayable": False,
                "request": deepcopy_jsonable(request),
                "result": _compact_result(result),
                "base_url": base_url,
                "browser_input": _browser_metadata(browser_input),
                "skip_reason": "start configures the replay session but is not a browser action",
            }
        )

    def record_action(
        self,
        *,
        request_id: str,
        request: Mapping[str, Any],
        action: Mapping[str, Any],
        browser_input: Mapping[str, Any],
        result: Mapping[str, Any],
        action_index: int | None = None,
        step: Mapping[str, Any] | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        browser = (
            result.get("browser")
            if isinstance(result.get("browser"), Mapping)
            else {}
        )
        command = {
            "command": "action",
            "request_id": request_id,
            "replayable": True,
            "action_index": action_index or self._next_action_index(),
            "request": deepcopy_jsonable(request),
            "step": deepcopy_jsonable(step or {}),
            "action": deepcopy_jsonable(action),
            "browser_input": _browser_metadata(browser_input),
            "browser_metadata": _browser_metadata_snapshot(
                base_url=str(self.manifest.get("base_url") or ""),
                browser_input=browser_input,
                browser=browser,
            ),
            "timing": {
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms,
            },
            "result": _action_result_summary(result),
        }
        self._append(command)

    def record_non_replayable(
        self,
        *,
        command: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
        reason: str,
    ) -> None:
        self._append(
            {
                "command": command or "unknown",
                "request_id": str(
                    result.get("request_id")
                    or request.get("request_id")
                    or "unknown"
                ),
                "replayable": False,
                "request": deepcopy_jsonable(request),
                "result": _compact_result(result),
                "skip_reason": reason,
            }
        )

    def record_schema_error(
        self,
        *,
        command: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        self._append(
            {
                "command": command or "unknown",
                "request_id": str(
                    result.get("request_id")
                    or request.get("request_id")
                    or "unknown"
                ),
                "replayable": False,
                "request": deepcopy_jsonable(request),
                "raw_decoded_request": deepcopy_jsonable(request),
                "validation_error": result.get("error"),
                "schema_path": result.get("schema_path"),
                "result": _compact_result(result),
                "skip_reason": "schema-invalid tool calls are preserved for audit only",
            }
        )

    def record_trace_state(self, trace_state: Mapping[str, Any] | None) -> None:
        if not isinstance(trace_state, Mapping):
            return
        trace = dict(self.manifest.get("trace") or {})
        for key in ("enabled", "path", "status", "started", "stopped", "error"):
            if key in trace_state:
                trace[key] = trace_state.get(key)
        if trace.get("error"):
            trace["trace_error"] = trace.get("error")
        self.manifest["trace"] = trace
        self._write()

    def write_support_files(self) -> None:
        repo_root_literal = json.dumps(str(REPO_ROOT))
        script = f"""#!/usr/bin/env python3
from pathlib import Path
import sys

REPO_ROOT = Path({repo_root_literal})
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.browser_replay import main

if __name__ == "__main__":
    raise SystemExit(
        main(default_manifest=Path(__file__).with_name("{MANIFEST_NAME}"))
    )
"""
        self.script_path.write_text(script, encoding="utf-8")
        try:
            self.script_path.chmod(0o755)
        except OSError:
            pass

        trace_path = self.trace_path
        base_url = str(self.manifest.get("base_url") or "")
        readme = f"""# Browser Replay

This directory contains replay artifacts for `web_client_session` run `{self.manifest.get("run_id")}`.

Re-execute accepted browser actions:

```bash
cd {shlex.quote(str(REPO_ROOT))}
.venv/bin/python {shlex.quote(str(self.script_path))} --base-url {shlex.quote(base_url)}
```

The replay expects the Jellyfin server at the base URL to be reachable and Playwright Chromium to be installed. Use `--headless true`, `--headless false`, `--slow-mo-ms N`, and `--stop-on-failure` as needed.

View the original Playwright trace when present:

```bash
cd {shlex.quote(str(REPO_ROOT))}
.venv/bin/python -m playwright show-trace {shlex.quote(str(trace_path))}
```
"""
        self.readme_path.write_text(readme, encoding="utf-8")

    def _load_or_create_manifest(
        self,
        *,
        run_id: str,
        base_url: str,
        browser_input: Mapping[str, Any],
        trace_path: str | Path | None,
    ) -> dict[str, Any]:
        if self.manifest_path.is_file():
            try:
                payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    payload.setdefault("commands", [])
                    return payload
            except Exception:
                pass
        trace = {
            "enabled": bool(trace_path),
            "path": str(Path(trace_path).expanduser().resolve()) if trace_path else None,
            "status": "pending" if trace_path else "unavailable",
            "error": None,
        }
        return {
            "version": 1,
            "kind": "web_client_session_browser_replay",
            "run_id": run_id,
            "artifacts_dir": str(self.artifacts_dir),
            "created_at": _timestamp(),
            "updated_at": _timestamp(),
            "base_url": base_url,
            "browser_input": _browser_metadata(browser_input),
            "trace": trace,
            "commands": [],
        }

    def _append(self, command: dict[str, Any]) -> None:
        commands = self.manifest.setdefault("commands", [])
        command = deepcopy_jsonable(command)
        command["sequence"] = len(commands) + 1
        command["recorded_at"] = _timestamp()
        commands.append(command)
        self._write()

    def _next_action_index(self) -> int:
        commands = self.manifest.get("commands")
        if not isinstance(commands, list):
            return 1
        return sum(
            1
            for command in commands
            if isinstance(command, Mapping) and command.get("replayable") is True
        ) + 1

    def _write(self) -> None:
        self.manifest["updated_at"] = _timestamp()
        self.manifest_path.write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )


def replay_manifest(
    manifest_path: str | Path,
    *,
    base_url: str | None = None,
    headless: bool | None = None,
    slow_mo_ms: int | None = None,
    stop_on_failure: bool = False,
    output_dir: str | Path | None = None,
    browser_driver_factory: Callable[..., Any] = BrowserDriver,
    printer: Callable[[str], None] | None = print,
) -> dict[str, Any]:
    """Replay accepted browser actions from a replay manifest."""

    manifest_file = Path(manifest_path).expanduser().resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise ValueError("replay manifest must contain a JSON object")

    replay_base_url = str(
        base_url or manifest.get("base_url") or "http://localhost:8096"
    )
    replay_dir = manifest_file.parent
    run_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else _unique_replay_dir(replay_dir)
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_dir.name
    driver = _make_replay_driver(
        browser_driver_factory=browser_driver_factory,
        artifacts_root=run_dir.parent,
        base_url=replay_base_url,
        run_id=run_id,
        headless=headless,
        slow_mo_ms=slow_mo_ms,
        trace_path=run_dir / "replay_trace.zip",
    )

    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed = False
    commands = manifest.get("commands") if isinstance(manifest.get("commands"), list) else []
    try:
        for command in commands:
            if not isinstance(command, Mapping):
                continue
            sequence = command.get("sequence")
            if command.get("replayable") is not True:
                reason = str(command.get("skip_reason") or "not replayable")
                skipped.append(
                    {
                        "sequence": sequence,
                        "command": command.get("command"),
                        "request_id": command.get("request_id"),
                        "reason": reason,
                    }
                )
                if printer:
                    printer(
                        f"skip sequence={sequence} command={command.get('command')} "
                        f"request_id={command.get('request_id')}: {reason}"
                    )
                continue

            action = command.get("action")
            if not isinstance(action, Mapping):
                skipped.append(
                    {
                        "sequence": sequence,
                        "command": command.get("command"),
                        "request_id": command.get("request_id"),
                        "reason": "missing replay action object",
                    }
                )
                continue
            browser_input = _replay_browser_input(manifest, command)
            step_id = None
            step = command.get("step")
            if isinstance(step, Mapping):
                step_id = step.get("step_id")
            try:
                result = _driver_run_single_action(
                    driver,
                    browser_input=browser_input,
                    action=dict(action),
                    run_id=run_id,
                    step_id=step_id,
                )
            except Exception as exc:
                result = {
                    "status": "fail",
                    "actions": [],
                    "screenshot_paths": [],
                    "final_url": None,
                    "dom_path": None,
                    "error": str(exc),
                }
            record = {
                "sequence": sequence,
                "request_id": command.get("request_id"),
                "action_index": command.get("action_index"),
                "action": deepcopy_jsonable(action),
                "status": result.get("status"),
                "browser": result,
            }
            results.append(record)
            _write_json(run_dir / "action_result_log.json", results)
            if _browser_failed(result):
                failed = True
                if stop_on_failure:
                    break
    finally:
        trace_state = None
        try:
            driver.close()
        finally:
            if hasattr(driver, "trace_state"):
                trace_state = driver.trace_state()

    summary = {
        "status": "fail" if failed else "pass",
        "manifest_path": str(manifest_file),
        "base_url": replay_base_url,
        "output_dir": str(run_dir),
        "action_count": len(results),
        "skipped_count": len(skipped),
        "results": results,
        "skipped": skipped,
        "trace": trace_state,
    }
    _write_json(run_dir / "replay_result.json", summary)
    if not (run_dir / "action_result_log.json").exists():
        _write_json(run_dir / "action_result_log.json", results)
    return summary


def main(
    default_manifest: str | Path | None = None,
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Replay web_client_session browser actions."
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        default=str(default_manifest) if default_manifest else None,
    )
    parser.add_argument("--base-url", dest="base_url")
    parser.add_argument("--headless", choices=["true", "false"])
    parser.add_argument("--slow-mo-ms", type=int, default=0)
    parser.add_argument("--stop-on-failure", action="store_true")
    args = parser.parse_args(argv)
    if not args.manifest:
        parser.error("manifest path is required")

    summary = replay_manifest(
        args.manifest,
        base_url=args.base_url,
        headless=_headless_arg(args.headless),
        slow_mo_ms=args.slow_mo_ms,
        stop_on_failure=args.stop_on_failure,
    )
    print(
        json.dumps(
            {
                "status": summary["status"],
                "output_dir": summary["output_dir"],
                "action_count": summary["action_count"],
                "skipped_count": summary["skipped_count"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if summary["status"] == "fail" else 0


def _make_replay_driver(
    *,
    browser_driver_factory: Callable[..., Any],
    artifacts_root: Path,
    base_url: str,
    run_id: str,
    headless: bool | None,
    slow_mo_ms: int | None,
    trace_path: Path,
) -> Any:
    try:
        driver = browser_driver_factory(
            artifacts_root=artifacts_root,
            base_url=base_url,
            run_id=run_id,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    except TypeError:
        try:
            driver = browser_driver_factory(
                artifacts_root=artifacts_root,
                base_url=base_url,
                run_id=run_id,
            )
        except TypeError:
            driver = browser_driver_factory(artifacts_root)
    if hasattr(driver, "configure"):
        driver.configure(base_url=base_url, run_id=run_id)
    if hasattr(driver, "configure_browser"):
        driver.configure_browser(headless=headless, slow_mo_ms=slow_mo_ms)
    if hasattr(driver, "configure_tracing"):
        driver.configure_tracing(trace_path=trace_path)
    return driver


def _driver_run_single_action(
    driver: Any,
    *,
    browser_input: dict[str, Any],
    action: dict[str, Any],
    run_id: str,
    step_id: Any,
) -> dict[str, Any]:
    if hasattr(driver, "run_single_action"):
        return driver.run_single_action(
            browser_input,
            action,
            run_id=run_id,
            step_id=step_id,
        )
    action_input = dict(browser_input)
    action_input["actions"] = [action]
    return driver.run(action_input, run_id=run_id, step_id=step_id)


def _replay_browser_input(
    manifest: Mapping[str, Any],
    command: Mapping[str, Any],
) -> dict[str, Any]:
    browser_input = {}
    if isinstance(manifest.get("browser_input"), Mapping):
        browser_input.update(deepcopy(dict(manifest["browser_input"])))
    if isinstance(command.get("browser_input"), Mapping):
        browser_input.update(deepcopy(dict(command["browser_input"])))
    browser_input.pop("actions", None)
    return browser_input


def _browser_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    metadata = deepcopy_jsonable(dict(value))
    if isinstance(metadata, dict):
        metadata.pop("actions", None)
    return metadata if isinstance(metadata, dict) else {}


def _browser_metadata_snapshot(
    *,
    base_url: str,
    browser_input: Mapping[str, Any],
    browser: Mapping[str, Any],
) -> dict[str, Any]:
    viewport = browser_input.get("viewport")
    if not isinstance(viewport, Mapping):
        viewport = DEFAULT_VIEWPORT
    return {
        "base_url": base_url,
        "viewport": deepcopy_jsonable(viewport),
        "locale": browser.get("locale") or browser_locale(browser_input.get("locale")),
        "auth_mode": _auth_mode(browser_input.get("auth") or browser.get("auth")),
    }


def _action_result_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    browser = (
        result.get("browser")
        if isinstance(result.get("browser"), Mapping)
        else {}
    )
    return {
        "status": result.get("status"),
        "error": result.get("error"),
        "final_url": browser.get("final_url"),
        "dom_path": browser.get("dom_path"),
        "screenshot_path": result.get("screenshot_path"),
        "screenshot_paths": deepcopy_jsonable(browser.get("screenshot_paths", [])),
        "browser_actions": deepcopy_jsonable(browser.get("actions", [])),
    }


def _compact_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "request_id": result.get("request_id"),
        "status": result.get("status"),
        "run_id": result.get("run_id"),
        "error_code": result.get("error_code"),
        "schema_path": result.get("schema_path"),
        "error": result.get("error"),
    }


def _auth_mode(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("mode") or "none")
    return str(value or "none")


def _browser_failed(result: Mapping[str, Any]) -> bool:
    if result.get("status") == "fail":
        return True
    return bool(result.get("error"))


def _headless_arg(value: str | None) -> bool | None:
    if value is None:
        return None
    return value == "true"


def _unique_replay_dir(replay_dir: Path) -> Path:
    root = replay_dir / "replay-runs"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = root / timestamp
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = root / f"{timestamp}_{index}"
        if not candidate.exists():
            return candidate
    return root / f"{timestamp}_{os.getpid()}"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
