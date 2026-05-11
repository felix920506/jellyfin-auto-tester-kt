"""Helpers for compact ExecutionResult channel handoffs."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping


def compact_execution_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return an LLM-facing ExecutionResult payload without the embedded plan."""

    compact = {
        key: deepcopy(value)
        for key, value in result.items()
        if key != "plan"
    }
    for key, value in _artifact_path_fields(result).items():
        compact.setdefault(key, value)
    return compact


def compact_report_execution_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return a Stage 3 handoff payload without raw execution logs.

    The report writer can hydrate the complete ExecutionResult from
    ``result_path`` or ``artifacts_dir/result.json``. Keeping Stage 3 handoffs
    path-based prevents report-agent transcripts from carrying original log
    bodies while preserving deterministic report generation.
    """

    compact = compact_execution_result(result)
    compact.pop("execution_log", None)
    compact.pop("jellyfin_logs", None)
    return compact


def compact_if_execution_result(value: Any) -> Any:
    """Compact canonical final results while leaving intermediate statuses alone."""

    if _looks_like_execution_result(value):
        return compact_execution_result(value)
    return value


def hydrate_execution_result(
    payload: Mapping[str, Any],
    *,
    fallback_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a canonical ExecutionResult, loading artifact state when needed."""

    result = deepcopy(dict(payload))
    if isinstance(result.get("plan"), dict):
        return result

    for path in _candidate_result_paths(result):
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict) and isinstance(loaded.get("plan"), dict):
            return loaded

    if fallback_plan is not None:
        result["plan"] = deepcopy(dict(fallback_plan))
        return result

    raise ValueError(
        "execution result payload is missing plan and could not be hydrated "
        "from result_path or artifacts_dir/result.json"
    )


def _looks_like_execution_result(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        isinstance(value.get("plan"), Mapping)
        and isinstance(value.get("execution_log"), list)
        and value.get("overall_result")
        in {"reproduced", "not_reproduced", "inconclusive"}
        and bool(value.get("run_id"))
        and bool(value.get("artifacts_dir"))
    )


def _artifact_path_fields(result: Mapping[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    artifacts_dir = result.get("artifacts_dir")
    if not artifacts_dir:
        return fields

    root = Path(str(artifacts_dir)).expanduser()
    fields["result_path"] = str(root / "result.json")

    plan_json_path = root / "plan.json"
    if plan_json_path.is_file():
        fields["plan_json_path"] = str(plan_json_path)

    plan_markdown_path = root / "plan.md"
    if plan_markdown_path.is_file():
        fields["plan_markdown_path"] = str(plan_markdown_path)

    return fields


def _candidate_result_paths(payload: Mapping[str, Any]) -> list[Path]:
    paths: list[Path] = []
    result_path = payload.get("result_path")
    if result_path:
        paths.append(Path(str(result_path)).expanduser())

    artifacts_dir = payload.get("artifacts_dir")
    if artifacts_dir:
        candidate = Path(str(artifacts_dir)).expanduser() / "result.json"
        if candidate not in paths:
            paths.append(candidate)

    return paths
