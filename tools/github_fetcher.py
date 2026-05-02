"""GitHub URL fetching tool for the analysis stage.

Wraps PyGithub to fetch structured payloads for GitHub URLs, page through
comments where available, and resolve issue/PR/discussion references found in
the fetched content.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
from typing import Any
from urllib.parse import unquote, urlparse

from github import Auth, Github, GithubException, UnknownObjectException
from kohakuterrarium.modules.tool.base import BaseTool, ExecutionMode, ToolResult
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(
    "kohakuterrarium.jellyfin_auto_tester.tools.github_fetcher",
    logging.NOTSET,
)

USER_AGENT = "jellyfin-auto-tester-stage1"
MAX_TEXT_CONTENT_CHARS = 20000
MAX_PATCH_CHARS = 12000

NUMBERED_RESOURCE_URL_RE = re.compile(
    r"^https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/"
    r"(?P<kind>issues|pull|discussions)/"
    r"(?P<number>\d+)"
    r"(?:[/?#].*)?$"
)

GITHUB_URL_IN_TEXT_RE = re.compile(r"https?://github\.com/[^\s<>\]'\")]+")
REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

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


@dataclass(frozen=True)
class GitHubUrl:
    original_url: str
    owner: str
    repo: str
    path_parts: tuple[str, ...]
    fragment: str = ""


class GitHubFetchError(RuntimeError):
    """Raised when no supported GitHub API shape can fetch the URL."""


def github_fetcher(
    url: str | None = None,
    include_comments: bool = True,
    include_linked: bool = True,
    *,
    issue_url: str | None = None,
) -> dict[str, Any]:
    """Fetch a GitHub URL and related context.

    Args:
        url: Full GitHub URL. Issues, pull requests, discussions, commits,
            files, directories, and repository root URLs are supported.
        include_comments: Whether to include paginated comments where available.
        include_linked: Whether to resolve referenced issues, pull requests,
            and discussions.
        issue_url: Backward-compatible alias for ``url``.

    Returns:
        A dictionary with fields normalized for the inferred GitHub resource
        kind.
    """

    github_url = _resolve_github_url(url=url, issue_url=issue_url)
    parsed_url = _parse_any_github_url(github_url)
    client = _client()
    repo = client.get_repo(f"{parsed_url.owner}/{parsed_url.repo}")
    return _fetch_parsed_github_url(
        client=client,
        repo=repo,
        parsed_url=parsed_url,
        include_comments=include_comments,
        include_linked=include_linked,
    )


def _resolve_github_url(url: str | None = None, issue_url: str | None = None) -> str:
    github_url = url or issue_url
    if not github_url:
        raise ValueError("url must be a GitHub URL")
    return str(github_url).strip()


def _fetch_parsed_github_url(
    client: Github,
    repo: Any,
    parsed_url: GitHubUrl,
    include_comments: bool,
    include_linked: bool,
) -> dict[str, Any]:
    path_parts = parsed_url.path_parts
    if not path_parts:
        return _format_repository_payload(repo)

    path_kind = path_parts[0]
    if _is_numbered_resource(path_parts):
        return _fetch_numbered_resource(
            client=client,
            repo=repo,
            parsed_url=parsed_url,
            include_comments=include_comments,
            include_linked=include_linked,
        )

    if path_kind in ("commit", "commits"):
        return _fetch_commit_payload(
            client=client,
            repo=repo,
            parsed_url=parsed_url,
            include_comments=include_comments,
            include_linked=include_linked,
        )

    if path_kind in ("blob", "raw", "tree"):
        return _fetch_content_payload(repo=repo, parsed_url=parsed_url)

    raise ValueError(
        "url must be a supported GitHub resource URL: repository root, "
        "issues/<number>, pull/<number>, discussions/<number>, commit/<sha>, "
        "blob/<ref>/<path>, or tree/<ref>/<path>"
    )


def _is_numbered_resource(path_parts: tuple[str, ...]) -> bool:
    return (
        len(path_parts) >= 2
        and path_parts[0] in ("issues", "pull", "discussions")
        and path_parts[1].isdigit()
    )


def _fetch_numbered_resource(
    client: Github,
    repo: Any,
    parsed_url: GitHubUrl,
    include_comments: bool,
    include_linked: bool,
) -> dict[str, Any]:
    path_kind = parsed_url.path_parts[0]
    number = int(parsed_url.path_parts[1])
    errors: list[str] = []

    for resource_kind in _numbered_resource_attempt_order(path_kind):
        try:
            if resource_kind == "discussion":
                return _fetch_discussion_payload(
                    client=client,
                    repo=repo,
                    owner=parsed_url.owner,
                    repo_name=parsed_url.repo,
                    number=number,
                    include_comments=include_comments,
                    include_linked=include_linked,
                )
            return _fetch_issue_or_pull_payload(
                client=client,
                repo=repo,
                owner=parsed_url.owner,
                repo_name=parsed_url.repo,
                number=number,
                include_comments=include_comments,
                include_linked=include_linked,
            )
        except (AttributeError, UnknownObjectException, GithubException) as exc:
            errors.append(f"{resource_kind}: {exc}")
            continue

    attempted = ", ".join(errors) if errors else "no supported API attempts"
    raise GitHubFetchError(
        f"failed to fetch {parsed_url.original_url} as issue, pull request, "
        f"or discussion ({attempted})"
    )


def _numbered_resource_attempt_order(path_kind: str) -> tuple[str, ...]:
    if path_kind == "discussions":
        return ("discussion", "issue_or_pull")
    return ("issue_or_pull", "discussion")


def _fetch_issue_or_pull_payload(
    client: Github,
    repo: Any,
    owner: str,
    repo_name: str,
    number: int,
    include_comments: bool,
    include_linked: bool,
) -> dict[str, Any]:
    issue = repo.get_issue(number)
    is_pull_request = issue.pull_request is not None

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
            source_kind="pull" if is_pull_request else "issues",
        )

    payload: dict[str, Any] = {
        "kind": "pull_request" if is_pull_request else "issue",
        "number": number,
        "url": issue.html_url
        or f"https://github.com/{owner}/{repo_name}/{'pull' if is_pull_request else 'issues'}/{number}",
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
    if is_pull_request:
        payload["merged"] = _pull_request_merged(repo, owner, repo_name, number)
    return payload


def _fetch_discussion_payload(
    client: Github,
    repo: Any,
    owner: str,
    repo_name: str,
    number: int,
    include_comments: bool,
    include_linked: bool,
) -> dict[str, Any]:
    discussion = repo.get_discussion(number, DISCUSSION_SCHEMA)
    payload = _format_discussion_payload(
        discussion,
        number=number,
        include_comments=include_comments,
    )
    if include_linked:
        linked_issues, linked_prs, linked_discussions = _get_linked_items(
            client=client,
            issue_body=payload.get("body") or "",
            comment_bodies=[c.get("body") or "" for c in payload.get("comments", [])],
            source_owner=owner,
            source_repo=repo_name,
            source_number=number,
            source_kind="discussions",
        )
        payload["linked_issues"] = linked_issues
        payload["linked_prs"] = linked_prs
        payload["linked_discussions"] = linked_discussions
    return payload


def _parse_issue_url(issue_url: str) -> tuple[str, str, int]:
    owner, repo, _kind, number = _parse_github_url(issue_url)
    return owner, repo, number


def _parse_github_url(issue_url: str) -> tuple[str, str, str, int]:
    match = NUMBERED_RESOURCE_URL_RE.match(issue_url.strip())
    if not match:
        raise ValueError(
            "url must look like "
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


def _parse_any_github_url(github_url: str) -> GitHubUrl:
    parsed = urlparse(github_url.strip())
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != "github.com":
        raise ValueError("url must be a github.com URL")

    parts = tuple(unquote(part) for part in parsed.path.strip("/").split("/") if part)
    if len(parts) < 2:
        raise ValueError("url must include a GitHub owner and repository")

    owner, repo, *path_parts = parts
    if not REPO_SEGMENT_RE.match(owner) or not REPO_SEGMENT_RE.match(repo):
        raise ValueError("url must include a valid GitHub owner and repository")

    return GitHubUrl(
        original_url=github_url.strip(),
        owner=owner,
        repo=repo,
        path_parts=tuple(path_parts),
        fragment=parsed.fragment,
    )


def _client() -> Github:
    token = os.getenv("GITHUB_TOKEN")
    auth = Auth.Token(token) if token else None
    return Github(auth=auth, user_agent=USER_AGENT)


def _fetch_commit_payload(
    client: Github,
    repo: Any,
    parsed_url: GitHubUrl,
    include_comments: bool,
    include_linked: bool,
) -> dict[str, Any]:
    if len(parsed_url.path_parts) < 2:
        raise ValueError("commit URL must include a commit SHA")

    sha = parsed_url.path_parts[1]
    commit = repo.get_commit(sha)
    message = _commit_message(commit)
    comments: list[dict[str, str | None]] = []
    if include_comments and hasattr(commit, "get_comments"):
        comments = [_format_comment(c) for c in commit.get_comments()]

    linked_issues: list[dict[str, Any]] = []
    linked_prs: list[dict[str, Any]] = []
    linked_discussions: list[dict[str, Any]] = []
    if include_linked:
        linked_issues, linked_prs, linked_discussions = _get_linked_items(
            client=client,
            issue_body=message,
            comment_bodies=[c.get("body") or "" for c in comments],
            source_owner=parsed_url.owner,
            source_repo=parsed_url.repo,
            source_number=-1,
            source_kind="",
        )

    return {
        "kind": "commit",
        "sha": getattr(commit, "sha", sha),
        "url": getattr(commit, "html_url", "") or parsed_url.original_url,
        "title": message.splitlines()[0] if message else "",
        "message": message,
        "author": _commit_actor_login(commit, "author"),
        "committer": _commit_actor_login(commit, "committer"),
        "created_at": _format_datetime(_commit_date(commit, "author")),
        "comments": comments,
        "files": [_format_commit_file(f) for f in getattr(commit, "files", [])],
        "linked_issues": linked_issues,
        "linked_prs": linked_prs,
        "linked_discussions": linked_discussions,
    }


def _fetch_content_payload(repo: Any, parsed_url: GitHubUrl) -> dict[str, Any]:
    path_kind = parsed_url.path_parts[0]
    allow_empty_path = path_kind == "tree"
    rest = parsed_url.path_parts[1:]
    if not rest:
        raise ValueError(f"{path_kind} URL must include a ref")

    content, ref, path = _resolve_content_from_url_parts(
        repo=repo,
        parts=rest,
        allow_empty_path=allow_empty_path,
    )
    if isinstance(content, list):
        return {
            "kind": "directory",
            "url": parsed_url.original_url,
            "ref": ref,
            "path": path,
            "entries": [_format_content_entry(item) for item in content],
        }

    payload = _format_file_content_payload(
        content=content,
        parsed_url=parsed_url,
        ref=ref,
        path=path,
    )
    selection = _line_selection(parsed_url.fragment)
    if selection and payload.get("content"):
        start, end = selection
        payload["line_start"] = start
        payload["line_end"] = end
        payload["selected_content"] = _select_lines(
            str(payload["content"]),
            start,
            end,
        )
    return payload


def _resolve_content_from_url_parts(
    repo: Any,
    parts: tuple[str, ...],
    allow_empty_path: bool,
) -> tuple[Any, str, str]:
    errors: list[str] = []
    max_ref_parts = len(parts) if allow_empty_path else len(parts) - 1
    for split_at in range(1, max_ref_parts + 1):
        ref = "/".join(parts[:split_at])
        path = "/".join(parts[split_at:])
        if not path and not allow_empty_path:
            continue
        try:
            return repo.get_contents(path, ref=ref), ref, path
        except (UnknownObjectException, GithubException) as exc:
            errors.append(f"ref={ref!r} path={path!r}: {exc}")
            continue

    attempted = "; ".join(errors) if errors else "no possible ref/path split"
    raise GitHubFetchError(f"failed to fetch GitHub content ({attempted})")


def _format_file_content_payload(
    content: Any,
    parsed_url: GitHubUrl,
    ref: str,
    path: str,
) -> dict[str, Any]:
    text, truncated, binary = _decoded_content_text(content)
    return {
        "kind": "file",
        "url": getattr(content, "html_url", "") or parsed_url.original_url,
        "download_url": getattr(content, "download_url", "") or "",
        "ref": ref,
        "path": path or getattr(content, "path", ""),
        "name": getattr(content, "name", ""),
        "size": getattr(content, "size", None),
        "encoding": getattr(content, "encoding", ""),
        "binary": binary,
        "truncated": truncated,
        "content": text,
    }


def _decoded_content_text(content: Any) -> tuple[str, bool, bool]:
    data = getattr(content, "decoded_content", b"")
    if isinstance(data, str):
        text = data
    else:
        try:
            text = bytes(data).decode("utf-8")
        except (TypeError, UnicodeDecodeError):
            return "", False, True
    truncated = len(text) > MAX_TEXT_CONTENT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CONTENT_CHARS] + "\n[truncated]"
    return text, truncated, False


def _format_content_entry(item: Any) -> dict[str, Any]:
    return {
        "type": getattr(item, "type", ""),
        "path": getattr(item, "path", ""),
        "name": getattr(item, "name", ""),
        "size": getattr(item, "size", None),
        "url": getattr(item, "html_url", ""),
    }


def _format_commit_file(file_change: Any) -> dict[str, Any]:
    patch = getattr(file_change, "patch", "") or ""
    truncated = len(patch) > MAX_PATCH_CHARS
    if truncated:
        patch = patch[:MAX_PATCH_CHARS] + "\n[truncated]"
    return {
        "filename": getattr(file_change, "filename", ""),
        "status": getattr(file_change, "status", ""),
        "additions": getattr(file_change, "additions", 0),
        "deletions": getattr(file_change, "deletions", 0),
        "changes": getattr(file_change, "changes", 0),
        "patch": patch,
        "patch_truncated": truncated,
    }


def _format_repository_payload(repo: Any) -> dict[str, Any]:
    return {
        "kind": "repository",
        "url": getattr(repo, "html_url", ""),
        "full_name": getattr(repo, "full_name", ""),
        "description": getattr(repo, "description", "") or "",
        "default_branch": getattr(repo, "default_branch", "") or "",
        "open_issues_count": getattr(repo, "open_issues_count", None),
        "comments": [],
        "linked_issues": [],
        "linked_prs": [],
        "linked_discussions": [],
    }


def _line_selection(fragment: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"L(?P<start>\d+)(?:-L(?P<end>\d+))?", fragment or "")
    if not match:
        return None
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    return (start, max(start, end))


def _select_lines(text: str, start: int, end: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[start - 1 : end])


def _commit_message(commit: Any) -> str:
    nested = getattr(commit, "commit", None)
    return getattr(nested, "message", "") or getattr(commit, "message", "") or ""


def _commit_actor_login(commit: Any, field: str) -> str | None:
    actor = getattr(commit, field, None)
    if actor is not None and getattr(actor, "login", None):
        return actor.login
    nested = getattr(commit, "commit", None)
    nested_actor = getattr(nested, field, None)
    return getattr(nested_actor, "name", None)


def _commit_date(commit: Any, field: str) -> Any:
    nested = getattr(commit, "commit", None)
    nested_actor = getattr(nested, field, None)
    return getattr(nested_actor, "date", None)


def _pull_request_merged(repo: Any, owner: str, repo_name: str, number: int) -> bool:
    try:
        return bool(repo.get_pull(number).merged)
    except (AttributeError, KeyError, GithubException) as exc:
        logger.warning(
            "github_fetcher failed to check PR merge status",
            owner=owner,
            repo=repo_name,
            number=number,
            error=str(exc),
        )
        return False


def _get_linked_items(
    client: Github,
    issue_body: str,
    comment_bodies: list[str],
    source_owner: str,
    source_repo: str,
    source_number: int,
    source_kind: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    texts = [issue_body, *comment_bodies]
    references = _extract_references(texts, source_owner, source_repo)
    for kind in _self_reference_kinds(source_kind):
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
                try:
                    discussion = repo.get_discussion(number, DISCUSSION_SCHEMA)
                    linked_discussions.append(_format_discussion_summary(discussion))
                    continue
                except (AttributeError, UnknownObjectException, GithubException):
                    issue = repo.get_issue(number)
            else:
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
            linked_prs.append(
                {
                    "url": issue.html_url
                    or f"https://github.com/{owner}/{repo_name}/pull/{number}",
                    "title": issue.title or "",
                    "state": issue.state or "unknown",
                    "merged": _pull_request_merged(repo, owner, repo_name, number),
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


def _self_reference_kinds(source_kind: str) -> tuple[str, ...]:
    if source_kind in ("issues", "pull"):
        return ("unknown", "issues", "pull")
    if source_kind == "discussions":
        return ("discussions",)
    return ()


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
    number: int | None = None,
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
        "number": number if number is not None else getattr(discussion, "number", None),
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


def _url_from_tool_args(args: dict[str, Any]) -> str:
    for key in (
        "url",
        "github_url",
        "issue_url",
        "pull_url",
        "pr_url",
        "discussion_url",
        "content_url",
        "html_url",
        "content",
    ):
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        match = GITHUB_URL_IN_TEXT_RE.search(text)
        return _clean_url(match.group(0) if match else text)
    return ""


def _clean_url(url: str) -> str:
    return url.strip().rstrip(".,;:")


def _bool_tool_arg(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("false", "0", "no", "off"):
            return False
        if normalized in ("true", "1", "yes", "on"):
            return True
    return bool(value)


class GitHubFetcherTool(BaseTool):
    """Fetch a GitHub URL with inferred resource type and linked refs."""

    @property
    def tool_name(self) -> str:
        return "github_fetcher"

    @property
    def description(self) -> str:
        return (
            "Fetch any github.com URL, infer whether it is an issue, pull "
            "request, discussion, commit, file, directory, or repository, and "
            "retry issue/PR/discussion API shapes when the URL type is wrong."
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "Any github.com URL. Do not pass a separate issue, PR, "
                        "discussion, code, or commit type; the tool infers it."
                    ),
                },
                "include_comments": {
                    "type": "boolean",
                    "description": "Include comments when the resource supports them.",
                },
                "include_linked": {
                    "type": "boolean",
                    "description": (
                        "Resolve referenced issues, pull requests, and discussions."
                    ),
                },
            },
            "required": ["url"],
        }

    def prompt_contribution(self) -> str | None:
        return (
            "For GitHub resources, call `github_fetcher` with only `url` plus "
            "optional `include_comments`/`include_linked`; never specify a "
            "separate issue/PR/discussion/code type."
        )

    async def _execute(self, args: dict[str, Any], **kwargs: Any) -> ToolResult:
        logger.debug("github_fetcher invoked", tool_args=args, kwargs_keys=list(kwargs))
        github_url = _url_from_tool_args(args)
        if not github_url:
            logger.warning(
                "github_fetcher rejected: missing url",
                arg_keys=list(args.keys()),
            )
            return ToolResult(
                error=(
                    "No url provided. Usage: "
                    "github_fetcher(url='https://github.com/<owner>/<repo>/issues/<n>') "
                    "or any other github.com resource URL."
                )
            )
        include_comments = _bool_tool_arg(args.get("include_comments"), True)
        include_linked = _bool_tool_arg(args.get("include_linked"), True)

        token_present = bool(os.getenv("GITHUB_TOKEN"))
        logger.debug(
            "github_fetcher calling PyGithub",
            github_url=github_url,
            include_comments=include_comments,
            include_linked=include_linked,
            github_token_present=token_present,
        )
        try:
            payload = github_fetcher(
                url=github_url,
                include_comments=include_comments,
                include_linked=include_linked,
            )
        except (
            ValueError,
            GitHubFetchError,
            GithubException,
            UnknownObjectException,
        ) as exc:
            logger.warning(
                "github_fetcher failed",
                exc_type=type(exc).__name__,
                error=str(exc),
                github_url=github_url,
            )
            return ToolResult(error=f"github_fetcher failed: {exc}")
        except Exception as exc:
            logger.exception(
                "github_fetcher crashed unexpectedly",
                exc_type=type(exc).__name__,
                github_url=github_url,
            )
            return ToolResult(error=f"github_fetcher crashed: {type(exc).__name__}: {exc}")

        logger.debug(
            "github_fetcher succeeded",
            github_url=github_url,
            kind=payload.get("kind"),
            comments=len(payload.get("comments", [])),
            linked_issues=len(payload.get("linked_issues", [])),
            linked_prs=len(payload.get("linked_prs", [])),
        )
        return ToolResult(
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            exit_code=0,
        )
