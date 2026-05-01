"""GitHub issue fetching tool for the analysis stage.

The tool intentionally uses only the Python standard library so Stage 1 can run
in a minimal agent environment. It fetches the canonical issue payload, pages
through comments, and resolves issue/PR references found in the issue text.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

GITHUB_API_URL = "https://api.github.com"
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

    owner, repo, number = _parse_issue_url(issue_url)
    issue = _get_issue(owner, repo, number)

    comments: list[dict[str, str | None]] = []
    if include_comments:
        comments = [
            _format_comment(comment)
            for comment in _get_paginated(
                _api_url(
                    "/repos/{owner}/{repo}/issues/{number}/comments?per_page=100",
                    owner=owner,
                    repo=repo,
                    number=number,
                )
            )
        ]

    linked_issues: list[dict[str, Any]] = []
    linked_prs: list[dict[str, Any]] = []
    if include_linked:
        linked_issues, linked_prs = _get_linked_items(
            issue=issue,
            comments=comments,
            source_owner=owner,
            source_repo=repo,
            source_number=number,
        )

    return {
        "title": issue.get("title") or "",
        "body": issue.get("body") or "",
        "labels": _format_labels(issue.get("labels", [])),
        "state": issue.get("state") or "",
        "created_at": issue.get("created_at") or "",
        "author": _user_login(issue.get("user")),
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


def _get_issue(owner: str, repo: str, number: int) -> dict[str, Any]:
    payload = _get_json(
        _api_url(
            "/repos/{owner}/{repo}/issues/{number}",
            owner=owner,
            repo=repo,
            number=number,
        )
    )
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub issue endpoint returned a non-object payload")
    return payload


def _get_linked_items(
    issue: dict[str, Any],
    comments: list[dict[str, str | None]],
    source_owner: str,
    source_repo: str,
    source_number: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    texts = [issue.get("body") or ""]
    texts.extend(comment.get("body") or "" for comment in comments)

    references = _extract_references(texts, source_owner, source_repo)
    references.discard((source_owner.lower(), source_repo.lower(), source_number))

    linked_issues: list[dict[str, Any]] = []
    linked_prs: list[dict[str, Any]] = []

    for owner, repo, number in sorted(references):
        summary = _fetch_linked_summary(owner, repo, number)
        if _is_pull_request(summary):
            linked_prs.append(_format_pull_request_summary(owner, repo, number, summary))
        else:
            linked_issues.append(_format_issue_summary(owner, repo, number, summary))

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


def _fetch_linked_summary(owner: str, repo: str, number: int) -> dict[str, Any]:
    try:
        return _get_issue(owner, repo, number)
    except RuntimeError as exc:
        return {
            "title": "",
            "state": "unknown",
            "html_url": f"https://github.com/{owner}/{repo}/issues/{number}",
            "fetch_error": str(exc),
        }


def _format_issue_summary(
    owner: str,
    repo: str,
    number: int,
    issue: dict[str, Any],
) -> dict[str, Any]:
    return {
        "url": issue.get("html_url") or f"https://github.com/{owner}/{repo}/issues/{number}",
        "title": issue.get("title") or "",
        "state": issue.get("state") or "unknown",
    }


def _format_pull_request_summary(
    owner: str,
    repo: str,
    number: int,
    issue: dict[str, Any],
) -> dict[str, Any]:
    merged = False
    try:
        pull_request = _get_json(
            _api_url(
                "/repos/{owner}/{repo}/pulls/{number}",
                owner=owner,
                repo=repo,
                number=number,
            )
        )
        if isinstance(pull_request, dict):
            merged = bool(pull_request.get("merged", False))
    except RuntimeError:
        merged = False

    return {
        "url": issue.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}",
        "title": issue.get("title") or "",
        "state": issue.get("state") or "unknown",
        "merged": merged,
    }


def _is_pull_request(issue: dict[str, Any]) -> bool:
    return isinstance(issue.get("pull_request"), dict)


def _format_comment(comment: dict[str, Any]) -> dict[str, str | None]:
    return {
        "author": _user_login(comment.get("user")),
        "body": comment.get("body") or "",
        "created_at": comment.get("created_at") or "",
    }


def _format_labels(labels: Any) -> list[str]:
    if not isinstance(labels, list):
        return []

    formatted: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
            if name:
                formatted.append(str(name))
        elif label:
            formatted.append(str(label))
    return formatted


def _user_login(user: Any) -> str | None:
    if isinstance(user, dict):
        login = user.get("login")
        if login:
            return str(login)
    return None


def _get_paginated(url: str) -> list[Any]:
    items: list[Any] = []
    next_url: str | None = url

    while next_url:
        payload, headers = _get_json_with_headers(next_url)
        if not isinstance(payload, list):
            raise RuntimeError("GitHub paginated endpoint returned a non-list payload")
        items.extend(payload)
        next_url = _next_link(headers.get("Link"))

    return items


def _get_json(url: str) -> Any:
    payload, _headers = _get_json_with_headers(url)
    return payload


def _get_json_with_headers(url: str) -> tuple[Any, Any]:
    request = Request(url, headers=_github_headers())

    try:
        with urlopen(request, timeout=30) as response:
            raw_body = response.read().decode("utf-8")
            return json.loads(raw_body), response.headers
    except HTTPError as exc:
        details = _read_error_body(exc)
        raise RuntimeError(f"GitHub API request failed with HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise RuntimeError(f"GitHub API request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GitHub API returned invalid JSON from {url}") from exc


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


def _api_url(path_template: str, **parts: object) -> str:
    quoted_parts = {name: quote(str(value), safe="") for name, value in parts.items()}
    return GITHUB_API_URL + path_template.format(**quoted_parts)


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None

    for entry in link_header.split(","):
        sections = [section.strip() for section in entry.split(";")]
        if len(sections) < 2:
            continue
        url_section = sections[0]
        rel_sections = sections[1:]
        if not (url_section.startswith("<") and url_section.endswith(">")):
            continue
        if any(section == 'rel="next"' for section in rel_sections):
            return url_section[1:-1]

    return None


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
