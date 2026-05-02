"""GitHub code and issue search tool for the analysis stage.

Wraps PyGithub's search APIs so the analysis agent can find issues, pull
requests, and code without falling back to a generic web search.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from github import Auth, Github, GithubException
from kohakuterrarium.modules.tool.base import BaseTool, ExecutionMode, ToolResult
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(
    "kohakuterrarium.jellyfin_auto_tester.tools.github_search",
    logging.NOTSET,
)

USER_AGENT = "jellyfin-auto-tester-stage1"

CODE_SEARCH_QUALIFIER_RE = re.compile(
    r"(?<![\w-])(?:filename|path|extension|language|symbol|content):",
    re.IGNORECASE,
)
CODE_SEARCH_IN_QUALIFIER_RE = re.compile(
    r"(?<![\w-])in:(?:file|path)\b",
    re.IGNORECASE,
)
ISSUE_KIND_QUALIFIER_RE = re.compile(
    r"(?<![\w-])(?:is:(?:issue|pr|pull-request)|type:(?:issue|pr))\b",
    re.IGNORECASE,
)


def github_search(
    query: str,
    kind: str = "auto",
    max_results: int = 10,
) -> dict[str, Any]:
    """Search GitHub for issues, pull requests, or code.

    Args:
        query: GitHub search query string. Qualifiers such as ``repo:``,
            ``is:issue``, ``is:pr``, ``label:``, and ``in:title`` are all
            supported. Example: ``repo:jellyfin/jellyfin is:issue transcoding``.
            For ``kind="issues"``, GitHub requires either ``is:issue`` or
            ``is:pull-request``. When neither is present, the tool performs
            separate issue and pull-request searches and combines the results.
        kind: Type of search to perform. One of ``"auto"``, ``"issues"``
            (covers both issues and pull requests by running separate searches
            unless narrowed with ``is:issue`` or ``is:pull-request``/``is:pr``
            qualifiers), or ``"code"``.
        max_results: Maximum number of results to return (1–30, default 10).

    Returns:
        A dictionary with a ``total_count`` key and an ``items`` list. Each
        item contains the fields most relevant for reproduction analysis.
    """
    kind = _resolve_kind(query, kind)
    if kind not in ("issues", "code"):
        raise ValueError("kind must be 'auto', 'issues', or 'code'")

    max_results = max(1, min(30, max_results))

    client = _client()
    if kind == "issues":
        total_count, items = _search_issues_and_pull_requests(
            client,
            query,
            max_results,
        )
    else:
        results = client.search_code(query=query)
        total_count = results.totalCount
        items = [_format_code_item(item) for item in _take(results, max_results)]

    return {"total_count": total_count, "items": items}


def _resolve_kind(query: str, kind: str) -> str:
    requested = str(kind or "auto").lower()
    if requested == "issues" and _looks_like_code_search(query):
        return "code"
    if requested != "auto":
        return requested
    if _looks_like_code_search(query):
        return "code"
    return "issues"


def _looks_like_code_search(query: str) -> bool:
    if _has_issue_kind_qualifier(query):
        return False
    return bool(
        CODE_SEARCH_QUALIFIER_RE.search(query)
        or CODE_SEARCH_IN_QUALIFIER_RE.search(query)
    )


def _search_issues_and_pull_requests(
    client: Github,
    query: str,
    max_results: int,
) -> tuple[int, list[dict[str, Any]]]:
    result_groups: list[list[dict[str, Any]]] = []
    seen_urls: set[str] = set()
    total_count = 0

    for effective_query in _issue_search_queries(query):
        results = client.search_issues(query=effective_query)
        total_count += results.totalCount
        group: list[dict[str, Any]] = []
        for item in _take(results, max_results):
            formatted = _format_issue_item(item)
            url = formatted.get("url")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            group.append(formatted)
        result_groups.append(group)

    return total_count, _interleave_results(result_groups, max_results)


def _interleave_results(
    result_groups: list[list[dict[str, Any]]],
    max_results: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    max_group_size = max((len(group) for group in result_groups), default=0)
    for index in range(max_group_size):
        for group in result_groups:
            if index >= len(group):
                continue
            items.append(group[index])
            if len(items) >= max_results:
                return items
    return items


def _issue_search_queries(query: str) -> list[str]:
    if _has_issue_kind_qualifier(query):
        return [query]
    return [f"{query} is:issue", f"{query} is:pull-request"]


def _ensure_issue_kind_qualifier(query: str) -> str:
    """GitHub's issue search rejects queries without ``is:issue`` or
    ``is:pull-request``."""
    if _has_issue_kind_qualifier(query):
        return query
    return f"{query} is:issue"


def _has_issue_kind_qualifier(query: str) -> bool:
    return bool(ISSUE_KIND_QUALIFIER_RE.search(query))


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


class GitHubSearchTool(BaseTool):
    """Search GitHub for issues, pull requests, or code."""

    @property
    def tool_name(self) -> str:
        return "github_search"

    @property
    def description(self) -> str:
        return (
            "Search GitHub for issues, pull requests, or code using the "
            "GitHub Search API qualifier syntax. With kind='issues' the search "
            "covers both issues AND pull requests by default — pull requests "
            "often hold root-cause discussion and fixes, so prefer keeping "
            "both unless you explicitly need to narrow with is:issue / is:pr."
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "GitHub search query using qualifier syntax, for example "
                        "repo:jellyfin/jellyfin is:issue transcoding"
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": ["auto", "issues", "code"],
                    "description": (
                        "Search issues/pull requests or code. Default: auto, "
                        "which detects code-only qualifiers such as filename:."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "description": "Maximum number of results to return. Default: 10.",
                },
            },
            "required": ["query"],
        }

    def prompt_contribution(self) -> str | None:
        return (
            "Use `query` for the GitHub search string, or place the query in "
            "the tool block body. `kind` defaults to `auto`; use `kind='code'` "
            "for code search if the query has no code-only qualifier."
        )

    async def _execute(self, args: dict[str, Any], **kwargs: Any) -> ToolResult:
        logger.debug("github_search invoked", tool_args=args, kwargs_keys=list(kwargs))
        query = _query_from_args(args)
        if not query:
            logger.warning(
                "github_search rejected: missing query",
                arg_keys=list(args.keys()),
            )
            return ToolResult(
                error=(
                    "No query provided. Usage: "
                    "github_search(query='repo:owner/name is:issue ...') "
                    "or put the search query in the tool block body."
                )
            )
        kind = args.get("kind", "auto")
        max_results = int(args.get("max_results", 10))
        resolved_kind = _resolve_kind(query, kind)

        token_present = bool(os.getenv("GITHUB_TOKEN"))
        logger.debug(
            "github_search calling PyGithub",
            query=query,
            kind=resolved_kind,
            max_results=max_results,
            github_token_present=token_present,
        )
        try:
            payload = github_search(
                query=query,
                kind=resolved_kind,
                max_results=max_results,
            )
        except (ValueError, GithubException) as exc:
            logger.warning(
                "github_search failed",
                exc_type=type(exc).__name__,
                error=str(exc),
                query=query,
                kind=resolved_kind,
            )
            return ToolResult(error=f"github_search failed: {exc}")
        except Exception as exc:
            logger.exception(
                "github_search crashed unexpectedly",
                exc_type=type(exc).__name__,
                query=query,
                kind=resolved_kind,
            )
            return ToolResult(error=f"github_search crashed: {type(exc).__name__}: {exc}")

        logger.debug(
            "github_search succeeded",
            query=query,
            kind=resolved_kind,
            total_count=payload.get("total_count"),
            returned=len(payload.get("items", [])),
        )
        return ToolResult(
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            exit_code=0,
        )


def _query_from_args(args: dict[str, Any]) -> str:
    for key in ("query", "content"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
