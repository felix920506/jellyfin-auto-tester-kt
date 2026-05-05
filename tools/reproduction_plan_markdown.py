"""Human-readable Markdown handoff helpers for Stage 1 plans."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping


PLAN_MARKDOWN_TITLE = "ReproductionPlan Markdown v1"
REQUIRED_SECTIONS = (
    "Goal",
    "Issue Context",
    "Execution Target",
    "Environment",
    "Prerequisites",
    "Steps",
    "Failure Indicators",
    "Confidence",
    "Ambiguities",
)
ALLOWED_STEP_TOOLS = {"bash", "http_request", "screenshot", "docker_exec", "browser"}
ALLOWED_STEP_ROLES = {"setup", "trigger", "verify"}
ALLOWED_EXECUTION_TARGETS = {"standard", "web_client"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
DEMO_SERVER_URLS = {
    "stable": "https://demo.jellyfin.org/stable",
    "unstable": "https://demo.jellyfin.org/unstable",
}
DEFAULT_ENVIRONMENT = {
    "ports": {"host": 8096, "container": 8096},
    "volumes": [],
    "env_vars": {},
}

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_STEP_RE = re.compile(r"^###\s+Step\s+(\d+)(?::\s*(.*?))?\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$", re.MULTILINE)
_KEY_VALUE_RE = re.compile(r"^\s*[-*]\s+([^:\n]+):\s*(.*?)\s*$")
_FENCE_RE = re.compile(r"```([A-Za-z0-9_-]*)\s*\n?(.*?)\n?```", re.DOTALL)


class ReproductionPlanMarkdownError(ValueError):
    """Raised when a Markdown handoff cannot be accepted."""


def parse_reproduction_plan_markdown(markdown: str) -> dict[str, Any]:
    """Inspect and lint a ``ReproductionPlan Markdown v1`` handoff.

    The Stage 1 handoff is intentionally written for a human or AI Stage 2
    agent. This function therefore validates the document contract and extracts
    enough routing metadata for the pipeline fabric, but it does not compile the
    full deterministic runner schema.
    """

    if not isinstance(markdown, str) or not markdown.strip():
        raise ReproductionPlanMarkdownError("plan Markdown is empty")

    text = markdown.strip()
    if not re.search(
        rf"^#\s+{re.escape(PLAN_MARKDOWN_TITLE)}\s*$",
        text,
        re.MULTILINE,
    ):
        raise ReproductionPlanMarkdownError(
            f"missing '# {PLAN_MARKDOWN_TITLE}' title"
        )

    sections = _split_sections(text)
    _validate_required_sections(sections)
    _validate_json_fence_scope(text)

    goal = _parse_key_value_bullets(sections["Goal"])
    target = _parse_key_value_bullets(sections["Execution Target"])
    execution_target = str(target.get("execution_target") or "standard").strip()
    if execution_target not in ALLOWED_EXECUTION_TARGETS:
        raise ReproductionPlanMarkdownError(
            "Execution Target must be standard or web_client"
        )

    is_verification = target.get("is_verification", False)
    if not isinstance(is_verification, bool):
        raise ReproductionPlanMarkdownError(
            "Execution Target is_verification must be true or false"
        )
    original_run_id = target.get("original_run_id")
    if original_run_id is not None and not isinstance(original_run_id, str):
        raise ReproductionPlanMarkdownError(
            "Execution Target original_run_id must be a string or null"
        )

    steps = _parse_steps_section(sections["Steps"])
    confidence = _parse_confidence(sections["Confidence"])
    plan: dict[str, Any] = {
        "issue_url": _required_text(goal, "issue_url", "Goal"),
        "issue_title": _required_text(goal, "issue_title", "Goal"),
        "reproduction_goal": _required_text(
            goal,
            "reproduction_goal",
            "Goal",
        ),
        "target_version": _required_text(
            target,
            "target_version",
            "Execution Target",
        ),
        "execution_target": execution_target,
        "server_mode": str(target.get("server_mode") or "docker").strip().lower(),
        "docker_image": target.get("docker_image"),
        "environment": _parse_environment_notes(sections["Environment"]),
        "environment_notes": sections["Environment"].strip(),
        "prerequisites": [],
        "prerequisite_notes": _parse_text_list_section(sections["Prerequisites"]),
        "reproduction_steps": steps,
        "failure_indicators": _parse_text_list_section(
            sections["Failure Indicators"],
        ),
        "confidence": confidence,
        "ambiguities": _parse_text_list_section(sections["Ambiguities"]),
        "is_verification": is_verification,
        "original_run_id": original_run_id,
    }

    if plan["server_mode"] == "demo":
        release_track = str(target.get("demo_release_track") or "").strip().lower()
        if release_track not in DEMO_SERVER_URLS:
            raise ReproductionPlanMarkdownError(
                "Execution Target demo_release_track must be stable or unstable"
            )
        requires_admin = target.get("demo_requires_admin", False)
        if not isinstance(requires_admin, bool):
            raise ReproductionPlanMarkdownError(
                "Execution Target demo_requires_admin must be true or false"
            )
        base_url = str(
            target.get("demo_base_url") or DEMO_SERVER_URLS[release_track]
        ).strip()
        if base_url != DEMO_SERVER_URLS[release_track]:
            raise ReproductionPlanMarkdownError(
                "Execution Target demo_base_url must match the selected demo track"
            )
        plan["execution_target"] = "web_client"
        plan["server_target"] = {
            "mode": "demo",
            "release_track": release_track,
            "base_url": base_url,
            "username": str(target.get("demo_username") or "demo"),
            "password": _demo_password_value(target.get("demo_password")),
            "requires_admin": requires_admin,
        }
    else:
        docker_image = _required_text(target, "docker_image", "Execution Target")
        plan["docker_image"] = docker_image
        plan.pop("server_target", None)

    _validate_handoff_steps(plan)
    return plan


def parse_reproduction_plan_markdown_file(path: str | Path) -> dict[str, Any]:
    """Read and inspect a Markdown handoff file."""

    return parse_reproduction_plan_markdown(
        Path(path).expanduser().read_text(encoding="utf-8")
    )


def render_reproduction_plan_markdown(plan: Mapping[str, Any]) -> str:
    """Render an internal plan dict as an AI/human-readable Stage 1 handoff."""

    plan_dict = deepcopy(dict(plan))
    plan_dict["environment"] = _normalize_environment(
        plan_dict.get("environment"),
        field="environment",
    )
    validate_reproduction_plan(plan_dict)

    lines: list[str] = [
        f"# {PLAN_MARKDOWN_TITLE}",
        "",
        "## Goal",
        f"- Issue URL: {plan_dict['issue_url']}",
        f"- Issue Title: {plan_dict['issue_title']}",
        f"- Reproduction Goal: {plan_dict['reproduction_goal']}",
        "",
        "## Issue Context",
        str(plan_dict.get("issue_context") or "No additional context."),
        "",
        "## Execution Target",
        f"- Execution Target: {plan_dict.get('execution_target', 'standard')}",
        f"- Target Version: {plan_dict['target_version']}",
    ]

    server_target = plan_dict.get("server_target")
    if isinstance(server_target, Mapping) and server_target.get("mode") == "demo":
        release_track = str(server_target.get("release_track") or "stable")
        lines.extend(
            [
                "- Server Mode: demo",
                f"- Demo Release Track: {release_track}",
                f"- Demo Base URL: {server_target.get('base_url') or DEMO_SERVER_URLS[release_track]}",
                f"- Demo Username: {server_target.get('username', 'demo')}",
                f"- Demo Password: {server_target.get('password', '')}",
                f"- Demo Requires Admin: {_json_scalar(bool(server_target.get('requires_admin', False)))}",
            ]
        )
    else:
        lines.extend(
            [
                f"- Docker Image: {plan_dict['docker_image']}",
                "- Server Mode: docker",
            ]
        )

    lines.extend(
        [
            f"- Is Verification: {_json_scalar(bool(plan_dict.get('is_verification', False)))}",
            f"- Original Run ID: {_json_scalar(plan_dict.get('original_run_id'))}",
            "",
            "## Environment",
            *_render_environment(plan_dict),
            "",
            "## Prerequisites",
            *_render_prerequisites(plan_dict.get("prerequisites", [])),
            "",
            "## Steps",
        ]
    )

    for step in plan_dict["reproduction_steps"]:
        lines.extend(_render_step(step))

    lines.extend(
        [
            "",
            "## Failure Indicators",
            *_render_text_list(plan_dict.get("failure_indicators", [])),
            "",
            "## Confidence",
            str(plan_dict.get("confidence") or "medium"),
            "",
            "## Ambiguities",
            *_render_text_list(plan_dict.get("ambiguities", [])),
            "",
        ]
    )
    return "\n".join(lines)


def validate_reproduction_plan(plan: Mapping[str, Any]) -> None:
    """Validate the internal ReproductionPlan shape needed by Stage 2."""

    if not isinstance(plan, Mapping):
        raise ReproductionPlanMarkdownError("plan must be an object")

    required = {
        "issue_url",
        "issue_title",
        "target_version",
        "prerequisites",
        "environment",
        "reproduction_steps",
        "reproduction_goal",
        "failure_indicators",
        "confidence",
        "ambiguities",
        "is_verification",
        "original_run_id",
    }
    missing = sorted(key for key in required if key not in plan)
    if missing:
        raise ReproductionPlanMarkdownError(
            f"plan missing required field(s): {', '.join(missing)}"
        )

    _require_non_empty_string(plan.get("issue_url"), "issue_url")
    _require_non_empty_string(plan.get("issue_title"), "issue_title")
    _require_non_empty_string(plan.get("target_version"), "target_version")
    _require_non_empty_string(plan.get("reproduction_goal"), "reproduction_goal")

    execution_target = str(plan.get("execution_target") or "standard").strip()
    if execution_target not in ALLOWED_EXECUTION_TARGETS:
        raise ReproductionPlanMarkdownError(
            "execution_target must be standard or web_client"
        )
    confidence = str(plan.get("confidence") or "").strip()
    if confidence not in ALLOWED_CONFIDENCE:
        raise ReproductionPlanMarkdownError("confidence must be high, medium, or low")
    if not isinstance(plan.get("is_verification"), bool):
        raise ReproductionPlanMarkdownError("is_verification must be a boolean")
    original_run_id = plan.get("original_run_id")
    if original_run_id is not None and not isinstance(original_run_id, str):
        raise ReproductionPlanMarkdownError("original_run_id must be a string or null")

    server_target = plan.get("server_target")
    demo_mode = isinstance(server_target, Mapping) and server_target.get("mode") == "demo"
    if demo_mode:
        if execution_target != "web_client":
            raise ReproductionPlanMarkdownError(
                "demo server mode requires execution_target web_client"
            )
        _validate_demo_server_target(server_target)
    else:
        _require_non_empty_string(plan.get("docker_image"), "docker_image")

    if not isinstance(plan.get("prerequisites"), list):
        raise ReproductionPlanMarkdownError("prerequisites must be a list")
    _validate_environment(plan.get("environment"))
    _validate_string_list(plan.get("failure_indicators"), "failure_indicators")
    _validate_string_list(plan.get("ambiguities"), "ambiguities")

    steps = plan.get("reproduction_steps")
    if not isinstance(steps, list) or not steps:
        raise ReproductionPlanMarkdownError("reproduction_steps must be a non-empty list")

    trigger_count = 0
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, Mapping):
            raise ReproductionPlanMarkdownError(f"step {index} must be an object")
        _validate_step(step, index)
        if step.get("role") == "trigger":
            trigger_count += 1
    if trigger_count != 1:
        raise ReproductionPlanMarkdownError(
            f"reproduction_steps must contain exactly one trigger step; found {trigger_count}"
        )


def _split_sections(text: str) -> dict[str, str]:
    matches = list(_SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if name in sections:
            raise ReproductionPlanMarkdownError(f"duplicate section: {name}")
        sections[name] = text[start:end].strip()
    return sections


def _validate_required_sections(sections: Mapping[str, str]) -> None:
    names = list(sections)
    missing = [section for section in REQUIRED_SECTIONS if section not in sections]
    if missing:
        raise ReproductionPlanMarkdownError(
            f"missing required section(s): {', '.join(missing)}"
        )
    unexpected = [name for name in names if name not in REQUIRED_SECTIONS]
    if unexpected:
        raise ReproductionPlanMarkdownError(
            f"unexpected top-level section(s): {', '.join(unexpected)}"
        )
    ordered = [name for name in names if name in REQUIRED_SECTIONS]
    if ordered != list(REQUIRED_SECTIONS):
        raise ReproductionPlanMarkdownError(
            "top-level sections must appear in the required order"
        )


def _validate_json_fence_scope(text: str) -> None:
    for match in _FENCE_RE.finditer(text):
        info = match.group(1).strip().lower()
        if info != "json":
            continue
        heading = _nearest_heading(text, match.start())
        if heading != "Exact Request Payload":
            raise ReproductionPlanMarkdownError(
                "JSON fences are allowed only under an Exact Request Payload subsection"
            )
        try:
            json.loads(match.group(2).strip())
        except json.JSONDecodeError as exc:
            raise ReproductionPlanMarkdownError(
                f"Exact Request Payload contains malformed JSON at line {exc.lineno} "
                f"column {exc.colno}: {exc.msg}"
            ) from exc


def _nearest_heading(text: str, position: int) -> str | None:
    heading = None
    for match in _HEADING_RE.finditer(text[:position]):
        heading = match.group(2).strip()
    return heading


def _parse_key_value_bullets(text: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for line in text.splitlines():
        match = _KEY_VALUE_RE.match(line)
        if not match:
            continue
        key = _normalized_key(match.group(1))
        values[key] = _parse_scalar(match.group(2))
    return values


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _required_text(values: Mapping[str, Any], key: str, section: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        label = key.replace("_", " ")
        raise ReproductionPlanMarkdownError(f"{section} missing required {label}")
    return value.strip()


def _parse_scalar(value: str) -> Any:
    text = value.strip()
    lower = text.lower()
    if lower in {"true", "false"}:
        return lower == "true"
    if lower in {"null", "none"}:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text.startswith('"') or text.startswith("'"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, (str, bool, int, float)) or parsed is None:
            return parsed
    return text


def _demo_password_value(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in {"<blank>", "blank", "(blank)", "<empty>", "empty", "none"}:
        return ""
    return text


def _parse_environment_notes(text: str) -> dict[str, Any]:
    environment = deepcopy(DEFAULT_ENVIRONMENT)
    fields = _parse_key_value_bullets(text)
    host = fields.get("host_port") or fields.get("host")
    container = fields.get("container_port") or fields.get("container")
    if host is not None:
        environment["ports"]["host"] = _normalize_port(host, "environment.host_port")
    if container is not None:
        environment["ports"]["container"] = _normalize_port(
            container,
            "environment.container_port",
        )
    return environment


def _parse_text_list_section(text: str) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("-", "*")):
            item = stripped[1:].strip()
        else:
            item = stripped
        if item.lower() in {"none", "n/a", "not applicable"}:
            continue
        if item:
            items.append(item)
    return items


def _parse_confidence(text: str) -> str:
    fields = _parse_key_value_bullets(text)
    value = fields.get("confidence") or fields.get("level")
    if value is None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                value = stripped.lstrip("-* ").strip()
                break
    confidence = str(value or "").strip().lower()
    if confidence not in ALLOWED_CONFIDENCE:
        raise ReproductionPlanMarkdownError(
            "Confidence must be high, medium, or low"
        )
    return confidence


def _parse_steps_section(text: str) -> list[dict[str, Any]]:
    matches = list(_STEP_RE.finditer(text))
    if not matches:
        raise ReproductionPlanMarkdownError("Steps must contain at least one step")

    steps: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        heading_id = int(match.group(1))
        heading_action = (match.group(2) or "").strip()
        fields = _parse_key_value_bullets(body)
        step_id = fields.get("step_id", heading_id)
        if not isinstance(step_id, int):
            raise ReproductionPlanMarkdownError("step_id must be an integer")
        step = {
            "step_id": step_id,
            "role": str(fields.get("role") or "").strip(),
            "action": str(fields.get("action") or heading_action).strip(),
            "tool": str(fields.get("tool") or "").strip(),
            "expected_outcome": str(
                fields.get("expected_outcome") or fields.get("expected_result") or ""
            ).strip(),
            "notes": body,
        }
        if not step["expected_outcome"]:
            step["expected_outcome"] = step["action"] or "Step completes"
        _validate_handoff_step(step, step_id)
        steps.append(step)
    return steps


def _validate_handoff_steps(plan: Mapping[str, Any]) -> None:
    steps = plan.get("reproduction_steps")
    if not isinstance(steps, list) or not steps:
        raise ReproductionPlanMarkdownError("Steps must contain at least one step")
    trigger_count = sum(
        1
        for step in steps
        if isinstance(step, Mapping) and step.get("role") == "trigger"
    )
    if trigger_count != 1:
        raise ReproductionPlanMarkdownError(
            f"Steps must contain exactly one trigger step; found {trigger_count}"
        )
    if plan.get("server_mode") == "demo":
        non_browser = [
            str(step.get("tool"))
            for step in steps
            if isinstance(step, Mapping) and step.get("tool") != "browser"
        ]
        if non_browser:
            raise ReproductionPlanMarkdownError(
                "demo server mode only supports browser steps"
            )


def _validate_handoff_step(step: Mapping[str, Any], display_index: int) -> None:
    step_id = step.get("step_id")
    if not isinstance(step_id, int) or step_id < 1:
        raise ReproductionPlanMarkdownError(
            f"step {display_index} step_id must be a positive integer"
        )
    role = step.get("role")
    _require_non_empty_string(role, f"step {step_id} role")
    _require_non_empty_string(step.get("action"), f"step {step_id} action")
    tool = step.get("tool")
    if tool not in ALLOWED_STEP_TOOLS:
        raise ReproductionPlanMarkdownError(
            f"step {step_id} tool must be one of: {', '.join(sorted(ALLOWED_STEP_TOOLS))}"
        )


def _validate_demo_server_target(server_target: Mapping[str, Any]) -> None:
    release_track = str(server_target.get("release_track") or "").strip()
    if release_track not in DEMO_SERVER_URLS:
        raise ReproductionPlanMarkdownError(
            "server_target.release_track must be stable or unstable"
        )
    if server_target.get("base_url") != DEMO_SERVER_URLS[release_track]:
        raise ReproductionPlanMarkdownError(
            "server_target.base_url must match the selected demo release track"
        )
    if bool(server_target.get("requires_admin", False)):
        raise ReproductionPlanMarkdownError(
            "demo server mode cannot require admin privileges"
        )


def _validate_environment(value: Any) -> None:
    _normalize_environment(value, field="environment")


def _normalize_environment(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        environment: dict[str, Any] = {}
    elif isinstance(value, Mapping):
        environment = dict(value)
    else:
        raise ReproductionPlanMarkdownError(f"{field} must be an object")

    normalized = dict(environment)
    normalized["ports"] = _normalize_environment_ports(
        environment.get("ports"),
        field=f"{field}.ports",
    )
    normalized["volumes"] = _normalize_environment_volumes(
        environment.get("volumes"),
        field=f"{field}.volumes",
    )
    normalized["env_vars"] = _normalize_environment_env_vars(
        environment.get("env_vars"),
        field=f"{field}.env_vars",
    )
    return normalized


def _normalize_environment_ports(value: Any, *, field: str) -> dict[str, int]:
    if value is None:
        return dict(DEFAULT_ENVIRONMENT["ports"])
    if not isinstance(value, Mapping):
        raise ReproductionPlanMarkdownError(f"{field} must be an object")

    if "host" in value or "container" in value:
        host = _normalize_port(value.get("host", 8096), f"{field}.host")
        container = _normalize_port(
            value.get("container", 8096),
            f"{field}.container",
        )
        return {"host": host, "container": container}

    if value:
        container, host = next(iter(value.items()))
        return {
            "host": _normalize_port(host, f"{field}.host"),
            "container": _normalize_port(
                str(container).split("/", 1)[0],
                f"{field}.container",
            ),
        }

    return dict(DEFAULT_ENVIRONMENT["ports"])


def _normalize_port(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ReproductionPlanMarkdownError(f"{field} must be an integer port")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ReproductionPlanMarkdownError(
            f"{field} must be an integer port"
        ) from exc
    if port < 1 or port > 65535:
        raise ReproductionPlanMarkdownError(f"{field} must be an integer port")
    return port


def _normalize_environment_volumes(value: Any, *, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReproductionPlanMarkdownError(f"{field} must be a list")
    return list(value)


def _normalize_environment_env_vars(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ReproductionPlanMarkdownError(f"{field} must be an object")
    return dict(value)


def _validate_string_list(value: Any, field: str) -> None:
    if not isinstance(value, list):
        raise ReproductionPlanMarkdownError(f"{field} must be a list")
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item.strip():
            raise ReproductionPlanMarkdownError(
                f"{field}[{index}] must be a non-empty string"
            )


def _validate_step(step: Mapping[str, Any], display_index: int) -> None:
    step_id = step.get("step_id")
    if not isinstance(step_id, int) or step_id < 1:
        raise ReproductionPlanMarkdownError(
            f"step {display_index} step_id must be a positive integer"
        )
    role = step.get("role")
    if role not in ALLOWED_STEP_ROLES:
        raise ReproductionPlanMarkdownError(
            f"step {step_id} role must be setup, trigger, or verify"
        )
    _require_non_empty_string(step.get("action"), f"step {step_id} action")
    tool = step.get("tool")
    if tool not in ALLOWED_STEP_TOOLS:
        raise ReproductionPlanMarkdownError(
            f"step {step_id} tool must be one of: {', '.join(sorted(ALLOWED_STEP_TOOLS))}"
        )
    if not isinstance(step.get("input"), Mapping):
        raise ReproductionPlanMarkdownError(f"step {step_id} input must be an object")
    _require_non_empty_string(
        step.get("expected_outcome"),
        f"step {step_id} expected_outcome",
    )
    _validate_criteria(step.get("success_criteria"), f"step {step_id} success_criteria")
    capture = step.get("capture")
    if capture is not None and not isinstance(capture, Mapping):
        raise ReproductionPlanMarkdownError(f"step {step_id} capture must be an object")


def _validate_criteria(value: Any, field: str) -> None:
    if not isinstance(value, Mapping):
        raise ReproductionPlanMarkdownError(f"{field} must be an object")
    operators = [operator for operator in ("all_of", "any_of") if operator in value]
    if len(operators) != 1:
        raise ReproductionPlanMarkdownError(
            f"{field} must contain exactly one of all_of or any_of"
        )
    assertions = value.get(operators[0])
    if not isinstance(assertions, list) or not assertions:
        raise ReproductionPlanMarkdownError(
            f"{field}.{operators[0]} must be a non-empty list"
        )
    for index, assertion in enumerate(assertions, start=1):
        if not isinstance(assertion, Mapping):
            raise ReproductionPlanMarkdownError(
                f"{field}.{operators[0]}[{index}] must be an object"
            )
        assertion_type = assertion.get("type")
        if not isinstance(assertion_type, str) or not assertion_type.strip():
            raise ReproductionPlanMarkdownError(
                f"{field}.{operators[0]}[{index}] missing assertion type"
            )


def _require_non_empty_string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReproductionPlanMarkdownError(f"{field} must be a non-empty string")


def _render_environment(plan: Mapping[str, Any]) -> list[str]:
    server_target = plan.get("server_target")
    if isinstance(server_target, Mapping) and server_target.get("mode") == "demo":
        return [
            f"- Use the public Jellyfin demo server at {server_target.get('base_url')}.",
            "- Stage 2 should not start Docker, run the startup wizard, or require admin access.",
        ]

    environment = _normalize_environment(plan.get("environment"), field="environment")
    ports = environment["ports"]
    lines = [
        (
            "- Stage 2 manages Docker lifecycle, waits for Jellyfin health, and "
            "provides an already configured Jellyfin server with admin auth plus "
            "playable video and audio/music content."
        ),
        f"- Host Port: {ports['host']}",
        f"- Container Port: {ports['container']}",
    ]
    volumes = environment.get("volumes") or []
    if volumes:
        lines.append("- Volumes:")
        for volume in volumes:
            lines.append(f"  - {_volume_description(volume)}")
    else:
        lines.append("- Volumes: none")
    env_vars = environment.get("env_vars") or {}
    if env_vars:
        lines.append("- Environment Variables:")
        for key, value in sorted(env_vars.items()):
            lines.append(f"  - `{key}={value}`")
    else:
        lines.append("- Environment Variables: none")
    return lines


def _volume_description(volume: Any) -> str:
    if isinstance(volume, Mapping):
        host = volume.get("host") or volume.get("source") or "host path"
        container = volume.get("container") or volume.get("target") or "container path"
        mode = volume.get("mode")
        suffix = f" ({mode})" if mode else ""
        return f"`{host}` mounted at `{container}`{suffix}"
    return str(volume)


def _render_prerequisites(values: Iterable[Any]) -> list[str]:
    items = list(values)
    if not items:
        return ["- None"]
    lines: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            description = str(item.get("description") or item.get("source") or item)
            source = item.get("source")
            target = item.get("target_name") or item.get("target")
            detail = description
            if source:
                detail += f"; source: {source}"
            if target:
                detail += f"; target: {target}"
            lines.append(f"- {detail}")
        else:
            lines.append(f"- {item}")
    return lines


def _render_step(step: Mapping[str, Any]) -> list[str]:
    lines = [
        "",
        f"### Step {step['step_id']}: {step['action']}",
        f"- Step ID: {step['step_id']}",
        f"- Role: {step['role']}",
        f"- Action: {step['action']}",
        f"- Tool: {step['tool']}",
        f"- Expected Outcome: {step['expected_outcome']}",
        *_step_instruction_lines(step),
    ]
    payload = _exact_json_payload(step)
    if payload is not None:
        lines.extend(["", "#### Exact Request Payload", _json_block(payload)])
    text_payload = _exact_text_payload(step)
    if text_payload is not None:
        lines.extend(["", "#### Exact Request Body", _text_block(text_payload)])
    return lines


def _step_instruction_lines(step: Mapping[str, Any]) -> list[str]:
    tool = step.get("tool")
    step_input = step.get("input") if isinstance(step.get("input"), Mapping) else {}
    lines: list[str] = []
    if tool == "http_request":
        method = step_input.get("method", "GET")
        path = step_input.get("path", "/")
        auth = step_input.get("auth", "auto")
        lines.append(f"- Request: {method} {path} with `{auth}` authentication.")
        headers = step_input.get("headers")
        if isinstance(headers, Mapping) and headers:
            header_text = ", ".join(f"{key}: {value}" for key, value in headers.items())
            lines.append(f"- Headers: {header_text}.")
        if step_input.get("body_text") is not None:
            lines.append("- Exact Request Body: use the text block supplied by the issue.")
        if step_input.get("body_base64") is not None:
            lines.append("- Exact Request Body: use the base64 body supplied by the issue.")
    elif tool == "bash":
        lines.append(f"- Command: `{step_input.get('command', '')}`")
    elif tool == "docker_exec":
        lines.append(f"- Container Command: `{step_input.get('command', '')}`")
    elif tool == "browser":
        for action in _browser_actions(step_input):
            lines.append(f"- Browser Action: {_browser_action_description(action)}")
    elif tool == "screenshot":
        target = step_input.get("url") or step_input.get("path") or "Jellyfin Web"
        lines.append(f"- Screenshot Target: {target}")

    capture = step.get("capture")
    if isinstance(capture, Mapping) and capture:
        lines.append(f"- Capture: {_mapping_description(capture)}")
    lines.append(f"- Reproduced When: {_criteria_description(step.get('success_criteria'))}")
    return lines


def _browser_actions(step_input: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    actions = step_input.get("actions")
    if isinstance(actions, list):
        return [action for action in actions if isinstance(action, Mapping)]
    return []


def _browser_action_description(action: Mapping[str, Any]) -> str:
    action_type = str(action.get("type") or "browser action")
    if action_type == "goto":
        return f"navigate to {action.get('url') or action.get('path') or 'the target page'}."
    if action_type == "click":
        return f"click {_target_description(action.get('target'))}."
    if action_type == "fill":
        return f"fill `{action.get('selector')}` with `{action.get('value')}`."
    if action_type == "press":
        return f"press `{action.get('key')}` in `{action.get('selector')}`."
    if action_type == "wait_for":
        return f"wait for `{action.get('selector')}` to be {action.get('state', 'visible')}."
    if action_type == "wait_for_text":
        return f"wait for text `{action.get('text')}`."
    if action_type == "wait_for_url":
        return f"wait for URL {action.get('pattern') or action.get('url') or action.get('path')}."
    if action_type == "wait_for_media":
        return f"wait for media state `{action.get('state')}`."
    if action_type == "screenshot":
        return f"capture screenshot `{action.get('label', 'browser')}`."
    if action_type == "evaluate":
        return "run the described browser evaluation."
    return f"perform `{action_type}`."


def _target_description(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "the target described in the plan"
    kind = value.get("kind")
    name = value.get("name")
    scope = value.get("scope")
    selector = value.get("selector")
    index = value.get("index")
    if kind == "control":
        scope_text = f" {scope}" if scope else ""
        return f"the{scope_text} control named `{name}`"
    if kind == "link":
        return f"the link named `{name}`"
    if kind == "text":
        return f"the text target `{name}`"
    if kind == "css":
        suffix = f" at index {index}" if index is not None else ""
        return f"CSS selector `{selector}`{suffix}"
    return _mapping_description(value)


def _criteria_description(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "the observable bug symptom appears."
    operator = "all_of" if "all_of" in value else "any_of" if "any_of" in value else None
    assertions = value.get(operator) if operator else None
    if not isinstance(assertions, list) or not assertions:
        return "the observable bug symptom appears."
    joiner = " and " if operator == "all_of" else " or "
    return joiner.join(_assertion_description(assertion) for assertion in assertions)


def _assertion_description(assertion: Any) -> str:
    if not isinstance(assertion, Mapping):
        return str(assertion)
    assertion_type = assertion.get("type")
    if assertion_type == "status_code":
        return f"HTTP status equals {assertion.get('equals')}"
    if assertion_type == "body_contains":
        return f"response body contains `{assertion.get('text')}`"
    if assertion_type == "body_matches":
        return f"response body matches `{assertion.get('pattern')}`"
    if assertion_type == "body_json_path":
        path = assertion.get("path")
        if "equals" in assertion:
            return f"response JSON path `{path}` equals `{assertion.get('equals')}`"
        return f"response JSON path `{path}` exists"
    if assertion_type == "exit_code":
        return f"exit code equals {assertion.get('equals')}"
    if assertion_type == "stdout_contains":
        return f"stdout contains `{assertion.get('text')}`"
    if assertion_type == "stderr_contains":
        return f"stderr contains `{assertion.get('text')}`"
    if assertion_type == "log_matches":
        return f"Jellyfin logs match `{assertion.get('pattern')}`"
    if assertion_type == "screenshot_present":
        return "a screenshot artifact is present"
    if assertion_type == "browser_action_run":
        return "the browser action completes"
    if assertion_type == "browser_element":
        if assertion.get("target"):
            return f"browser shows {_target_description(assertion.get('target'))}"
        selector = assertion.get("selector")
        state = assertion.get("state") or "visible"
        return f"browser selector `{selector}` is {state}"
    if assertion_type == "browser_text_contains":
        return f"browser text contains `{assertion.get('text')}`"
    if assertion_type == "browser_url_matches":
        return f"browser URL matches `{assertion.get('pattern')}`"
    if assertion_type == "browser_media_state":
        return f"browser media state is `{assertion.get('state')}`"
    if assertion_type == "browser_console_matches":
        return f"browser console matches `{assertion.get('pattern')}`"
    return f"{assertion_type}: {_mapping_description(assertion)}"


def _mapping_description(value: Mapping[str, Any]) -> str:
    parts = []
    for key, item in value.items():
        if isinstance(item, Mapping):
            parts.append(f"{key} ({_mapping_description(item)})")
        elif isinstance(item, list):
            parts.append(f"{key} [{len(item)} item(s)]")
        else:
            parts.append(f"{key}={item}")
    return ", ".join(parts)


def _exact_json_payload(step: Mapping[str, Any]) -> Any | None:
    step_input = step.get("input")
    if isinstance(step_input, Mapping) and "body_json" in step_input:
        return step_input["body_json"]
    return None


def _exact_text_payload(step: Mapping[str, Any]) -> str | None:
    step_input = step.get("input")
    if not isinstance(step_input, Mapping):
        return None
    if "body_text" in step_input:
        return str(step_input["body_text"])
    if "body_base64" in step_input:
        return str(step_input["body_base64"])
    return None


def _render_text_list(values: Iterable[Any]) -> list[str]:
    items = [str(value) for value in values if str(value).strip()]
    if not items:
        return ["- None"]
    return [f"- {item}" for item in items]


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(
        value,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
        default=str,
    ) + "\n```"


def _text_block(value: str) -> str:
    return "```text\n" + value.rstrip() + "\n```"


def _json_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)
