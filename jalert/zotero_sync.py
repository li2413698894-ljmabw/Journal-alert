"""Automatic Zotero Web API synchronization for journal-alert.

Features
--------
1. Sync selected papers directly to the user's Zotero library.
2. Avoid duplicate imports by DOI or title.
3. Create one top-level Zotero Collection named YYYY-MM-DD.
4. Put newly imported papers into that day's Collection.
5. Add journal-alert research tags and relevance information.
6. Use only Python standard library.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

from .enrich import enrich


ZOTERO_API_BASE = "https://api.zotero.org"
API_VERSION = "3"

SYNC_TIERS_DEFAULT = {
    "must_read",
    "worth_reading",
}

TIER_CN = {
    "must_read": "必读",
    "worth_reading": "值得一读",
    "other": "其他相关",
}


class ZoteroSyncError(RuntimeError):
    """Raised when Zotero API synchronization fails."""


# ============================================================
# 基础工具
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


def _headers(
    api_key: str,
    *,
    json_body: bool = False,
    write_token: bool = False,
) -> dict[str, str]:

    headers = {
        "Zotero-API-Key": api_key,
        "Zotero-API-Version": API_VERSION,
        "Accept": "application/json",
        "User-Agent": "journal-alert/4.1",
    }

    if json_body:
        headers["Content-Type"] = "application/json"

    if write_token:
        headers["Zotero-Write-Token"] = (
            secrets.token_hex(16)
        )

    return headers


def _request(
    *,
    user_id: str,
    api_key: str,
    method: str,
    path: str,
    params: dict | None = None,
    payload=None,
    timeout: int = 30,
):

    prefix = (
        f"{ZOTERO_API_BASE}"
        f"/users/"
        f"{urllib.parse.quote(user_id, safe='')}"
    )

    url = prefix + path

    if params:
        url += "?" + urllib.parse.urlencode(
            params
        )

    body = None

    if payload is not None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

    headers = _headers(
        api_key,
        json_body=payload is not None,
        write_token=(
            method.upper() == "POST"
        ),
    )

    request = urllib.request.Request(
        url,
        data=body,
        method=method.upper(),
        headers=headers,
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:

            raw = response.read()

            if not raw:
                return None

            text = raw.decode(
                "utf-8",
                errors="replace",
            )

            try:
                return json.loads(text)

            except json.JSONDecodeError:
                return text

    except urllib.error.HTTPError as exc:

        try:
            detail = exc.read().decode(
                "utf-8",
                errors="replace",
            )

        except Exception:
            detail = ""

        raise ZoteroSyncError(
            f"Zotero HTTP {exc.code}: "
            f"{detail[:500]}"
        ) from exc

    except urllib.error.URLError as exc:

        raise ZoteroSyncError(
            f"Zotero network error: {exc}"
        ) from exc


# ============================================================
# Zotero 连接测试
# ============================================================

def check_connection(
    user_id: str,
    api_key: str,
) -> None:
    """Verify that Zotero credentials can access the library."""

    _request(
        user_id=user_id,
        api_key=api_key,
        method="GET",
        path="/items",
        params={
            "limit": 1,
            "format": "json",
        },
    )


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

        result.extend(rows)

        if len(rows) < limit:
            break

        start += len(rows)

    return result


def find_collection(
    user_id: str,
    api_key: str,
    name: str,
) -> str | None:
    """Find an existing top-level Collection by exact name."""

    for row in _all_collections(
        user_id,
        api_key,
    ):

        data = (
            row.get("data", {})
            if isinstance(row, dict)
            else {}
        )

        if (
            _clean(data.get("name"))
            == name
            and not data.get(
                "parentCollection"
            )
        ):

            return (
                row.get("key")
                or data.get("key")
            )

    return None


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

    if not isinstance(
        response,
        dict,
    ):
        raise ZoteroSyncError(
            "Unexpected response while "
            "creating Zotero Collection."
        )

    successful = (
        response.get("successful")
        or response.get("success")
        or {}
    )

    saved = (
        successful.get("0")
        if isinstance(
            successful,
            dict,
        )
        else None
    )

    if isinstance(
        saved,
        str,
    ):
        return saved

    if isinstance(
        saved,
        dict,
    ):

        key = (
            saved.get("key")
            or saved.get(
                "data",
                {},
            ).get("key")
        )

        if key:
            return key

    # 极少数 API 响应形式不同，
    # 再读取一次 Collection 列表确认。
    key = find_collection(
        user_id,
        api_key,
        name,
    )

    if key:
        return key

    raise ZoteroSyncError(
        f"Collection created but key "
        f"could not be resolved: {name}"
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
        return existing, False

    created = create_collection(
        user_id,
        api_key,
        name,
    )

    return created, True


# ============================================================
# 作者
# ============================================================

def _author_to_creator(
    author: str,
) -> dict:

    author = _clean(author)

    if not author:
        return {}

    # Last, First
    if "," in author:

        last_name, first_name = (
            part.strip()
            for part in author.split(
                ",",
                1,
            )
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

    # 单字段姓名
    return {
        "creatorType": "author",
        "name": author,
    }


def _creators(item) -> list[dict]:

    authors = getattr(
        item,
        "authors",
        [],
    ) or []

    result = []

    for author in authors:

        creator = _author_to_creator(
            author
        )

        if creator:
            result.append(
                creator
            )

    return result


# ============================================================
# Zotero 去重
# ============================================================

def _search_zotero(
    user_id: str,
    api_key: str,
    query: str,
) -> list[dict]:

    rows = _request(
        user_id=user_id,
        api_key=api_key,
        method="GET",
        path="/items",
        params={
            "q": query,
            "qmode": "everything",
            "format": "json",
            "limit": 25,
        },
    )

    if not isinstance(
        rows,
        list,
    ):
        return []

    return rows


def find_existing_item(
    user_id: str,
    api_key: str,
    item,
) -> dict | None:

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

    # --------------------------------------------------------
    # 1. DOI 优先
    # --------------------------------------------------------

    if doi:

        candidates = _search_zotero(
            user_id,
            api_key,
            doi,
        )

        for candidate in candidates:

            data = candidate.get(
                "data",
                {},
            )

            candidate_doi = (
                _normalize_doi(
                    data.get(
                        "DOI",
                        "",
                    )
                )
            )

            if (
                candidate_doi
                and candidate_doi == doi
            ):
                return candidate

    # --------------------------------------------------------
    # 2. DOI 不存在时按标题兜底
    # --------------------------------------------------------

    raw_title = _clean(
        getattr(
            item,
            "title",
            "",
        )
    )

    if raw_title:

        candidates = _search_zotero(
            user_id,
            api_key,
            raw_title,
        )

        for candidate in candidates:

            data = candidate.get(
                "data",
                {},
            )

            candidate_title = (
                _normalize_title(
                    data.get(
                        "title",
                        "",
                    )
                )
            )

            if (
                candidate_title
                and candidate_title
                == title
            ):
                return candidate

    return None


# ============================================================
# Zotero Item
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

    relevance_reason = _clean(
        extra_info.get(
            "relevance_reason",
            "",
        )
    )

    # --------------------------------------------------------
    # Zotero Tags
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

    # 去重，保持顺序
    seen = set()
    tags = []

    for tag in tag_names:

        tag = _clean(tag)

        if (
            not tag
            or tag in seen
        ):
            continue

        seen.add(tag)

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
            "Research Use: "
            + " / ".join(
                research_tags
            )
        )

    if methods:

        extra_lines.append(
            "Methods: "
            + " / ".join(
                methods
            )
        )

    if relevance_reason:

        extra_lines.append(
            "Relevance: "
            + relevance_reason
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


def create_zotero_item(
    user_id: str,
    api_key: str,
    payload: dict,
) -> str:

    response = _request(
        user_id=user_id,
        api_key=api_key,
        method="POST",
        path="/items",
        payload=[
            payload
        ],
    )

    if not isinstance(
        response,
        dict,
    ):
        raise ZoteroSyncError(
            "Unexpected Zotero item "
            "creation response."
        )

    successful = (
        response.get("successful")
        or response.get("success")
        or {}
    )

    saved = (
        successful.get("0")
        if isinstance(
            successful,
            dict,
        )
        else None
    )

    if isinstance(
        saved,
        str,
    ):
        return saved

    if isinstance(
        saved,
        dict,
    ):

        key = (
            saved.get("key")
            or saved.get(
                "data",
                {},
            ).get("key")
        )

        if key:
            return key

    failed = (
        response.get("failed")
        or {}
    )

    if failed:

        raise ZoteroSyncError(
            "Zotero item rejected: "
            + json.dumps(
                failed,
                ensure_ascii=False,
            )[:500]
        )

    raise ZoteroSyncError(
        "Zotero item creation "
        "returned no item key."
    )


# ============================================================
# 对外统一同步接口
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
            "Zotero sync enabled but "
            "ZOTERO_USER_ID or "
            "ZOTERO_API_KEY is missing"
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

    # 验证连接，但绝不打印密钥
    check_connection(
        user_id,
        api_key,
    )

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

        scored = entry["scored"]

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
            "Zotero sync: no selected articles"
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

    # --------------------------------------------------------
    # 先查重
    # --------------------------------------------------------

    to_create = []
    existing_count = 0

    for entry in selected:

        existing = find_existing_item(
            user_id,
            api_key,
            entry["item"],
        )

        if existing:

            existing_count += 1

            log.info(
                "Zotero duplicate skipped: %s",
                getattr(
                    entry["item"],
                    "title",
                    "",
                )[:100],
            )

        else:

            to_create.append(
                entry
            )

    # 没有真正需要导入的文献，
    # 就不创建一个空的日期 Collection。
    if not to_create:

        log.info(
            (
                "Zotero sync: "
                "%d selected / "
                "%d already existing / "
                "0 new"
            ),
            len(selected),
            existing_count,
        )

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
    # 创建 / 复用当天日期 Collection
    # --------------------------------------------------------

    collection_name = day

    collection_key, created_collection = (
        ensure_collection(
            user_id,
            api_key,
            collection_name,
        )
    )

    if created_collection:

        log.info(
            "Zotero Collection created: %s",
            collection_name,
        )

    else:

        log.info(
            "Zotero Collection reused: %s",
            collection_name,
        )

    # --------------------------------------------------------
    # 创建论文
    # --------------------------------------------------------

    created_count = 0
    failed_count = 0

    for entry in to_create:

        item = entry["item"]
        scored = entry["scored"]

        try:

            payload = build_zotero_item(
                item,
                scored,
                collection_key=collection_key,
            )

            item_key = create_zotero_item(
                user_id,
                api_key,
                payload,
            )

            created_count += 1

            log.info(
                "Zotero item created: %s [%s]",
                getattr(
                    item,
                    "title",
                    "",
                )[:100],
                item_key,
            )

        except Exception as exc:

            failed_count += 1

            log.warning(
                "Zotero item failed: %s -> %s",
                getattr(
                    item,
                    "title",
                    "",
                )[:100],
                str(exc)[:300],
            )

    result = {
        "enabled": True,
        "selected": len(
            selected
        ),
        "created": created_count,
        "existing": existing_count,
        "failed": failed_count,
        "collection": collection_name,
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
