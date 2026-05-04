"""Independent Stage 2 peer for Jellyfin Web client reproductions."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.request import urlretrieve

try:
    from kohakuterrarium.modules.tool.base import BaseTool, ExecutionMode, ToolResult
except Exception:
    class _FallbackExecutionMode:
        DIRECT = "direct"

    class BaseTool:
        def __init__(self, config: Any | None = None, **_kwargs: Any) -> None:
            self.config = config

    @dataclass
    class ToolResult:
        output: str = ""
        exit_code: int | None = None
        error: str | None = None
        metadata: dict[str, Any] = field(default_factory=dict)

    ExecutionMode = _FallbackExecutionMode()

from tools.async_compat import run_sync_away_from_loop
from tools.browser import BrowserDriver
from tools.browser_errors import browser_infrastructure_error
from tools.criteria import (
    CaptureError,
    UnboundVariableError,
    evaluate_criteria,
    extract_captures,
    normalize_criteria_assertion,
    resolve_references,
)
from tools.docker_manager import DockerManager
from tools.jellyfin_http import JellyfinHTTP
from tools.screenshot import Screenshotter


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS_ROOT = Path(
    os.environ.get("JF_AUTO_TESTER_ARTIFACTS_ROOT", REPO_ROOT / "artifacts")
).resolve()
FORBIDDEN_DOCKER_OPS_RE = re.compile(r"\bdocker\s+(?:run|pull|start)\b")
DEMO_SERVER_URLS = {
    "stable": "https://demo.jellyfin.org/stable",
    "unstable": "https://demo.jellyfin.org/unstable",
}
STEP_TIMEOUT_S = 120
TASK_BROWSER_METADATA_FIELDS = {
    "path",
    "url",
    "auth",
    "label",
    "timeout_s",
    "viewport",
    "locale",
}
DEBUG_LOG_FILE = "web_client_runner.log"
LOGGER = logging.getLogger(
    "kohakuterrarium.jellyfin_auto_tester.tools.web_client_runner"
)


@dataclass
class _TaskBrowserSession:
    session_id: str
    run_id: str
    base_url: str
    artifacts_root: Path
    artifacts_dir: Path
    browser_input: dict[str, Any]
    step_id: Any
    browser_driver: Any


@dataclass
class _WebClientSession:
    session_id: str
    request_id: str
    run_id: str
    artifacts_root: Path
    artifacts_dir: Path
    browser_input: dict[str, Any]
    step_id: Any
    browser_driver: Any
    plan: dict[str, Any] | None = None
    server_target: dict[str, Any] | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    screenshots: dict[str, str | None] = field(default_factory=dict)
    execution_log: list[dict[str, Any]] = field(default_factory=list)
    browser_auth: dict[str, Any] | None = None
    container_id: str | None = None
    jellyfin_logs: str = ""
    error_summary: str | None = None
    setup_failed: bool = False
    container_crashed: bool = False


class WebClientRunner:
    """Run pure Jellyfin Web plans or delegated browser tasks."""

    def __init__(
        self,
        artifacts_root: str | Path | None = None,
        docker: Any | None = None,
        api: Any | None = None,
        screenshotter: Any | None = None,
        browser_driver: Any | None = None,
        command_runner: Any | None = None,
        uuid_factory: Any = uuid.uuid4,
    ) -> None:
        self.artifacts_root = Path(artifacts_root or DEFAULT_ARTIFACTS_ROOT).resolve()
        self.docker = docker or DockerManager(artifacts_root=self.artifacts_root)
        self.api = api or JellyfinHTTP(artifacts_root=self.artifacts_root)
        self.screenshotter = screenshotter or Screenshotter(
            artifacts_root=self.artifacts_root
        )
        self.browser_driver = browser_driver or BrowserDriver(
            artifacts_root=self.artifacts_root
        )
        self._browser_driver_injected = browser_driver is not None
        self.command_runner = command_runner or self._run_bash
        self.uuid_factory = uuid_factory
        self._task_sessions: dict[str, _TaskBrowserSession] = {}
        self._web_sessions: dict[str, _WebClientSession] = {}

    def execute_plan(
        self,
        plan: dict[str, Any],
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a pure web-client ReproductionPlan and return ExecutionResult."""

        run_id = run_id or str(self.uuid_factory())
        artifacts_dir = self.artifacts_root / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        _write_json(artifacts_dir / "plan.json", plan)
        self._debug_event(
            run_id,
            "execute_plan_start",
            _plan_debug_payload(plan),
        )

        server_target = _server_target(plan)
        if server_target.get("mode") == "demo":
            self._debug_event(
                run_id,
                "execute_plan_route_demo",
                {"base_url": _demo_base_url(server_target, plan)},
            )
            return self._execute_demo_plan(
                plan=plan,
                run_id=run_id,
                artifacts_dir=artifacts_dir,
                server_target=server_target,
            )

        if _trigger_tool(plan) != "browser":
            return self._unsupported_plan_result(
                plan=plan,
                run_id=run_id,
                artifacts_dir=artifacts_dir,
                reason="web_client_runner only accepts plans whose trigger uses tool: browser",
            )

        execution_log: list[dict[str, Any]] = []
        variables: dict[str, Any] = {}
        screenshots: dict[str, str | None] = {}
        container_id: str | None = None
        jellyfin_logs = ""
        error_summary: str | None = None
        setup_failed = False
        container_crashed = False

        try:
            self._debug_event(
                run_id,
                "docker_plan_setup_start",
                {"docker_image": plan.get("docker_image")},
            )
            prepared = self._prepare_environment(plan, run_id)
            self.docker.pull(plan["docker_image"], run_id=run_id)
            start_result = self.docker.start(
                image=plan["docker_image"],
                ports=prepared["environment"].get("ports"),
                volumes=prepared["environment"].get("volumes"),
                env_vars=prepared["environment"].get("env_vars"),
                run_id=run_id,
            )
            container_id = start_result.get("container_id")
            self._debug_event(
                run_id,
                "docker_plan_container_started",
                {
                    "container_id": container_id,
                    "base_url": start_result.get("base_url"),
                    "host_port": start_result.get("host_port"),
                },
            )
            if hasattr(self.api, "configure"):
                self.api.configure(base_url=start_result.get("base_url"), run_id=run_id)
            if hasattr(self.browser_driver, "configure"):
                self.browser_driver.configure(
                    base_url=start_result.get("base_url"),
                    run_id=run_id,
                )

            health = self.api.wait_healthy(timeout_s=60)
            self._debug_event(
                run_id,
                "docker_plan_health_checked",
                {
                    "healthy": health.get("healthy"),
                    "error": health.get("error"),
                },
            )
            if not health.get("healthy"):
                setup_failed = True
                error_summary = f"Jellyfin did not become healthy: {health.get('error')}"
            else:
                self.api.complete_startup_wizard()
                auth = self.api.authenticate()
                self._debug_event(
                    run_id,
                    "docker_plan_auth_checked",
                    {"success": auth.get("success")},
                )
                if not auth.get("success"):
                    setup_failed = True
                    error_summary = "authentication failed"

            if setup_failed:
                execution_log = self._skip_all_steps(
                    plan,
                    reason=error_summary or "setup failed",
                )
            else:
                for index, step in enumerate(plan.get("reproduction_steps", [])):
                    self._debug_event(run_id, "step_start", _step_debug_payload(step))
                    if container_id and not self._container_running(container_id):
                        container_crashed = True
                        execution_log.extend(
                            self._skip_steps(
                                plan.get("reproduction_steps", [])[index:],
                                reason="container exited unexpectedly",
                            )
                        )
                        break

                    entry = self._execute_step(
                        step=step,
                        run_id=run_id,
                        container_id=container_id,
                        variables=variables,
                        screenshots=screenshots,
                    )
                    execution_log.append(entry)
                    self._debug_event(run_id, "step_done", _entry_debug_payload(entry))

            if container_id:
                try:
                    jellyfin_logs = self.docker.logs(
                        container_id,
                        tail="all",
                        run_id=run_id,
                    ).get("logs", "")
                    self._debug_event(
                        run_id,
                        "docker_plan_logs_collected",
                        {"size": len(jellyfin_logs)},
                    )
                except Exception as exc:
                    error_summary = error_summary or f"failed to collect logs: {exc}"
                    self._debug_event(
                        run_id,
                        "docker_plan_logs_failed",
                        {"error": str(exc)},
                    )
        except Exception as exc:
            setup_failed = True
            error_summary = str(exc)
            self._debug_event(
                run_id,
                "execute_plan_error",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
            if not execution_log:
                execution_log = self._skip_all_steps(plan, reason=error_summary)
        finally:
            try:
                self.browser_driver.close()
            except Exception as exc:
                error_summary = error_summary or f"failed to close browser: {exc}"
                self._debug_event(
                    run_id,
                    "browser_close_failed",
                    {"error": str(exc)},
                )
            if container_id:
                try:
                    self.docker.stop(container_id, run_id=run_id)
                    self._debug_event(
                        run_id,
                        "docker_plan_container_stopped",
                        {"container_id": container_id},
                    )
                except Exception as exc:
                    error_summary = error_summary or f"failed to stop container: {exc}"
                    self._debug_event(
                        run_id,
                        "docker_plan_stop_failed",
                        {"container_id": container_id, "error": str(exc)},
                    )

        if jellyfin_logs:
            (artifacts_dir / "jellyfin_server.log").write_text(
                jellyfin_logs,
                encoding="utf-8",
            )

        overall_result = self._overall_result(
            execution_log,
            setup_failed=setup_failed,
            container_crashed=container_crashed,
        )
        result = {
            "plan": plan,
            "run_id": run_id,
            "is_verification": bool(plan.get("is_verification", False)),
            "original_run_id": plan.get("original_run_id"),
            "container_id": container_id,
            "execution_log": execution_log,
            "overall_result": overall_result,
            "artifacts_dir": str(artifacts_dir),
            "jellyfin_logs": jellyfin_logs,
            "error_summary": error_summary,
        }
        self._debug_event(
            run_id,
            "execute_plan_done",
            {
                "overall_result": overall_result,
                "error_summary": error_summary,
                "execution_log_count": len(execution_log),
            },
        )
        _write_json(artifacts_dir / "result.json", result)
        return result

    def _execute_demo_plan(
        self,
        *,
        plan: dict[str, Any],
        run_id: str,
        artifacts_dir: Path,
        server_target: dict[str, Any],
    ) -> dict[str, Any]:
        if plan.get("execution_target") != "web_client":
            return self._unsupported_plan_result(
                plan=plan,
                run_id=run_id,
                artifacts_dir=artifacts_dir,
                reason="demo server mode requires execution_target: web_client",
            )
        if bool(server_target.get("requires_admin")):
            return self._unsupported_plan_result(
                plan=plan,
                run_id=run_id,
                artifacts_dir=artifacts_dir,
                reason="demo server mode cannot satisfy admin-only reproduction plans",
            )
        non_browser_tools = _non_browser_step_tools(plan)
        if non_browser_tools:
            return self._unsupported_plan_result(
                plan=plan,
                run_id=run_id,
                artifacts_dir=artifacts_dir,
                reason=(
                    "demo server mode only supports browser reproduction steps; "
                    f"found non-browser tool(s): {', '.join(non_browser_tools)}"
                ),
            )

        execution_log: list[dict[str, Any]] = []
        variables: dict[str, Any] = {}
        screenshots: dict[str, str | None] = {}
        error_summary: str | None = None
        setup_failed = False
        base_url = _demo_base_url(server_target, plan)
        browser_auth = _demo_browser_auth(server_target)
        self._debug_event(
            run_id,
            "demo_plan_start",
            {
                "base_url": base_url,
                "release_track": server_target.get("release_track"),
                "step_count": len(plan.get("reproduction_steps", []))
                if isinstance(plan.get("reproduction_steps"), list)
                else 0,
            },
        )

        try:
            if hasattr(self.api, "configure"):
                self.api.configure(base_url=base_url, run_id=run_id)
            if hasattr(self.browser_driver, "configure"):
                self.browser_driver.configure(base_url=base_url, run_id=run_id)

            for step in plan.get("reproduction_steps", []):
                self._debug_event(run_id, "step_start", _step_debug_payload(step))
                entry = self._execute_step(
                    step=step,
                    run_id=run_id,
                    container_id=None,
                    variables=variables,
                    screenshots=screenshots,
                    browser_auth=browser_auth,
                )
                execution_log.append(entry)
                self._debug_event(run_id, "step_done", _entry_debug_payload(entry))
        except Exception as exc:
            setup_failed = True
            error_summary = str(exc)
            self._debug_event(
                run_id,
                "demo_plan_error",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
            if not execution_log:
                execution_log = self._skip_all_steps(plan, reason=error_summary)
        finally:
            try:
                self.browser_driver.close()
            except Exception as exc:
                error_summary = error_summary or f"failed to close browser: {exc}"
                self._debug_event(
                    run_id,
                    "browser_close_failed",
                    {"error": str(exc)},
                )

        overall_result = self._overall_result(
            execution_log,
            setup_failed=setup_failed,
            container_crashed=False,
        )
        demo_blocker = _demo_browser_blocker(execution_log)
        if demo_blocker and overall_result in {"not_reproduced", "inconclusive"}:
            if overall_result == "not_reproduced":
                overall_result = "inconclusive"
            error_summary = error_summary or demo_blocker

        result = {
            "plan": plan,
            "run_id": run_id,
            "is_verification": bool(plan.get("is_verification", False)),
            "original_run_id": plan.get("original_run_id"),
            "container_id": None,
            "execution_log": execution_log,
            "overall_result": overall_result,
            "artifacts_dir": str(artifacts_dir),
            "jellyfin_logs": "",
            "error_summary": error_summary,
        }
        self._debug_event(
            run_id,
            "demo_plan_done",
            {
                "overall_result": overall_result,
                "error_summary": error_summary,
                "execution_log_count": len(execution_log),
            },
        )
        _write_json(artifacts_dir / "result.json", result)
        return result

    def run_task(
        self,
        task: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run one command in a delegated interactive browser task session."""

        payload = dict(task or kwargs)
        request_id_value = payload.get("request_id")
        request_id = str(request_id_value or "unknown")
        session_id = str(payload.get("session_id") or "")
        command = str(payload.get("command") or "").strip().lower()

        if not request_id_value:
            return _web_client_error(request_id, "request_id is required", session_id)

        legacy_error = _legacy_task_payload_error(payload)
        if legacy_error:
            result = _web_client_error(request_id, legacy_error, session_id)
            self._write_task_result_if_possible(payload, result)
            return result

        if not command:
            result = _web_client_error(
                request_id,
                "web_client_task command is required; use start, action, or finalize",
                session_id,
            )
            self._write_task_result_if_possible(payload, result)
            return result

        try:
            if command == "start":
                return self._start_task_session(request_id=request_id, payload=payload)
            if command == "action":
                return self._run_task_session_action(
                    request_id=request_id,
                    session_id=session_id,
                    payload=payload,
                )
            if command == "finalize":
                return self._finalize_task_session(
                    request_id=request_id,
                    session_id=session_id,
                    payload=payload,
                )
            result = _web_client_error(
                request_id,
                f"unsupported web_client_task command: {command}",
                session_id,
            )
            self._write_task_result_if_possible(payload, result)
            return result
        except Exception as exc:
            result = _web_client_error(request_id, str(exc), session_id)
            self._write_task_result_if_possible(payload, result)
            return result

    def session(
        self,
        request: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run one command in the unified LLM-facing browser session contract."""

        payload = dict(request or kwargs)
        request_id_value = payload.get("request_id")
        request_id = str(request_id_value or "unknown")
        session_id = str(payload.get("session_id") or "")
        command = str(payload.get("command") or "").strip().lower()

        if not request_id_value:
            return _web_client_error(request_id, "request_id is required", session_id)

        legacy_error = _legacy_session_payload_error(payload)
        if legacy_error:
            result = _web_client_error(request_id, legacy_error, session_id)
            self._write_session_result_if_possible(payload, result)
            return result

        if not command:
            result = _web_client_error(
                request_id,
                "web_client_session command is required; use start, action, or finalize",
                session_id,
            )
            self._write_session_result_if_possible(payload, result)
            return result

        try:
            if command == "start":
                return self._start_web_client_session(
                    request_id=request_id,
                    payload=payload,
                )
            if command == "action":
                return self._run_web_client_session_action(
                    request_id=request_id,
                    session_id=session_id,
                    payload=payload,
                )
            if command == "finalize":
                return self._finalize_web_client_session(
                    request_id=request_id,
                    session_id=session_id,
                    payload=payload,
                )
            result = _web_client_error(
                request_id,
                f"unsupported web_client_session command: {command}",
                session_id,
            )
            self._write_session_result_if_possible(payload, result)
            return result
        except Exception as exc:
            result = _web_client_error(request_id, str(exc), session_id)
            self._write_session_result_if_possible(payload, result)
            return result

    def _run_browser_action_attempt(
        self,
        *,
        request_id: str,
        session: Any,
        browser_input: dict[str, Any],
        action: dict[str, Any],
        step_id: Any,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        browser_driver = session.browser_driver
        if hasattr(browser_driver, "run_single_action"):
            browser = browser_driver.run_single_action(
                browser_input,
                action,
                run_id=session.run_id,
                step_id=step_id,
            )
        else:
            action_input = dict(browser_input)
            action_input["actions"] = [action]
            browser = browser_driver.run(
                action_input,
                run_id=session.run_id,
                step_id=step_id,
            )
        selector_states = _inspect_selector_states(
            browser_driver,
            payload.get("selector_assertions"),
        )
        selector_error = _selector_assertion_error(
            selector_states,
            payload.get("selector_assertions"),
        )
        capture_values, capture_error = _capture_task_values(
            browser_driver,
            payload.get("capture"),
            browser,
        )
        screenshot_paths = [
            str(path)
            for path in browser.get("screenshot_paths", [])
            if path
        ]
        browser_screenshots = _browser_screenshot_map(browser_input, browser)
        error = (
            str(browser.get("error"))
            if browser.get("status") != "pass" and browser.get("error")
            else None
        )
        if selector_error:
            error = selector_error
        if capture_error:
            error = capture_error
        return {
            "request_id": request_id,
            "status": (
                "pass"
                if browser.get("status") == "pass" and error is None
                else "fail"
            ),
            "browser": browser,
            "screenshot_path": screenshot_paths[0] if screenshot_paths else None,
            "browser_screenshots": browser_screenshots,
            "selector_states": selector_states,
            "capture_values": capture_values,
            "error": error,
        }

    def _start_web_client_session(
        self,
        *,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(payload.get("run_id") or "")
        artifacts_root_value = payload.get("artifacts_root")
        if not run_id or not artifacts_root_value:
            result = _web_client_error(
                request_id,
                "run_id and artifacts_root are required for start",
            )
            self._write_session_result_if_possible(payload, result)
            return result

        browser_input, metadata_error = _task_browser_metadata(
            payload.get("browser_input")
        )
        if metadata_error:
            result = _web_client_error(request_id, metadata_error)
            self._write_session_result_if_possible(payload, result)
            return result

        artifacts_root = Path(str(artifacts_root_value)).expanduser().resolve()
        self.artifacts_root = artifacts_root
        artifacts_dir = artifacts_root / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            artifacts_dir / f"web_client_session_{_safe_label(request_id)}.json",
            payload,
        )

        plan: dict[str, Any] | None = None
        server_target: dict[str, Any] | None = None
        base_url = str(payload.get("base_url") or "")
        browser_auth: dict[str, Any] | None = None
        container_id: str | None = None
        setup_failed = False
        error_summary: str | None = None

        if payload.get("plan_path"):
            plan_path = Path(str(payload["plan_path"])).expanduser().resolve()
            plan = _read_json_file(plan_path)
            if not _looks_like_reproduction_plan(plan):
                result = _web_client_error(
                    request_id,
                    "plan_path must point to a ReproductionPlan JSON object",
                )
                self._write_session_result_if_possible(payload, result)
                return result
            _write_json(artifacts_dir / "plan.json", plan)
            server_target = _server_target(plan)
            self._debug_event(run_id, "session_plan_start", _plan_debug_payload(plan))
            try:
                if server_target.get("mode") == "demo":
                    if plan.get("execution_target") != "web_client":
                        setup_failed = True
                        error_summary = (
                            "demo server mode requires execution_target: web_client"
                        )
                    elif bool(server_target.get("requires_admin")):
                        setup_failed = True
                        error_summary = (
                            "demo server mode cannot satisfy admin-only reproduction plans"
                        )
                    else:
                        non_browser_tools = _non_browser_step_tools(plan)
                        if non_browser_tools:
                            setup_failed = True
                            error_summary = (
                                "demo server mode only supports browser reproduction "
                                f"steps; found non-browser tool(s): "
                                f"{', '.join(non_browser_tools)}"
                            )
                        else:
                            base_url = _demo_base_url(server_target, plan)
                            browser_auth = _demo_browser_auth(server_target)
                            if hasattr(self.api, "configure"):
                                self.api.configure(base_url=base_url, run_id=run_id)
                            if hasattr(self.browser_driver, "configure"):
                                self.browser_driver.configure(
                                    base_url=base_url,
                                    run_id=run_id,
                                )
                else:
                    if _trigger_tool(plan) != "browser":
                        setup_failed = True
                        error_summary = (
                            "web_client_runner only accepts plans whose trigger "
                            "uses tool: browser"
                        )
                    else:
                        prepared = self._prepare_environment(plan, run_id)
                        self.docker.pull(plan["docker_image"], run_id=run_id)
                        start_result = self.docker.start(
                            image=plan["docker_image"],
                            ports=prepared["environment"].get("ports"),
                            volumes=prepared["environment"].get("volumes"),
                            env_vars=prepared["environment"].get("env_vars"),
                            run_id=run_id,
                        )
                        container_id = start_result.get("container_id")
                        base_url = str(start_result.get("base_url") or base_url)
                        if hasattr(self.api, "configure"):
                            self.api.configure(base_url=base_url, run_id=run_id)
                        if hasattr(self.browser_driver, "configure"):
                            self.browser_driver.configure(
                                base_url=base_url,
                                run_id=run_id,
                            )
                        health = self.api.wait_healthy(timeout_s=60)
                        if not health.get("healthy"):
                            setup_failed = True
                            error_summary = (
                                f"Jellyfin did not become healthy: "
                                f"{health.get('error')}"
                            )
                        else:
                            self.api.complete_startup_wizard()
                            auth = self.api.authenticate()
                            if not auth.get("success"):
                                setup_failed = True
                                error_summary = "authentication failed"
            except Exception as exc:
                setup_failed = True
                error_summary = str(exc)
        elif base_url:
            if hasattr(self.api, "configure"):
                self.api.configure(base_url=base_url, run_id=run_id)
            if hasattr(self.browser_driver, "configure"):
                self.browser_driver.configure(base_url=base_url, run_id=run_id)
        else:
            result = _web_client_error(
                request_id,
                "start requires either plan_path or base_url",
            )
            self._write_session_result_if_possible(payload, result)
            return result

        session_id = str(payload.get("session_id") or self.uuid_factory())
        existing = self._web_sessions.pop(session_id, None)
        if existing is not None:
            _close_quietly(existing.browser_driver)

        if plan is not None:
            browser_driver = self.browser_driver
            if hasattr(browser_driver, "artifacts_root"):
                browser_driver.artifacts_root = artifacts_root
            if hasattr(browser_driver, "configure") and base_url:
                browser_driver.configure(base_url=base_url, run_id=run_id)
        else:
            browser_driver = self._task_browser_driver(artifacts_root, base_url, run_id)
        session = _WebClientSession(
            session_id=session_id,
            request_id=request_id,
            run_id=run_id,
            artifacts_root=artifacts_root,
            artifacts_dir=artifacts_dir,
            browser_input=browser_input,
            step_id=payload.get("step_id"),
            browser_driver=browser_driver,
            plan=plan,
            server_target=server_target,
            browser_auth=browser_auth,
            container_id=container_id,
            error_summary=error_summary,
            setup_failed=setup_failed,
        )
        if setup_failed and plan is not None:
            session.execution_log = self._skip_all_steps(
                plan,
                reason=error_summary or "setup failed",
            )
        self._web_sessions[session_id] = session

        result = _web_client_session_result(
            request_id=request_id,
            session_id=session_id,
            status="error" if setup_failed else "pass",
            error=error_summary,
        )
        result["run_id"] = run_id
        result["plan_loaded"] = plan is not None
        result["base_url"] = base_url
        _write_json(
            artifacts_dir / f"web_client_result_{_safe_label(request_id)}.json",
            result,
        )
        return result

    def _run_web_client_session_action(
        self,
        *,
        request_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not session_id:
            result = _web_client_error(request_id, "session_id is required for action")
            self._write_session_result_if_possible(payload, result)
            return result
        session = self._web_sessions.get(session_id)
        if session is None:
            result = _web_client_error(
                request_id,
                f"browser session not found: {session_id}",
                session_id,
            )
            self._write_session_result_if_possible(payload, result)
            return result
        if session.setup_failed:
            result = _web_client_error(
                request_id,
                session.error_summary or "session setup failed",
                session_id,
            )
            self._write_session_result_if_possible(payload, result)
            return result
        if session.container_id and not self._container_running(session.container_id):
            session.container_crashed = True
            session.error_summary = session.error_summary or "container exited unexpectedly"
            result = _web_client_error(request_id, session.error_summary, session_id)
            self._write_session_result_if_possible(payload, result)
            return result

        _write_json(
            session.artifacts_dir / f"web_client_session_{_safe_label(request_id)}.json",
            payload,
        )
        action = payload.get("action")
        if isinstance(action, list):
            result = _web_client_error(
                request_id,
                "action must be a single browser action object, not an array",
                session_id,
            )
            _write_json(
                session.artifacts_dir
                / f"web_client_result_{_safe_label(request_id)}.json",
                result,
            )
            return result
        if not isinstance(action, dict):
            result = _web_client_error(
                request_id,
                "action is required for action command and must be an object",
                session_id,
            )
            _write_json(
                session.artifacts_dir
                / f"web_client_result_{_safe_label(request_id)}.json",
                result,
            )
            return result

        override_input, metadata_error = _task_browser_metadata(
            payload.get("browser_input")
        )
        if metadata_error:
            result = _web_client_error(request_id, metadata_error, session_id)
            _write_json(
                session.artifacts_dir
                / f"web_client_result_{_safe_label(request_id)}.json",
                result,
            )
            return result

        browser_input = dict(session.browser_input)
        browser_input.update(override_input)
        browser_input["actions"] = [deepcopy(action)]
        if session.browser_auth is not None:
            browser_input = _inject_browser_auth(browser_input, session.browser_auth)

        if session.plan is not None:
            step = _session_action_step(payload, browser_input, action)
            entry = self._execute_step(
                step=step,
                run_id=session.run_id,
                container_id=session.container_id,
                variables=session.variables,
                screenshots=session.screenshots,
                browser_auth=session.browser_auth,
            )
            session.execution_log.append(entry)
            result = {
                "request_id": request_id,
                "status": str(entry.get("outcome") or "fail"),
                "session_id": session_id,
                "run_id": session.run_id,
                "browser": entry.get("browser"),
                "screenshot_path": entry.get("screenshot_path"),
                "browser_screenshots": (
                    _browser_screenshot_map(browser_input, entry.get("browser"))
                    if isinstance(entry.get("browser"), Mapping)
                    else {}
                ),
                "criteria_evaluation": entry.get("criteria_evaluation"),
                "execution_entry": entry,
                "error": entry.get("reason") if entry.get("outcome") == "fail" else None,
            }
        else:
            result = self._run_browser_action_attempt(
                request_id=request_id,
                session=session,
                browser_input=browser_input,
                action=action,
                step_id=payload.get("step_id", session.step_id),
                payload=payload,
            )
            result["session_id"] = session_id
            result["run_id"] = session.run_id

        _write_json(
            session.artifacts_dir / f"web_client_result_{_safe_label(request_id)}.json",
            result,
        )
        return result

    def _finalize_web_client_session(
        self,
        *,
        request_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not session_id:
            result = _web_client_error(request_id, "session_id is required for finalize")
            self._write_session_result_if_possible(payload, result)
            return result
        session = self._web_sessions.pop(session_id, None)
        if session is None:
            result = _web_client_error(
                request_id,
                f"browser session not found: {session_id}",
                session_id,
            )
            self._write_session_result_if_possible(payload, result)
            return result

        _write_json(
            session.artifacts_dir / f"web_client_session_{_safe_label(request_id)}.json",
            payload,
        )
        close_error = self._close_web_client_session_resources(session)
        if close_error:
            session.error_summary = session.error_summary or close_error

        if session.plan is None:
            result = _web_client_session_result(
                request_id=request_id,
                session_id=session_id,
                status="error" if close_error else "pass",
                error=close_error,
            )
            _write_json(
                session.artifacts_dir
                / f"web_client_result_{_safe_label(request_id)}.json",
                result,
            )
            return result

        if session.jellyfin_logs:
            (session.artifacts_dir / "jellyfin_server.log").write_text(
                session.jellyfin_logs,
                encoding="utf-8",
            )

        requested_result = str(payload.get("overall_result") or "").strip()
        if requested_result not in {"reproduced", "not_reproduced", "inconclusive"}:
            requested_result = "inconclusive"
        error_summary = (
            str(payload.get("error_summary"))
            if payload.get("error_summary") is not None
            else session.error_summary
        )
        if session.setup_failed or session.container_crashed:
            requested_result = "inconclusive"
        demo_blocker = (
            _demo_browser_blocker(session.execution_log)
            if (session.server_target or {}).get("mode") == "demo"
            else None
        )
        if demo_blocker and requested_result in {"not_reproduced", "inconclusive"}:
            if requested_result == "not_reproduced":
                requested_result = "inconclusive"
            error_summary = error_summary or demo_blocker

        result = {
            "plan": session.plan,
            "run_id": session.run_id,
            "is_verification": bool(session.plan.get("is_verification", False)),
            "original_run_id": session.plan.get("original_run_id"),
            "container_id": session.container_id,
            "execution_log": session.execution_log,
            "overall_result": requested_result,
            "artifacts_dir": str(session.artifacts_dir),
            "jellyfin_logs": session.jellyfin_logs,
            "error_summary": error_summary,
        }
        _write_json(session.artifacts_dir / "result.json", result)
        _write_json(
            session.artifacts_dir / f"web_client_result_{_safe_label(request_id)}.json",
            result,
        )
        return result

    def _close_web_client_session_resources(self, session: _WebClientSession) -> str | None:
        error_summary: str | None = None
        try:
            session.browser_driver.close()
        except Exception as exc:
            error_summary = f"failed to close browser: {exc}"
        if session.container_id:
            try:
                try:
                    session.jellyfin_logs = self.docker.logs(
                        session.container_id,
                        tail="all",
                        run_id=session.run_id,
                    ).get("logs", "")
                except Exception as exc:
                    error_summary = error_summary or f"failed to collect logs: {exc}"
                self.docker.stop(session.container_id, run_id=session.run_id)
            except Exception as exc:
                error_summary = error_summary or f"failed to stop container: {exc}"
        return error_summary

    def _write_session_result_if_possible(
        self,
        payload: Mapping[str, Any],
        result: dict[str, Any],
    ) -> None:
        session_id = str(payload.get("session_id") or "")
        if session_id and session_id in self._web_sessions:
            artifacts_dir = self._web_sessions[session_id].artifacts_dir
        elif payload.get("run_id") and payload.get("artifacts_root"):
            artifacts_dir = (
                Path(str(payload["artifacts_root"])).expanduser().resolve()
                / str(payload["run_id"])
            )
        else:
            return
        try:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            _write_json(
                artifacts_dir
                / f"web_client_result_{_safe_label(result.get('request_id'))}.json",
                result,
            )
        except Exception:
            return

    def _start_task_session(
        self,
        *,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(payload.get("run_id") or "")
        base_url = str(payload.get("base_url") or "")
        artifacts_root_value = payload.get("artifacts_root")
        if not run_id or not base_url or not artifacts_root_value:
            result = _web_client_error(
                request_id,
                "run_id, base_url, and artifacts_root are required for start",
            )
            self._write_task_result_if_possible(payload, result)
            return result

        browser_input, metadata_error = _task_browser_metadata(
            payload.get("browser_input")
        )
        if metadata_error:
            result = _web_client_error(request_id, metadata_error)
            self._write_task_result_if_possible(payload, result)
            return result

        artifacts_root = Path(str(artifacts_root_value)).expanduser().resolve()
        artifacts_dir = artifacts_root / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            artifacts_dir / f"web_client_task_{_safe_label(request_id)}.json",
            payload,
        )

        session_id = str(payload.get("session_id") or self.uuid_factory())
        existing = self._task_sessions.pop(session_id, None)
        if existing is not None:
            _close_quietly(existing.browser_driver)

        browser_driver = self._task_browser_driver(artifacts_root, base_url, run_id)
        self._task_sessions[session_id] = _TaskBrowserSession(
            session_id=session_id,
            run_id=run_id,
            base_url=base_url,
            artifacts_root=artifacts_root,
            artifacts_dir=artifacts_dir,
            browser_input=browser_input,
            step_id=payload.get("step_id"),
            browser_driver=browser_driver,
        )
        result = _web_client_session_result(
            request_id=request_id,
            session_id=session_id,
            status="pass",
        )
        _write_json(
            artifacts_dir / f"web_client_result_{_safe_label(request_id)}.json",
            result,
        )
        return result

    def _run_task_session_action(
        self,
        *,
        request_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not session_id:
            result = _web_client_error(request_id, "session_id is required for action")
            self._write_task_result_if_possible(payload, result)
            return result
        session = self._task_sessions.get(session_id)
        if session is None:
            result = _web_client_error(
                request_id,
                f"browser session not found: {session_id}",
                session_id,
            )
            self._write_task_result_if_possible(payload, result)
            return result

        _write_json(
            session.artifacts_dir / f"web_client_task_{_safe_label(request_id)}.json",
            payload,
        )
        action = payload.get("action")
        if isinstance(action, list):
            result = _web_client_error(
                request_id,
                "action must be a single browser action object, not an array",
                session_id,
            )
            _write_json(
                session.artifacts_dir
                / f"web_client_result_{_safe_label(request_id)}.json",
                result,
            )
            return result
        if not isinstance(action, dict):
            result = _web_client_error(
                request_id,
                "action is required for action command and must be an object",
                session_id,
            )
            _write_json(
                session.artifacts_dir
                / f"web_client_result_{_safe_label(request_id)}.json",
                result,
            )
            return result

        override_input, metadata_error = _task_browser_metadata(
            payload.get("browser_input")
        )
        if metadata_error:
            result = _web_client_error(request_id, metadata_error, session_id)
            _write_json(
                session.artifacts_dir
                / f"web_client_result_{_safe_label(request_id)}.json",
                result,
            )
            return result

        browser_input = dict(session.browser_input)
        browser_input.update(override_input)
        result = self._run_task_action_attempt(
            request_id=request_id,
            session=session,
            browser_input=browser_input,
            action=action,
            step_id=payload.get("step_id", session.step_id),
            payload=payload,
        )
        result["session_id"] = session_id
        _write_json(
            session.artifacts_dir / f"web_client_result_{_safe_label(request_id)}.json",
            result,
        )
        return result

    def _finalize_task_session(
        self,
        *,
        request_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not session_id:
            result = _web_client_error(request_id, "session_id is required for finalize")
            self._write_task_result_if_possible(payload, result)
            return result
        session = self._task_sessions.pop(session_id, None)
        if session is None:
            result = _web_client_error(
                request_id,
                f"browser session not found: {session_id}",
                session_id,
            )
            self._write_task_result_if_possible(payload, result)
            return result

        _write_json(
            session.artifacts_dir / f"web_client_task_{_safe_label(request_id)}.json",
            payload,
        )
        close_error: str | None = None
        try:
            session.browser_driver.close()
        except Exception as exc:
            close_error = str(exc)
        result = _web_client_session_result(
            request_id=request_id,
            session_id=session_id,
            status="error" if close_error else "pass",
            error=close_error,
        )
        _write_json(
            session.artifacts_dir / f"web_client_result_{_safe_label(request_id)}.json",
            result,
        )
        return result

    def _run_task_action_attempt(
        self,
        *,
        request_id: str,
        session: _TaskBrowserSession,
        browser_input: dict[str, Any],
        action: dict[str, Any],
        step_id: Any,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._run_browser_action_attempt(
            request_id=request_id,
            session=session,
            browser_input=browser_input,
            action=action,
            step_id=step_id,
            payload=payload,
        )

    def _write_task_result_if_possible(
        self,
        payload: Mapping[str, Any],
        result: dict[str, Any],
    ) -> None:
        session_id = str(payload.get("session_id") or "")
        if session_id and session_id in self._task_sessions:
            artifacts_dir = self._task_sessions[session_id].artifacts_dir
        elif payload.get("run_id") and payload.get("artifacts_root"):
            artifacts_dir = (
                Path(str(payload["artifacts_root"])).expanduser().resolve()
                / str(payload["run_id"])
            )
        else:
            return
        try:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            _write_json(
                artifacts_dir
                / f"web_client_result_{_safe_label(result.get('request_id'))}.json",
                result,
            )
        except Exception:
            return

    def _task_browser_driver(
        self,
        artifacts_root: Path,
        base_url: str,
        run_id: str,
    ) -> Any:
        if self._browser_driver_injected:
            if hasattr(self.browser_driver, "artifacts_root"):
                self.browser_driver.artifacts_root = artifacts_root
            if hasattr(self.browser_driver, "configure"):
                self.browser_driver.configure(base_url=base_url, run_id=run_id)
            return self.browser_driver
        return BrowserDriver(
            artifacts_root=artifacts_root,
            base_url=base_url,
            run_id=run_id,
        )

    def _debug_event(
        self,
        run_id: str | None,
        event: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        payload = dict(payload or {})
        _log_debug_event(event, run_id=run_id, payload=payload)
        log_dir = self.artifacts_root / run_id if run_id else self.artifacts_root
        try:
            _append_jsonl(
                log_dir / DEBUG_LOG_FILE,
                {"timestamp": _timestamp(), "event": event, **payload},
            )
        except Exception as exc:
            LOGGER.debug(
                "failed to write web-client debug event event=%s run_id=%s error=%s",
                event,
                run_id,
                exc,
            )

    def _unsupported_plan_result(
        self,
        *,
        plan: dict[str, Any],
        run_id: str,
        artifacts_dir: Path,
        reason: str,
    ) -> dict[str, Any]:
        result = {
            "plan": plan,
            "run_id": run_id,
            "is_verification": bool(plan.get("is_verification", False)),
            "original_run_id": plan.get("original_run_id"),
            "container_id": None,
            "execution_log": self._skip_all_steps(plan, reason=reason),
            "overall_result": "inconclusive",
            "artifacts_dir": str(artifacts_dir),
            "jellyfin_logs": "",
            "error_summary": reason,
        }
        self._debug_event(
            run_id,
            "unsupported_plan",
            {"reason": reason, **_plan_debug_payload(plan)},
        )
        _write_json(artifacts_dir / "result.json", result)
        return result

    def _prepare_environment(self, plan: dict[str, Any], run_id: str) -> dict[str, Any]:
        original_or_current = plan.get("original_run_id") or run_id
        prereq_dir = self.artifacts_root / original_or_current / "media"
        prereq_dir.mkdir(parents=True, exist_ok=True)
        self._prepare_prerequisites(plan.get("prerequisites", []), prereq_dir, run_id)

        environment = deepcopy(plan.get("environment") or {})
        environment.setdefault("ports", {"host": 8096, "container": 8096})
        environment.setdefault("volumes", [])
        environment.setdefault("env_vars", {})
        environment["volumes"] = self._with_media_volume(
            environment.get("volumes", []),
            prereq_dir,
            plan,
        )

        for volume in environment["volumes"]:
            Path(str(volume["host"])).expanduser().resolve().mkdir(
                parents=True,
                exist_ok=True,
            )

        return {"environment": environment, "prereq_dir": str(prereq_dir)}

    def _prepare_prerequisites(
        self,
        prerequisites: list[dict[str, Any]],
        prereq_dir: Path,
        run_id: str,
    ) -> None:
        for index, prereq in enumerate(prerequisites, start=1):
            target = self._prerequisite_target(prereq, prereq_dir, index)
            if target.exists():
                continue
            source = str(prereq.get("source") or "")
            command = prereq.get("command") or _command_from_source(source)
            if source.startswith("http://") or source.startswith("https://"):
                urlretrieve(source, target)
            elif command:
                result = self.command_runner(
                    str(command),
                    cwd=str(prereq_dir),
                    timeout_s=STEP_TIMEOUT_S,
                )
                if result.get("exit_code") != 0:
                    self._append_docker_ops_log(
                        run_id,
                        "prerequisite_failed",
                        {
                            "description": prereq.get("description"),
                            "stdout": result.get("stdout"),
                            "stderr": result.get("stderr"),
                            "exit_code": result.get("exit_code"),
                        },
                    )
            else:
                self._append_docker_ops_log(
                    run_id,
                    "prerequisite_skipped",
                    {
                        "description": prereq.get("description"),
                        "reason": "no source command",
                    },
                )

    def _execute_step(
        self,
        step: dict[str, Any],
        run_id: str,
        container_id: str | None,
        variables: dict[str, Any],
        screenshots: dict[str, str | None],
        browser_auth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        started_at = datetime.now(timezone.utc)
        entry = self._base_entry(step, started_at)
        context: dict[str, Any] = {
            "tool": step.get("tool"),
            "screenshots": screenshots,
        }

        try:
            step_input = resolve_references(step.get("input", {}), variables)
            criteria = resolve_references(step.get("success_criteria"), variables)
            if step.get("tool") == "browser" and browser_auth is not None:
                step_input = _inject_browser_auth(step_input, browser_auth)
        except UnboundVariableError as exc:
            entry.update(
                {
                    "outcome": "fail",
                    "reason": str(exc),
                    "duration_ms": _elapsed_ms(started),
                    "end_time": _timestamp(),
                }
            )
            return entry

        if step.get("tool") == "bash" and self._is_forbidden_docker_command(step_input):
            self._append_docker_ops_log(
                run_id,
                "forbidden_docker_step_skipped",
                {"step_id": step.get("step_id"), "command": step_input.get("command")},
            )
            entry.update(
                {
                    "outcome": "skip",
                    "reason": "container lifecycle command skipped",
                    "duration_ms": _elapsed_ms(started),
                    "end_time": _timestamp(),
                }
            )
            return entry

        try:
            dispatch_result = self._dispatch_step(
                step=step,
                step_input=step_input,
                run_id=run_id,
                container_id=container_id,
            )
            entry.update(dispatch_result["entry"])
            context.update(dispatch_result["context"])
            if context.get("screenshot_label"):
                screenshots[str(context["screenshot_label"])] = context.get(
                    "screenshot_path"
                )
            if isinstance(context.get("browser_screenshots"), dict):
                screenshots.update(context["browser_screenshots"])
            if context.get("browser") and hasattr(self.browser_driver, "inspect_selectors"):
                selectors = _browser_element_selectors(criteria)
                if selectors:
                    context["browser_elements"] = self.browser_driver.inspect_selectors(
                        selectors
                    )
            if context.get("browser"):
                context["browser_text"] = context["browser"].get("page_text")

            if _criteria_needs_logs(criteria) and container_id:
                context["logs_since_step_start"] = self.docker.logs(
                    container_id,
                    tail="all",
                    since=started_at,
                    run_id=run_id,
                ).get("logs", "")

            criteria_result = evaluate_criteria(criteria, context)
            entry["criteria_evaluation"] = criteria_result
            if criteria_result["passed"]:
                entry["outcome"] = "pass"
                if step.get("capture"):
                    try:
                        if context.get("browser") and hasattr(
                            self.browser_driver,
                            "capture_values",
                        ):
                            context["browser_capture_values"] = (
                                self.browser_driver.capture_values(step.get("capture"))
                            )
                        variables.update(extract_captures(step.get("capture"), context))
                    except CaptureError as exc:
                        entry["outcome"] = "fail"
                        entry["reason"] = f"capture failed: {exc.variable}"
                    except Exception as exc:
                        entry["outcome"] = "fail"
                        entry["reason"] = f"capture failed: {exc}"
            else:
                entry["outcome"] = "fail"
                entry["reason"] = _criteria_failure_reason(criteria_result)
        except TimeoutError:
            entry["outcome"] = "fail"
            entry["reason"] = "timeout"
        except subprocess.TimeoutExpired:
            entry["outcome"] = "fail"
            entry["reason"] = "timeout"
        except Exception as exc:
            entry["outcome"] = "fail"
            entry["reason"] = str(exc)

        if entry["outcome"] == "fail" and container_id:
            self._capture_failure_artifacts(step, entry, run_id, container_id, screenshots)

        entry["duration_ms"] = _elapsed_ms(started)
        entry["end_time"] = _timestamp()
        return entry

    def _dispatch_step(
        self,
        step: dict[str, Any],
        step_input: dict[str, Any],
        run_id: str,
        container_id: str | None,
    ) -> dict[str, Any]:
        tool = step.get("tool")
        if tool == "bash":
            result = self.command_runner(
                str(step_input.get("command", "")),
                cwd=step_input.get("cwd"),
                timeout_s=int(step_input.get("timeout_s", STEP_TIMEOUT_S)),
            )
            return {
                "entry": {
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "exit_code": result.get("exit_code"),
                },
                "context": {
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "exit_code": result.get("exit_code"),
                },
            }
        if tool == "docker_exec":
            if not container_id:
                raise RuntimeError("container_id unavailable")
            result = self.docker.exec(
                container_id,
                str(step_input.get("command", "")),
                timeout_s=int(step_input.get("timeout_s", STEP_TIMEOUT_S)),
                run_id=run_id,
            )
            return {
                "entry": {
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "exit_code": result.get("exit_code"),
                },
                "context": {
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "exit_code": result.get("exit_code"),
                },
            }
        if tool == "http_request":
            if "body" in step_input:
                raise ValueError(
                    "http_request input uses body; use body_json, body_text, or body_base64"
                )
            response = self.api.request(
                method=str(step_input.get("method", "GET")),
                path=str(step_input.get("path", "/")),
                params=step_input.get("params"),
                headers=step_input.get("headers"),
                auth=str(step_input.get("auth", "auto")),
                token=step_input.get("token"),
                body_json=step_input.get("body_json"),
                body_text=step_input.get("body_text"),
                body_base64=step_input.get("body_base64"),
                timeout_s=step_input.get("timeout_s"),
                follow_redirects=bool(step_input.get("follow_redirects", False)),
                allow_absolute_url=bool(step_input.get("allow_absolute_url", False)),
            )
            http = {
                "status_code": response.get("status_code"),
                "body": response.get("body"),
                "headers": response.get("headers", {}),
            }
            return {"entry": {"http": http}, "context": {"http": http}}
        if tool == "screenshot":
            label = str(step_input.get("label") or f"step_{step.get('step_id')}")
            url = self._screenshot_url(step_input)
            shot = self.screenshotter.capture(
                url=url,
                run_id=run_id,
                label=label,
                wait_selector=step_input.get("wait_selector"),
                wait_ms=int(step_input.get("wait_ms", 2000)),
                locale=step_input.get("locale"),
            )
            return {
                "entry": {"screenshot_path": shot.get("path")},
                "context": {
                    "screenshot_path": shot.get("path"),
                    "screenshot_label": label,
                },
            }
        if tool == "browser":
            browser = self.browser_driver.run(
                step_input,
                run_id=run_id,
                step_id=step.get("step_id"),
            )
            screenshot_paths = [
                str(path)
                for path in browser.get("screenshot_paths", [])
                if path
            ]
            browser_screenshots = _browser_screenshot_map(step_input, browser)
            screenshot_path = screenshot_paths[0] if screenshot_paths else None
            return {
                "entry": {
                    "browser": browser,
                    "screenshot_path": screenshot_path,
                },
                "context": {
                    "browser": browser,
                    "screenshot_path": screenshot_path,
                    "browser_screenshots": browser_screenshots,
                },
            }
        raise ValueError(f"unsupported step.tool: {tool}")

    def _capture_failure_artifacts(
        self,
        step: dict[str, Any],
        entry: dict[str, Any],
        run_id: str,
        container_id: str,
        screenshots: dict[str, str | None],
    ) -> None:
        artifacts_dir = self.artifacts_root / run_id
        try:
            logs = self.docker.logs(container_id, tail="all", run_id=run_id).get(
                "logs",
                "",
            )
            path = artifacts_dir / f"step_{step.get('step_id')}_fail_jellyfin.log"
            path.write_text(logs, encoding="utf-8")
            entry["failure_logs_path"] = str(path)
        except Exception as exc:
            entry["failure_logs_error"] = str(exc)

        if self._is_ui_step(step):
            try:
                label = f"step_{step.get('step_id')}_fail"
                shot = self.screenshotter.capture(
                    url=self._screenshot_url(step.get("input", {})),
                    run_id=run_id,
                    label=label,
                    locale=(step.get("input", {}) or {}).get("locale"),
                )
                screenshots[label] = shot.get("path")
                entry["failure_screenshot_path"] = shot.get("path")
            except Exception as exc:
                entry["failure_screenshot_error"] = str(exc)

    def _base_entry(self, step: dict[str, Any], started_at: datetime) -> dict[str, Any]:
        return {
            "step_id": step.get("step_id"),
            "role": step.get("role"),
            "action": step.get("action"),
            "tool": step.get("tool"),
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "http": None,
            "browser": None,
            "screenshot_path": None,
            "outcome": "fail",
            "reason": None,
            "criteria_evaluation": None,
            "duration_ms": 0,
            "start_time": started_at.isoformat(),
            "end_time": None,
        }

    def _skip_all_steps(self, plan: dict[str, Any], reason: str) -> list[dict[str, Any]]:
        return self._skip_steps(plan.get("reproduction_steps", []), reason=reason)

    def _skip_steps(
        self,
        steps: list[dict[str, Any]],
        reason: str,
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        skipped = []
        for step in steps:
            entry = self._base_entry(step, now)
            entry.update({"outcome": "skip", "reason": reason, "end_time": _timestamp()})
            skipped.append(entry)
        return skipped

    def _overall_result(
        self,
        execution_log: list[dict[str, Any]],
        setup_failed: bool,
        container_crashed: bool,
    ) -> str:
        if setup_failed or container_crashed:
            return "inconclusive"
        trigger = next(
            (
                entry
                for entry in reversed(execution_log)
                if entry.get("role") == "trigger"
            ),
            None,
        )
        if not trigger or trigger.get("outcome") == "skip":
            return "inconclusive"
        if trigger.get("reason") == "timeout":
            return "inconclusive"
        if trigger.get("outcome") == "fail" and browser_infrastructure_error(trigger):
            return "inconclusive"
        if trigger.get("outcome") == "pass":
            return "reproduced"
        if trigger.get("outcome") == "fail":
            return "not_reproduced"
        return "inconclusive"

    def _container_running(self, container_id: str) -> bool:
        try:
            state = self.docker.inspect(container_id).get("State", {})
        except Exception:
            return True
        if "Running" in state:
            return bool(state["Running"])
        return str(state.get("Status", "running")).lower() == "running"

    def _run_bash(
        self,
        command: str,
        cwd: str | None = None,
        timeout_s: int = STEP_TIMEOUT_S,
    ) -> dict[str, Any]:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        return {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "exit_code": completed.returncode,
        }

    def _append_docker_ops_log(
        self,
        run_id: str,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        path = self.artifacts_root / run_id / "docker_ops.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"timestamp": _timestamp(), "event": event, **payload}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def _with_media_volume(
        self,
        volumes: list[dict[str, Any]],
        prereq_dir: Path,
        plan: dict[str, Any],
    ) -> list[dict[str, Any]]:
        media_mount = (
            (plan.get("environment") or {}).get("media_mount_path")
            or plan.get("media_mount_path")
            or "/media"
        )
        result = [
            volume
            for volume in volumes
            if str(volume.get("container")) != str(media_mount)
        ]
        result.append({"host": str(prereq_dir), "container": str(media_mount), "mode": "rw"})
        return result

    def _prerequisite_target(
        self,
        prereq: dict[str, Any],
        prereq_dir: Path,
        index: int,
    ) -> Path:
        for key in ("path", "filename", "target"):
            if prereq.get(key):
                value = Path(str(prereq[key]))
                return value if value.is_absolute() else prereq_dir / value
        source = str(prereq.get("source") or "")
        if source.startswith("http://") or source.startswith("https://"):
            name = Path(urlparse(source).path).name
            if name:
                return prereq_dir / name
        return prereq_dir / f"prerequisite_{index}"

    def _is_forbidden_docker_command(self, step_input: dict[str, Any]) -> bool:
        return bool(FORBIDDEN_DOCKER_OPS_RE.search(str(step_input.get("command", ""))))

    def _is_ui_step(self, step: dict[str, Any]) -> bool:
        step_input = step.get("input", {})
        return step.get("tool") in {"screenshot", "browser"} or bool(step_input.get("url"))

    def _screenshot_url(self, step_input: dict[str, Any]) -> str:
        if step_input.get("url"):
            return str(step_input["url"])
        base_url = getattr(self.api, "base_url", "http://localhost:8096")
        path = str(step_input.get("path") or "/web")
        return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def execute_plan(
    plan: dict[str, Any],
    run_id: str | None = None,
) -> dict[str, Any]:
    return run_sync_away_from_loop(
        lambda: WebClientRunner().execute_plan(plan=plan, run_id=run_id)
    )


def session(
    request: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return run_sync_away_from_loop(
        lambda: _run_session_on_worker(request=request, **kwargs),
        worker_key="web_client_session",
    )


_DEFAULT_SESSION_RUNNER: WebClientRunner | None = None


def _run_session_on_worker(
    request: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    global _DEFAULT_SESSION_RUNNER
    if _DEFAULT_SESSION_RUNNER is None:
        _DEFAULT_SESSION_RUNNER = WebClientRunner()
    return _DEFAULT_SESSION_RUNNER.session(request=request, **kwargs)


class WebClientSessionTool(BaseTool):
    """KT tool wrapper for unified one-action browser sessions."""

    is_concurrency_safe = False

    def __init__(self, config: Any | None = None, **_unused: Any) -> None:
        super().__init__(config=config)

    @property
    def tool_name(self) -> str:
        return "web_client_session"

    @property
    def description(self) -> str:
        return (
            "Run one command in a Jellyfin Web browser session. start opens a "
            "plan-backed or task browser session, action executes exactly one "
            "browser action, and finalize closes resources and returns the raw "
            "result JSON."
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "request": {
                    "type": "object",
                    "description": (
                        "A WebClientSession request. If omitted, the "
                        "whole tool payload is treated as the request."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Bracket-call JSON body.",
                },
            },
        }

    def prompt_contribution(self) -> str | None:
        return (
            "Call `web_client_session` with bracket-body JSON: "
            "`command=start`, then one `command=action` call with exactly one "
            "top-level action object per browser move, then `command=finalize`. "
            "Never submit `actions` arrays or an `action` array."
        )

    async def _execute(self, args: dict[str, Any], **_kwargs: Any) -> ToolResult:
        request, error = _session_tool_args(args)
        if error:
            return ToolResult(error=error)
        try:
            result = session(request=request)
        except Exception as exc:
            return ToolResult(error=f"web_client_session failed: {exc}")
        return ToolResult(
            output=json.dumps(result, ensure_ascii=False, indent=2),
            exit_code=0,
        )


class WebClientExecutePlanTool(BaseTool):
    """KT tool wrapper for full web-client ReproductionPlan execution."""

    is_concurrency_safe = False

    def __init__(self, config: Any | None = None, **_unused: Any) -> None:
        super().__init__(config=config)

    @property
    def tool_name(self) -> str:
        return "web_client_execute_plan"

    @property
    def description(self) -> str:
        return (
            "Internal compatibility helper for batch Jellyfin Web "
            "ReproductionPlan execution. Do not expose this to the "
            "web-client agent."
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "plan": {
                    "type": "object",
                    "description": (
                        "A ReproductionPlan. If omitted, the whole tool "
                        "payload is treated as the plan."
                    ),
                },
                "run_id": {
                    "type": "string",
                    "description": "Optional run identifier for artifacts.",
                },
                "content": {
                    "type": "string",
                    "description": "Bracket-call JSON body.",
                },
            },
        }

    def prompt_contribution(self) -> str | None:
        return None

    async def _execute(self, args: dict[str, Any], **_kwargs: Any) -> ToolResult:
        plan, run_id, error = _execute_plan_tool_args(args)
        if error:
            return ToolResult(error=error)
        try:
            result = execute_plan(plan=plan, run_id=run_id)
        except Exception as exc:
            return ToolResult(error=f"web_client_execute_plan failed: {exc}")
        return ToolResult(
            output=json.dumps(result, ensure_ascii=False, indent=2),
            exit_code=0,
        )


def _execute_plan_tool_args(
    args: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    payload, raw_args, error = _tool_payload_object(
        args,
        tool_name="web_client_execute_plan",
    )
    if error:
        return None, None, error

    run_id = _optional_tool_string(
        payload.get("run_id")
        if "run_id" in payload
        else raw_args.get("run_id")
    )
    if "plan" in payload:
        plan, plan_error = _json_object_value(payload["plan"], "plan")
        if plan_error:
            return None, None, plan_error
        if not _looks_like_reproduction_plan(plan):
            return (
                None,
                None,
                "plan must be a ReproductionPlan object with reproduction_steps",
            )
        return plan, run_id, None

    plan = _without_tool_only_keys(payload, {"run_id"})
    if not _looks_like_reproduction_plan(plan):
        return (
            None,
            None,
            (
                "web_client_execute_plan missing plan: expected "
                "{\"plan\": {...}} or raw ReproductionPlan JSON"
            ),
        )
    return plan, run_id, None


def _session_tool_args(
    args: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    payload, _raw_args, error = _tool_payload_object(
        args,
        tool_name="web_client_session",
    )
    if error:
        return None, error

    if "request" in payload:
        request, request_error = _json_object_value(payload["request"], "request")
        if request_error:
            return None, request_error
        if not _looks_like_web_client_session_request(request):
            return None, "request must be a WebClientSession command object"
        return request, None

    request = _without_tool_only_keys(payload)
    if not _looks_like_web_client_session_request(request):
        return (
            None,
            (
                "web_client_session missing command: expected "
                "{\"request\": {...}} or raw command JSON"
            ),
        )
    return request, None


def _tool_payload_object(
    args: Mapping[str, Any] | None,
    *,
    tool_name: str,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    if args is None:
        raw_args: dict[str, Any] = {}
    elif isinstance(args, Mapping):
        raw_args = dict(args)
    else:
        return {}, {}, f"{tool_name} arguments must be an object"

    if "content" not in raw_args:
        return dict(raw_args), raw_args, None

    content = raw_args.get("content")
    if content is None or (isinstance(content, str) and not content.strip()):
        return {}, raw_args, f"{tool_name} JSON body is empty"

    payload, error = _json_object_value(content, f"{tool_name} JSON body")
    if error:
        return {}, raw_args, error
    return payload, raw_args, None


def _json_object_value(
    value: Any,
    label: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(value, Mapping):
        return deepcopy(dict(value)), None
    if not isinstance(value, str):
        return None, f"{label} must be a JSON object"

    text = value.strip()
    if not text:
        return None, f"{label} is empty"
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        return (
            None,
            (
                f"malformed JSON in {label}: {exc.msg} "
                f"at line {exc.lineno} column {exc.colno}"
            ),
        )
    if not isinstance(decoded, Mapping):
        return None, f"{label} must decode to a JSON object"
    return deepcopy(dict(decoded)), None


def _without_tool_only_keys(
    payload: Mapping[str, Any],
    extra: set[str] | None = None,
) -> dict[str, Any]:
    tool_keys = {"_tool_call_id", "content", *(extra or set())}
    return {
        key: deepcopy(value)
        for key, value in payload.items()
        if key not in tool_keys
    }


def _looks_like_reproduction_plan(value: Any) -> bool:
    return isinstance(value, Mapping) and isinstance(
        value.get("reproduction_steps"),
        list,
    )


def _looks_like_web_client_session_request(value: Any) -> bool:
    return isinstance(value, Mapping) and (
        "command" in value or "request_id" in value
    )


def _optional_tool_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _trigger_tool(plan: dict[str, Any]) -> str | None:
    for step in plan.get("reproduction_steps", []):
        if isinstance(step, dict) and step.get("role") == "trigger":
            return str(step.get("tool") or "")
    return None


def _server_target(plan: Mapping[str, Any]) -> dict[str, Any]:
    value = plan.get("server_target")
    if not isinstance(value, Mapping):
        return {"mode": "docker"}
    server_target = dict(value)
    mode = str(server_target.get("mode") or "docker").strip().lower()
    server_target["mode"] = "demo" if mode == "demo" else "docker"
    return server_target


def _non_browser_step_tools(plan: Mapping[str, Any]) -> list[str]:
    tools = []
    for step in plan.get("reproduction_steps", []):
        if not isinstance(step, Mapping):
            continue
        tool = str(step.get("tool") or "")
        if tool and tool != "browser" and tool not in tools:
            tools.append(tool)
    return tools


def _demo_base_url(
    server_target: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> str:
    if server_target.get("base_url"):
        return str(server_target["base_url"])
    release_track = _demo_release_track(
        server_target.get("release_track") or plan.get("target_version")
    )
    return DEMO_SERVER_URLS[release_track]


def _demo_release_track(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"unstable", "latest-unstable", "master"}:
        return "unstable"
    return "stable"


def _demo_browser_auth(server_target: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "mode": "auto",
        "username": str(server_target.get("username", "demo")),
        "password": str(server_target.get("password", "")),
    }


def _inject_browser_auth(
    step_input: Mapping[str, Any],
    browser_auth: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(step_input)
    if updated.get("auth") in (None, "auto"):
        updated["auth"] = deepcopy(browser_auth)
    return updated


def _demo_browser_blocker(execution_log: list[dict[str, Any]]) -> str | None:
    for entry in execution_log:
        if not isinstance(entry, Mapping) or entry.get("outcome") != "fail":
            continue
        infrastructure_error = browser_infrastructure_error(entry)
        if infrastructure_error:
            return f"demo browser flow could not complete: {infrastructure_error}"
        browser = entry.get("browser")
        if not isinstance(browser, Mapping) or browser.get("status") != "fail":
            continue
        error = browser.get("error") or entry.get("reason") or "browser action failed"
        return f"demo browser flow could not complete: {error}"
    return None


def _command_from_source(source: str) -> str | None:
    prefixes = ("generate with ffmpeg:", "generate:", "command:")
    for prefix in prefixes:
        if source.lower().startswith(prefix):
            return source[len(prefix) :].strip()
    if source.startswith("ffmpeg "):
        return source
    return None


def _criteria_needs_logs(criteria: Any) -> bool:
    if not isinstance(criteria, dict):
        return False
    assertions = criteria.get("all_of") or criteria.get("any_of") or []
    return any(
        isinstance(assertion, dict) and assertion.get("type") == "log_matches"
        for assertion in assertions
    )


def _criteria_failure_reason(criteria_result: dict[str, Any]) -> str:
    for assertion in criteria_result.get("assertions", []):
        if not assertion.get("passed"):
            return str(assertion.get("message") or "success criteria failed")
    return "success criteria failed"


def _browser_screenshot_map(
    step_input: Mapping[str, Any],
    browser: Mapping[str, Any],
) -> dict[str, str]:
    screenshots = {}
    for action in browser.get("actions", []):
        if not isinstance(action, dict) or not action.get("screenshot_path"):
            continue
        label = str(action.get("label") or step_input.get("label") or "browser")
        screenshots[label] = str(action["screenshot_path"])
    return screenshots


def _browser_element_selectors(criteria: Any) -> list[str]:
    if not isinstance(criteria, dict):
        return []
    assertions = criteria.get("all_of") or criteria.get("any_of") or []
    selectors = []
    for assertion in assertions:
        assertion = normalize_criteria_assertion(assertion)
        if (
            isinstance(assertion, dict)
            and assertion.get("type") == "browser_element"
            and assertion.get("selector")
        ):
            selectors.append(str(assertion["selector"]))
    return selectors


def _inspect_selector_states(
    browser_driver: Any,
    selector_assertions: Any,
) -> dict[str, dict[str, Any]]:
    selectors = [selector for selector, _state in _selector_expectations(selector_assertions)]
    if not selectors or not hasattr(browser_driver, "inspect_selectors"):
        return {}
    return browser_driver.inspect_selectors(selectors)


def _selector_assertion_error(
    selector_states: Mapping[str, Mapping[str, Any]],
    selector_assertions: Any,
) -> str | None:
    for selector, expected in _selector_expectations(selector_assertions):
        actual = selector_states.get(selector) or {"attached": False, "visible": False}
        attached = bool(actual.get("attached"))
        visible = bool(actual.get("visible"))
        if expected in {"exists", "attached"}:
            passed = attached
        elif expected == "detached":
            passed = not attached
        elif expected == "visible":
            passed = visible
        elif expected == "hidden":
            passed = attached and not visible
        else:
            return f"unsupported selector state: {expected}"
        if not passed:
            return f"selector {selector} did not match state {expected}"
    return None


def _selector_expectations(selector_assertions: Any) -> list[tuple[str, str]]:
    if isinstance(selector_assertions, Mapping):
        expectations = []
        for selector, value in selector_assertions.items():
            if isinstance(value, Mapping):
                state = value.get("state", "visible")
                selector = value.get("selector") or selector
            else:
                state = value
            expectations.append((str(selector), str(state)))
        return expectations
    if isinstance(selector_assertions, list):
        expectations = []
        for item in selector_assertions:
            if not isinstance(item, Mapping) or not item.get("selector"):
                continue
            expectations.append((str(item["selector"]), str(item.get("state", "visible"))))
        return expectations
    return []


def _capture_task_values(
    browser_driver: Any,
    capture_map: Any,
    browser: Mapping[str, Any],
) -> tuple[dict[str, Any], str | None]:
    if not isinstance(capture_map, Mapping) or not capture_map:
        return {}, None
    context: dict[str, Any] = {
        "browser": browser,
        "browser_text": browser.get("page_text"),
    }
    try:
        if hasattr(browser_driver, "capture_values"):
            context["browser_capture_values"] = browser_driver.capture_values(capture_map)
        return extract_captures(capture_map, context), None
    except Exception as exc:
        return {}, f"capture failed: {exc}"


def _legacy_task_payload_error(payload: Mapping[str, Any]) -> str | None:
    browser_input = payload.get("browser_input")
    if isinstance(browser_input, Mapping) and "actions" in browser_input:
        return (
            "legacy browser_input.actions is not supported in web_client_task; "
            "use command=start, then submit exactly one top-level action per "
            "command=action call"
        )
    if "actions" in payload:
        return (
            "top-level actions arrays are not supported in web_client_task; "
            "submit exactly one action object as action"
        )
    if isinstance(payload.get("action"), list):
        return "action must be a single browser action object, not an array"
    return None


def _legacy_session_payload_error(payload: Mapping[str, Any]) -> str | None:
    browser_input = payload.get("browser_input")
    if isinstance(browser_input, Mapping) and "actions" in browser_input:
        return (
            "browser_input.actions arrays are not supported in web_client_session; "
            "submit exactly one top-level action object per command=action call"
        )
    if "actions" in payload:
        return (
            "top-level actions arrays are not supported in web_client_session; "
            "submit exactly one action object as action"
        )
    if isinstance(payload.get("action"), list):
        return "action must be a single browser action object, not an array"
    return None


def _task_browser_metadata(value: Any) -> tuple[dict[str, Any], str | None]:
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return {}, "browser_input must be an object when supplied"
    forbidden = sorted(set(value) - TASK_BROWSER_METADATA_FIELDS)
    if forbidden:
        return (
            {},
            "browser_input may only contain session metadata fields; "
            f"unsupported field(s): {', '.join(forbidden)}",
        )
    return deepcopy(value), None


def _session_action_step(
    payload: Mapping[str, Any],
    browser_input: Mapping[str, Any],
    action: Mapping[str, Any],
) -> dict[str, Any]:
    criteria = payload.get("success_criteria")
    if not isinstance(criteria, Mapping):
        criteria = {"all_of": [{"type": "browser_action_run"}]}
    else:
        criteria = deepcopy(dict(criteria))

    selector_assertions = _selector_criteria_assertions(
        payload.get("selector_assertions")
    )
    if selector_assertions:
        all_of = criteria.get("all_of") if isinstance(criteria, Mapping) else None
        any_of = criteria.get("any_of") if isinstance(criteria, Mapping) else None
        if isinstance(all_of, list):
            all_of.extend(selector_assertions)
        elif isinstance(any_of, list):
            any_of.extend(selector_assertions)
        else:
            criteria = {
                "all_of": [{"type": "browser_action_run"}, *selector_assertions]
            }

    label = (
        payload.get("action_label")
        or payload.get("label")
        or action.get("label")
        or action.get("type")
        or "browser action"
    )
    step = {
        "step_id": payload.get("step_id"),
        "role": payload.get("role"),
        "action": str(label),
        "tool": "browser",
        "input": deepcopy(dict(browser_input)),
        "success_criteria": criteria,
    }
    if payload.get("capture") is not None:
        step["capture"] = deepcopy(payload.get("capture"))
    return step


def _selector_criteria_assertions(selector_assertions: Any) -> list[dict[str, Any]]:
    return [
        {
            "type": "browser_element",
            "selector": selector,
            "state": state,
        }
        for selector, state in _selector_expectations(selector_assertions)
    ]


def _web_client_session_result(
    *,
    request_id: str,
    session_id: str,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    result = _web_client_error(request_id, error, session_id)
    result["status"] = status
    return result


def _web_client_error(
    request_id: str,
    error: str | None,
    session_id: str | None = None,
) -> dict[str, Any]:
    result = {
        "request_id": request_id,
        "status": "error",
        "browser": None,
        "screenshot_path": None,
        "browser_screenshots": {},
        "selector_states": {},
        "capture_values": {},
        "error": error,
    }
    if session_id:
        result["session_id"] = session_id
    return result


def _close_quietly(target: Any) -> None:
    try:
        target.close()
    except Exception:
        return


def _plan_debug_payload(plan: Mapping[str, Any]) -> dict[str, Any]:
    steps = plan.get("reproduction_steps")
    server_target = plan.get("server_target")
    if not isinstance(server_target, Mapping):
        server_target = {}
    return {
        "issue_url": plan.get("issue_url"),
        "execution_target": plan.get("execution_target"),
        "server_mode": server_target.get("mode"),
        "docker_image": plan.get("docker_image"),
        "step_count": len(steps) if isinstance(steps, list) else 0,
    }


def _step_debug_payload(step: Mapping[str, Any]) -> dict[str, Any]:
    step_input = step.get("input")
    actions = step_input.get("actions") if isinstance(step_input, Mapping) else None
    return {
        "step_id": step.get("step_id"),
        "role": step.get("role"),
        "tool": step.get("tool"),
        "action": step.get("action"),
        "browser_action_count": len(actions) if isinstance(actions, list) else 0,
    }


def _entry_debug_payload(entry: Mapping[str, Any]) -> dict[str, Any]:
    browser = entry.get("browser") if isinstance(entry.get("browser"), Mapping) else {}
    return {
        "step_id": entry.get("step_id"),
        "role": entry.get("role"),
        "tool": entry.get("tool"),
        "outcome": entry.get("outcome"),
        "reason": entry.get("reason"),
        "duration_ms": entry.get("duration_ms"),
        "browser_status": browser.get("status"),
        "browser_error": browser.get("error"),
    }


def _log_debug_event(
    event: str,
    *,
    run_id: str | None,
    payload: Mapping[str, Any],
) -> None:
    if not LOGGER.isEnabledFor(logging.DEBUG):
        return
    fields = " ".join(
        f"{key}={_log_value(value)}"
        for key, value in payload.items()
        if value is not None
    )
    if fields:
        LOGGER.debug("web-client %s run_id=%s %s", event, run_id, fields)
    else:
        LOGGER.debug("web-client %s run_id=%s", event, run_id)


def _log_value(value: Any) -> str:
    text = str(value).replace("\n", "\\n")
    if len(text) > 240:
        text = f"{text[:237]}..."
    return text


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _read_json_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _safe_label(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "item")).strip("._")
    return text or "item"
