import asyncio
import json
import unittest
from unittest.mock import patch

from tools import github_search as search


class _FakeLabel:
    def __init__(self, name):
        self.name = name


class _FakeIssue:
    def __init__(self, *, title, url, is_pr=False):
        self.html_url = url
        self.title = title
        self.state = "open"
        self.labels = [_FakeLabel("bug")]
        self.created_at = None
        self.updated_at = None
        self.body = "body"
        self.pull_request = object() if is_pr else None


class _FakeCodeItem:
    def __init__(self, *, path, url):
        self.path = path
        self.html_url = url
        self.repository = type("Repo", (), {"full_name": "jellyfin/jellyfin"})()


class _FakePaginated:
    def __init__(self, items, total_count=None):
        self._items = items
        self.totalCount = len(items) if total_count is None else total_count

    def __iter__(self):
        return iter(self._items)


class _FakeClient:
    def __init__(self):
        self.issue_queries = []
        self.code_queries = []

    def search_issues(self, query):
        self.issue_queries.append(query)
        if query.endswith("is:issue"):
            return _FakePaginated(
                [
                    _FakeIssue(
                        title="Issue result",
                        url="https://github.com/jellyfin/jellyfin/issues/1",
                    )
                ],
                total_count=3,
            )
        if query.endswith("is:pull-request"):
            return _FakePaginated(
                [
                    _FakeIssue(
                        title="Pull request result",
                        url="https://github.com/jellyfin/jellyfin/pull/2",
                        is_pr=True,
                    )
                ],
                total_count=2,
            )
        return _FakePaginated([])

    def search_code(self, query):
        self.code_queries.append(query)
        return _FakePaginated(
            [
                _FakeCodeItem(
                    path="MediaBrowser.Model/Dlna/LG Smart TV.xml",
                    url="https://github.com/jellyfin/jellyfin/blob/master/profile.xml",
                )
            ]
        )


class GitHubSearchTests(unittest.TestCase):
    def test_tool_accepts_content_alias_from_block_body(self):
        payload = {
            "total_count": 1,
            "items": [
                {
                    "path": "Emby.Dlna/Didl/Filter.cs",
                    "repo": "jellyfin/jellyfin",
                    "url": "https://github.com/jellyfin/jellyfin/blob/master/file.cs",
                }
            ],
        }

        with patch.object(search, "github_search", return_value=payload) as github_search:
            result = asyncio.run(
                search.GitHubSearchTool()._execute(
                    {
                        "kind": "code",
                        "content": '  "LG Smart TV" repo:jellyfin/jellyfin  ',
                    }
                )
            )

        self.assertIsNone(result.error)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(json.loads(result.output), payload)
        github_search.assert_called_once_with(
            query='"LG Smart TV" repo:jellyfin/jellyfin',
            kind="code",
            max_results=10,
        )

    def test_tool_infers_code_search_from_filename_qualifier(self):
        payload = {"total_count": 0, "items": []}

        with patch.object(search, "github_search", return_value=payload) as github_search:
            result = asyncio.run(
                search.GitHubSearchTool()._execute(
                    {"query": 'filename:"LG Smart TV.xml" repo:jellyfin/jellyfin'}
                )
            )

        self.assertIsNone(result.error)
        github_search.assert_called_once_with(
            query='filename:"LG Smart TV.xml" repo:jellyfin/jellyfin',
            kind="code",
            max_results=10,
        )

    def test_tool_routes_conflicting_issue_kind_with_filename_qualifier_to_code(self):
        payload = {"total_count": 0, "items": []}

        with patch.object(search, "github_search", return_value=payload) as github_search:
            result = asyncio.run(
                search.GitHubSearchTool()._execute(
                    {
                        "kind": "issues",
                        "query": 'filename:"LG Smart TV.xml" repo:jellyfin/jellyfin',
                    }
                )
            )

        self.assertIsNone(result.error)
        github_search.assert_called_once_with(
            query='filename:"LG Smart TV.xml" repo:jellyfin/jellyfin',
            kind="code",
            max_results=10,
        )

    def test_tool_prefers_query_over_content_alias(self):
        payload = {"total_count": 0, "items": []}

        with patch.object(search, "github_search", return_value=payload) as github_search:
            result = asyncio.run(
                search.GitHubSearchTool()._execute(
                    {
                        "query": "repo:jellyfin/jellyfin is:issue playback",
                        "content": "repo:jellyfin/jellyfin wrong",
                    }
                )
            )

        self.assertIsNone(result.error)
        github_search.assert_called_once_with(
            query="repo:jellyfin/jellyfin is:issue playback",
            kind="issues",
            max_results=10,
        )

    def test_tool_schema_declares_query_parameter(self):
        schema = search.GitHubSearchTool().get_parameters_schema()

        self.assertIn("query", schema["properties"])
        self.assertEqual(schema["required"], ["query"])
        self.assertNotIn("content", schema["properties"])
        self.assertEqual(
            schema["properties"]["kind"]["enum"],
            ["auto", "issues", "code"],
        )

    def test_auto_kind_routes_filename_query_to_code_search(self):
        client = _FakeClient()

        with patch.object(search, "_client", return_value=client):
            result = search.github_search(
                'filename:"LG Smart TV.xml" repo:jellyfin/jellyfin'
            )

        self.assertEqual(
            result["query"],
            'filename:"LG Smart TV.xml" repo:jellyfin/jellyfin',
        )
        self.assertEqual(result["requested_kind"], "auto")
        self.assertEqual(result["kind"], "code")
        self.assertEqual(result["effective_queries"], [result["query"]])
        self.assertEqual(
            client.code_queries,
            ['filename:"LG Smart TV.xml" repo:jellyfin/jellyfin'],
        )
        self.assertEqual(client.issue_queries, [])
        self.assertEqual(
            result["items"][0]["path"],
            "MediaBrowser.Model/Dlna/LG Smart TV.xml",
        )

    def test_issue_search_without_kind_qualifier_runs_valid_issue_and_pr_queries(self):
        client = _FakeClient()

        with patch.object(search, "_client", return_value=client):
            result = search.github_search("repo:jellyfin/jellyfin playback")

        self.assertEqual(result["query"], "repo:jellyfin/jellyfin playback")
        self.assertEqual(result["requested_kind"], "auto")
        self.assertEqual(result["kind"], "issues")
        self.assertEqual(
            result["effective_queries"],
            [
                "repo:jellyfin/jellyfin playback is:issue",
                "repo:jellyfin/jellyfin playback is:pull-request",
            ],
        )
        self.assertEqual(
            client.issue_queries,
            [
                "repo:jellyfin/jellyfin playback is:issue",
                "repo:jellyfin/jellyfin playback is:pull-request",
            ],
        )
        self.assertEqual(client.code_queries, [])
        self.assertEqual(result["total_count"], 5)
        self.assertEqual(
            [item["kind"] for item in result["items"]],
            ["issue", "pull_request"],
        )

    def test_issue_and_pr_results_are_interleaved(self):
        issue1 = {"kind": "issue", "url": "issue-1"}
        issue2 = {"kind": "issue", "url": "issue-2"}
        pr1 = {"kind": "pull_request", "url": "pr-1"}

        self.assertEqual(
            search._interleave_results([[issue1, issue2], [pr1]], 3),
            [issue1, pr1, issue2],
        )


if __name__ == "__main__":
    unittest.main()
