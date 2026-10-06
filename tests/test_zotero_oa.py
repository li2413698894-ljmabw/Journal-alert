from __future__ import annotations

import unittest
from unittest.mock import patch

from jalert.zotero_oa import (
    OA_TITLE_PREFIX,
    _already_linked,
    _attachment_payload,
    backfill,
)


class ZoteroOATests(unittest.TestCase):
    def setUp(self) -> None:
        self.oa = {
            "checked": True,
            "is_oa": True,
            "status": "gold",
            "pdf_url": "https://example.org/article.pdf",
            "oa_url": "https://example.org/article",
            "version": "publishedVersion",
            "license": "cc-by",
        }

    def test_attachment_prefers_legal_pdf(self) -> None:
        payload = _attachment_payload("PARENT1", self.oa)

        self.assertEqual(payload["parentItem"], "PARENT1")
        self.assertEqual(payload["linkMode"], "linked_url")
        self.assertEqual(payload["url"], self.oa["pdf_url"])
        self.assertEqual(payload["contentType"], "application/pdf")
        self.assertTrue(payload["title"].startswith(OA_TITLE_PREFIX))
        self.assertIn("No paywall bypass", payload["note"])

    def test_attachment_falls_back_to_legal_full_text_page(self) -> None:
        oa = dict(self.oa, pdf_url="")
        payload = _attachment_payload("PARENT1", oa)

        self.assertEqual(payload["url"], self.oa["oa_url"])
        self.assertEqual(payload["contentType"], "text/html")
        self.assertIn("Open Access Full Text", payload["title"])

    def test_existing_attachment_is_deduplicated(self) -> None:
        children = [
            {
                "data": {
                    "itemType": "attachment",
                    "title": "A manually saved PDF",
                    "url": "https://EXAMPLE.org/article.pdf#page=1",
                }
            }
        ]
        self.assertTrue(
            _already_linked(children, "https://example.org/article.pdf")
        )

    @patch("jalert.zotero_oa._create_attachment")
    @patch("jalert.zotero_oa._children_for")
    @patch("jalert.zotero_oa.lookup_doi")
    @patch("jalert.zotero_oa._all_top_items")
    def test_backfill_caches_doi_and_skips_existing(
        self,
        all_top_items,
        lookup_doi,
        children_for,
        create_attachment,
    ) -> None:
        all_top_items.return_value = [
            {"key": "ONE", "data": {"DOI": "10.1234/example"}},
            {"key": "TWO", "data": {"DOI": "https://doi.org/10.1234/EXAMPLE"}},
        ]
        lookup_doi.return_value = self.oa
        children_for.side_effect = [
            [],
            [
                {
                    "data": {
                        "title": f"{OA_TITLE_PREFIX} Open Access PDF",
                        "url": self.oa["pdf_url"],
                    }
                }
            ],
        ]
        create_attachment.return_value = "CHILD1"

        result = backfill(
            user_id="123",
            api_key="secret-not-logged",
            email="test@example.com",
            limit=5,
        )

        lookup_doi.assert_called_once_with("10.1234/example", "test@example.com")
        create_attachment.assert_called_once()
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["existing"], 1)
        self.assertEqual(result["failed"], 0)


if __name__ == "__main__":
    unittest.main()
