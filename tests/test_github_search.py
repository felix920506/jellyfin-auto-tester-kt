import asyncio
import json
import unittest
from unittest.mock import patch

from tools import github_search as search


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


if __name__ == "__main__":
    unittest.main()
