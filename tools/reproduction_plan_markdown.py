"""Markdown handoff parser for Stage 1 ReproductionPlan documents."""

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
_STEP_SUBSECTION_RE = re.compile(r"^####\s+(.+?)\s*$", re.MULTILINE)
_KEY_VALUE_RE = re.compile(r"^\s*[-*]\s+([^:\n]+):\s*(.*?)\s*$")
_JSON_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?```", re.DOTALL)


class ReproductionPlanMarkdownError(ValueError):
    """Raised when a Markdown plan cannot be parsed or validated."""


def parse_reproduction_plan_markdown(markdown: str) -> dict[str, Any]:
    """Parse a ``ReproductionPlan Markdown v1`` document into a plan dict."""

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
    missing = [section for section in REQUIRED_SECTIONS if section not in sections]
    if missing:
        raise ReproductionPlanMarkdownError(
            f"missing required section(s): {', '.join(missing)}"
        )

    goal = _parse_key_value_bullets(sections["Goal"])
    target = _parse_key_value_bullets(sections["Execution Target"])
    is_verification = target.get("is_verification", False)
    if not isinstance(is_verification, bool):
        raise ReproductionPlanMarkdownError(
            "Execution Target is_verification must be true or false"
        )

    plan: dict[str, Any] = {
        "issue_url": _required_text(goal, "issue_url", "Goal"),
        "issue_title": _required_text(goal, "issue_title", "Goal"),
        "target_version": _required_text(
            target,
            "target_version",
            "Execution Target",
        ),
        "prerequisites": _parse_json_array_section(
            sections["Prerequisites"],
            "Prerequisites",
        ),
        "environment": _parse_environment_section(sections["Environment"]),
        "reproduction_steps": _parse_steps_section(sections["Steps"]),
        "reproduction_goal": _required_text(
            goal,
            "reproduction_goal",
            "Goal",
        ),
        "failure_indicators": _parse_text_list_section(
            sections["Failure Indicators"],
        ),
        "execution_target": str(
            target.get("execution_target") or "standard"
        ).strip(),
        "confidence": _parse_confidence(sections["Confidence"]),
        "ambiguities": _parse_text_list_section(sections["Ambiguities"]),
        "is_verification": is_verification,
        "original_run_id": target.get("original_run_id"),
    }

    server_mode = str(target.get("server_mode") or "docker").strip().lower()
    if server_mode == "demo":
        release_track = str(target.get("demo_release_track") or "").strip().lower()
        if release_track not in DEMO_SERVER_URLS:
            raise ReproductionPlanMarkdownError(
                "Execution Target demo_release_track must be stable or unstable"
            )
        base_url = str(
            target.get("demo_base_url") or DEMO_SERVER_URLS[release_track]
        ).strip()
        requires_admin = target.get("demo_requires_admin", False)
        if not isinstance(requires_admin, bool):
            raise ReproductionPlanMarkdownError(
                "Execution Target demo_requires_admin must be true or false"
            )
        plan["execution_target"] = "web_client"
        plan["server_target"] = {
            "mode": "demo",
            "release_track": release_track,
            "base_url": base_url,
            "username": str(target.get("demo_username") or "demo"),
            "password": str(target.get("demo_password") or ""),
            "requires_admin": requires_admin,
        }
    else:
        docker_image = _required_text(target, "docker_image", "Execution Target")
        plan["docker_image"] = docker_image

    validate_reproduction_plan(plan)
    return plan


def parse_reproduction_plan_markdown_file(path: str | Path) -> dict[str, Any]:
    """Read and parse a Markdown plan file."""

    return parse_reproduction_plan_markdown(
        Path(path).expanduser().read_text(encoding="utf-8")
    )


def render_reproduction_plan_markdown(plan: Mapping[str, Any]) -> str:
    """Render a plan dict as ``ReproductionPlan Markdown v1``."""

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
            _json_block(plan_dict["environment"]),
            "",
            "## Prerequisites",
            _json_block(plan_dict.get("prerequisites", [])),
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


def _parse_environment_section(text: str) -> dict[str, Any]:
    environment = _parse_json_object_section(text, "Environment")
    return _normalize_environment(environment, field="environment")


def _parse_json_array_section(text: str, label: str) -> list[Any]:
    value = _parse_json_section(text, label)
    if not isinstance(value, list):
        raise ReproductionPlanMarkdownError(f"{label} must be a JSON array")
    return value


def _parse_json_object_section(text: str, label: str) -> dict[str, Any]:
    value = _parse_json_section(text, label)
    if not isinstance(value, dict):
        raise ReproductionPlanMarkdownError(f"{label} must be a JSON object")
    return value


def _parse_json_section(text: str, label: str) -> Any:
    block = _first_json_block(text)
    if block is None:
        stripped = text.strip()
        if stripped in {"", "- None", "- none", "None", "none"}:
            raise ReproductionPlanMarkdownError(f"{label} must contain a JSON block")
        block = stripped
    try:
        return json.loads(block)
    except json.JSONDecodeError as exc:
        raise ReproductionPlanMarkdownError(
            f"{label} contains malformed JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc


def _first_json_block(text: str) -> str | None:
    match = _JSON_FENCE_RE.search(text)
    if not match:
        return None
    return match.group(1).strip()


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
        steps.append(_parse_step(body, heading_id, heading_action))
    return steps


def _parse_step(body: str, heading_id: int, heading_action: str) -> dict[str, Any]:
    subsections = _split_step_subsections(body)
    prelude = subsections.pop("_prelude", "")
    fields = _parse_key_value_bullets(prelude)
    step_id = fields.get("step_id", heading_id)
    if not isinstance(step_id, int):
        raise ReproductionPlanMarkdownError("step_id must be an integer")

    input_section = _required_step_section(subsections, "Input", step_id)
    criteria_section = _required_step_section(subsections, "Success Criteria", step_id)
    step: dict[str, Any] = {
        "step_id": step_id,
        "role": str(fields.get("role") or "").strip(),
        "action": str(fields.get("action") or heading_action).strip(),
        "tool": str(fields.get("tool") or "").strip(),
        "input": _parse_json_object_section(input_section, f"Step {step_id} Input"),
        "success_criteria": _parse_json_object_section(
            criteria_section,
            f"Step {step_id} Success Criteria",
        ),
    }
    step["expected_outcome"] = str(
        fields.get("expected_outcome") or step["action"] or "Step completes"
    ).strip()
    capture_section = subsections.get("Capture")
    if capture_section and capture_section.strip():
        step["capture"] = _parse_json_object_section(
            capture_section,
            f"Step {step_id} Capture",
        )
    _validate_step(step, step_id)
    return step


def _split_step_subsections(text: str) -> dict[str, str]:
    matches = list(_STEP_SUBSECTION_RE.finditer(text))
    subsections: dict[str, str] = {}
    prelude_end = matches[0].start() if matches else len(text)
    subsections["_prelude"] = text[:prelude_end].strip()
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if name in subsections:
            raise ReproductionPlanMarkdownError(f"duplicate step subsection: {name}")
        subsections[name] = text[start:end].strip()
    return subsections


def _required_step_section(
    subsections: Mapping[str, str],
    name: str,
    step_id: int,
) -> str:
    value = subsections.get(name)
    if not value:
        raise ReproductionPlanMarkdownError(f"Step {step_id} missing {name} subsection")
    return value


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


def _render_step(step: Mapping[str, Any]) -> list[str]:
    lines = [
        "",
        f"### Step {step['step_id']}: {step['action']}",
        f"- Step ID: {step['step_id']}",
        f"- Role: {step['role']}",
        f"- Action: {step['action']}",
        f"- Tool: {step['tool']}",
        f"- Expected Outcome: {step['expected_outcome']}",
        "",
        "#### Input",
        _json_block(step["input"]),
    ]
    if "capture" in step:
        lines.extend(["", "#### Capture", _json_block(step["capture"])])
    lines.extend(
        [
            "",
            "#### Success Criteria",
            _json_block(step["success_criteria"]),
        ]
    )
    return lines


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


def _json_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)
