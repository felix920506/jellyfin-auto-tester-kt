"""Deterministic assertion and capture helpers for Stage 2 execution."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping


VARIABLE_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class UnboundVariableError(ValueError):
    """Raised when a step references a variable that has not been captured."""

    name: str

    def __str__(self) -> str:
        return f"unbound variable: {self.name}"


@dataclass(frozen=True)
class CaptureError(ValueError):
    """Raised when a capture expression cannot extract its value."""

    variable: str
    reason: str

    def __str__(self) -> str:
        return f"capture failed: {self.variable}: {self.reason}"


def resolve_references(value: Any, variables: Mapping[str, Any]) -> Any:
    """Resolve ${var_name} references recursively in a JSON-like value."""

    if isinstance(value, str):
        return _resolve_string(value, variables)
    if isinstance(value, list):
        return [resolve_references(item, variables) for item in value]
    if isinstance(value, tuple):
        return tuple(resolve_references(item, variables) for item in value)
    if isinstance(value, dict):
        return {
            key: resolve_references(item, variables)
            for key, item in value.items()
        }
    return value


def evaluate_criteria(
    criteria: Mapping[str, Any] | None,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate a Stage 2 success_criteria object against one step result."""

    if not criteria:
        return {
            "passed": False,
            "operator": None,
            "assertions": [
                _assertion_result(
                    assertion_type="criteria",
                    passed=False,
                    actual=None,
                    expected="all_of or any_of",
                    message="missing success_criteria",
                )
            ],
        }

    if "all_of" in criteria and "any_of" in criteria:
        return _invalid_criteria("criteria must contain only one of all_of or any_of")
    if "all_of" in criteria:
        operator = "all_of"
        assertions = criteria.get("all_of")
    elif "any_of" in criteria:
        operator = "any_of"
        assertions = criteria.get("any_of")
    else:
        return _invalid_criteria("criteria must contain all_of or any_of")

    if not isinstance(assertions, list) or not assertions:
        return _invalid_criteria(f"{operator} must be a non-empty list")

    results = [_evaluate_assertion(assertion, context) for assertion in assertions]
    passed = (
        all(result["passed"] for result in results)
        if operator == "all_of"
        else any(result["passed"] for result in results)
    )
    return {"passed": passed, "operator": operator, "assertions": results}


def extract_captures(
    capture_map: Mapping[str, Mapping[str, Any]] | None,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract and return variable bindings declared by a step capture block."""

    values: dict[str, Any] = {}
    for variable, expression in (capture_map or {}).items():
        try:
            values[variable] = _extract_capture_value(expression, context)
        except CaptureError as exc:
            raise CaptureError(variable, exc.reason) from exc
        except Exception as exc:  # pragma: no cover - defensive normalization
            raise CaptureError(variable, str(exc)) from exc
    return values


def extract_json_path(payload: Any, path: str) -> Any:
    """Evaluate a small JSONPath subset used by reproduction plans.

    Supported syntax is rooted at ``$`` with dotted object keys, bracketed list
    indexes, and bracketed quoted object keys, e.g. ``$.Items[0].Id``.
    """

    if not isinstance(path, str) or not path.startswith("$"):
        raise ValueError("JSONPath must start with '$'")

    current = payload
    index = 1
    while index < len(path):
        char = path[index]
        if char == ".":
            key, index = _read_dot_key(path, index + 1)
            current = _lookup_key(current, key)
        elif char == "[":
            token, index = _read_bracket_token(path, index + 1)
            if isinstance(token, int):
                current = _lookup_index(current, token)
            else:
                current = _lookup_key(current, token)
        else:
            raise ValueError(f"unexpected JSONPath token at offset {index}")
    return current


def _resolve_string(value: str, variables: Mapping[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in variables:
            raise UnboundVariableError(name)
        return str(variables[name])

    return VARIABLE_RE.sub(replace, value)


def _evaluate_assertion(assertion: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(assertion, Mapping):
        return _assertion_result(
            assertion_type="unknown",
            passed=False,
            actual=assertion,
            expected="assertion object",
            message="assertion must be an object",
        )

    assertion_type = str(assertion.get("type", "unknown"))
    try:
        if assertion_type == "status_code":
            return _evaluate_status_code(assertion, context)
        if assertion_type == "body_contains":
            return _evaluate_body_contains(assertion, context)
        if assertion_type == "body_matches":
            return _evaluate_body_matches(assertion, context)
        if assertion_type == "body_json_path":
            return _evaluate_body_json_path(assertion, context)
        if assertion_type == "exit_code":
            return _evaluate_exit_code(assertion, context)
        if assertion_type == "stdout_contains":
            return _evaluate_stream_contains("stdout", assertion, context)
        if assertion_type == "stderr_contains":
            return _evaluate_stream_contains("stderr", assertion, context)
        if assertion_type == "log_matches":
            return _evaluate_log_matches(assertion, context)
        if assertion_type == "screenshot_present":
            return _evaluate_screenshot_present(assertion, context)
        return _assertion_result(
            assertion_type=assertion_type,
            passed=False,
            actual=None,
            expected="supported assertion type",
            message=f"unsupported assertion type: {assertion_type}",
        )
    except Exception as exc:  # pragma: no cover - keeps output JSON stable
        return _assertion_result(
            assertion_type=assertion_type,
            passed=False,
            actual=None,
            expected=_expected_value(assertion),
            message=str(exc),
        )


def _evaluate_status_code(
    assertion: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    actual = _http(context).get("status_code")
    if actual is None:
        return _not_applicable("status_code", assertion, context)
    return _compare_numeric("status_code", actual, assertion)


def _evaluate_body_contains(
    assertion: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    body = _http_body(context)
    if body is None:
        return _not_applicable("body_contains", assertion, context)
    expected = str(assertion.get("value", ""))
    passed = expected in body
    return _assertion_result(
        assertion_type="body_contains",
        passed=passed,
        actual=body,
        expected=expected,
        message="body contains expected value" if passed else "body did not contain expected value",
    )


def _evaluate_body_matches(
    assertion: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    body = _http_body(context)
    if body is None:
        return _not_applicable("body_matches", assertion, context)
    pattern = str(assertion.get("pattern", ""))
    match = re.search(pattern, body, flags=re.MULTILINE)
    return _assertion_result(
        assertion_type="body_matches",
        passed=match is not None,
        actual=body,
        expected=pattern,
        message="body matched pattern" if match else "body did not match pattern",
    )


def _evaluate_body_json_path(
    assertion: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    body = _http_body(context)
    if body is None:
        return _not_applicable("body_json_path", assertion, context)
    path = str(assertion.get("path", ""))
    expected = assertion.get("equals")
    try:
        actual = extract_json_path(json.loads(body), path)
    except (json.JSONDecodeError, ValueError, KeyError, IndexError, TypeError) as exc:
        return _assertion_result(
            assertion_type="body_json_path",
            passed=False,
            actual=None,
            expected=expected,
            message=str(exc),
        )
    return _assertion_result(
        assertion_type="body_json_path",
        passed=actual == expected,
        actual=actual,
        expected=expected,
        message="JSONPath value matched" if actual == expected else "JSONPath value did not match",
    )


def _evaluate_exit_code(
    assertion: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    actual = context.get("exit_code")
    if actual is None:
        return _not_applicable("exit_code", assertion, context)
    return _compare_numeric("exit_code", actual, assertion)


def _evaluate_stream_contains(
    stream_name: str,
    assertion: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    actual = context.get(stream_name)
    assertion_type = f"{stream_name}_contains"
    if actual is None:
        return _not_applicable(assertion_type, assertion, context)
    expected = str(assertion.get("value", ""))
    actual_text = str(actual)
    passed = expected in actual_text
    return _assertion_result(
        assertion_type=assertion_type,
        passed=passed,
        actual=actual_text,
        expected=expected,
        message=(
            f"{stream_name} contains expected value"
            if passed
            else f"{stream_name} did not contain expected value"
        ),
    )


def _evaluate_log_matches(
    assertion: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    logs = (
        context.get("logs_since_step_start")
        if assertion.get("since_step_start", True)
        else context.get("jellyfin_logs")
    )
    if logs is None:
        logs = context.get("jellyfin_logs") or context.get("logs")
    if logs is None:
        return _not_applicable("log_matches", assertion, context)
    pattern = str(assertion.get("pattern", ""))
    match = re.search(pattern, str(logs), flags=re.MULTILINE)
    return _assertion_result(
        assertion_type="log_matches",
        passed=match is not None,
        actual=str(logs),
        expected=pattern,
        message="logs matched pattern" if match else "logs did not match pattern",
    )


def _evaluate_screenshot_present(
    assertion: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    label = str(assertion.get("label", ""))
    screenshots = context.get("screenshots")
    path = None
    if isinstance(screenshots, Mapping):
        path = screenshots.get(label)
    if path is None and context.get("screenshot_label") == label:
        path = context.get("screenshot_path")

    passed = isinstance(path, str) and bool(path) and os.path.exists(path)
    return _assertion_result(
        assertion_type="screenshot_present",
        passed=passed,
        actual=path,
        expected=label,
        message="screenshot exists" if passed else "screenshot was not captured",
    )


def _compare_numeric(
    assertion_type: str,
    actual: Any,
    assertion: Mapping[str, Any],
) -> dict[str, Any]:
    if "equals" in assertion:
        expected = assertion["equals"]
        passed = actual == expected
        message = "value matched" if passed else "value did not match"
    elif "in" in assertion:
        expected = assertion["in"]
        passed = actual in expected
        message = "value was in expected set" if passed else "value was not in expected set"
    else:
        expected = "equals or in"
        passed = False
        message = "assertion missing equals or in"
    return _assertion_result(assertion_type, passed, actual, expected, message)


def _extract_capture_value(
    expression: Mapping[str, Any],
    context: Mapping[str, Any],
) -> Any:
    source = expression.get("from")
    if source == "body_json_path":
        body = _require_http_body(source, context)
        try:
            return extract_json_path(json.loads(body), str(expression.get("path", "")))
        except (json.JSONDecodeError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise CaptureError("", str(exc)) from exc
    if source == "body_regex":
        return _extract_regex(_require_http_body(source, context), expression)
    if source == "header":
        name = str(expression.get("name", ""))
        headers = _http(context).get("headers") or {}
        if not isinstance(headers, Mapping):
            raise CaptureError("", "HTTP headers are unavailable")
        for header_name, value in headers.items():
            if str(header_name).lower() == name.lower():
                return value
        raise CaptureError("", f"missing HTTP header: {name}")
    if source == "stdout_regex":
        if context.get("stdout") is None:
            raise CaptureError("", "stdout is unavailable")
        return _extract_regex(str(context.get("stdout")), expression)
    if source == "stdout_trimmed":
        if context.get("stdout") is None:
            raise CaptureError("", "stdout is unavailable")
        return str(context.get("stdout")).strip()
    if source == "exit_code":
        if context.get("exit_code") is None:
            raise CaptureError("", "exit_code is unavailable")
        return context.get("exit_code")
    raise CaptureError("", f"unsupported capture source: {source}")


def _extract_regex(text: str, expression: Mapping[str, Any]) -> str:
    pattern = str(expression.get("pattern", ""))
    group = int(expression.get("group", 1))
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        raise CaptureError("", "regex did not match")
    try:
        return match.group(group)
    except IndexError as exc:
        raise CaptureError("", f"regex group {group} did not exist") from exc


def _require_http_body(source: str, context: Mapping[str, Any]) -> str:
    body = _http_body(context)
    if body is None:
        raise CaptureError("", f"{source} requires an HTTP response body")
    return body


def _http(context: Mapping[str, Any]) -> Mapping[str, Any]:
    http = context.get("http") or {}
    return http if isinstance(http, Mapping) else {}


def _http_body(context: Mapping[str, Any]) -> str | None:
    body = _http(context).get("body")
    if body is None:
        return None
    return body if isinstance(body, str) else json.dumps(body, sort_keys=True)


def _not_applicable(
    assertion_type: str,
    assertion: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    return _assertion_result(
        assertion_type=assertion_type,
        passed=False,
        actual=None,
        expected=_expected_value(assertion),
        message=f"criterion not applicable to step.tool: {context.get('tool')}",
    )


def _expected_value(assertion: Mapping[str, Any]) -> Any:
    if "equals" in assertion:
        return assertion["equals"]
    if "in" in assertion:
        return assertion["in"]
    if "value" in assertion:
        return assertion["value"]
    if "pattern" in assertion:
        return assertion["pattern"]
    if "label" in assertion:
        return assertion["label"]
    return None


def _assertion_result(
    assertion_type: str,
    passed: bool,
    actual: Any,
    expected: Any,
    message: str,
) -> dict[str, Any]:
    return {
        "type": assertion_type,
        "passed": passed,
        "actual": actual,
        "expected": expected,
        "message": message,
    }


def _invalid_criteria(message: str) -> dict[str, Any]:
    return {
        "passed": False,
        "operator": None,
        "assertions": [
            _assertion_result(
                assertion_type="criteria",
                passed=False,
                actual=None,
                expected="valid success_criteria",
                message=message,
            )
        ],
    }


def _read_dot_key(path: str, index: int) -> tuple[str, int]:
    match = re.match(r"[A-Za-z_][A-Za-z0-9_-]*", path[index:])
    if not match:
        raise ValueError(f"expected object key at offset {index}")
    key = match.group(0)
    return key, index + len(key)


def _read_bracket_token(path: str, index: int) -> tuple[int | str, int]:
    if index >= len(path):
        raise ValueError("unterminated bracket token")

    quote = path[index]
    if quote in {"'", '"'}:
        end = index + 1
        chars: list[str] = []
        while end < len(path):
            char = path[end]
            if char == "\\" and end + 1 < len(path):
                chars.append(path[end + 1])
                end += 2
                continue
            if char == quote:
                if end + 1 >= len(path) or path[end + 1] != "]":
                    raise ValueError(f"expected closing bracket at offset {end + 1}")
                return "".join(chars), end + 2
            chars.append(char)
            end += 1
        raise ValueError("unterminated quoted key")

    end = path.find("]", index)
    if end == -1:
        raise ValueError("unterminated bracket token")
    token = path[index:end].strip()
    if not re.fullmatch(r"-?\d+", token):
        raise ValueError(f"unsupported bracket token: {token}")
    return int(token), end + 1


def _lookup_key(value: Any, key: str) -> Any:
    if not isinstance(value, Mapping):
        raise TypeError(f"cannot read key {key!r} from non-object")
    if key not in value:
        raise KeyError(key)
    return value[key]


def _lookup_index(value: Any, index: int) -> Any:
    if not isinstance(value, list):
        raise TypeError(f"cannot read index {index} from non-array")
    return value[index]
