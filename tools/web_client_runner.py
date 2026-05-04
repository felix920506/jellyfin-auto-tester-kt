"""Independent Stage 2 peer for Jellyfin Web client reproductions."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.request import urlretrieve

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
ALLOWED_BROWSER_REPAIR_FIELDS = {
    "actions",
    "path",
    "url",
    "label",
    "timeout_s",
    "viewport",
    "locale",
}
REPAIR_POLICY_CONTROL_FIELDS = {
    "enabled",
    "max_attempts",
    "browser_input",
    "retry_browser_input",
    "input",
}


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

        server_target = _server_target(plan)
        if server_target.get("mode") == "demo":
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
            if hasattr(self.api, "configure"):
                self.api.configure(base_url=start_result.get("base_url"), run_id=run_id)
            if hasattr(self.browser_driver, "configure"):
                self.browser_driver.configure(
                    base_url=start_result.get("base_url"),
                    run_id=run_id,
                )

            health = self.api.wait_healthy(timeout_s=60)
            if not health.get("healthy"):
                setup_failed = True
                error_summary = f"Jellyfin did not become healthy: {health.get('error')}"
            else:
                self.api.complete_startup_wizard()
                auth = self.api.authenticate()
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

            if container_id:
                try:
                    jellyfin_logs = self.docker.logs(
                        container_id,
                        tail="all",
                        run_id=run_id,
                    ).get("logs", "")
                except Exception as exc:
                    error_summary = error_summary or f"failed to collect logs: {exc}"
        except Exception as exc:
            setup_failed = True
            error_summary = str(exc)
            if not execution_log:
                execution_log = self._skip_all_steps(plan, reason=error_summary)
        finally:
            try:
                self.browser_driver.close()
            except Exception as exc:
                error_summary = error_summary or f"failed to close browser: {exc}"
            if container_id:
                try:
                    self.docker.stop(container_id, run_id=run_id)
                except Exception as exc:
                    error_summary = error_summary or f"failed to stop container: {exc}"

        if jellyfin_logs:
            (artifacts_dir / "jellyfin_server.log").write_text(
                jellyfin_logs,
                encoding="utf-8",
            )

        result = {
            "plan": plan,
            "run_id": run_id,
            "is_verification": bool(plan.get("is_verification", False)),
            "original_run_id": plan.get("original_run_id"),
            "container_id": container_id,
            "execution_log": execution_log,
            "overall_result": self._overall_result(
                execution_log,
                setup_failed=setup_failed,
                container_crashed=container_crashed,
            ),
            "artifacts_dir": str(artifacts_dir),
            "jellyfin_logs": jellyfin_logs,
            "error_summary": error_summary,
        }
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

        try:
            if hasattr(self.api, "configure"):
                self.api.configure(base_url=base_url, run_id=run_id)
            if hasattr(self.browser_driver, "configure"):
                self.browser_driver.configure(base_url=base_url, run_id=run_id)

            for step in plan.get("reproduction_steps", []):
                execution_log.append(
                    self._execute_step(
                        step=step,
                        run_id=run_id,
                        container_id=None,
                        variables=variables,
                        screenshots=screenshots,
                        browser_auth=browser_auth,
                    )
                )
        except Exception as exc:
            setup_failed = True
            error_summary = str(exc)
            if not execution_log:
                execution_log = self._skip_all_steps(plan, reason=error_summary)
        finally:
            try:
                self.browser_driver.close()
            except Exception as exc:
                error_summary = error_summary or f"failed to close browser: {exc}"

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
        _write_json(artifacts_dir / "result.json", result)
        return result

    def run_task(
        self,
        task: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run a delegated browser task against an already-running Jellyfin URL."""

        payload = dict(task or kwargs)
        request_id = str(payload.get("request_id") or self.uuid_factory())
        run_id = str(payload.get("run_id") or "")
        base_url = str(payload.get("base_url") or "")
        artifacts_root = Path(
            payload.get("artifacts_root") or self.artifacts_root
        ).expanduser().resolve()
        step_id = payload.get("step_id")
        browser_input = payload.get("browser_input")

        if not run_id or not base_url or not isinstance(browser_input, dict):
            return _web_client_error(
                request_id,
                "run_id, base_url, and browser_input are required",
            )

        artifacts_dir = artifacts_root / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        _write_json(artifacts_dir / f"web_client_task_{_safe_label(request_id)}.json", payload)

        browser_driver = self._task_browser_driver(artifacts_root, base_url, run_id)
        attempts: list[dict[str, Any]] = []
        try:
            result = self._run_task_attempt(
                request_id=request_id,
                browser_driver=browser_driver,
                browser_input=browser_input,
                run_id=run_id,
                step_id=step_id,
                payload=payload,
            )
            attempts.append(result)

            if result["status"] != "pass":
                repair_input, repair_error = _repair_input_from_policy(
                    original_input=browser_input,
                    repair_policy=payload.get("repair_policy"),
                )
                if repair_error:
                    result["error"] = f"repair rejected: {repair_error}"
                elif repair_input is not None:
                    repaired = self._run_task_attempt(
                        request_id=request_id,
                        browser_driver=browser_driver,
                        browser_input=repair_input,
                        run_id=run_id,
                        step_id=step_id,
                        payload=payload,
                    )
                    repaired["repair_attempted"] = True
                    attempts.append(repaired)
                    result = repaired

            if len(attempts) > 1:
                result["browser_attempts"] = [
                    attempt.get("browser") for attempt in attempts
                ]
            _write_json(
                artifacts_dir / f"web_client_result_{_safe_label(request_id)}.json",
                result,
            )
            return result
        except Exception as exc:
            result = _web_client_error(request_id, str(exc))
            _write_json(
                artifacts_dir / f"web_client_result_{_safe_label(request_id)}.json",
                result,
            )
            return result
        finally:
            try:
                browser_driver.close()
            except Exception:
                pass

    def _run_task_attempt(
        self,
        *,
        request_id: str,
        browser_driver: Any,
        browser_input: dict[str, Any],
        run_id: str,
        step_id: Any,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        browser = browser_driver.run(browser_input, run_id=run_id, step_id=step_id)
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
            "status": "pass" if browser.get("status") == "pass" and error is None else "fail",
            "browser": browser,
            "screenshot_path": screenshot_paths[0] if screenshot_paths else None,
            "browser_screenshots": browser_screenshots,
            "selector_states": selector_states,
            "capture_values": capture_values,
            "error": error,
        }

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


def run_task(
    task: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return run_sync_away_from_loop(
        lambda: WebClientRunner().run_task(task=task, **kwargs)
    )


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


def _repair_input_from_policy(
    *,
    original_input: Any,
    repair_policy: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(repair_policy, Mapping):
        return None, None
    if repair_policy.get("enabled") is False:
        return None, None
    if int(repair_policy.get("max_attempts", 1)) < 1:
        return None, None

    proposed = (
        repair_policy.get("browser_input")
        or repair_policy.get("retry_browser_input")
        or repair_policy.get("input")
    )
    if proposed is None:
        direct_fields = set(repair_policy) - REPAIR_POLICY_CONTROL_FIELDS
        if direct_fields:
            forbidden = sorted(direct_fields - ALLOWED_BROWSER_REPAIR_FIELDS)
            if forbidden:
                return None, f"forbidden browser repair fields: {', '.join(forbidden)}"
            proposed = {
                key: deepcopy(repair_policy[key])
                for key in direct_fields
                if key in ALLOWED_BROWSER_REPAIR_FIELDS
            }
    if proposed is None:
        return None, None
    try:
        return _sanitize_browser_repair_input(original_input, proposed), None
    except ValueError as exc:
        return None, str(exc)


def _sanitize_browser_repair_input(
    original_input: Any,
    proposed_input: Any,
) -> dict[str, Any]:
    if not isinstance(proposed_input, dict):
        raise ValueError("repair input must be a browser input object")
    forbidden = sorted(set(proposed_input) - ALLOWED_BROWSER_REPAIR_FIELDS)
    if forbidden:
        raise ValueError(f"forbidden browser repair fields: {', '.join(forbidden)}")
    repaired = dict(original_input) if isinstance(original_input, dict) else {}
    for key in ALLOWED_BROWSER_REPAIR_FIELDS:
        if key in proposed_input:
            repaired[key] = deepcopy(proposed_input[key])
    if "actions" in repaired and not isinstance(repaired["actions"], list):
        raise ValueError("browser repair actions must be a list")
    for index, action in enumerate(repaired.get("actions", []), start=1):
        if not isinstance(action, dict):
            raise ValueError(f"browser repair action {index} must be an object")
    return repaired


def _web_client_error(request_id: str, error: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "status": "error",
        "browser": None,
        "screenshot_path": None,
        "browser_screenshots": {},
        "selector_states": {},
        "capture_values": {},
        "error": error,
    }


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
