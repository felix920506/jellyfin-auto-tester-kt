"""Deterministic report generation helpers for Stage 3."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from tools.execution_result_handoff import hydrate_execution_result


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS_ROOT = Path(
    os.environ.get("JF_AUTO_TESTER_ARTIFACTS_ROOT", REPO_ROOT / "artifacts")
).resolve()
DEFAULT_ARTIFACTS_BASE = "/artifacts"
MAX_LOG_EXCERPT_LINES = 50
MAX_BODY_CHARS = 2000
MAX_STREAM_CHARS = 1200


def summarize_execution_result(execution_result: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic step and trigger status for an ExecutionResult."""

    if not isinstance(execution_result, dict):
        raise TypeError("execution_result must be a dict")
    execution_result = hydrate_execution_result(execution_result)

    steps, source = _step_statuses(execution_result)
    trigger = _trigger_status(execution_result, steps, source)
    return {
        "run_id": _text(execution_result.get("run_id"), "unknown"),
        "overall_result": execution_result.get("overall_result"),
        "source": source,
        "steps": steps,
        "trigger": trigger,
    }


def collect_report_evidence(
    execution_result: dict[str, Any],
    output_dir: str | Path | None = None,
    artifacts_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return deterministic evidence selected for report rendering."""

    if not isinstance(execution_result, dict):
        raise TypeError("execution_result must be a dict")
    execution_result = hydrate_execution_result(execution_result)

    resolved_output_dir = (
        Path(output_dir).expanduser()
        if output_dir is not None
        else Path(
            str(
                execution_result.get("artifacts_dir")
                or DEFAULT_ARTIFACTS_ROOT / _text(execution_result.get("run_id"), "unknown")
            )
        ).expanduser()
    )
    resolved_artifacts_root = (
        Path(artifacts_root).expanduser()
        if artifacts_root is not None
        else resolved_output_dir.parent
    )
    return {
        "logs": _log_evidence(execution_result),
        "http_responses": _http_evidence(execution_result),
        "browser": _browser_evidence_items(execution_result),
        "screenshots": _screenshot_evidence(
            execution_result,
            resolved_output_dir,
            resolved_artifacts_root,
        ),
    }


def select_report_steps(execution_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the deterministic minimal step set for report and verification."""

    if not isinstance(execution_result, dict):
        raise TypeError("execution_result must be a dict")
    execution_result = hydrate_execution_result(execution_result)
    return _minimal_steps(execution_result)


def render_report_markdown(
    execution_result: dict[str, Any],
    verification_result: dict[str, Any] | None = None,
    artifacts_base: str | Path = DEFAULT_ARTIFACTS_BASE,
    written_steps: list[dict[str, Any]] | None = None,
) -> str:
    """Render the full report Markdown without writing it to disk."""

    if not isinstance(execution_result, dict):
        raise TypeError("execution_result must be a dict")
    execution_result = hydrate_execution_result(execution_result)
    if verification_result is not None:
        if not isinstance(verification_result, dict):
            raise TypeError("verification_result must be a dict")
        verification_result = hydrate_execution_result(verification_result)

    run_id = _require_text(execution_result, "run_id")
    output_run_id = (
        _require_text(execution_result, "original_run_id")
        if execution_result.get("is_verification")
        else run_id
    )
    artifacts_root = _resolve_artifacts_root(execution_result, artifacts_base)
    return _render_report(
        execution_result=execution_result,
        verification_result=verification_result,
        output_dir=artifacts_root / output_run_id,
        artifacts_root=artifacts_root,
        written_steps=written_steps,
    )


def generate(
    execution_result: dict[str, Any],
    verification_result: dict[str, Any] | None = None,
    artifacts_base: str | Path = DEFAULT_ARTIFACTS_BASE,
    written_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate ``report.md`` for an ExecutionResult.

    The report is written under the original run's artifact directory. When
    called for a verification result, ``execution_result["original_run_id"]`` is
    used as the output directory. When called with a first-run result plus a
    separate ``verification_result``, the first-run ``run_id`` is used.
    """

    if not isinstance(execution_result, dict):
        raise TypeError("execution_result must be a dict")
    execution_result = hydrate_execution_result(execution_result)
    if verification_result is not None:
        if not isinstance(verification_result, dict):
            raise TypeError("verification_result must be a dict")
        verification_result = hydrate_execution_result(verification_result)

    run_id = _require_text(execution_result, "run_id")
    output_run_id = (
        _require_text(execution_result, "original_run_id")
        if execution_result.get("is_verification")
        else run_id
    )

    artifacts_root = _resolve_artifacts_root(execution_result, artifacts_base)
    output_dir = artifacts_root / output_run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    report = render_report_markdown(
        execution_result,
        verification_result=verification_result,
        artifacts_base=artifacts_base,
        written_steps=written_steps,
    )
    path = output_dir / "report.md"
    path.write_text(report, encoding="utf-8")

    verified = _verification_passed(execution_result, verification_result)
    return {
        "path": str(path),
        "word_count": len(report.split()),
        "verified": verified,
        "verification_status": _verification_status(verified),
    }


def load_original_context(
    verification_result: dict[str, Any],
    artifacts_base: str | Path = DEFAULT_ARTIFACTS_BASE,
) -> dict[str, Any]:
    """Load the first-run context for a verification ExecutionResult."""

    if not isinstance(verification_result, dict):
        raise TypeError("verification_result must be a dict")
    verification_result = hydrate_execution_result(verification_result)

    original_run_id = _require_text(verification_result, "original_run_id")
    artifacts_root = _resolve_artifacts_root(verification_result, artifacts_base)
    original_dir = artifacts_root / original_run_id
    result_path = original_dir / "result.json"
    report_path = original_dir / "report.md"

    original_result = hydrate_execution_result(
        _read_json_object(result_path, "original result")
    )
    if not report_path.is_file():
        raise FileNotFoundError(f"original report not found: {report_path}")

    return {
        "original_result": original_result,
        "report_path": str(report_path),
        "report_markdown": report_path.read_text(encoding="utf-8"),
    }


def build_verification_plan(
    original_result: dict[str, Any],
    written_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a verification ReproductionPlan from deterministic report steps."""

    if not isinstance(original_result, dict):
        raise TypeError("original_result must be a dict")
    original_result = hydrate_execution_result(original_result)
    if written_steps is None:
        written_steps = select_report_steps(original_result)
    if not isinstance(written_steps, list) or not written_steps:
        raise ValueError("written_steps must be a non-empty list")

    original_run_id = _require_text(original_result, "run_id")
    original_plan = original_result.get("plan")
    if not isinstance(original_plan, dict):
        raise ValueError("original_result.plan must be an object")

    steps = [_normalize_step(step, index) for index, step in enumerate(written_steps, start=1)]
    trigger_count = sum(1 for step in steps if step.get("role") == "trigger")
    if trigger_count != 1:
        raise ValueError("written_steps must contain exactly one trigger step")

    verification_plan = deepcopy(original_plan)
    if "docker_image" in original_plan:
        verification_plan["docker_image"] = deepcopy(original_plan.get("docker_image"))
    else:
        verification_plan.pop("docker_image", None)
    if "server_target" in original_plan:
        verification_plan["server_target"] = deepcopy(original_plan["server_target"])
    verification_plan["environment"] = deepcopy(original_plan.get("environment") or {})
    verification_plan["prerequisites"] = deepcopy(original_plan.get("prerequisites") or [])
    verification_plan["reproduction_steps"] = steps
    verification_plan["is_verification"] = True
    verification_plan["original_run_id"] = original_run_id
    return verification_plan


def _render_report(
    execution_result: dict[str, Any],
    verification_result: dict[str, Any] | None,
    output_dir: Path,
    artifacts_root: Path,
    written_steps: list[dict[str, Any]] | None,
) -> str:
    plan = _plan(execution_result)
    result_label = _result_label(execution_result.get("overall_result"))
    verified = _verification_passed(execution_result, verification_result)
    verification_status = _verification_status(verified)
    run_id = str(execution_result.get("run_id", "unknown"))

    sections = [
        f"# Reproduction Report: {_text(plan.get('issue_title'), 'Untitled Jellyfin issue')}",
        "",
        f"**Issue:** {_text(plan.get('issue_url'), 'Unknown')}",
        f"**Jellyfin Version:** {_text(plan.get('target_version'), 'Unknown')}",
        f"**Result:** {result_label}",
        f"**Verified:** {verification_status}",
        f"**Run ID:** {run_id}",
        f"**Date:** {datetime.now(timezone.utc).isoformat()}",
        "",
        "---",
        "",
        "## Summary",
        "",
        _summary(execution_result, verification_result),
        "",
        "## Environment",
        "",
        _environment_table(plan, execution_result),
        "",
        "## Prerequisites",
        "",
        _prerequisites(plan),
        "",
        "## Reproduction Steps",
        "",
        _steps_intro(plan),
        "",
        _reproduction_steps(execution_result, written_steps),
        "",
        "## Evidence",
        "",
        _evidence(execution_result, output_dir, artifacts_root),
        "",
        "## Analysis",
        "",
        _analysis(execution_result),
        "",
        "## Verification",
        "",
        _verification_section(execution_result, verification_result),
    ]

    if verification_result is not None and verified is False:
        sections.extend(
            [
                "",
                "## Verification Failure",
                "",
                _verification_failure_section(execution_result, verification_result),
            ]
        )

    sections.extend(["", "## Notes for Maintainers", "", _notes(execution_result)])
    return "\n".join(sections).rstrip() + "\n"


def _summary(
    execution_result: dict[str, Any],
    verification_result: dict[str, Any] | None,
) -> str:
    plan = _plan(execution_result)
    version = _text(plan.get("target_version"), "the target version")
    overall = execution_result.get("overall_result")
    trigger = summarize_execution_result(execution_result).get("trigger") or {}
    if overall == "reproduced":
        sentence = (
            f"The automated run reproduced the reported issue on Jellyfin {version}; "
            "the trigger step observed the expected failure symptom."
        )
    elif overall == "not_reproduced":
        sentence = (
            f"The automated run did not reproduce the reported issue on Jellyfin {version}; "
            "the trigger step did not meet its failure criteria."
        )
    else:
        reason = _text(execution_result.get("error_summary") or (trigger or {}).get("reason"), "the run was blocked")
        sentence = f"The automated run was inconclusive on Jellyfin {version}: {reason}."

    verified = _verification_passed(execution_result, verification_result)
    if verified is True:
        return f"{sentence} A second run using only the written steps produced a consistent result."
    if verified is False:
        return f"{sentence} A second run using only the written steps did not produce a consistent result."
    return sentence


def _environment_table(plan: dict[str, Any], execution_result: dict[str, Any]) -> str:
    host = execution_result.get("host") or execution_result.get("host_environment") or {}
    host_os = _first_text(
        host.get("os") if isinstance(host, dict) else None,
        execution_result.get("host_os"),
        "Unknown",
    )
    architecture = _first_text(
        host.get("architecture") if isinstance(host, dict) else None,
        execution_result.get("architecture"),
        "Unknown",
    )
    if _is_demo_plan(plan):
        server_target = _server_target(plan)
        return "\n".join(
            [
                "| Field | Value |",
                "|---|---|",
                "| Server Target | `Public demo server` |",
                f"| Demo URL | `{_escape_table(_demo_url(plan))}` |",
                f"| Release Track | `{_escape_table(_text(server_target.get('release_track'), 'stable'))}` |",
                f"| Host OS | `{_escape_table(host_os)}` |",
                f"| Architecture | `{_escape_table(architecture)}` |",
            ]
        )
    docker_image = _text(plan.get("docker_image"), "Unknown")
    return "\n".join(
        [
            "| Field | Value |",
            "|---|---|",
            f"| Docker Image | `{_escape_table(docker_image)}` |",
            f"| Host OS | `{_escape_table(host_os)}` |",
            f"| Architecture | `{_escape_table(architecture)}` |",
        ]
    )


def _prerequisites(plan: dict[str, Any]) -> str:
    if _is_demo_plan(plan):
        server_target = _server_target(plan)
        username = _text(server_target.get("username"), "demo")
        password = _text(server_target.get("password"), "")
        password_text = "blank password" if password == "" else "configured password"
        lines = [
            f"- Public demo server: `{_inline_code(_demo_url(plan))}`",
            f"- Login as `{_inline_code(username)}` with {password_text}.",
            "- No Docker container, custom media setup, admin configuration, or Jellyfin server logs are expected in demo mode.",
        ]
        prereqs = plan.get("prerequisites") if isinstance(plan.get("prerequisites"), list) else []
        if prereqs:
            lines.append("- Additional browser-only prerequisites declared by the plan:")
            for prereq in prereqs:
                if isinstance(prereq, dict):
                    lines.append(f"  - {_text(prereq.get('description'), 'Unspecified prerequisite')}")
                else:
                    lines.append(f"  - {_text(prereq)}")
        else:
            lines.append("- No additional media files or library state were declared in the plan.")
        return "\n".join(lines)

    version = _text(plan.get("target_version"), "<version>")
    docker_image = _text(plan.get("docker_image"), f"jellyfin/jellyfin:{version}")
    env = plan.get("environment") if isinstance(plan.get("environment"), dict) else {}
    ports = env.get("ports") if isinstance(env.get("ports"), dict) else {}
    host_port = ports.get("host", 8096)
    container_port = ports.get("container", 8096)
    lines = [
        "- Docker installed and running",
        f"- Jellyfin `{version}` started and healthy:",
        "  ```bash",
        f"  docker run -d --name jf-test -p {host_port}:{container_port} \\",
        "    -v /tmp/jellyfin-media:/media \\",
        f"    {docker_image}",
        f"  # wait until: curl -s http://localhost:{host_port}/health returns \"Healthy\"",
        "  ```",
    ]
    prereqs = plan.get("prerequisites") if isinstance(plan.get("prerequisites"), list) else []
    if prereqs:
        lines.append("- Additional plan prerequisites:")
        for prereq in prereqs:
            if not isinstance(prereq, dict):
                lines.append(f"  - {_text(prereq)}")
                continue
            description = _text(prereq.get("description"), "Unspecified prerequisite")
            source = prereq.get("source")
            if source:
                lines.append(f"  - {description} (`source`: `{_inline_code(source)}`)")
            else:
                lines.append(f"  - {description}")
    else:
        lines.append("- No additional media files or library state were declared in the plan.")
    return "\n".join(lines)


def _steps_intro(plan: dict[str, Any]) -> str:
    if _is_demo_plan(plan):
        return "> Steps run against the public demo server. Each browser step was executed and verified by the automated pipeline."
    return "> Steps begin after Jellyfin is healthy. Each step was executed and verified by the automated pipeline."


def _reproduction_steps(
    execution_result: dict[str, Any],
    written_steps: list[dict[str, Any]] | None,
) -> str:
    steps = written_steps or select_report_steps(execution_result)
    if not steps:
        return "No executable reproduction steps were available in the execution result."

    entry_by_id = {
        entry.get("step_id"): entry
        for entry in execution_result.get("execution_log", [])
        if isinstance(entry, dict)
    }
    blocks = []
    for number, step in enumerate(steps, start=1):
        entry = entry_by_id.get(step.get("step_id"), {})
        blocks.append(_step_block(number, step, entry if isinstance(entry, dict) else {}))
    return "\n\n".join(blocks)


def _minimal_steps(execution_result: dict[str, Any]) -> list[dict[str, Any]]:
    plan_steps = _plan(execution_result).get("reproduction_steps")
    if not isinstance(plan_steps, list):
        return []
    trigger_index = next(
        (index for index, step in enumerate(plan_steps) if isinstance(step, dict) and step.get("role") == "trigger"),
        None,
    )
    if trigger_index is None:
        return [deepcopy(step) for step in plan_steps if isinstance(step, dict)]

    selected: list[dict[str, Any]] = []
    selected.extend(
        deepcopy(step)
        for step in plan_steps[:trigger_index]
        if isinstance(step, dict) and step.get("role") == "setup"
    )
    selected.append(deepcopy(plan_steps[trigger_index]))
    selected.extend(
        deepcopy(step)
        for step in plan_steps[trigger_index + 1 :]
        if isinstance(step, dict) and step.get("role") == "verify"
    )
    return selected


def _step_block(number: int, step: dict[str, Any], entry: dict[str, Any]) -> str:
    action = _text(step.get("action"), f"Step {number}")
    lines = [f"{number}. **{action}**"]
    lines.extend(_step_invocation_lines(step))
    expected = _text(step.get("expected_outcome"), "Expected outcome was not specified.")
    lines.append(f"   - Expected outcome: {expected}")
    if entry:
        observed = _observed_step_summary(entry)
        lines.append(f"   - Observed outcome: {observed}")
    return "\n".join(lines)


def _step_invocation_lines(step: dict[str, Any]) -> list[str]:
    tool = step.get("tool")
    step_input = step.get("input") if isinstance(step.get("input"), dict) else {}
    if tool in {"bash", "docker_exec"}:
        command = _text(step_input.get("command"), "")
        if command:
            return [
                f"   - Run with `{tool}`:",
                "",
                _indent_code(command, "bash", spaces=5),
            ]
    if tool == "http_request":
        method = _text(step_input.get("method"), "GET")
        path = _text(step_input.get("path"), "/")
        lines = [f"   - Send HTTP request: `{method.upper()} {path}`"]
        if step_input.get("auth"):
            lines.append(f"   - Auth mode: `{_inline_code(str(step_input['auth']))}`")
        if step_input.get("params"):
            lines.append(f"   - Params: `{_inline_code(json.dumps(step_input['params'], sort_keys=True, default=str))}`")
        if step_input.get("headers"):
            lines.append(f"   - Headers: `{_inline_code(json.dumps(step_input['headers'], sort_keys=True, default=str))}`")
        if "body_json" in step_input:
            lines.extend(["   - JSON body:", "", _indent_code(_json_or_text(step_input.get("body_json")), "json", spaces=5)])
        if "body_text" in step_input:
            lines.extend(["   - Text body:", "", _indent_code(str(step_input.get("body_text") or ""), "text", spaces=5)])
        if "body_base64" in step_input:
            lines.extend(["   - Base64 body:", "", _indent_code(str(step_input.get("body_base64") or ""), "text", spaces=5)])
        return lines
    if tool == "screenshot":
        target = step_input.get("url") or step_input.get("path") or "/web"
        label = step_input.get("label") or f"step_{step.get('step_id', 'unknown')}"
        return [f"   - Capture screenshot `{_inline_code(label)}` at `{_inline_code(target)}`."]
    if tool == "browser":
        target = step_input.get("url") or step_input.get("path") or "current browser page"
        actions = step_input.get("actions") if isinstance(step_input.get("actions"), list) else []
        summary = ", ".join(
            str(action.get("type"))
            for action in actions
            if isinstance(action, dict) and action.get("type")
        )
        lines = [f"   - Run browser flow at `{_inline_code(target)}`."]
        if summary:
            lines.append(f"   - Browser actions: `{_inline_code(summary)}`")
        return lines
    return [f"   - Tool input: `{_inline_code(json.dumps(step_input, sort_keys=True, default=str))}`"]


def _observed_step_summary(entry: dict[str, Any]) -> str:
    parts = [str(entry.get("outcome", "unknown"))]
    if entry.get("reason"):
        parts.append(str(entry["reason"]))
    http = entry.get("http") if isinstance(entry.get("http"), dict) else None
    if http and http.get("status_code") is not None:
        parts.append(f"HTTP {http['status_code']}")
    if entry.get("exit_code") is not None:
        parts.append(f"exit code {entry['exit_code']}")
    return "; ".join(parts)


def _evidence(
    execution_result: dict[str, Any],
    output_dir: Path,
    artifacts_root: Path,
) -> str:
    evidence = collect_report_evidence(execution_result, output_dir, artifacts_root)
    parts = [
        "### Jellyfin Server Logs (relevant excerpt)",
        "",
        _format_log_evidence(evidence["logs"]),
        "",
        "### HTTP Responses",
        "",
        _format_http_evidence(evidence["http_responses"]),
    ]

    browser = _format_browser_evidence(evidence["browser"])
    if browser:
        parts.extend(["", "### Browser Evidence", "", browser])

    screenshots = _format_screenshot_evidence(evidence["screenshots"])
    if screenshots:
        parts.extend(["", "### Screenshots", "", screenshots])
    return "\n".join(parts)


def _log_excerpt(execution_result: dict[str, Any]) -> str:
    return _format_log_evidence(_log_evidence(execution_result))


def _log_evidence(execution_result: dict[str, Any]) -> dict[str, Any]:
    logs = _jellyfin_logs(execution_result)
    if not logs.strip():
        if _is_demo_plan(_plan(execution_result)):
            return {
                "available": False,
                "lines": [],
                "message": "Public demo server mode does not collect Jellyfin server logs.",
            }
        return {
            "available": False,
            "lines": [],
            "message": "No Jellyfin server logs were captured.",
        }

    indicators = _failure_indicators(execution_result)
    selected = []
    for line in logs.splitlines():
        if _relevant_log_line(line, indicators):
            selected.append(line)
        if len(selected) >= MAX_LOG_EXCERPT_LINES:
            break

    if not selected:
        return {
            "available": True,
            "lines": [],
            "message": "No ERROR/WARN lines or configured failure indicators were found in captured logs.",
        }
    return {
        "available": True,
        "lines": [_truncate(line, 500) for line in selected],
        "message": None,
    }


def _format_log_evidence(logs: dict[str, Any]) -> str:
    lines = logs.get("lines") if isinstance(logs.get("lines"), list) else []
    message = _text(logs.get("message"))
    content = "\n".join(str(line) for line in lines) if lines else message
    return "```text\n" + _text(content, "No Jellyfin server logs were captured.") + "\n```"


def _jellyfin_logs(execution_result: dict[str, Any]) -> str:
    if execution_result.get("jellyfin_logs"):
        return str(execution_result["jellyfin_logs"])
    artifacts_dir = execution_result.get("artifacts_dir")
    if artifacts_dir:
        log_path = Path(str(artifacts_dir)) / "jellyfin_server.log"
        if log_path.exists():
            return log_path.read_text(encoding="utf-8", errors="replace")
    return ""


def _failure_indicators(execution_result: dict[str, Any]) -> list[str]:
    plan = _plan(execution_result)
    indicators = [
        str(item)
        for item in plan.get("failure_indicators", [])
        if item is not None and str(item)
    ]
    for step in plan.get("reproduction_steps", []) if isinstance(plan.get("reproduction_steps"), list) else []:
        if not isinstance(step, dict):
            continue
        criteria = step.get("success_criteria")
        if not isinstance(criteria, dict):
            continue
        for assertion in criteria.get("all_of") or criteria.get("any_of") or []:
            if isinstance(assertion, dict) and assertion.get("type") == "log_matches":
                indicators.append(str(assertion.get("pattern", "")))
    return [item for item in indicators if item]


def _relevant_log_line(line: str, indicators: Iterable[str]) -> bool:
    upper = line.upper()
    if "ERROR" in upper or "WARN" in upper:
        return True
    for indicator in indicators:
        if not indicator:
            continue
        try:
            if re.search(indicator, line, flags=re.IGNORECASE):
                return True
        except re.error:
            if indicator.lower() in line.lower():
                return True
    return False


def _http_responses(execution_result: dict[str, Any]) -> str:
    return _format_http_evidence(_http_evidence(execution_result))


def _http_evidence(execution_result: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [
        entry
        for entry in execution_result.get("execution_log", [])
        if isinstance(entry, dict) and isinstance(entry.get("http"), dict)
    ]

    step_by_id = {
        step.get("step_id"): step
        for step in _plan(execution_result).get("reproduction_steps", [])
        if isinstance(step, dict)
    }
    responses = []
    for entry in entries:
        step = step_by_id.get(entry.get("step_id"), {})
        step_input = step.get("input") if isinstance(step.get("input"), dict) else {}
        method = _text(step_input.get("method"), "HTTP")
        path = _text(step_input.get("path"), f"step {entry.get('step_id')}")
        http = entry["http"]
        body = http.get("body")
        responses.append(
            {
                "step_id": entry.get("step_id"),
                "method": method.upper(),
                "path": path,
                "status_code": http.get("status_code"),
                "body": _format_body(body) if body else None,
                "body_format": "json" if body and _looks_json(body) else "text",
            }
        )
    return responses


def _format_http_evidence(responses: list[dict[str, Any]]) -> str:
    if not responses:
        return "No HTTP responses were captured."
    blocks = []
    for response in responses:
        blocks.append(
            f"- `{response.get('method', 'HTTP')} {response.get('path', '/')}` "
            f"-> HTTP {response.get('status_code')}"
        )
        if response.get("body"):
            blocks.append(
                _indent_code(
                    str(response["body"]),
                    _text(response.get("body_format"), "text"),
                    spaces=2,
                )
            )
    return "\n".join(blocks)


def _browser_evidence(execution_result: dict[str, Any]) -> str:
    return _format_browser_evidence(_browser_evidence_items(execution_result))


def _browser_evidence_items(execution_result: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [
        entry
        for entry in execution_result.get("execution_log", [])
        if isinstance(entry, dict) and isinstance(entry.get("browser"), dict)
    ]
    items = []
    for entry in entries:
        browser = entry["browser"]
        actions = browser.get("actions") if isinstance(browser.get("actions"), list) else []
        console = browser.get("console") if isinstance(browser.get("console"), list) else []
        failed_network = browser.get("failed_network") if isinstance(browser.get("failed_network"), list) else []
        media_state = browser.get("media_state") if isinstance(browser.get("media_state"), dict) else {}
        items.append(
            {
                "step_id": entry.get("step_id"),
                "status": browser.get("status", "unknown"),
                "action_summary": _browser_action_summary(actions),
                "final_url": browser.get("final_url"),
                "media_state": media_state.get("state"),
                "console": [
                    {"type": item.get("type", "console"), "text": _text(item.get("text"), "")}
                    for item in console[:5]
                    if isinstance(item, dict)
                ],
                "failed_network": [
                    {
                        "status": item.get("status") or item.get("error") or "failed",
                        "url": _text(item.get("url"), ""),
                    }
                    for item in failed_network[:5]
                    if isinstance(item, dict)
                ],
                "dom_summary": _truncate(str(browser["dom_summary"]), 500)
                if browser.get("dom_summary")
                else None,
            }
        )
    return items


def _format_browser_evidence(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    blocks = []
    for browser_item in items:
        action_summary = _text(browser_item.get("action_summary"))
        blocks.append(
            f"- Step {browser_item.get('step_id')} browser `{browser_item.get('status', 'unknown')}`"
            + (f": {action_summary}" if action_summary else "")
        )
        if browser_item.get("final_url"):
            blocks.append(f"  Final URL: `{_inline_code(browser_item['final_url'])}`")
        if browser_item.get("media_state"):
            blocks.append(f"  Media state: `{_inline_code(browser_item['media_state'])}`")
        console = browser_item.get("console") if isinstance(browser_item.get("console"), list) else []
        if console:
            blocks.append("  Console warnings/errors:")
            for console_item in console[:5]:
                if isinstance(console_item, dict):
                    blocks.append(
                        f"  - `{_inline_code(console_item.get('type', 'console'))}` "
                        f"{_text(console_item.get('text'), '')}"
                    )
        failed_network = (
            browser_item.get("failed_network")
            if isinstance(browser_item.get("failed_network"), list)
            else []
        )
        if failed_network:
            blocks.append("  Failed network responses:")
            for failed in failed_network[:5]:
                if isinstance(failed, dict):
                    blocks.append(f"  - `{_inline_code(failed.get('status', 'failed'))}` {_text(failed.get('url'), '')}")
        if browser_item.get("dom_summary"):
            blocks.append(f"  DOM summary: {browser_item['dom_summary']}")
    return "\n".join(blocks)


def _browser_action_summary(actions: list[Any]) -> str:
    parts = []
    for action in actions[:12]:
        if not isinstance(action, dict):
            continue
        label = str(action.get("type") or "action")
        if action.get("selector"):
            label += f" {action['selector']}"
        if action.get("status") == "fail" and action.get("error"):
            label += f" failed ({action['error']})"
        parts.append(label)
    if len(actions) > 12:
        parts.append("...")
    return ", ".join(parts)


def _screenshots(
    execution_result: dict[str, Any],
    output_dir: Path,
    artifacts_root: Path,
) -> str:
    return _format_screenshot_evidence(
        _screenshot_evidence(execution_result, output_dir, artifacts_root)
    )


def _screenshot_evidence(
    execution_result: dict[str, Any],
    output_dir: Path,
    artifacts_root: Path,
) -> list[dict[str, Any]]:
    paths = []
    for entry in execution_result.get("execution_log", []):
        if not isinstance(entry, dict):
            continue
        for key in ("screenshot_path", "failure_screenshot_path"):
            if entry.get(key):
                paths.append((entry, str(entry[key])))
        browser = entry.get("browser") if isinstance(entry.get("browser"), dict) else {}
        for path in browser.get("screenshot_paths", []) if isinstance(browser.get("screenshot_paths"), list) else []:
            if path:
                paths.append((entry, str(path)))

    screenshots = []
    seen = set()
    for entry, path in paths:
        relative = _relative_artifact_path(path, output_dir, artifacts_root)
        if relative in seen:
            continue
        seen.add(relative)
        label = f"Step {entry.get('step_id')} screenshot"
        if path == entry.get("failure_screenshot_path"):
            label = f"Step {entry.get('step_id')} failure"
        screenshots.append(
            {
                "step_id": entry.get("step_id"),
                "path": path,
                "relative_path": relative,
                "label": label,
            }
        )
    return screenshots


def _format_screenshot_evidence(screenshots: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"![{_text(item.get('label'), 'Screenshot')}]({item.get('relative_path')})"
        for item in screenshots
    )


def _analysis(execution_result: dict[str, Any]) -> str:
    lines = []
    summary = summarize_execution_result(execution_result)
    trigger = summary.get("trigger") if isinstance(summary.get("trigger"), dict) else None
    lines.append(f"- Overall result: `{_text(summary.get('overall_result'), 'unknown')}`.")
    if trigger:
        lines.append(
            "- Trigger step: "
            f"`{_text(trigger.get('action'), 'unnamed')}` ended as "
            f"`{_text(trigger.get('status'), 'unknown')}`"
            + (f" with reason `{_inline_code(trigger['reason'])}`." if trigger.get("reason") else ".")
        )
    failures = [
        step
        for step in summary.get("steps", [])
        if isinstance(step, dict) and step.get("status") in {"fail", "skip", "inconclusive"}
    ]
    if failures:
        for step in failures[:5]:
            lines.append(
                f"- Step {step.get('step_id')} `{_text(step.get('action'), 'unnamed')}` "
                f"{step.get('status')}: {_text(step.get('reason'), 'no reason recorded')}."
            )
    else:
        lines.append("- All executed steps met their structured success criteria.")

    excerpt = _plain_log_excerpt(execution_result)
    if excerpt:
        lines.append("- Relevant Jellyfin log lines were captured in the Evidence section.")
    if execution_result.get("error_summary"):
        lines.append(f"- Error summary: {_text(execution_result['error_summary'])}.")
    return "\n".join(lines)


def _verification_section(
    execution_result: dict[str, Any],
    verification_result: dict[str, Any] | None,
) -> str:
    if verification_result is None:
        return "**Verification Run ID:** Pending\n**Result:** Pending\n\nVerification has not run yet."

    passed = _verification_passed(execution_result, verification_result)
    status = "Passed" if passed else "Failed"
    lines = [
        f"**Verification Run ID:** {_text(verification_result.get('run_id'), 'Unknown')}",
        f"**Result:** {status}",
        "",
        _verification_comparison(execution_result, verification_result),
    ]
    return "\n".join(lines)


def _verification_failure_section(
    execution_result: dict[str, Any],
    verification_result: dict[str, Any],
) -> str:
    return "\n".join(
        [
            _verification_comparison(execution_result, verification_result),
            "",
            f"- Original run artifacts: `{_inline_code(execution_result.get('artifacts_dir', 'unknown'))}`",
            f"- Verification run artifacts: `{_inline_code(verification_result.get('artifacts_dir', 'unknown'))}`",
        ]
    )


def _verification_comparison(
    execution_result: dict[str, Any],
    verification_result: dict[str, Any],
) -> str:
    original_trigger = _trigger_entry(execution_result) or {}
    verification_trigger = _trigger_entry(verification_result) or {}
    lines = [
        "Original result "
        f"`{_text(execution_result.get('overall_result'), 'unknown')}`; "
        "verification result "
        f"`{_text(verification_result.get('overall_result'), 'unknown')}`.",
        "Original trigger outcome "
        f"`{_text(original_trigger.get('outcome'), 'unknown')}`; "
        "verification trigger outcome "
        f"`{_text(verification_trigger.get('outcome'), 'unknown')}`.",
    ]
    if verification_trigger.get("reason"):
        lines.append(f"Verification trigger reason: `{_inline_code(verification_trigger['reason'])}`.")
    return " ".join(lines)


def _notes(execution_result: dict[str, Any]) -> str:
    plan = _plan(execution_result)
    notes = []
    ambiguities = plan.get("ambiguities") if isinstance(plan.get("ambiguities"), list) else []
    if ambiguities:
        notes.append("Plan ambiguities:")
        notes.extend(f"- {_text(item)}" for item in ambiguities)
    if not _screenshots(execution_result, Path("."), DEFAULT_ARTIFACTS_ROOT):
        notes.append("- No screenshots were captured for this run.")
    if execution_result.get("overall_result") == "not_reproduced":
        notes.append("- The issue may depend on Jellyfin version, media metadata, host platform, or timing differences not present in this run.")
    if execution_result.get("overall_result") == "inconclusive":
        notes.append("- Reproduction was blocked before a definitive trigger result could be observed.")
    if not notes:
        notes.append("- No additional caveats were recorded.")
    return "\n".join(notes)


def _verification_passed(
    execution_result: dict[str, Any],
    verification_result: dict[str, Any] | None,
) -> bool | None:
    if verification_result is None:
        return None
    if execution_result.get("overall_result") == "inconclusive":
        return False
    if verification_result.get("overall_result") != execution_result.get("overall_result"):
        return False

    original_trigger = _trigger_entry(execution_result)
    verification_trigger = _trigger_entry(verification_result)
    if not original_trigger or not verification_trigger:
        return False
    return original_trigger.get("outcome") == verification_trigger.get("outcome")


def _verification_status(verified: bool | None) -> str:
    if verified is None:
        return "Pending"
    return "Yes" if verified else "No"


def _result_label(value: Any) -> str:
    return {
        "reproduced": "Reproduced",
        "not_reproduced": "Not Reproduced",
        "inconclusive": "Inconclusive",
    }.get(value, "Inconclusive")


def _plan(execution_result: Mapping[str, Any]) -> dict[str, Any]:
    plan = execution_result.get("plan")
    return plan if isinstance(plan, dict) else {}


def _server_target(plan: Mapping[str, Any]) -> dict[str, Any]:
    value = plan.get("server_target")
    return dict(value) if isinstance(value, Mapping) else {"mode": "docker"}


def _is_demo_plan(plan: Mapping[str, Any]) -> bool:
    return str(_server_target(plan).get("mode") or "").lower() == "demo"


def _demo_url(plan: Mapping[str, Any]) -> str:
    server_target = _server_target(plan)
    if server_target.get("base_url"):
        return str(server_target["base_url"])
    release_track = str(server_target.get("release_track") or "stable").lower()
    if release_track == "unstable":
        return "https://demo.jellyfin.org/unstable"
    return "https://demo.jellyfin.org/stable"


def _trigger_entry(execution_result: Mapping[str, Any]) -> dict[str, Any] | None:
    for entry in execution_result.get("execution_log", []):
        if isinstance(entry, dict) and entry.get("role") == "trigger":
            return entry
    return None


def _step_statuses(execution_result: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    step_summaries = execution_result.get("step_summaries")
    if isinstance(step_summaries, list) and step_summaries:
        return [_status_from_step_summary(step) for step in step_summaries if isinstance(step, dict)], "step_summaries"
    return _legacy_step_statuses(execution_result), "execution_log"


def _status_from_step_summary(step: dict[str, Any]) -> dict[str, Any]:
    criteria = step.get("criteria_evaluation") if isinstance(step.get("criteria_evaluation"), dict) else None
    return {
        "step_id": step.get("step_id"),
        "role": step.get("role"),
        "action": _text(step.get("planned_action") or step.get("action"), "unnamed"),
        "tool": step.get("tool"),
        "status": _text(step.get("status"), "inconclusive"),
        "reason": step.get("reason"),
        "criteria_passed": criteria.get("passed") if criteria else None,
        "decisive_attempt_id": step.get("decisive_attempt_id"),
        "evidence_refs": deepcopy(step.get("evidence_refs") if isinstance(step.get("evidence_refs"), list) else []),
        "source": "step_summaries",
    }


def _legacy_step_statuses(execution_result: dict[str, Any]) -> list[dict[str, Any]]:
    entries_by_step: dict[str, list[dict[str, Any]]] = {}
    unkeyed: list[dict[str, Any]] = []
    for entry in execution_result.get("execution_log", []):
        if not isinstance(entry, dict):
            continue
        key = _step_key(entry.get("step_id"))
        if key is None:
            unkeyed.append(entry)
        else:
            entries_by_step.setdefault(key, []).append(entry)

    statuses: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    plan_steps = _plan(execution_result).get("reproduction_steps")
    if isinstance(plan_steps, list):
        for step in plan_steps:
            if not isinstance(step, dict):
                continue
            key = _step_key(step.get("step_id"))
            if key is None:
                continue
            seen_keys.add(key)
            statuses.append(
                _status_from_legacy_step(step, _decisive_execution_entry(entries_by_step.get(key, [])))
            )

    for key, entries in entries_by_step.items():
        if key in seen_keys:
            continue
        entry = _decisive_execution_entry(entries)
        if entry is not None:
            statuses.append(_status_from_legacy_step({}, entry))

    for entry in unkeyed:
        statuses.append(_status_from_legacy_step({}, entry))
    return statuses


def _status_from_legacy_step(
    plan_step: dict[str, Any],
    entry: dict[str, Any] | None,
) -> dict[str, Any]:
    criteria = entry.get("criteria_evaluation") if entry and isinstance(entry.get("criteria_evaluation"), dict) else None
    outcome = entry.get("outcome") if entry else None
    return {
        "step_id": plan_step.get("step_id") if plan_step else (entry or {}).get("step_id"),
        "role": plan_step.get("role") if plan_step else (entry or {}).get("role"),
        "action": _text(plan_step.get("action") if plan_step else (entry or {}).get("action"), "unnamed"),
        "tool": plan_step.get("tool") if plan_step else (entry or {}).get("tool"),
        "status": _text(outcome, "inconclusive"),
        "reason": (entry or {}).get("reason"),
        "criteria_passed": criteria.get("passed") if criteria else None,
        "decisive_attempt_id": (entry or {}).get("attempt_id"),
        "evidence_refs": [],
        "source": "execution_log",
    }


def _decisive_execution_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    complete = [
        entry
        for entry in entries
        if isinstance(entry.get("criteria_evaluation"), dict)
        and isinstance(entry["criteria_evaluation"].get("passed"), bool)
    ]
    if complete:
        return complete[-1]
    terminal = [entry for entry in entries if entry.get("outcome") in {"pass", "fail", "skip"}]
    if terminal:
        return terminal[-1]
    return entries[-1] if entries else None


def _trigger_status(
    execution_result: dict[str, Any],
    steps: list[dict[str, Any]],
    source: str,
) -> dict[str, Any] | None:
    trigger_summary = execution_result.get("trigger_summary")
    if isinstance(trigger_summary, dict):
        step_id = trigger_summary.get("step_id")
        matching_step = next((step for step in steps if step.get("step_id") == step_id), {})
        return {
            "step_id": step_id,
            "role": "trigger",
            "action": _text(matching_step.get("action") or trigger_summary.get("planned_action"), "trigger"),
            "tool": matching_step.get("tool"),
            "status": _text(trigger_summary.get("status"), "inconclusive"),
            "reason": trigger_summary.get("reason"),
            "criteria_passed": matching_step.get("criteria_passed"),
            "decisive_attempt_id": trigger_summary.get("decisive_attempt_id"),
            "evidence_refs": deepcopy(matching_step.get("evidence_refs") if isinstance(matching_step.get("evidence_refs"), list) else []),
            "source": "trigger_summary",
        }
    for step in steps:
        if step.get("role") == "trigger":
            return deepcopy(step)
    if source == "execution_log":
        entry = _trigger_entry(execution_result)
        if entry:
            return _status_from_legacy_step({}, entry)
    return None


def _step_key(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _plain_log_excerpt(execution_result: dict[str, Any]) -> str:
    excerpt = _log_excerpt(execution_result)
    return excerpt.replace("```text", "").replace("```", "").strip()


def _normalize_step(step: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ValueError("each written step must be an object")
    normalized = deepcopy(step)
    normalized.setdefault("step_id", index)
    missing = [
        key
        for key in ("step_id", "role", "action", "tool", "input", "expected_outcome", "success_criteria")
        if key not in normalized
    ]
    if missing:
        raise ValueError(f"written step {index} is missing required keys: {', '.join(missing)}")
    if not isinstance(normalized.get("input"), dict):
        raise ValueError(f"written step {index}.input must be an object")
    return normalized


def _resolve_artifacts_root(
    execution_result: dict[str, Any],
    artifacts_base: str | Path,
) -> Path:
    base = Path(artifacts_base).expanduser()
    artifacts_dir = execution_result.get("artifacts_dir")
    if str(artifacts_base) == DEFAULT_ARTIFACTS_BASE and artifacts_dir:
        return Path(str(artifacts_dir)).expanduser().resolve().parent
    if str(artifacts_base) == DEFAULT_ARTIFACTS_BASE:
        return DEFAULT_ARTIFACTS_ROOT
    return base.resolve()


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _relative_artifact_path(path: str, output_dir: Path, artifacts_root: Path) -> str:
    target = Path(path).expanduser()
    try:
        return target.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        pass
    try:
        return target.resolve().relative_to(artifacts_root.resolve()).as_posix()
    except ValueError:
        pass
    return os.path.relpath(target, output_dir).replace(os.sep, "/")


def _format_body(body: Any) -> str:
    text = str(body)
    if _looks_json(text):
        try:
            return _truncate(json.dumps(json.loads(text), indent=2, sort_keys=True), MAX_BODY_CHARS)
        except json.JSONDecodeError:
            pass
    return _truncate(text, MAX_BODY_CHARS)


def _looks_json(value: Any) -> bool:
    text = str(value).strip()
    return text.startswith("{") or text.startswith("[")


def _json_or_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True)
    return _format_body(value)


def _indent_code(value: str, language: str, spaces: int) -> str:
    indent = " " * spaces
    fence = f"```{language}".rstrip()
    body = str(value).rstrip("\n")
    return "\n".join(f"{indent}{line}" for line in [fence, *body.splitlines(), "```"])


def _truncate(value: str, max_chars: int) -> str:
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 15].rstrip() + "\n...[truncated]"


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _first_text(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _require_text(mapping: Mapping[str, Any], key: str) -> str:
    value = _text(mapping.get(key))
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _inline_code(value: Any) -> str:
    return str(value).replace("`", "\\`")


def _escape_table(value: Any) -> str:
    return _inline_code(value).replace("|", "\\|")
