import asyncio
import datetime as dt
import logging
import unittest
from unittest.mock import patch

from tools import github_fetcher as fetcher


class _FakeUser:
    def __init__(self, login):
        self.login = login


class _FakeLabel:
    def __init__(self, name):
        self.name = name


class _FakeComment:
    def __init__(self, body, created_at, login):
        self.body = body
        self.created_at = created_at
        self.user = _FakeUser(login)


class _FakeIssue:
    def __init__(
        self,
        *,
        title="",
        body="",
        labels=(),
        state="open",
        created_at=None,
        login=None,
        html_url="",
        comments=(),
        is_pr=False,
    ):
        self.title = title
        self.body = body
        self.labels = [_FakeLabel(n) for n in labels]
        self.state = state
        self.created_at = created_at
        self.user = _FakeUser(login) if login else None
        self.html_url = html_url
        self._comments = list(comments)
        self.pull_request = object() if is_pr else None

    def get_comments(self):
        return list(self._comments)


class _FakePull:
    def __init__(self, merged):
        self.merged = merged


class _FakeRepo:
    def __init__(self, issues, pulls=None):
        self._issues = issues
        self._pulls = pulls or {}

    def get_issue(self, number):
        return self._issues[number]

    def get_pull(self, number):
        return self._pulls[number]


class _FakeClient:
    def __init__(self, repos):
        self._repos = repos

    def get_repo(self, full_name):
        return self._repos[full_name]


class GitHubFetcherTests(unittest.TestCase):
    def test_parse_issue_url(self):
        self.assertEqual(
            fetcher._parse_issue_url("https://github.com/jellyfin/jellyfin/issues/123"),
            ("jellyfin", "jellyfin", 123),
        )
        self.assertEqual(
            fetcher._parse_issue_url("https://github.com/jellyfin/jellyfin/pull/456"),
            ("jellyfin", "jellyfin", 456),
        )

    def test_parse_issue_url_rejects_non_github_url(self):
        with self.assertRaises(ValueError):
            fetcher._parse_issue_url("https://example.com/jellyfin/jellyfin/issues/123")

    def test_extract_references_finds_local_cross_repo_and_direct_links(self):
        references = fetcher._extract_references(
            [
                "Fixes #1, relates to jellyfin/jellyfin-web#2, "
                "and https://github.com/jellyfin/jellyfin/pull/3."
            ],
            "jellyfin",
            "jellyfin",
        )

        self.assertEqual(
            references,
            {
                ("jellyfin", "jellyfin", 1),
                ("jellyfin", "jellyfin-web", 2),
                ("jellyfin", "jellyfin", 3),
            },
        )

    def test_github_fetcher_normalizes_issue_comments_and_linked_items(self):
        created = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        comment_created = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)

        target = _FakeIssue(
            title="Playback regression",
            body="Repro steps. Fixes #11 and see #12.",
            labels=("bug", "playback"),
            state="open",
            created_at=created,
            login="reporter",
            html_url="https://github.com/jellyfin/jellyfin/issues/10",
            comments=[_FakeComment("Also mentioned in #11.", comment_created, "maintainer")],
        )
        linked_issue = _FakeIssue(
            title="Related issue",
            state="closed",
            html_url="https://github.com/jellyfin/jellyfin/issues/11",
        )
        linked_pr_issue = _FakeIssue(
            title="Fix playback regression",
            state="closed",
            html_url="https://github.com/jellyfin/jellyfin/pull/12",
            is_pr=True,
        )

        repo = _FakeRepo(
            issues={10: target, 11: linked_issue, 12: linked_pr_issue},
            pulls={12: _FakePull(merged=True)},
        )
        client = _FakeClient({"jellyfin/jellyfin": repo})

        with patch.object(fetcher, "_client", return_value=client):
            result = fetcher.github_fetcher(
                "https://github.com/jellyfin/jellyfin/issues/10"
            )

        self.assertEqual(result["title"], "Playback regression")
        self.assertEqual(result["labels"], ["bug", "playback"])
        self.assertEqual(result["author"], "reporter")
        self.assertEqual(result["created_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(
            result["comments"],
            [
                {
                    "author": "maintainer",
                    "body": "Also mentioned in #11.",
                    "created_at": "2026-01-02T00:00:00Z",
                }
            ],
        )
        self.assertEqual(
            result["linked_issues"],
            [
                {
                    "url": "https://github.com/jellyfin/jellyfin/issues/11",
                    "title": "Related issue",
                    "state": "closed",
                }
            ],
        )
        self.assertEqual(
            result["linked_prs"],
            [
                {
                    "url": "https://github.com/jellyfin/jellyfin/pull/12",
                    "title": "Fix playback regression",
                    "state": "closed",
                    "merged": True,
                }
            ],
        )

    def test_tool_debug_logging_does_not_conflict_with_log_record_args(self):
        original_level = fetcher.logger.level
        fetcher.logger.setLevel(logging.DEBUG)
        try:
            result = asyncio.run(fetcher.GitHubFetcherTool()._execute({}))
        finally:
            fetcher.logger.setLevel(original_level)

        self.assertIn("No issue_url provided", result.error)

    def test_tool_accepts_url_alias(self):
        payload = {"comments": [], "linked_issues": [], "linked_prs": []}

        with patch.object(fetcher, "github_fetcher", return_value=payload) as github_fetcher:
            result = asyncio.run(
                fetcher.GitHubFetcherTool()._execute(
                    {"url": "https://github.com/jellyfin/jellyfin-web/pull/7328"}
                )
            )

        self.assertIsNone(result.error)
        self.assertEqual(result.exit_code, 0)
        github_fetcher.assert_called_once_with(
            issue_url="https://github.com/jellyfin/jellyfin-web/pull/7328",
            include_comments=True,
            include_linked=True,
        )


if __name__ == "__main__":
    unittest.main()
