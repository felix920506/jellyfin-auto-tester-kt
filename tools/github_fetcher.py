"""GitHub issue fetching tool for the analysis stage.

Wraps PyGithub to fetch the canonical issue payload, page through comments,
and resolve issue/PR references found in the issue text.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from github import Auth, Github, GithubException, UnknownObjectException
from kohakuterrarium.modules.tool.base import BaseTool, ExecutionMode, ToolResult

USER_AGENT = "jellyfin-auto-tester-stage1"

ISSUE_URL_RE = re.compile(
    r"^https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/"
    r"(?P<kind>issues|pull)/"
    r"(?P<number>\d+)"
    r"(?:[/?#].*)?$"
)

DIRECT_REFERENCE_RE = re.compile(
    r"https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/"
    r"(?P<kind>issues|pull)/"
    r"(?P<number>\d+)"
)

CROSS_REPO_REFERENCE_RE = re.compile(
    r"(?<![\w.-])"
    r"(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)"
    r"#(?P<number>\d+)\b"
)

LOCAL_REFERENCE_RE = re.compile(r"(?<![\w/.-])#(?P<number>\d+)\b")


def github_fetcher(
    issue_url: str,
    include_comments: bool = True,
    include_linked: bool = True,
) -> dict[str, Any]:
    """Fetch a GitHub issue and related context.

    Args:
        issue_url: Full GitHub issue or pull request URL.
        include_comments: Whether to include paginated issue comments.
        include_linked: Whether to resolve referenced issues and pull requests.

    Returns:
        A dictionary with issue fields, normalized comments, and linked
        issue/pull request summaries.
    """

    owner, repo_name, number = _parse_issue_url(issue_url)
    client = _client()
    repo = client.get_repo(f"{owner}/{repo_name}")
    issue = repo.get_issue(number)

    comments: list[dict[str, str | None]] = []
    if include_comments:
        comments = [_format_comment(c) for c in issue.get_comments()]

    linked_issues: list[dict[str, Any]] = []
    linked_prs: list[dict[str, Any]] = []
    if include_linked:
        linked_issues, linked_prs = _get_linked_items(
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
    }


def _parse_issue_url(issue_url: str) -> tuple[str, str, int]:
    match = ISSUE_URL_RE.match(issue_url.strip())
    if not match:
        raise ValueError(
            "issue_url must look like "
            "https://github.com/<owner>/<repo>/issues/<number>"
        )

    return match.group("owner"), match.group("repo"), int(match.group("number"))


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    texts = [issue_body, *comment_bodies]
    references = _extract_references(texts, source_owner, source_repo)
    references.discard((source_owner.lower(), source_repo.lower(), source_number))

    linked_issues: list[dict[str, Any]] = []
    linked_prs: list[dict[str, Any]] = []

    for owner, repo_name, number in sorted(references):
        try:
            repo = client.get_repo(f"{owner}/{repo_name}")
            issue = repo.get_issue(number)
        except (UnknownObjectException, GithubException) as exc:
            linked_issues.append(
                {
                    "url": f"https://github.com/{owner}/{repo_name}/issues/{number}",
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
            except GithubException:
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

    return linked_issues, linked_prs


def _extract_references(
    texts: list[str],
    default_owner: str,
    default_repo: str,
) -> set[tuple[str, str, int]]:
    references: set[tuple[str, str, int]] = set()

    for text in texts:
        for match in DIRECT_REFERENCE_RE.finditer(text):
            references.add(
                (
                    match.group("owner").lower(),
                    match.group("repo").lower(),
                    int(match.group("number")),
                )
            )

        for match in CROSS_REPO_REFERENCE_RE.finditer(text):
            references.add(
                (
                    match.group("owner").lower(),
                    match.group("repo").lower(),
                    int(match.group("number")),
                )
            )

        for match in LOCAL_REFERENCE_RE.finditer(text):
            references.add(
                (
                    default_owner.lower(),
                    default_repo.lower(),
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
    """Fetch a GitHub issue or pull request along with comments and linked refs."""

    @property
    def tool_name(self) -> str:
        return "github_fetcher"

    @property
    def description(self) -> str:
        return (
            "Fetch a GitHub issue or pull request, paginate its comments, and "
            "resolve referenced issues/PRs."
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    async def _execute(self, args: dict[str, Any], **kwargs: Any) -> ToolResult:
        issue_url = args.get("issue_url", "")
        if not issue_url:
            return ToolResult(
                error="No issue_url provided. Usage: github_fetcher(issue_url='https://github.com/<owner>/<repo>/issues/<n>')"
            )
        include_comments = bool(args.get("include_comments", True))
        include_linked = bool(args.get("include_linked", True))

        try:
            payload = github_fetcher(
                issue_url=issue_url,
                include_comments=include_comments,
                include_linked=include_linked,
            )
        except (ValueError, GithubException, UnknownObjectException) as exc:
            return ToolResult(error=f"github_fetcher failed: {exc}")

        return ToolResult(
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            exit_code=0,
        )
