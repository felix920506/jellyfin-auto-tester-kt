"""GitHub code and issue search tool for the analysis stage.

Wraps PyGithub's search APIs so the analysis agent can find issues, pull
requests, and code without falling back to a generic web search.
"""

from __future__ import annotations

import os
from typing import Any

from github import Auth, Github

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

    client = _client()
    if kind == "issues":
        results = client.search_issues(query=query)
        items = [_format_issue_item(item) for item in _take(results, max_results)]
    else:
        results = client.search_code(query=query)
        items = [_format_code_item(item) for item in _take(results, max_results)]

    return {"total_count": results.totalCount, "items": items}


def _client() -> Github:
    token = os.getenv("GITHUB_TOKEN")
    auth = Auth.Token(token) if token else None
    return Github(auth=auth, user_agent=USER_AGENT)


def _take(paginated: Any, count: int) -> list[Any]:
    out: list[Any] = []
    for item in paginated:
        if len(out) >= count:
            break
        out.append(item)
    return out


def _format_issue_item(item: Any) -> dict[str, Any]:
    is_pr = getattr(item, "pull_request", None) is not None
    return {
        "kind": "pull_request" if is_pr else "issue",
        "url": item.html_url or "",
        "title": item.title or "",
        "state": item.state or "",
        "labels": [label.name for label in (item.labels or []) if label.name],
        "created_at": _format_datetime(item.created_at),
        "updated_at": _format_datetime(item.updated_at),
        "body_excerpt": _truncate(item.body or "", 400),
    }


def _format_code_item(item: Any) -> dict[str, Any]:
    repo = getattr(item, "repository", None)
    return {
        "path": item.path or "",
        "repo": repo.full_name if repo else "",
        "url": item.html_url or "",
    }


def _format_datetime(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        iso = value.isoformat()
        if iso.endswith("+00:00"):
            return iso[:-6] + "Z"
        return iso
    return str(value)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"
