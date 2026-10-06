"""Automatic Zotero Web API synchronization for journal-alert.

V4.1.1

Features
--------
1. Sync selected papers directly to the user's Zotero library.
2. Load the Zotero library once and deduplicate locally.
3. Deduplicate by DOI first, title second.
4. Create one Zotero Collection named YYYY-MM-DD.
5. Batch-create items instead of one request per paper.
6. Respect Zotero Retry-After / Backoff headers.
7. Retry 429 / 409 / 5xx failures safely.
8. Add journal-alert research tags and relevance information.
9. Use only the Python standard library.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from .enrich import enrich


ZOTERO_API_BASE = "https://api.zotero.org"
API_VERSION = "3"

# Zotero 官方最多允许一次创建 50 个对象。
# 这里保守地使用 25，降低请求体大小和限流风险。
BATCH_SIZE = 25

# 单次请求失败后的最大重试次数
MAX_RETRIES = 6

# 没有 Retry-After 时，指数退避最长等待时间
MAX_BACKOFF_SECONDS = 60


TIER_CN = {
    "must_read": "必读",
    "worth_reading": "值得一读",
    "other": "其他相关",
}


log = logging.getLogger("jalert")


class ZoteroSyncError(RuntimeError):
    """Raised when Zotero API synchronization fails."""


# ============================================================
# 基础文本处理
# ============================================================

def _clean(value) -> str:
    if value is None:
        return ""

    return str(value).strip()


def _normalize_doi(value: str) -> str:
    value = _clean(value).lower()

    value = re.sub(
        r"^https?://(?:dx\.)?doi\.org/",
        "",
        value,
    )

    value = re.sub(
        r"^doi:\s*",
        "",
        value,
    )

    return value.strip()


def _normalize_title(value: str) -> str:
    value = _clean(value).lower()

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    value = re.sub(
        r"[^\w\s]",
        "",
        value,
    )

    return value.strip()


# ============================================================
# HTTP / 限流处理
# ============================================================

def _parse_wait_seconds(
    value,
) -> float | None:

    if value is None:
        return None

    try:
        seconds = float(
            str(value).strip()
        )

        if seconds > 0:
            return seconds

    except (
        TypeError,
        ValueError,
    ):
        pass

    return None


def _retry_wait(
    exc,
    attempt: int,
) -> float:

    retry_after = None

    if isinstance(
        exc,
        urllib.error.HTTPError,
    ):

        retry_after = _parse_wait_seconds(
            exc.headers.get(
                "Retry-After"
            )
        )

    # Zotero 明确给了 Retry-After 时，
    # 必须至少等待它要求的时间。
    if retry_after is not None:
        return retry_after

    # 否则指数退避：
    # 2, 4, 8, 16, 32, 60 秒
    return min(
        2 ** (attempt + 1),
        MAX_BACKOFF_SECONDS,
    )


def _headers(
    api_key: str,
    *,
    json_body: bool = False,
    write_token: str = "",
) -> dict[str, str]:

    headers = {
        "Zotero-API-Key": api_key,
        "Zotero-API-Version": API_VERSION,
        "Accept": "application/json",
        "User-Agent": "journal-alert/4.1.1",
    }

    if json_body:
        headers[
            "Content-Type"
        ] = "application/json"

    if write_token:
        headers[
            "Zotero-Write-Token"
        ] = write_token

    return headers


def _request(
    *,
    user_id: str,
    api_key: str,
    method: str,
    path: str,
    params: dict | None = None,
    payload=None,
    timeout: int = 45,
    max_retries: int = MAX_RETRIES,
):

    method = method.upper()

    prefix = (
        f"{ZOTERO_API_BASE}"
        f"/users/"
        f"{urllib.parse.quote(user_id, safe='')}"
    )

    url = prefix + path

    if params:

        url += (
            "?"
            + urllib.parse.urlencode(
                params
            )
        )

    body = None

    if payload is not None:

        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

    # 一个逻辑写入请求只生成一个 token。
    # 如果网络重试，不重新生成。
    write_token = ""

    if method == "POST":

        write_token = (
            secrets.token_hex(16)
        )

    last_error = None

    for attempt in range(
        max_retries + 1
    ):

        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=_headers(
                api_key,
                json_body=(
                    payload is not None
                ),
                write_token=write_token,
            ),
        )

        try:

            with urllib.request.urlopen(
                request,
                timeout=timeout,
            ) as response:

                raw = response.read()

                # --------------------------------------------
                # Zotero Backoff
                # --------------------------------------------

                backoff = (
                    _parse_wait_seconds(
                        response.headers.get(
                            "Backoff"
                        )
                    )
                )

                if backoff is not None:

                    log.warning(
                        (
                            "Zotero requested "
                            "Backoff: %.0f second(s)"
                        ),
                        backoff,
                    )

                    time.sleep(
                        backoff
                    )

                if not raw:
                    return None

                text = raw.decode(
                    "utf-8",
                    errors="replace",
                )

                try:

                    return json.loads(
                        text
                    )

                except json.JSONDecodeError:

                    return text

        except urllib.error.HTTPError as exc:

            last_error = exc

            try:

                detail = (
                    exc.read().decode(
                        "utf-8",
                        errors="replace",
                    )
                )

            except Exception:

                detail = ""

            # 429 = 限流
            # 409 = Zotero 文库暂时锁定
            # 5xx = 服务端暂时错误
            retryable = exc.code in {
                409,
                429,
                500,
                502,
                503,
                504,
            }

            if (
                retryable
                and attempt < max_retries
            ):

                wait_seconds = (
                    _retry_wait(
                        exc,
                        attempt,
                    )
                )

                log.warning(
                    (
                        "Zotero HTTP %d; "
                        "retry %d/%d after "
                        "%.0f second(s)"
                    ),
                    exc.code,
                    attempt + 1,
                    max_retries,
                    wait_seconds,
                )

                time.sleep(
                    wait_seconds
                )

                continue

            raise ZoteroSyncError(
                (
                    f"Zotero HTTP "
                    f"{exc.code}: "
                    f"{detail[:500]}"
                )
            ) from exc

        except urllib.error.URLError as exc:

            last_error = exc

            if attempt < max_retries:

                wait_seconds = min(
                    2 ** (
                        attempt + 1
                    ),
                    MAX_BACKOFF_SECONDS,
                )

                log.warning(
                    (
                        "Zotero network error; "
                        "retry %d/%d after "
                        "%.0f second(s): %s"
                    ),
                    attempt + 1,
                    max_retries,
                    wait_seconds,
                    str(exc)[:200],
                )

                time.sleep(
                    wait_seconds
                )

                continue

            raise ZoteroSyncError(
                f"Zotero network error: "
                f"{exc}"
            ) from exc

    raise ZoteroSyncError(
        "Zotero request failed after "
        f"{max_retries} retries: "
        f"{last_error}"
    )


# ============================================================
# 一次读取 Zotero 文库
# ============================================================

def _all_top_items(
    user_id: str,
    api_key: str,
) -> list[dict]:
    """Load all top-level Zotero items.

    Attachments and notes are excluded by using /items/top.
    Results are paginated 100 at a time.
    """

    result: list[dict] = []

    start = 0
    limit = 100

    while True:

        rows = _request(
            user_id=user_id,
            api_key=api_key,
            method="GET",
            path="/items/top",
            params={
                "format": "json",
                "limit": limit,
                "start": start,
            },
        )

        if not isinstance(
            rows,
            list,
        ):
            break

        result.extend(
            rows
        )

        if len(rows) < limit:
            break

        start += len(rows)

    return result


def _build_existing_index(
    rows: list[dict],
) -> tuple[
    dict[str, str],
    dict[str, str],
]:
    """Build DOI and title indexes in memory."""

    doi_index: dict[
        str,
        str,
    ] = {}

    title_index: dict[
        str,
        str,
    ] = {}

    for row in rows:

        if not isinstance(
            row,
            dict,
        ):
            continue

        data = (
            row.get(
                "data",
                {},
            )
            or {}
        )

        key = (
            row.get("key")
            or data.get("key")
            or ""
        )

        doi = _normalize_doi(
            data.get(
                "DOI",
                "",
            )
        )

        title = _normalize_title(
            data.get(
                "title",
                "",
            )
        )

        if doi and doi not in doi_index:

            doi_index[
                doi
            ] = key

        if (
            title
            and title
            not in title_index
        ):

            title_index[
                title
            ] = key

    return (
        doi_index,
        title_index,
    )


def _find_existing_local(
    item,
    *,
    doi_index: dict[str, str],
    title_index: dict[str, str],
) -> str | None:

    doi = _normalize_doi(
        getattr(
            item,
            "doi",
            "",
        )
    )

    if (
        doi
        and doi in doi_index
    ):

        return doi_index[
            doi
        ]

    title = _normalize_title(
        getattr(
            item,
            "title",
            "",
        )
    )

    if (
        title
        and title
        in title_index
    ):

        return title_index[
            title
        ]

    return None


# ============================================================
# Collection
# ============================================================

def _all_collections(
    user_id: str,
    api_key: str,
) -> list[dict]:

    result: list[dict] = []

    start = 0
    limit = 100

    while True:

        rows = _request(
            user_id=user_id,
            api_key=api_key,
            method="GET",
            path="/collections",
            params={
                "format": "json",
                "limit": limit,
                "start": start,
            },
        )

        if not isinstance(
            rows,
            list,
        ):
            break

        result.extend(
            rows
        )

        if len(rows) < limit:
            break

        start += len(rows)

    return result


def find_collection(
    user_id: str,
    api_key: str,
    name: str,
) -> str | None:

    rows = _all_collections(
        user_id,
        api_key,
    )

    for row in rows:

        if not isinstance(
            row,
            dict,
        ):
            continue

        data = (
            row.get(
                "data",
                {},
            )
            or {}
        )

        row_name = _clean(
            data.get(
                "name",
                "",
            )
        )

        parent = data.get(
            "parentCollection"
        )

        if (
            row_name == name
            and not parent
        ):

            return (
                row.get("key")
                or data.get("key")
            )

    return None


def _response_success_map(
    response,
) -> dict:

    if not isinstance(
        response,
        dict,
    ):
        return {}

    successful = response.get(
        "successful"
    )

    if isinstance(
        successful,
        dict,
    ):
        return successful

    success = response.get(
        "success"
    )

    if isinstance(
        success,
        dict,
    ):
        return success

    return {}


def _extract_created_key(
    value,
) -> str:

    if isinstance(
        value,
        str,
    ):
        return value

    if isinstance(
        value,
        dict,
    ):

        key = (
            value.get("key")
            or (
                value.get(
                    "data",
                    {},
                )
                or {}
            ).get("key")
        )

        if key:
            return str(key)

    return ""


def create_collection(
    user_id: str,
    api_key: str,
    name: str,
) -> str:

    response = _request(
        user_id=user_id,
        api_key=api_key,
        method="POST",
        path="/collections",
        payload=[
            {
                "name": name,
                "parentCollection": False,
            }
        ],
    )

    successful = (
        _response_success_map(
            response
        )
    )

    saved = successful.get(
        "0"
    )

    key = _extract_created_key(
        saved
    )

    if key:
        return key

    failed = {}

    if isinstance(
        response,
        dict,
    ):

        failed = (
            response.get(
                "failed"
            )
            or {}
        )

    if failed:

        raise ZoteroSyncError(
            (
                "Zotero Collection "
                "creation failed: "
                + json.dumps(
                    failed,
                    ensure_ascii=False,
                )[:500]
            )
        )

    # 极少数情况下重新读取确认
    key = find_collection(
        user_id,
        api_key,
        name,
    )

    if key:
        return key

    raise ZoteroSyncError(
        (
            "Collection was requested "
            "but no key was returned: "
            f"{name}"
        )
    )


def ensure_collection(
    user_id: str,
    api_key: str,
    name: str,
) -> tuple[str, bool]:

    existing = find_collection(
        user_id,
        api_key,
        name,
    )

    if existing:

        return (
            existing,
            False,
        )

    created = create_collection(
        user_id,
        api_key,
        name,
    )

    return (
        created,
        True,
    )


# ============================================================
# 作者
# ============================================================

def _author_to_creator(
    author: str,
) -> dict:

    author = _clean(
        author
    )

    if not author:
        return {}

    # Crossref 有时提供：
    # Last, First
    if "," in author:

        parts = author.split(
            ",",
            1,
        )

        last_name = (
            parts[0].strip()
        )

        first_name = (
            parts[1].strip()
        )

        return {
            "creatorType": "author",
            "firstName": first_name,
            "lastName": last_name,
        }

    parts = author.split()

    if len(parts) >= 2:

        return {
            "creatorType": "author",
            "firstName": " ".join(
                parts[:-1]
            ),
            "lastName": parts[-1],
        }

    return {
        "creatorType": "author",
        "name": author,
    }


def _creators(
    item,
) -> list[dict]:

    authors = getattr(
        item,
        "authors",
        [],
    ) or []

    result: list[dict] = []

    for author in authors:

        creator = (
            _author_to_creator(
                author
            )
        )

        if creator:

            result.append(
                creator
            )

    return result


# ============================================================
# Zotero Item 构建
# ============================================================

def build_zotero_item(
    item,
    scored,
    *,
    collection_key: str,
) -> dict:

    extra_info = enrich(
        item,
        scored,
    )

    tier = getattr(
        scored,
        "tier",
        "",
    )

    tier_cn = TIER_CN.get(
        tier,
        tier or "相关文献",
    )

    research_tags = (
        extra_info.get(
            "research_tags",
            [],
        )
        or []
    )

    methods = (
        extra_info.get(
            "methods",
            [],
        )
        or []
    )

    relevance_reason = (
        _clean(
            extra_info.get(
                "relevance_reason",
                "",
            )
        )
    )

    # --------------------------------------------------------
    # Tags
    # --------------------------------------------------------

    tag_names = [
        "journal-alert",
        tier_cn,
    ]

    tag_names.extend(
        research_tags
    )

    tag_names.extend(
        methods
    )

    seen_tags = set()

    tags = []

    for tag in tag_names:

        tag = _clean(
            tag
        )

        if (
            not tag
            or tag in seen_tags
        ):
            continue

        seen_tags.add(
            tag
        )

        tags.append(
            {
                "tag": tag,
            }
        )

    # --------------------------------------------------------
    # Extra
    # --------------------------------------------------------

    extra_lines = [
        (
            "Journal Alert Score: "
            f"{getattr(scored, 'score', 0)}"
        ),
        (
            "Journal Alert Tier: "
            f"{tier_cn}"
        ),
    ]

    if research_tags:

        extra_lines.append(
            (
                "Research Use: "
                + " / ".join(
                    research_tags
                )
            )
        )

    if methods:

        extra_lines.append(
            (
                "Methods: "
                + " / ".join(
                    methods
                )
            )
        )

    if relevance_reason:

        extra_lines.append(
            (
                "Relevance: "
                + relevance_reason
            )
        )

    doi = _normalize_doi(
        getattr(
            item,
            "doi",
            "",
        )
    )

    payload = {
        "itemType": "journalArticle",

        "title": _clean(
            getattr(
                item,
                "title",
                "",
            )
        ),

        "creators": _creators(
            item
        ),

        "abstractNote": _clean(
            getattr(
                item,
                "abstract",
                "",
            )
        ),

        "publicationTitle": _clean(
            getattr(
                item,
                "journal",
                "",
            )
        ),

        "date": _clean(
            getattr(
                item,
                "date",
                "",
            )
        ),

        "DOI": doi,

        "ISSN": _clean(
            getattr(
                item,
                "issn",
                "",
            )
        ),

        "url": _clean(
            getattr(
                item,
                "url",
                "",
            )
        ),

        "extra": "\n".join(
            extra_lines
        ),

        "tags": tags,

        "collections": [
            collection_key
        ],
    }

    return payload


# ============================================================
# 批量创建文献
# ============================================================

def _chunks(
    values: list,
    size: int,
):

    for start in range(
        0,
        len(values),
        size,
    ):

        yield values[
            start:
            start + size
        ]


def create_items_batch(
    *,
    user_id: str,
    api_key: str,
    payloads: list[dict],
) -> tuple[
    int,
    int,
]:

    if not payloads:

        return (
            0,
            0,
        )

    response = _request(
        user_id=user_id,
        api_key=api_key,
        method="POST",
        path="/items",
        payload=payloads,
    )

    if not isinstance(
        response,
        dict,
    ):

        raise ZoteroSyncError(
            (
                "Unexpected Zotero "
                "batch response."
            )
        )

    successful = (
        _response_success_map(
            response
        )
    )

    failed = (
        response.get(
            "failed"
        )
        or {}
    )

    unchanged = (
        response.get(
            "unchanged"
        )
        or {}
    )

    created_count = len(
        successful
    )

    failed_count = len(
        failed
    )

    # unchanged 不视为失败
    if unchanged:

        log.info(
            (
                "Zotero batch unchanged: "
                "%d"
            ),
            len(unchanged),
        )

    if failed:

        for index, detail in (
            failed.items()
        ):

            log.warning(
                (
                    "Zotero item index %s "
                    "failed: %s"
                ),
                index,
                json.dumps(
                    detail,
                    ensure_ascii=False,
                )[:500],
            )

    return (
        created_count,
        failed_count,
    )


# ============================================================
# 对外统一同步入口
# ============================================================

def sync_entries(
    *,
    entries: list[dict],
    cfg: dict,
    day: str,
    log,
) -> dict:

    zotero_cfg = (
        cfg.get(
            "zotero",
            {},
        )
        or {}
    )

    if not zotero_cfg.get(
        "enabled",
        False,
    ):

        return {
            "enabled": False,
            "selected": 0,
            "created": 0,
            "existing": 0,
            "failed": 0,
            "collection": "",
        }

    user_id = os.environ.get(
        "ZOTERO_USER_ID",
        "",
    ).strip()

    api_key = os.environ.get(
        "ZOTERO_API_KEY",
        "",
    ).strip()

    if (
        not user_id
        or not api_key
    ):

        log.warning(
            (
                "Zotero sync enabled "
                "but ZOTERO_USER_ID or "
                "ZOTERO_API_KEY is missing"
            )
        )

        return {
            "enabled": True,
            "selected": 0,
            "created": 0,
            "existing": 0,
            "failed": 0,
            "collection": "",
            "credentials": False,
        }

    log.info(
        "Zotero credentials detected"
    )

    # --------------------------------------------------------
    # 1. 按等级选择
    # --------------------------------------------------------

    allowed_tiers = set(
        zotero_cfg.get(
            "sync_tiers",
            [
                "must_read",
                "worth_reading",
            ],
        )
    )

    selected = []

    for entry in entries:

        scored = entry[
            "scored"
        ]

        tier = getattr(
            scored,
            "tier",
            "",
        )

        if tier in allowed_tiers:

            selected.append(
                entry
            )

    if not selected:

        log.info(
            (
                "Zotero sync: "
                "no selected articles"
            )
        )

        return {
            "enabled": True,
            "selected": 0,
            "created": 0,
            "existing": 0,
            "failed": 0,
            "collection": "",
            "credentials": True,
        }

    log.info(
        (
            "Zotero sync: "
            "%d article(s) selected"
        ),
        len(selected),
    )

    # --------------------------------------------------------
    # 2. 一次读取 Zotero 文库
    # --------------------------------------------------------

    existing_rows = (
        _all_top_items(
            user_id,
            api_key,
        )
    )

    log.info(
        (
            "Zotero library index loaded: "
            "%d top-level item(s)"
        ),
        len(existing_rows),
    )

    (
        doi_index,
        title_index,
    ) = _build_existing_index(
        existing_rows
    )

    # --------------------------------------------------------
    # 3. 本地查重
    # --------------------------------------------------------

    to_create = []

    existing_count = 0

    for entry in selected:

        item = entry[
            "item"
        ]

        existing_key = (
            _find_existing_local(
                item,
                doi_index=doi_index,
                title_index=title_index,
            )
        )

        if existing_key:

            existing_count += 1

            log.info(
                (
                    "Zotero duplicate skipped: "
                    "%s"
                ),
                getattr(
                    item,
                    "title",
                    "",
                )[:100],
            )

            continue

        to_create.append(
            entry
        )

        # 同一批数据内部也立即加入索引，
        # 防止当前运行自身产生重复。
        doi = _normalize_doi(
            getattr(
                item,
                "doi",
                "",
            )
        )

        title = _normalize_title(
            getattr(
                item,
                "title",
                "",
            )
        )

        if doi:

            doi_index[
                doi
            ] = "__pending__"

        if title:

            title_index[
                title
            ] = "__pending__"

    log.info(
        (
            "Zotero dedupe: "
            "%d selected / "
            "%d existing / "
            "%d to create"
        ),
        len(selected),
        existing_count,
        len(to_create),
    )

    # --------------------------------------------------------
    # 4. 没有新内容，不建空日期 Collection
    # --------------------------------------------------------

    if not to_create:

        return {
            "enabled": True,
            "selected": len(
                selected
            ),
            "created": 0,
            "existing": existing_count,
            "failed": 0,
            "collection": "",
            "credentials": True,
        }

    # --------------------------------------------------------
    # 5. 创建当天日期 Collection
    # --------------------------------------------------------

    if zotero_cfg.get(
        "daily_collection",
        True,
    ):

        collection_name = day

    else:

        collection_name = (
            zotero_cfg.get(
                "collection_name",
                "Journal Alert",
            )
        )

    (
        collection_key,
        created_collection,
    ) = ensure_collection(
        user_id,
        api_key,
        collection_name,
    )

    if created_collection:

        log.info(
            (
                "Zotero Collection created: "
                "%s"
            ),
            collection_name,
        )

    else:

        log.info(
            (
                "Zotero Collection reused: "
                "%s"
            ),
            collection_name,
        )

    # --------------------------------------------------------
    # 6. 构建全部待写入 payload
    # --------------------------------------------------------

    payload_entries = []

    for entry in to_create:

        payload_entries.append(
            (
                entry,
                build_zotero_item(
                    entry["item"],
                    entry["scored"],
                    collection_key=(
                        collection_key
                    ),
                ),
            )
        )

    # --------------------------------------------------------
    # 7. 批量写入
    # --------------------------------------------------------

    created_count = 0
    failed_count = 0

    total_batches = (
        (
            len(payload_entries)
            + BATCH_SIZE
            - 1
        )
        // BATCH_SIZE
    )

    for batch_number, batch in enumerate(
        _chunks(
            payload_entries,
            BATCH_SIZE,
        ),
        start=1,
    ):

        payloads = [
            payload
            for _entry, payload
            in batch
        ]

        log.info(
            (
                "Zotero batch %d/%d: "
                "uploading %d item(s)"
            ),
            batch_number,
            total_batches,
            len(payloads),
        )

        try:

            (
                batch_created,
                batch_failed,
            ) = create_items_batch(
                user_id=user_id,
                api_key=api_key,
                payloads=payloads,
            )

            created_count += (
                batch_created
            )

            failed_count += (
                batch_failed
            )

            log.info(
                (
                    "Zotero batch %d/%d: "
                    "created=%d failed=%d"
                ),
                batch_number,
                total_batches,
                batch_created,
                batch_failed,
            )

        except Exception as exc:

            # 整批失败时记录，但不能影响日报和微信
            failed_count += len(
                payloads
            )

            log.warning(
                (
                    "Zotero batch %d/%d "
                    "failed after retries: %s"
                ),
                batch_number,
                total_batches,
                str(exc)[:500],
            )

    # --------------------------------------------------------
    # 8. 最终统计
    # --------------------------------------------------------

    result = {
        "enabled": True,

        "selected": len(
            selected
        ),

        "created": created_count,

        "existing": existing_count,

        "failed": failed_count,

        "collection": (
            collection_name
        ),

        "credentials": True,
    }

    log.info(
        (
            "Zotero sync complete: "
            "collection=%s / "
            "selected=%d / "
            "created=%d / "
            "existing=%d / "
            "failed=%d"
        ),
        collection_name,
        len(selected),
        created_count,
        existing_count,
        failed_count,
    )

    return result
