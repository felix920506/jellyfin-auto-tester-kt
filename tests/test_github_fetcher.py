import asyncio
import datetime as dt
import logging
import unittest
from unittest.mock import patch

from github import GithubException

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


class _FakeCategory:
    def __init__(self, name):
        self.name = name


class _FakeDiscussionComment:
    def __init__(self, body, created_at, login):
        self.body = body
        self.body_text = body
        self.created_at = created_at
        self.author = _FakeUser(login)


class _FakeDiscussion:
    def __init__(
        self,
        *,
        number=1,
        title="",
        body="",
        category="General",
        created_at=None,
        updated_at=None,
        login=None,
        url="",
        comments=(),
    ):
        self.number = number
        self.title = title
        self.body = body
        self.body_text = body
        self.category = _FakeCategory(category)
        self.created_at = created_at
        self.updated_at = updated_at
        self.author = _FakeUser(login) if login else None
        self.url = url
        self._comments = list(comments)

    def get_comments(self, _schema):
        return list(self._comments)


class _FakePyGithubDiscussion:
    def __init__(
        self,
        *,
        number=1,
        title="",
        body="",
        category="General",
        created_at=None,
        updated_at=None,
        login=None,
        url="",
        comments=(),
    ):
        self.number = number
        self.title = title
        self.body = body
        self.body_text = body
        self.category = _FakeCategory(category)
        self.created_at = created_at
        self.updated_at = updated_at
        self.author = _FakeUser(login) if login else None
        self._comments = list(comments)
        self._rawData = {
            "url": url,
            "repository": {"nameWithOwner": "jellyfin/jellyfin"},
        }

    @property
    def url(self):
        raise GithubException(400, {"message": "Returned object contains no URL"}, None)

    def get_comments(self, _schema):
        return list(self._comments)


class _FakeContent:
    def __init__(
        self,
        *,
        path="",
        name="",
        decoded_content=b"",
        content_type="file",
        size=None,
        html_url="",
        download_url="",
        encoding="base64",
    ):
        self.path = path
        self.name = name
        self.decoded_content = decoded_content
        self.type = content_type
        self.size = size
        self.html_url = html_url
        self.download_url = download_url
        self.encoding = encoding


class _FakeCommitActor:
    def __init__(self, name="", date=None):
        self.name = name
        self.date = date


class _FakeCommitData:
    def __init__(self, message="", author=None, committer=None):
        self.message = message
        self.author = author
        self.committer = committer


class _FakeCommitFile:
    def __init__(
        self,
        *,
        filename="",
        status="modified",
        additions=0,
        deletions=0,
        changes=0,
        patch="",
    ):
        self.filename = filename
        self.status = status
        self.additions = additions
        self.deletions = deletions
        self.changes = changes
        self.patch = patch


class _FakeCommit:
    def __init__(
        self,
        *,
        sha="",
        message="",
        html_url="",
        author_login=None,
        committer_login=None,
        created_at=None,
        comments=(),
        files=(),
    ):
        self.sha = sha
        self.html_url = html_url
        self.author = _FakeUser(author_login) if author_login else None
        self.committer = _FakeUser(committer_login) if committer_login else None
        actor = _FakeCommitActor(author_login or "", created_at)
        committer = _FakeCommitActor(committer_login or "", created_at)
        self.commit = _FakeCommitData(message, author=actor, committer=committer)
        self._comments = list(comments)
        self.files = list(files)

    def get_comments(self):
        return list(self._comments)


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
    def __init__(
        self,
        issues,
        pulls=None,
        discussions=None,
        commits=None,
        contents=None,
    ):
        self._issues = issues
        self._pulls = pulls or {}
        self._discussions = discussions or {}
        self._commits = commits or {}
        self._contents = contents or {}
        self.html_url = "https://github.com/jellyfin/jellyfin"
        self.full_name = "jellyfin/jellyfin"
        self.description = ""
        self.default_branch = "main"
        self.open_issues_count = 0

    def get_issue(self, number):
        if number not in self._issues:
            raise GithubException(404, {"message": "Not Found"}, None)
        return self._issues[number]

    def get_pull(self, number):
        return self._pulls[number]

    def get_discussion(self, number, _schema):
        if number not in self._discussions:
            raise GithubException(404, {"message": "Not Found"}, None)
        return self._discussions[number]

    def get_commit(self, sha):
        if sha not in self._commits:
            raise GithubException(404, {"message": "Not Found"}, None)
        return self._commits[sha]

    def get_contents(self, path, ref=None):
        key = (ref or "", path)
        if key not in self._contents:
            raise GithubException(404, {"message": "Not Found"}, None)
        return self._contents[key]


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
        self.assertEqual(
            fetcher._parse_issue_url(
                "https://github.com/jellyfin/jellyfin/discussions/789"
            ),
            ("jellyfin", "jellyfin", 789),
        )

    def test_parse_github_url_includes_resource_kind(self):
        self.assertEqual(
            fetcher._parse_github_url(
                "https://github.com/jellyfin/jellyfin/discussions/789"
            ),
            ("jellyfin", "jellyfin", "discussions", 789),
        )

    def test_parse_issue_url_rejects_non_github_url(self):
        with self.assertRaises(ValueError):
            fetcher._parse_issue_url("https://example.com/jellyfin/jellyfin/issues/123")

    def test_extract_references_finds_local_cross_repo_and_direct_links(self):
        references = fetcher._extract_references(
            [
                "Fixes #1, relates to jellyfin/jellyfin-web#2, "
                "and https://github.com/jellyfin/jellyfin/pull/3. "
                "See https://github.com/jellyfin/jellyfin/discussions/4 too."
            ],
            "jellyfin",
            "jellyfin",
        )

        self.assertEqual(
            references,
            {
                ("jellyfin", "jellyfin", "unknown", 1),
                ("jellyfin", "jellyfin-web", "unknown", 2),
                ("jellyfin", "jellyfin", "pull", 3),
                ("jellyfin", "jellyfin", "discussions", 4),
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

        self.assertEqual(
            result["requested_url"],
            "https://github.com/jellyfin/jellyfin/issues/10",
        )
        self.assertNotIn("url", result)
        self.assertNotIn("number", result)
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
        self.assertNotIn("linked_discussions", result)

    def test_github_fetcher_fetches_discussion_url(self):
        created = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        updated = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
        comment_created = dt.datetime(2026, 1, 3, tzinfo=dt.timezone.utc)
        discussion = _FakeDiscussion(
            number=7328,
            title="LG Smart TV profile",
            body="Discussion body",
            category="Troubleshooting",
            created_at=created,
            updated_at=updated,
            login="reporter",
            url="https://github.com/jellyfin/jellyfin/discussions/7328",
            comments=[
                _FakeDiscussionComment("Discussion comment", comment_created, "helper")
            ],
        )
        repo = _FakeRepo(issues={}, discussions={7328: discussion})
        client = _FakeClient({"jellyfin/jellyfin": repo})

        with patch.object(fetcher, "_client", return_value=client):
            result = fetcher.github_fetcher(
                "https://github.com/jellyfin/jellyfin/discussions/7328"
            )

        self.assertEqual(
            result["requested_url"],
            "https://github.com/jellyfin/jellyfin/discussions/7328",
        )
        self.assertEqual(result["kind"], "discussion")
        self.assertEqual(result["title"], "LG Smart TV profile")
        self.assertEqual(result["category"], "Troubleshooting")
        self.assertEqual(result["author"], "reporter")
        self.assertEqual(result["created_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["updated_at"], "2026-01-02T00:00:00Z")
        self.assertEqual(
            result["comments"],
            [
                {
                    "author": "helper",
                    "body": "Discussion comment",
                    "created_at": "2026-01-03T00:00:00Z",
                }
            ],
        )

    def test_target_issue_url_falls_back_to_discussion(self):
        discussion = _FakeDiscussion(
            number=7328,
            title="LG Smart TV profile",
            category="Troubleshooting",
            url="https://github.com/jellyfin/jellyfin/discussions/7328",
        )
        repo = _FakeRepo(issues={}, discussions={7328: discussion})
        client = _FakeClient({"jellyfin/jellyfin": repo})

        with patch.object(fetcher, "_client", return_value=client):
            result = fetcher.github_fetcher(
                "https://github.com/jellyfin/jellyfin/issues/7328"
            )

        self.assertEqual(
            result["requested_url"],
            "https://github.com/jellyfin/jellyfin/issues/7328",
        )
        self.assertEqual(
            result["resolved_url"],
            "https://github.com/jellyfin/jellyfin/discussions/7328",
        )
        self.assertEqual(result["kind"], "discussion")
        self.assertEqual(result["title"], "LG Smart TV profile")

    def test_target_discussion_url_falls_back_to_issue(self):
        issue = _FakeIssue(
            title="Playback regression",
            html_url="https://github.com/jellyfin/jellyfin/issues/10",
        )
        repo = _FakeRepo(issues={10: issue}, discussions={})
        client = _FakeClient({"jellyfin/jellyfin": repo})

        with patch.object(fetcher, "_client", return_value=client):
            result = fetcher.github_fetcher(
                "https://github.com/jellyfin/jellyfin/discussions/10"
            )

        self.assertEqual(
            result["requested_url"],
            "https://github.com/jellyfin/jellyfin/discussions/10",
        )
        self.assertEqual(
            result["resolved_url"],
            "https://github.com/jellyfin/jellyfin/issues/10",
        )
        self.assertEqual(result["kind"], "issue")
        self.assertEqual(result["title"], "Playback regression")

    def test_github_fetcher_fetches_code_url(self):
        content = _FakeContent(
            path="README.md",
            name="README.md",
            decoded_content=b"first\nsecond\nthird\n",
            size=19,
            html_url="https://github.com/jellyfin/jellyfin/blob/main/README.md",
        )
        repo = _FakeRepo(issues={}, contents={("main", "README.md"): content})
        client = _FakeClient({"jellyfin/jellyfin": repo})

        with patch.object(fetcher, "_client", return_value=client):
            result = fetcher.github_fetcher(
                "https://github.com/jellyfin/jellyfin/blob/main/README.md#L2"
            )

        self.assertEqual(
            result["requested_url"],
            "https://github.com/jellyfin/jellyfin/blob/main/README.md#L2",
        )
        self.assertEqual(
            result["resolved_url"],
            "https://github.com/jellyfin/jellyfin/blob/main/README.md",
        )
        self.assertEqual(result["kind"], "file")
        self.assertEqual(result["path"], "README.md")
        self.assertNotIn("size", result)
        self.assertNotIn("encoding", result)
        self.assertEqual(result["content"], "first\nsecond\nthird\n")
        self.assertEqual(result["line_start"], 2)
        self.assertEqual(result["selected_content"], "second")

    def test_github_fetcher_fetches_commit_url(self):
        created = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        commit = _FakeCommit(
            sha="abc123",
            message="Fix playback regression\n\nRefs #10",
            html_url="https://github.com/jellyfin/jellyfin/commit/abc123",
            author_login="maintainer",
            committer_login="committer",
            created_at=created,
            comments=[_FakeComment("Ship it", created, "reviewer")],
            files=[
                _FakeCommitFile(
                    filename="src/playback.py",
                    additions=2,
                    deletions=1,
                    changes=3,
                    patch="@@ patch",
                )
            ],
        )
        repo = _FakeRepo(issues={}, commits={"abc123": commit})
        client = _FakeClient({"jellyfin/jellyfin": repo})

        with patch.object(fetcher, "_client", return_value=client):
            result = fetcher.github_fetcher(
                "https://github.com/jellyfin/jellyfin/commit/abc123",
                include_linked=False,
            )

        self.assertEqual(
            result["requested_url"],
            "https://github.com/jellyfin/jellyfin/commit/abc123",
        )
        self.assertNotIn("sha", result)
        self.assertEqual(result["kind"], "commit")
        self.assertEqual(result["title"], "Fix playback regression")
        self.assertEqual(result["author"], "maintainer")
        self.assertEqual(result["created_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["comments"][0]["body"], "Ship it")
        self.assertEqual(result["files"][0]["filename"], "src/playback.py")

    def test_linked_item_falls_back_to_discussion_after_issue_404(self):
        target = _FakeIssue(
            title="Playback regression",
            body="See #7328.",
            html_url="https://github.com/jellyfin/jellyfin/issues/10",
        )
        discussion = _FakeDiscussion(
            number=7328,
            title="LG Smart TV profile",
            category="Troubleshooting",
            url="https://github.com/jellyfin/jellyfin/discussions/7328",
        )
        repo = _FakeRepo(
            issues={10: target},
            discussions={7328: discussion},
        )
        client = _FakeClient({"jellyfin/jellyfin": repo})

        with patch.object(fetcher, "_client", return_value=client):
            result = fetcher.github_fetcher(
                "https://github.com/jellyfin/jellyfin/issues/10"
            )

        self.assertNotIn("linked_issues", result)
        self.assertNotIn("linked_prs", result)
        self.assertEqual(
            result["linked_discussions"],
            [
                {
                    "url": "https://github.com/jellyfin/jellyfin/discussions/7328",
                    "title": "LG Smart TV profile",
                    "category": "Troubleshooting",
                }
            ],
        )

    def test_linked_discussion_uses_raw_graphql_url(self):
        target = _FakeIssue(
            title="Playback regression",
            body="See #7328.",
            html_url="https://github.com/jellyfin/jellyfin/issues/10",
        )
        discussion = _FakePyGithubDiscussion(
            number=7328,
            title="LG Smart TV profile",
            category="Troubleshooting",
            url="https://github.com/jellyfin/jellyfin/discussions/7328",
        )
        repo = _FakeRepo(
            issues={10: target},
            discussions={7328: discussion},
        )
        client = _FakeClient({"jellyfin/jellyfin": repo})

        with patch.object(fetcher, "_client", return_value=client):
            result = fetcher.github_fetcher(
                "https://github.com/jellyfin/jellyfin/issues/10"
            )

        self.assertEqual(
            result["linked_discussions"],
            [
                {
                    "url": "https://github.com/jellyfin/jellyfin/discussions/7328",
                    "title": "LG Smart TV profile",
                    "category": "Troubleshooting",
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

        self.assertIn("No url provided", result.error)

    def test_tool_schema_declares_url_parameter_only(self):
        schema = fetcher.GitHubFetcherTool().get_parameters_schema()

        self.assertEqual(schema["required"], ["url"])
        self.assertIn("url", schema["properties"])
        self.assertNotIn("issue_url", schema["properties"])

    def test_tool_accepts_url_input(self):
        payload = {
            "comments": [],
            "linked_issues": [],
            "linked_prs": [],
            "linked_discussions": [],
        }

        with patch.object(fetcher, "github_fetcher", return_value=payload) as github_fetcher:
            result = asyncio.run(
                fetcher.GitHubFetcherTool()._execute(
                    {"url": "https://github.com/jellyfin/jellyfin-web/pull/7328"}
                )
            )

        self.assertIsNone(result.error)
        self.assertEqual(result.exit_code, 0)
        github_fetcher.assert_called_once_with(
            url="https://github.com/jellyfin/jellyfin-web/pull/7328",
            include_comments=True,
            include_linked=True,
        )

    def test_tool_extracts_url_from_content_body(self):
        payload = {
            "comments": [],
            "linked_issues": [],
            "linked_prs": [],
            "linked_discussions": [],
        }

        with patch.object(fetcher, "github_fetcher", return_value=payload) as github_fetcher:
            result = asyncio.run(
                fetcher.GitHubFetcherTool()._execute(
                    {"content": "fetch https://github.com/jellyfin/jellyfin/issues/10."}
                )
            )

        self.assertIsNone(result.error)
        github_fetcher.assert_called_once_with(
            url="https://github.com/jellyfin/jellyfin/issues/10",
            include_comments=True,
            include_linked=True,
        )


if __name__ == "__main__":
    unittest.main()
