#!/usr/bin/env python
"""Run a real, read-only Open Access lookup and render check.

This command deliberately does not open the state database, write a report,
send a notification, or contact Zotero.  It is used by the manual OA smoke
workflow to verify the production network path without affecting daily runs.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jalert.fetch import Item  # noqa: E402
from jalert.open_access import _apply_result, lookup_doi  # noqa: E402
from jalert.report import build_markdown  # noqa: E402
from jalert.score import Match, Scored  # noqa: E402


DEFAULT_DOI = "10.1371/journal.pone.0000308"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a legal Open Access lookup without changing journal-alert state."
    )
    parser.add_argument(
        "--doi",
        default=DEFAULT_DOI,
        help="DOI to query (defaults to a known Open Access article)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    email = os.environ.get("JALERT_MAILTO", "").strip()
    if not email:
        print("OA smoke test cannot run: JALERT_MAILTO repository variable is empty.")
        return 2

    result = lookup_doi(args.doi, email, timeout=30)
    target = result.get("pdf_url") or result.get("oa_url")

    if not result.get("checked"):
        print(
            "OA lookup failed: "
            f"status={result.get('status', '')} "
            f"error={result.get('error', '')[:300]}"
        )
        return 1

    if not result.get("is_oa") or not target:
        print(
            "OA lookup completed but did not return an Open Access location: "
            f"status={result.get('status', '')}"
        )
        return 1

    item = Item(
        uid=f"doi:{args.doi.lower()}",
        title="Open Access smoke-test article",
        doi=args.doi,
        journal="OA smoke test",
        date=str(date.today()),
        abstract="A synthetic entry used only to verify report rendering.",
        source="unpaywall",
    )
    _apply_result(item, result)
    scored = Scored(
        score=15,
        matches=[Match("OA smoke", "open access", "title", 15)],
    )
    cfg = {
        "project": {"name": "journal-alert OA smoke"},
        "window": {"days": 1},
        "output": {"frontmatter": False, "source_status": "none"},
        "keywords": [{"label": "OA smoke"}],
        "tiers": {"must_read": 15, "worth_reading": 8, "min_score": 5},
    }
    rendered = build_markdown(
        day=str(date.today()),
        cfg=cfg,
        entries=[{"item": item, "scored": scored}],
        statuses=[],
        fetched_total=1,
        matched_total=1,
        new_count=1,
        seen_before=0,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    if target not in rendered or "**开放获取**" not in rendered:
        print("OA lookup succeeded, but the report did not render the OA link.")
        return 1

    print(
        "OA smoke test passed: "
        f"status={result.get('status', '')} "
        f"direct_pdf={'yes' if result.get('pdf_url') else 'no'} "
        f"version={result.get('version', '') or 'unknown'} "
        f"license={result.get('license', '') or 'unknown'}"
    )
    print(f"Open Access target: {target}")
    print("Report rendering: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
