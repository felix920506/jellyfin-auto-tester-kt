import unittest
from unittest.mock import patch

from creatures.analysis.tools import github_fetcher as fetcher


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
        def get_issue(owner, repo, number):
            issues = {
                10: {
                    "title": "Playback regression",
                    "body": "Repro steps. Fixes #11 and see #12.",
                    "labels": [{"name": "bug"}, {"name": "playback"}],
                    "state": "open",
                    "created_at": "2026-01-01T00:00:00Z",
                    "user": {"login": "reporter"},
                },
                11: {
                    "html_url": "https://github.com/jellyfin/jellyfin/issues/11",
                    "title": "Related issue",
                    "state": "closed",
                },
                12: {
                    "html_url": "https://github.com/jellyfin/jellyfin/pull/12",
                    "title": "Fix playback regression",
                    "state": "closed",
                    "pull_request": {},
                },
            }
            return issues[number]

        comments = [
            {
                "body": "Also mentioned in #11.",
                "created_at": "2026-01-02T00:00:00Z",
                "user": {"login": "maintainer"},
            }
        ]

        with (
            patch.object(fetcher, "_get_issue", side_effect=get_issue),
            patch.object(fetcher, "_get_paginated", return_value=comments),
            patch.object(fetcher, "_get_json", return_value={"merged": True}),
        ):
            result = fetcher.github_fetcher(
                "https://github.com/jellyfin/jellyfin/issues/10"
            )

        self.assertEqual(result["title"], "Playback regression")
        self.assertEqual(result["labels"], ["bug", "playback"])
        self.assertEqual(result["author"], "reporter")
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

    def test_next_link_extracts_rel_next(self):
        link_header = (
            '<https://api.github.com/repos/o/r/issues/1/comments?page=2>; '
            'rel="next", '
            '<https://api.github.com/repos/o/r/issues/1/comments?page=3>; '
            'rel="last"'
        )

        self.assertEqual(
            fetcher._next_link(link_header),
            "https://api.github.com/repos/o/r/issues/1/comments?page=2",
        )


if __name__ == "__main__":
    unittest.main()
