"""Backfill legal Open Access links into existing Zotero items.

This module is deliberately separate from the daily and weekly pipelines.  It
reads existing top-level Zotero items, resolves their DOI through Unpaywall,
and creates a linked-URL child attachment for a legal OA PDF or landing page.
It never downloads files or attempts to bypass publisher access controls.
"""

from __future__ import annotations

import argparse
import logging
import os
import urllib.parse
from datetime import datetime, timezone

from .open_access import lookup_doi
from .zotero_sync import (
    ZoteroSyncError,
    _all_top_items,
    _clean,
    _extract_created_key,
    _normalize_doi,
    _request,
    _response_success_map,
)


OA_TITLE_PREFIX = "[journal-alert OA]"


def _normalize_url(value: str) -> str:
    """Normalize a URL enough to identify an existing linked attachment."""

    value = _clean(value)
    if not value:
        return ""

    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return value.rstrip("/").lower()

    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            parsed.query,
            "",
        )
    )


def _children_for(
    *,
    user_id: str,
    api_key: str,
    parent_key: str,
) -> list[dict]:
    """Load every child item for one Zotero parent."""

    result: list[dict] = []
    start = 0
    limit = 100

    while True:
        rows = _request(
            user_id=user_id,
            api_key=api_key,
            method="GET",
            path=f"/items/{urllib.parse.quote(parent_key, safe='')}/children",
            params={
                "format": "json",
                "limit": limit,
                "start": start,
            },
        )

        if not isinstance(rows, list):
            break

        result.extend(rows)
        if len(rows) < limit:
            break
        start += len(rows)

    return result


def _already_linked(children: list[dict], target_url: str) -> bool:
    """Return True when the legal OA target was already attached."""

    normalized_target = _normalize_url(target_url)

    for row in children:
        if not isinstance(row, dict):
            continue
        data = row.get("data", {}) or {}
        if not isinstance(data, dict):
            continue

        if _normalize_url(data.get("url", "")) == normalized_target:
            return True

        # Also make our own operation idempotent if an older linked URL was
        # later redirected or normalized differently by Zotero.
        if _clean(data.get("title", "")).startswith(OA_TITLE_PREFIX):
            return True

    return False


def _attachment_payload(parent_key: str, oa: dict) -> dict:
    """Build a Zotero linked-URL child attachment for an Unpaywall result."""

    pdf_url = _clean(oa.get("pdf_url", ""))
    landing_url = _clean(oa.get("oa_url", ""))
    target_url = pdf_url or landing_url
    if not target_url:
        raise ValueError("Open Access result has no linkable URL")

    is_pdf = bool(pdf_url)
    details = ["Legal Open Access location reported by Unpaywall."]
    for label, key in (
        ("Status", "status"),
        ("Version", "version"),
        ("License", "license"),
    ):
        value = _clean(oa.get(key, ""))
        if value:
            details.append(f"{label}: {value}.")
    details.append("No paywall bypass or file scraping was used.")

    return {
        "itemType": "attachment",
        "parentItem": parent_key,
        "linkMode": "linked_url",
        "title": (
            f"{OA_TITLE_PREFIX} Open Access PDF"
            if is_pdf
            else f"{OA_TITLE_PREFIX} Open Access Full Text"
        ),
        "url": target_url,
        "accessDate": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "note": " ".join(details),
        "contentType": "application/pdf" if is_pdf else "text/html",
        "charset": "",
    }


def _create_attachment(
    *,
    user_id: str,
    api_key: str,
    payload: dict,
) -> str:
    response = _request(
        user_id=user_id,
        api_key=api_key,
        method="POST",
        path="/items",
        payload=[payload],
    )

    saved = _response_success_map(response).get("0")
    key = _extract_created_key(saved)
    if key:
        return key

    failed = response.get("failed", {}) if isinstance(response, dict) else {}
    raise ZoteroSyncError(
        "Zotero OA attachment creation failed"
        + (f": {str(failed)[:300]}" if failed else "")
    )


def backfill(
    *,
    user_id: str,
    api_key: str,
    email: str,
    limit: int = 5,
    dry_run: bool = False,
    logger: logging.Logger | None = None,
) -> dict:
    """Backfill legal OA links for at most ``limit`` DOI-bearing items."""

    if limit < 1:
        raise ValueError("limit must be at least 1")

    logger = logger or logging.getLogger("jalert.zotero_oa")
    rows = _all_top_items(user_id, api_key)
    cache: dict[str, dict] = {}
    result = {
        "scanned": 0,
        "candidates": 0,
        "checked": 0,
        "open": 0,
        "created": 0,
        "existing": 0,
        "no_oa": 0,
        "failed": 0,
        "dry_run": bool(dry_run),
    }

    for row in rows:
        if result["candidates"] >= limit:
            break
        result["scanned"] += 1

        if not isinstance(row, dict):
            continue
        data = row.get("data", {}) or {}
        if not isinstance(data, dict):
            continue

        parent_key = _clean(row.get("key") or data.get("key"))
        doi = _normalize_doi(data.get("DOI", ""))
        if not parent_key or not doi:
            continue

        result["candidates"] += 1
        try:
            if doi not in cache:
                cache[doi] = lookup_doi(doi, email)
            oa = cache[doi]

            if oa.get("checked"):
                result["checked"] += 1

            target_url = _clean(oa.get("pdf_url", "")) or _clean(
                oa.get("oa_url", "")
            )
            if not oa.get("is_oa") or not target_url:
                result["no_oa"] += 1
                continue

            result["open"] += 1
            children = _children_for(
                user_id=user_id,
                api_key=api_key,
                parent_key=parent_key,
            )
            if _already_linked(children, target_url):
                result["existing"] += 1
                continue

            payload = _attachment_payload(parent_key, oa)
            if not dry_run:
                _create_attachment(
                    user_id=user_id,
                    api_key=api_key,
                    payload=payload,
                )
            result["created"] += 1

        except Exception as exc:
            result["failed"] += 1
            logger.warning(
                "Zotero OA backfill skipped one item after %s",
                type(exc).__name__,
            )

    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attach legal Unpaywall OA links to existing Zotero items."
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    user_id = os.environ.get("ZOTERO_USER_ID", "").strip()
    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    email = os.environ.get("JALERT_MAILTO", "").strip()

    if not user_id or not api_key or not email:
        print("Zotero OA backfill cannot run: required Actions Secrets are missing.")
        return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        result = backfill(
            user_id=user_id,
            api_key=api_key,
            email=email,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"Zotero OA backfill failed: {type(exc).__name__}")
        return 1

    print(
        "Zotero OA backfill complete: "
        f"candidates={result['candidates']} "
        f"checked={result['checked']} "
        f"open={result['open']} "
        f"created={result['created']} "
        f"existing={result['existing']} "
        f"no_oa={result['no_oa']} "
        f"failed={result['failed']} "
        f"dry_run={str(result['dry_run']).lower()}"
    )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
