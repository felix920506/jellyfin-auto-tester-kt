"""Report generation helpers for Stage 3.

The report stage is mostly agent reasoning, but this module keeps the output
format deterministic and makes the one-pass verification request reproducible.
"""

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

    report = _render_report(
        execution_result=execution_result,
        verification_result=verification_result,
        output_dir=output_dir,
        artifacts_root=artifacts_root,
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


def build_verification_plan(
    original_result: dict[str, Any],
    written_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a verification ReproductionPlan from first-pass report steps."""

    if not isinstance(original_result, dict):
        raise TypeError("original_result must be a dict")
    original_result = hydrate_execution_result(original_result)
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
    trigger = _trigger_entry(execution_result)
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
    steps = written_steps or _minimal_steps(execution_result)
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
    parts = [
        "### Jellyfin Server Logs (relevant excerpt)",
        "",
        _log_excerpt(execution_result),
        "",
        "### HTTP Responses",
        "",
        _http_responses(execution_result),
    ]

    browser = _browser_evidence(execution_result)
    if browser:
        parts.extend(["", "### Browser Evidence", "", browser])

    screenshots = _screenshots(execution_result, output_dir, artifacts_root)
    if screenshots:
        parts.extend(["", "### Screenshots", "", screenshots])
    return "\n".join(parts)


def _log_excerpt(execution_result: dict[str, Any]) -> str:
    logs = _jellyfin_logs(execution_result)
    if not logs.strip():
        if _is_demo_plan(_plan(execution_result)):
            return "```text\nPublic demo server mode does not collect Jellyfin server logs.\n```"
        return "```text\nNo Jellyfin server logs were captured.\n```"

    indicators = _failure_indicators(execution_result)
    selected = []
    for line in logs.splitlines():
        if _relevant_log_line(line, indicators):
            selected.append(line)
        if len(selected) >= MAX_LOG_EXCERPT_LINES:
            break

    if not selected:
        selected = ["No ERROR/WARN lines or configured failure indicators were found in captured logs."]
    return "```text\n" + "\n".join(_truncate(line, 500) for line in selected) + "\n```"


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
    entries = [
        entry
        for entry in execution_result.get("execution_log", [])
        if isinstance(entry, dict) and isinstance(entry.get("http"), dict)
    ]
    if not entries:
        return "No HTTP responses were captured."

    step_by_id = {
        step.get("step_id"): step
        for step in _plan(execution_result).get("reproduction_steps", [])
        if isinstance(step, dict)
    }
    blocks = []
    for entry in entries:
        step = step_by_id.get(entry.get("step_id"), {})
        step_input = step.get("input") if isinstance(step.get("input"), dict) else {}
        method = _text(step_input.get("method"), "HTTP")
        path = _text(step_input.get("path"), f"step {entry.get('step_id')}")
        http = entry["http"]
        blocks.append(f"- `{method.upper()} {path}` -> HTTP {http.get('status_code')}")
        body = http.get("body")
        if body:
            blocks.append(_indent_code(_format_body(body), "json" if _looks_json(body) else "text", spaces=2))
    return "\n".join(blocks)


def _browser_evidence(execution_result: dict[str, Any]) -> str:
    entries = [
        entry
        for entry in execution_result.get("execution_log", [])
        if isinstance(entry, dict) and isinstance(entry.get("browser"), dict)
    ]
    if not entries:
        return ""

    blocks = []
    for entry in entries:
        browser = entry["browser"]
        actions = browser.get("actions") if isinstance(browser.get("actions"), list) else []
        action_summary = _browser_action_summary(actions)
        blocks.append(
            f"- Step {entry.get('step_id')} browser `{browser.get('status', 'unknown')}`"
            + (f": {action_summary}" if action_summary else "")
        )
        if browser.get("final_url"):
            blocks.append(f"  Final URL: `{_inline_code(browser['final_url'])}`")
        if browser.get("media_state"):
            state = browser.get("media_state", {}).get("state")
            blocks.append(f"  Media state: `{_inline_code(state or 'unknown')}`")
        console = browser.get("console") if isinstance(browser.get("console"), list) else []
        if console:
            blocks.append("  Console warnings/errors:")
            for item in console[:5]:
                if isinstance(item, dict):
                    blocks.append(
                        f"  - `{_inline_code(item.get('type', 'console'))}` "
                        f"{_text(item.get('text'), '')}"
                    )
        failed_network = browser.get("failed_network") if isinstance(browser.get("failed_network"), list) else []
        if failed_network:
            blocks.append("  Failed network responses:")
            for item in failed_network[:5]:
                if isinstance(item, dict):
                    status = item.get("status") or item.get("error") or "failed"
                    blocks.append(f"  - `{_inline_code(status)}` {_text(item.get('url'), '')}")
        if browser.get("dom_summary"):
            blocks.append(f"  DOM summary: {_truncate(str(browser['dom_summary']), 500)}")
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

    lines = []
    seen = set()
    for entry, path in paths:
        relative = _relative_artifact_path(path, output_dir, artifacts_root)
        if relative in seen:
            continue
        seen.add(relative)
        label = f"Step {entry.get('step_id')} screenshot"
        if path == entry.get("failure_screenshot_path"):
            label = f"Step {entry.get('step_id')} failure"
        lines.append(f"![{label}]({relative})")
    return "\n".join(lines)


def _analysis(execution_result: dict[str, Any]) -> str:
    lines = []
    overall = execution_result.get("overall_result")
    trigger = _trigger_entry(execution_result)
    lines.append(f"- Overall result: `{_text(overall, 'unknown')}`.")
    if trigger:
        lines.append(
            "- Trigger step: "
            f"`{_text(trigger.get('action'), 'unnamed')}` ended as "
            f"`{_text(trigger.get('outcome'), 'unknown')}`"
            + (f" with reason `{_inline_code(trigger['reason'])}`." if trigger.get("reason") else ".")
        )
    failures = [
        entry
        for entry in execution_result.get("execution_log", [])
        if isinstance(entry, dict) and entry.get("outcome") in {"fail", "skip"}
    ]
    if failures:
        for entry in failures[:5]:
            lines.append(
                f"- Step {entry.get('step_id')} `{_text(entry.get('action'), 'unnamed')}` "
                f"{entry.get('outcome')}: {_text(entry.get('reason'), 'no reason recorded')}."
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
