"""GitHub issue fetching tool for the analysis stage.

Wraps PyGithub to fetch the canonical issue payload, page through comments,
and resolve issue/PR references found in the issue text.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from github import Auth, Github, GithubException, UnknownObjectException
from kohakuterrarium.modules.tool.base import BaseTool, ExecutionMode, ToolResult
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(
    "kohakuterrarium.jellyfin_auto_tester.tools.github_fetcher",
    logging.NOTSET,
)

USER_AGENT = "jellyfin-auto-tester-stage1"

ISSUE_URL_RE = re.compile(
    r"^https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/"
    r"(?P<kind>issues|pull|discussions)/"
    r"(?P<number>\d+)"
    r"(?:[/?#].*)?$"
)

DIRECT_REFERENCE_RE = re.compile(
    r"https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/"
    r"(?P<kind>issues|pull|discussions)/"
    r"(?P<number>\d+)"
)

CROSS_REPO_REFERENCE_RE = re.compile(
    r"(?<![\w.-])"
    r"(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)"
    r"#(?P<number>\d+)\b"
)

LOCAL_REFERENCE_RE = re.compile(r"(?<![\w/.-])#(?P<number>\d+)\b")

DISCUSSION_SCHEMA = """
id
number
title
body
bodyText
url
createdAt
updatedAt
author { login }
category { name }
"""

DISCUSSION_COMMENT_SCHEMA = """
id
body
bodyText
url
createdAt
updatedAt
author { login }
"""


def github_fetcher(
    issue_url: str,
    include_comments: bool = True,
    include_linked: bool = True,
) -> dict[str, Any]:
    """Fetch a GitHub issue, pull request, or discussion and related context.

    Args:
        issue_url: Full GitHub issue, pull request, or discussion URL.
        include_comments: Whether to include paginated issue comments.
        include_linked: Whether to resolve referenced issues, pull requests,
            and discussions.

    Returns:
        A dictionary with issue fields, normalized comments, and linked
        issue/pull request/discussion summaries.
    """

    owner, repo_name, kind, number = _parse_github_url(issue_url)
    client = _client()
    repo = client.get_repo(f"{owner}/{repo_name}")
    if kind == "discussions":
        discussion = repo.get_discussion(number, DISCUSSION_SCHEMA)
        return _format_discussion_payload(
            discussion,
            include_comments=include_comments,
        )

    issue = repo.get_issue(number)

    comments: list[dict[str, str | None]] = []
    if include_comments:
        comments = [_format_comment(c) for c in issue.get_comments()]

    linked_issues: list[dict[str, Any]] = []
    linked_prs: list[dict[str, Any]] = []
    linked_discussions: list[dict[str, Any]] = []
    if include_linked:
        linked_issues, linked_prs, linked_discussions = _get_linked_items(
            client=client,
            issue_body=issue.body or "",
            comment_bodies=[c.get("body") or "" for c in comments],
            source_owner=owner,
            source_repo=repo_name,
            source_number=number,
        )

    return {
        "title": issue.title or "",
        "body": issue.body or "",
        "labels": [label.name for label in issue.labels if label.name],
        "state": issue.state or "",
        "created_at": _format_datetime(issue.created_at),
        "author": issue.user.login if issue.user else None,
        "comments": comments,
        "linked_issues": linked_issues,
        "linked_prs": linked_prs,
        "linked_discussions": linked_discussions,
    }


def _parse_issue_url(issue_url: str) -> tuple[str, str, int]:
    owner, repo, _kind, number = _parse_github_url(issue_url)
    return owner, repo, number


def _parse_github_url(issue_url: str) -> tuple[str, str, str, int]:
    match = ISSUE_URL_RE.match(issue_url.strip())
    if not match:
        raise ValueError(
            "issue_url must look like "
            "https://github.com/<owner>/<repo>/issues/<number>, "
            "https://github.com/<owner>/<repo>/pull/<number>, or "
            "https://github.com/<owner>/<repo>/discussions/<number>"
        )

    return (
        match.group("owner"),
        match.group("repo"),
        match.group("kind"),
        int(match.group("number")),
    )


def _client() -> Github:
    token = os.getenv("GITHUB_TOKEN")
    auth = Auth.Token(token) if token else None
    return Github(auth=auth, user_agent=USER_AGENT)


def _get_linked_items(
    client: Github,
    issue_body: str,
    comment_bodies: list[str],
    source_owner: str,
    source_repo: str,
    source_number: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    texts = [issue_body, *comment_bodies]
    references = _extract_references(texts, source_owner, source_repo)
    for kind in ("unknown", "issues", "pull", "discussions"):
        references.discard(
            (source_owner.lower(), source_repo.lower(), kind, source_number)
        )

    linked_issues: list[dict[str, Any]] = []
    linked_prs: list[dict[str, Any]] = []
    linked_discussions: list[dict[str, Any]] = []

    for owner, repo_name, kind, number in sorted(references):
        try:
            repo = client.get_repo(f"{owner}/{repo_name}")
            if kind == "discussions":
                discussion = repo.get_discussion(number, DISCUSSION_SCHEMA)
                linked_discussions.append(_format_discussion_summary(discussion))
                continue
            try:
                issue = repo.get_issue(number)
            except (UnknownObjectException, GithubException) as issue_exc:
                discussion = _try_get_discussion(repo, number)
                if discussion is not None:
                    linked_discussions.append(_format_discussion_summary(discussion))
                    continue
                raise issue_exc
        except (UnknownObjectException, GithubException) as exc:
            logger.warning(
                "github_fetcher failed to fetch linked item",
                owner=owner,
                repo=repo_name,
                number=number,
                error=str(exc),
            )
            linked_issues.append(
                {
                    "url": _fallback_reference_url(owner, repo_name, kind, number),
                    "title": "",
                    "state": "unknown",
                    "fetch_error": str(exc),
                }
            )
            continue

        if issue.pull_request is not None:
            merged = False
            try:
                merged = bool(repo.get_pull(number).merged)
            except GithubException as exc:
                logger.warning(
                    "github_fetcher failed to check PR merge status",
                    owner=owner,
                    repo=repo_name,
                    number=number,
                    error=str(exc),
                )
                merged = False
            linked_prs.append(
                {
                    "url": issue.html_url
                    or f"https://github.com/{owner}/{repo_name}/pull/{number}",
                    "title": issue.title or "",
                    "state": issue.state or "unknown",
                    "merged": merged,
                }
            )
        else:
            linked_issues.append(
                {
                    "url": issue.html_url
                    or f"https://github.com/{owner}/{repo_name}/issues/{number}",
                    "title": issue.title or "",
                    "state": issue.state or "unknown",
                }
            )

    return linked_issues, linked_prs, linked_discussions


def _try_get_discussion(repo: Any, number: int) -> Any | None:
    try:
        return repo.get_discussion(number, DISCUSSION_SCHEMA)
    except (AttributeError, UnknownObjectException, GithubException):
        return None


def _fallback_reference_url(owner: str, repo_name: str, kind: str, number: int) -> str:
    path_kind = "issues" if kind == "unknown" else kind
    return f"https://github.com/{owner}/{repo_name}/{path_kind}/{number}"


def _extract_references(
    texts: list[str],
    default_owner: str,
    default_repo: str,
) -> set[tuple[str, str, str, int]]:
    references: set[tuple[str, str, str, int]] = set()

    for text in texts:
        for match in DIRECT_REFERENCE_RE.finditer(text):
            references.add(
                (
                    match.group("owner").lower(),
                    match.group("repo").lower(),
                    match.group("kind"),
                    int(match.group("number")),
                )
            )

        for match in CROSS_REPO_REFERENCE_RE.finditer(text):
            references.add(
                (
                    match.group("owner").lower(),
                    match.group("repo").lower(),
                    "unknown",
                    int(match.group("number")),
                )
            )

        for match in LOCAL_REFERENCE_RE.finditer(text):
            references.add(
                (
                    default_owner.lower(),
                    default_repo.lower(),
                    "unknown",
                    int(match.group("number")),
                )
            )

    return references


def _format_comment(comment: Any) -> dict[str, str | None]:
    user = getattr(comment, "user", None)
    return {
        "author": user.login if user else None,
        "body": comment.body or "",
        "created_at": _format_datetime(comment.created_at),
    }


def _format_discussion_payload(
    discussion: Any,
    include_comments: bool = True,
) -> dict[str, Any]:
    comments: list[dict[str, str | None]] = []
    if include_comments:
        comments = [
            _format_discussion_comment(comment)
            for comment in discussion.get_comments(DISCUSSION_COMMENT_SCHEMA)
        ]

    return {
        "kind": "discussion",
        "url": _discussion_url(discussion),
        "title": discussion.title or "",
        "body": _discussion_body(discussion),
        "labels": [],
        "state": "",
        "category": _discussion_category_name(discussion),
        "created_at": _format_datetime(discussion.created_at),
        "updated_at": _format_datetime(discussion.updated_at),
        "author": _author_login(discussion),
        "comments": comments,
        "linked_issues": [],
        "linked_prs": [],
        "linked_discussions": [],
    }


def _format_discussion_summary(discussion: Any) -> dict[str, Any]:
    return {
        "url": _discussion_url(discussion),
        "title": discussion.title or "",
        "state": "",
        "category": _discussion_category_name(discussion),
    }


def _format_discussion_comment(comment: Any) -> dict[str, str | None]:
    return {
        "author": _author_login(comment),
        "body": _discussion_body(comment),
        "created_at": _format_datetime(comment.created_at),
    }


def _discussion_body(value: Any) -> str:
    body_text = getattr(value, "body_text", None)
    if body_text:
        return body_text
    return getattr(value, "body", "") or ""


def _discussion_url(discussion: Any) -> str:
    return getattr(discussion, "url", "") or getattr(discussion, "html_url", "") or ""


def _discussion_category_name(discussion: Any) -> str:
    category = getattr(discussion, "category", None)
    return getattr(category, "name", "") if category else ""


def _author_login(value: Any) -> str | None:
    author = getattr(value, "author", None) or getattr(value, "user", None)
    return author.login if author else None


def _format_datetime(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        iso = value.isoformat()
        if iso.endswith("+00:00"):
            return iso[:-6] + "Z"
        return iso
    return str(value)


class GitHubFetcherTool(BaseTool):
    """Fetch a GitHub issue, pull request, or discussion with linked refs."""

    @property
    def tool_name(self) -> str:
        return "github_fetcher"

    @property
    def description(self) -> str:
        return (
            "Fetch a GitHub issue, pull request, or discussion, paginate its "
            "comments, and resolve referenced issues/PRs/discussions."
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    async def _execute(self, args: dict[str, Any], **kwargs: Any) -> ToolResult:
        logger.debug("github_fetcher invoked", tool_args=args, kwargs_keys=list(kwargs))
        issue_url = args.get("issue_url") or args.get("url", "")
        if not issue_url:
            logger.warning(
                "github_fetcher rejected: missing issue_url",
                arg_keys=list(args.keys()),
            )
            return ToolResult(
                error=(
                    "No issue_url provided. Usage: "
                    "github_fetcher(issue_url='https://github.com/<owner>/<repo>/issues/<n>') "
                    "or github_fetcher(url='https://github.com/<owner>/<repo>/pull/<n>') "
                    "or github_fetcher(url='https://github.com/<owner>/<repo>/discussions/<n>')"
                )
            )
        include_comments = bool(args.get("include_comments", True))
        include_linked = bool(args.get("include_linked", True))

        token_present = bool(os.getenv("GITHUB_TOKEN"))
        logger.debug(
            "github_fetcher calling PyGithub",
            issue_url=issue_url,
            include_comments=include_comments,
            include_linked=include_linked,
            github_token_present=token_present,
        )
        try:
            payload = github_fetcher(
                issue_url=issue_url,
                include_comments=include_comments,
                include_linked=include_linked,
            )
        except (ValueError, GithubException, UnknownObjectException) as exc:
            logger.warning(
                "github_fetcher failed",
                exc_type=type(exc).__name__,
                error=str(exc),
                issue_url=issue_url,
            )
            return ToolResult(error=f"github_fetcher failed: {exc}")
        except Exception as exc:
            logger.exception(
                "github_fetcher crashed unexpectedly",
                exc_type=type(exc).__name__,
                issue_url=issue_url,
            )
            return ToolResult(error=f"github_fetcher crashed: {type(exc).__name__}: {exc}")

        logger.debug(
            "github_fetcher succeeded",
            issue_url=issue_url,
            comments=len(payload.get("comments", [])),
            linked_issues=len(payload.get("linked_issues", [])),
            linked_prs=len(payload.get("linked_prs", [])),
        )
        return ToolResult(
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            exit_code=0,
        )
