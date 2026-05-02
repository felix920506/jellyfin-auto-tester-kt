"""GitHub code and issue search tool for the analysis stage.

Uses only the Python standard library. Wraps the GitHub Search API to let the
analysis agent find issues, pull requests, and code without falling back to a
generic web search.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GITHUB_API_URL = "https://api.github.com"
USER_AGENT = "jellyfin-auto-tester-stage1"


def github_search(
    query: str,
    kind: str = "issues",
    max_results: int = 10,
) -> dict[str, Any]:
    """Search GitHub for issues, pull requests, or code.

    Args:
        query: GitHub search query string. Qualifiers such as ``repo:``,
            ``is:issue``, ``is:pr``, ``label:``, and ``in:title`` are all
            supported. Example: ``repo:jellyfin/jellyfin is:issue transcoding``.
        kind: Type of search to perform. One of ``"issues"`` (covers both
            issues and pull requests), or ``"code"``.
        max_results: Maximum number of results to return (1–30, default 10).

    Returns:
        A dictionary with a ``total_count`` key and an ``items`` list. Each
        item contains the fields most relevant for reproduction analysis.
    """
    if kind not in ("issues", "code"):
        raise ValueError("kind must be 'issues' or 'code'")

    max_results = max(1, min(30, max_results))

    params = urlencode({"q": query, "per_page": max_results})
    url = f"{GITHUB_API_URL}/search/{kind}?{params}"

    payload = _get_json(url)
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub Search API returned a non-object payload")

    total_count: int = payload.get("total_count", 0)
    raw_items: list[Any] = payload.get("items", [])

    if kind == "issues":
        items = [_format_issue_item(item) for item in raw_items if isinstance(item, dict)]
    else:
        items = [_format_code_item(item) for item in raw_items if isinstance(item, dict)]

    return {"total_count": total_count, "items": items}


def _format_issue_item(item: dict[str, Any]) -> dict[str, Any]:
    pull_request = item.get("pull_request")
    kind = "pull_request" if isinstance(pull_request, dict) else "issue"
    return {
        "kind": kind,
        "url": item.get("html_url") or "",
        "title": item.get("title") or "",
        "state": item.get("state") or "",
        "labels": _format_labels(item.get("labels", [])),
        "created_at": item.get("created_at") or "",
        "updated_at": item.get("updated_at") or "",
        "body_excerpt": _truncate(item.get("body") or "", 400),
    }


def _format_code_item(item: dict[str, Any]) -> dict[str, Any]:
    repo = item.get("repository") or {}
    return {
        "path": item.get("path") or "",
        "repo": repo.get("full_name") or "",
        "url": item.get("html_url") or "",
    }


def _format_labels(labels: Any) -> list[str]:
    if not isinstance(labels, list):
        return []
    result: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
            if name:
                result.append(str(name))
        elif label:
            result.append(str(label))
    return result


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _get_json(url: str) -> Any:
    request = Request(url, headers=_github_headers())
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = _read_error_body(exc)
        raise RuntimeError(f"GitHub Search API failed with HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise RuntimeError(f"GitHub Search API request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GitHub Search API returned invalid JSON from {url}") from exc


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _read_error_body(exc: HTTPError) -> str:
    try:
        raw_body = exc.read().decode("utf-8")
    except Exception:
        return exc.reason

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body.strip() or exc.reason

    if isinstance(payload, dict):
        message = payload.get("message")
        if message:
            return str(message)

    return raw_body.strip() or exc.reason
