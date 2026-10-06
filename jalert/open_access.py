"""Open-access full-text lookup for journal-alert.

V4.2A
-----
Uses the Unpaywall DOI API to find legal open-access versions.

The module does NOT bypass publisher paywalls.
It only returns legally available OA locations reported by Unpaywall.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


UNPAYWALL_API = "https://api.unpaywall.org/v2"

MAX_RETRIES = 4


def _clean(value) -> str:
    if value is None:
        return ""

    return str(value).strip()


def _normalize_doi(value: str) -> str:
    value = _clean(value).lower()

    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "http://dx.doi.org/",
        "https://dx.doi.org/",
        "doi:",
    ):
        if value.startswith(prefix):
            value = value[len(prefix):]

    return value.strip()


def _empty_result(
    *,
    checked: bool = False,
    status: str = "",
    error: str = "",
) -> dict:

    return {
        "checked": checked,
        "is_oa": False,
        "status": status,
        "pdf_url": "",
        "oa_url": "",
        "version": "",
        "host_type": "",
        "license": "",
        "source": "Unpaywall",
        "error": error,
    }


def _wait_seconds(
    exc,
    attempt: int,
) -> float:

    if isinstance(
        exc,
        urllib.error.HTTPError,
    ):
        value = exc.headers.get(
            "Retry-After"
        )

        if value:
            try:
                return max(
                    1.0,
                    float(value),
                )
            except ValueError:
                pass

    return min(
        2 ** (attempt + 1),
        30,
    )


def lookup_doi(
    doi: str,
    email: str,
    *,
    timeout: int = 30,
) -> dict:
    """Look up one DOI in Unpaywall."""

    doi = _normalize_doi(
        doi
    )

    email = _clean(
        email
    )

    if not doi:

        return _empty_result(
            checked=False,
            status="no_doi",
        )

    if not email:

        return _empty_result(
            checked=False,
            status="no_email",
        )

    encoded_doi = (
        urllib.parse.quote(
            doi,
            safe="",
        )
    )

    query = urllib.parse.urlencode(
        {
            "email": email,
        }
    )

    url = (
        f"{UNPAYWALL_API}/"
        f"{encoded_doi}"
        f"?{query}"
    )

    last_error = ""

    for attempt in range(
        MAX_RETRIES + 1
    ):

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "journal-alert/4.2 "
                    f"mailto:{email}"
                ),
            },
        )

        try:

            with urllib.request.urlopen(
                request,
                timeout=timeout,
            ) as response:

                raw = response.read()

                data = json.loads(
                    raw.decode(
                        "utf-8",
                        errors="replace",
                    )
                )

                return _parse_result(
                    data
                )

        except urllib.error.HTTPError as exc:

            # DOI 不存在或格式不被接受：
            # 不是程序故障，直接标记为查不到。
            if exc.code in (
                404,
                422,
            ):

                return _empty_result(
                    checked=True,
                    status="not_found",
                )

            retryable = exc.code in (
                429,
                500,
                502,
                503,
                504,
            )

            try:
                detail = (
                    exc.read()
                    .decode(
                        "utf-8",
                        errors="replace",
                    )
                )
            except Exception:
                detail = ""

            last_error = (
                f"HTTP {exc.code}: "
                f"{detail[:300]}"
            )

            if (
                retryable
                and attempt < MAX_RETRIES
            ):

                time.sleep(
                    _wait_seconds(
                        exc,
                        attempt,
                    )
                )

                continue

            return _empty_result(
                checked=False,
                status="http_error",
                error=last_error,
            )

        except Exception as exc:

            last_error = (
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            if attempt < MAX_RETRIES:

                time.sleep(
                    min(
                        2 ** (
                            attempt + 1
                        ),
                        30,
                    )
                )

                continue

            return _empty_result(
                checked=False,
                status="network_error",
                error=last_error,
            )

    return _empty_result(
        checked=False,
        status="failed",
        error=last_error,
    )


def _parse_result(
    data: dict,
) -> dict:
    """Convert Unpaywall response to journal-alert format."""

    is_oa = bool(
        data.get(
            "is_oa",
            False,
        )
    )

    oa_status = _clean(
        data.get(
            "oa_status",
            "",
        )
    )

    locations = (
        data.get(
            "oa_locations",
            []
        )
        or []
    )

    best = (
        data.get(
            "best_oa_location"
        )
        or {}
    )

    pdf_url = _clean(
        best.get(
            "url_for_pdf",
            "",
        )
    )

    oa_url = (
        _clean(
            best.get(
                "url_for_landing_page",
                "",
            )
        )
        or _clean(
            best.get(
                "url",
                "",
            )
        )
    )

    # best_oa_location 没有 PDF 时，
    # 再遍历其它合法 OA location，
    # 优先寻找一个可直接打开的 PDF。
    if not pdf_url:

        for location in locations:

            if not isinstance(
                location,
                dict,
            ):
                continue

            candidate = _clean(
                location.get(
                    "url_for_pdf",
                    "",
                )
            )

            if candidate:

                pdf_url = candidate

                if not oa_url:

                    oa_url = (
                        _clean(
                            location.get(
                                "url_for_landing_page",
                                "",
                            )
                        )
                        or _clean(
                            location.get(
                                "url",
                                "",
                            )
                        )
                    )

                if not best:
                    best = location

                break

    # 没有 PDF，但是有开放网页时也保留。
    if (
        not oa_url
        and locations
    ):

        for location in locations:

            if not isinstance(
                location,
                dict,
            ):
                continue

            candidate = (
                _clean(
                    location.get(
                        "url_for_landing_page",
                        "",
                    )
                )
                or _clean(
                    location.get(
                        "url",
                        "",
                    )
                )
            )

            if candidate:

                oa_url = candidate

                if not best:
                    best = location

                break

    return {
        "checked": True,

        "is_oa": is_oa,

        "status": (
            oa_status
            or (
                "open"
                if is_oa
                else "closed"
            )
        ),

        "pdf_url": pdf_url,

        "oa_url": oa_url,

        "version": _clean(
            best.get(
                "version",
                "",
            )
        ),

        "host_type": _clean(
            best.get(
                "host_type",
                "",
            )
        ),

        "license": _clean(
            best.get(
                "license",
                "",
            )
        ),

        "source": "Unpaywall",

        "error": "",
    }


def _apply_result(
    item,
    result: dict,
) -> None:
    """Attach OA metadata to an Item instance."""

    item.oa_checked = bool(
        result.get(
            "checked",
            False,
        )
    )

    item.oa_is_oa = bool(
        result.get(
            "is_oa",
            False,
        )
    )

    item.oa_status = _clean(
        result.get(
            "status",
            "",
        )
    )

    item.oa_pdf_url = _clean(
        result.get(
            "pdf_url",
            "",
        )
    )

    item.oa_url = _clean(
        result.get(
            "oa_url",
            "",
        )
    )

    item.oa_version = _clean(
        result.get(
            "version",
            "",
        )
    )

    item.oa_host_type = _clean(
        result.get(
            "host_type",
            "",
        )
    )

    item.oa_license = _clean(
        result.get(
            "license",
            "",
        )
    )

    item.oa_source = _clean(
        result.get(
            "source",
            "Unpaywall",
        )
    )


def annotate_entries(
    *,
    entries: list[dict],
    cfg: dict,
    log,
) -> dict:
    """Look up OA availability for a list of journal-alert entries."""

    oa_cfg = (
        cfg.get(
            "open_access",
            {},
        )
        or {}
    )

    if not oa_cfg.get(
        "enabled",
        False,
    ):

        return {
            "enabled": False,
            "checked": 0,
            "oa": 0,
            "pdf": 0,
            "landing": 0,
            "failed": 0,
        }

    email = os.environ.get(
        "JALERT_MAILTO",
        "",
    ).strip()

    if not email:

        log.warning(
            (
                "Open-access lookup enabled but "
                "JALERT_MAILTO is missing"
            )
        )

        return {
            "enabled": True,
            "checked": 0,
            "oa": 0,
            "pdf": 0,
            "landing": 0,
            "failed": len(
                entries
            ),
        }

    cache: dict[
        str,
        dict,
    ] = {}

    checked = 0
    oa_count = 0
    pdf_count = 0
    landing_count = 0
    failed_count = 0

    for entry in entries:

        item = entry[
            "item"
        ]

        doi = _normalize_doi(
            getattr(
                item,
                "doi",
                "",
            )
        )

        if not doi:

            _apply_result(
                item,
                _empty_result(
                    checked=False,
                    status="no_doi",
                ),
            )

            continue

        if doi not in cache:

            cache[
                doi
            ] = lookup_doi(
                doi,
                email,
            )

        result = cache[
            doi
        ]

        _apply_result(
            item,
            result,
        )

        if result.get(
            "checked"
        ):

            checked += 1

        elif result.get(
            "error"
        ):

            failed_count += 1

        if result.get(
            "is_oa"
        ):

            oa_count += 1

        if result.get(
            "pdf_url"
        ):

            pdf_count += 1

        elif result.get(
            "oa_url"
        ):

            landing_count += 1

    log.info(
        (
            "Open-access lookup: "
            "checked=%d / "
            "oa=%d / "
            "direct_pdf=%d / "
            "oa_page=%d / "
            "failed=%d"
        ),
        checked,
        oa_count,
        pdf_count,
        landing_count,
        failed_count,
    )

    return {
        "enabled": True,
        "checked": checked,
        "oa": oa_count,
        "pdf": pdf_count,
        "landing": landing_count,
        "failed": failed_count,
    }
