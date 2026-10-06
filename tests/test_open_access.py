from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from jalert.fetch import Item
from jalert.open_access import _parse_result, annotate_entries
from jalert.report import build_markdown
from jalert.score import Match, Scored


class OpenAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.oa_result = {
            "checked": True,
            "is_oa": True,
            "status": "gold",
            "pdf_url": "https://example.org/article.pdf",
            "oa_url": "https://example.org/article",
            "version": "publishedVersion",
            "host_type": "publisher",
            "license": "cc-by",
            "source": "Unpaywall",
            "error": "",
        }

    def test_parse_result_uses_alternate_pdf_location(self) -> None:
        result = _parse_result(
            {
                "is_oa": True,
                "oa_status": "hybrid",
                "best_oa_location": {
                    "url_for_landing_page": "https://publisher.example/article",
                    "version": "publishedVersion",
                },
                "oa_locations": [
                    {
                        "url_for_pdf": "https://repository.example/article.pdf",
                        "url_for_landing_page": "https://repository.example/article",
                    }
                ],
            }
        )

        self.assertTrue(result["checked"])
        self.assertTrue(result["is_oa"])
        self.assertEqual(result["pdf_url"], "https://repository.example/article.pdf")
        self.assertEqual(result["oa_url"], "https://publisher.example/article")

    @patch("jalert.open_access.lookup_doi")
    def test_annotation_is_cached_and_applied(self, lookup_doi) -> None:
        lookup_doi.return_value = self.oa_result
        first = Item(uid="one", title="One", doi="10.1234/example")
        second = Item(uid="two", title="Two", doi="https://doi.org/10.1234/EXAMPLE")

        with patch.dict("os.environ", {"JALERT_MAILTO": "test@example.com"}):
            result = annotate_entries(
                entries=[
                    {"item": first, "scored": Scored(score=15)},
                    {"item": second, "scored": Scored(score=15)},
                ],
                cfg={"open_access": {"enabled": True}},
                log=logging.getLogger("oa-test"),
            )

        lookup_doi.assert_called_once_with("10.1234/example", "test@example.com")
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["oa"], 2)
        self.assertEqual(result["pdf"], 2)
        self.assertEqual(first.oa_pdf_url, self.oa_result["pdf_url"])
        self.assertEqual(second.oa_license, "cc-by")

    def test_report_prefers_and_labels_open_access_link(self) -> None:
        item = Item(
            uid="doi:10.1234/example",
            title="Open article",
            url="https://publisher.example/paywalled",
            doi="10.1234/example",
            journal="Test Journal",
            date="2026-10-06",
            abstract="Open access test.",
            oa_checked=True,
            oa_is_oa=True,
            oa_status="gold",
            oa_pdf_url=self.oa_result["pdf_url"],
            oa_url=self.oa_result["oa_url"],
            oa_version="publishedVersion",
            oa_license="cc-by",
        )
        scored = Scored(
            score=15,
            matches=[Match("OA", "open access", "title", 15)],
        )
        rendered = build_markdown(
            day="2026-10-06",
            cfg={
                "project": {"name": "test"},
                "window": {"days": 1},
                "output": {"frontmatter": False, "source_status": "none"},
                "keywords": [{"label": "OA"}],
                "tiers": {"must_read": 15, "worth_reading": 8, "min_score": 5},
            },
            entries=[{"item": item, "scored": scored}],
            statuses=[],
            fetched_total=1,
            matched_total=1,
            new_count=1,
            generated_at="2026-10-06 12:00",
        )

        self.assertIn(f"[Open article]({self.oa_result['pdf_url']})", rendered)
        self.assertIn("**开放获取**", rendered)
        self.assertIn("开放全文 PDF", rendered)
        self.assertIn("publishedVersion / cc-by", rendered)


if __name__ == "__main__":
    unittest.main()
