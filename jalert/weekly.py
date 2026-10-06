"""Weekly research digest and Zotero BibTeX exporter.

V3 features:
1. Read the literature accumulated in state/seen.sqlite during the last N days.
2. Build a weekly Markdown research digest.
3. Export a weekly BibTeX file.
4. Export a cumulative Zotero-compatible library.bib.
5. Push a compact weekly summary through the existing push channels.

Standard-library only.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from .config import load_config
from .enrich import enrich
from . import push as push_module
from .state import Store


# ============================================================
# Logging
# ============================================================

def setup_logging(cfg: dict) -> logging.Logger:
    logger = logging.getLogger("jalert.weekly")

    level = str(
        cfg.get("log", {}).get("level", "INFO")
    ).upper()

    logger.setLevel(
        getattr(logging, level, logging.INFO)
    )

    logger.handlers.clear()

    handler = logging.StreamHandler()

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s",
            "%H:%M:%S",
        )
    )

    logger.addHandler(handler)

    return logger


# ============================================================
# Helpers
# ============================================================

def _parse_authors(raw: str | None) -> list[str]:
    if not raw:
        return []

    try:
        value = json.loads(raw)

        if isinstance(value, list):
            return [
                str(x).strip()
                for x in value
                if str(x).strip()
            ]

    except (json.JSONDecodeError, TypeError):
        pass

    return [
        x.strip()
        for x in str(raw).split(";")
        if x.strip()
    ]


def _parse_labels(raw: str | None) -> list[str]:
    if not raw:
        return []

    return [
        part.strip()
        for part in str(raw).split("/")
        if part.strip()
    ]


def _article_objects(row: dict):
    """Convert one SQLite row into Item-like and Scored-like objects."""

    item = SimpleNamespace(
        uid=row.get("uid", ""),
        doi=row.get("doi", "") or "",
        title=row.get("title", "") or "",
        abstract=row.get("abstract", "") or "",
        authors=_parse_authors(
            row.get("authors")
        ),
        journal=row.get("journal", "") or "",
        issn=row.get("issn", "") or "",
        url=row.get("url", "") or "",
        date=row.get("pub_date", "") or "",
        source=row.get("source", "") or "",
    )

    scored = SimpleNamespace(
        score=int(
            row.get("score", 0) or 0
        ),
        labels=_parse_labels(
            row.get("keywords")
        ),
    )

    return item, scored


def _article_url(item) -> str:
    if getattr(item, "url", ""):
        return item.url

    if getattr(item, "doi", ""):
        return (
            "https://doi.org/"
            + item.doi
        )

    return ""


def _week_info(today: date) -> tuple[str, date, date]:
    iso = today.isocalendar()

    week_id = (
        f"{iso.year}-W{iso.week:02d}"
    )

    monday = today - timedelta(
        days=today.weekday()
    )

    sunday = monday + timedelta(
        days=6
    )

    return (
        week_id,
        monday,
        sunday,
    )


def _tier_name(score: int, cfg: dict) -> str:
    tiers = cfg.get("tiers", {})

    must = int(
        tiers.get("must_read", 15)
    )

    worth = int(
        tiers.get("worth_reading", 8)
    )

    if score >= must:
        return "必读"

    if score >= worth:
        return "值得一读"

    return "其他相关"


# ============================================================
# BibTeX
# ============================================================

def _bib_escape(value: str) -> str:
    """Escape a conservative subset of BibTeX-sensitive characters."""

    value = str(value or "")

    value = value.replace(
        "\\",
        r"\\",
    )

    value = value.replace(
        "{",
        r"\{",
    )

    value = value.replace(
        "}",
        r"\}",
    )

    value = value.replace(
        "&",
        r"\&",
    )

    value = value.replace(
        "%",
        r"\%",
    )

    value = value.replace(
        "#",
        r"\#",
    )

    value = value.replace(
        "_",
        r"\_",
    )

    return value.strip()


def _citation_key(
    row: dict,
    used: set[str],
) -> str:

    authors = _parse_authors(
        row.get("authors")
    )

    surname = "Article"

    if authors:

        parts = re.findall(
            r"[A-Za-z0-9]+",
            authors[0],
        )

        if parts:
            surname = parts[-1]

    year = (
        str(row.get("pub_date") or "")[:4]
        or "ND"
    )

    title = str(
        row.get("title") or ""
    )

    words = re.findall(
        r"[A-Za-z0-9]+",
        title,
    )

    keyword = (
        words[0]
        if words
        else "Paper"
    )

    base = (
        f"{surname}{year}{keyword}"
    )

    base = re.sub(
        r"[^A-Za-z0-9]+",
        "",
        base,
    )

    if not base:
        base = "Article"

    key = base
    counter = 2

    while key in used:
        key = f"{base}{counter}"
        counter += 1

    used.add(key)

    return key


def build_bibtex(
    rows: list[dict],
) -> str:

    used: set[str] = set()

    blocks: list[str] = []

    seen_identity: set[str] = set()

    for row in rows:

        doi = (
            row.get("doi") or ""
        ).strip().lower()

        uid = (
            row.get("uid") or ""
        ).strip()

        identity = (
            f"doi:{doi}"
            if doi
            else uid
        )

        if identity in seen_identity:
            continue

        seen_identity.add(identity)

        key = _citation_key(
            row,
            used,
        )

        title = _bib_escape(
            row.get("title", "")
        )

        journal = _bib_escape(
            row.get("journal", "")
        )

        url = _bib_escape(
            row.get("url", "")
        )

        pub_date = str(
            row.get("pub_date") or ""
        )

        year = (
            pub_date[:4]
            if len(pub_date) >= 4
            else ""
        )

        authors = _parse_authors(
            row.get("authors")
        )

        author_text = " and ".join(
            _bib_escape(author)
            for author in authors
        )

        fields = [
            f"  title = {{{title}}}",
        ]

        if author_text:
            fields.append(
                f"  author = {{{author_text}}}"
            )

        if journal:
            fields.append(
                f"  journal = {{{journal}}}"
            )

        if year:
            fields.append(
                f"  year = {{{year}}}"
            )

        if doi:
            fields.append(
                f"  doi = {{{_bib_escape(doi)}}}"
            )

        if url:
            fields.append(
                f"  url = {{{url}}}"
            )

        block = (
            f"@article{{{key},\n"
            + ",\n".join(fields)
            + "\n}"
        )

        blocks.append(block)

    if not blocks:
        return ""

    return (
        "\n\n".join(blocks)
        + "\n"
    )


# ============================================================
# Weekly enrichment
# ============================================================

def enrich_rows(
    rows: list[dict],
) -> list[dict]:

    result = []

    for row in rows:

        item, scored = (
            _article_objects(row)
        )

        info = enrich(
            item,
            scored,
        )

        result.append(
            {
                "row": row,
                "item": item,
                "scored": scored,
                "research_tags": (
                    info.get(
                        "research_tags",
                        [],
                    )
                    or []
                ),
                "reason": (
                    info.get(
                        "relevance_reason",
                        "",
                    )
                    or ""
                ),
                "methods": (
                    info.get(
                        "methods",
                        [],
                    )
                    or []
                ),
            }
        )

    result.sort(
        key=lambda x: (
            -int(
                x["scored"].score
            ),
            x["item"].journal,
            x["item"].title,
        )
    )

    return result


# ============================================================
# Weekly Markdown
# ============================================================

def build_weekly_markdown(
    *,
    week_id: str,
    start_date: date,
    end_date: date,
    cfg: dict,
    entries: list[dict],
    generated_at: str,
) -> str:

    project = cfg.get(
        "project",
        {},
    ).get(
        "name",
        "文献雷达",
    )

    must_threshold = int(
        cfg.get(
            "tiers",
            {},
        ).get(
            "must_read",
            15,
        )
    )

    must_count = sum(
        1
        for entry in entries
        if entry["scored"].score
        >= must_threshold
    )

    tag_counter = Counter()

    method_counter = Counter()

    for entry in entries:

        for tag in entry[
            "research_tags"
        ]:
            tag_counter[tag] += 1

        for method in entry[
            "methods"
        ]:
            method_counter[
                method
            ] += 1

    lines = [
        "---",
        f"week: {week_id}",
        f"start: {start_date.isoformat()}",
        f"end: {end_date.isoformat()}",
        f"generated: {generated_at}",
        f"articles: {len(entries)}",
        f"must_read: {must_count}",
        "tags: [journal-alert, weekly-review]",
        "---",
        "",
        f"# {project} · {week_id} 周报",
        "",
        (
            f"> 周期：**{start_date.isoformat()} — "
            f"{end_date.isoformat()}**"
        ),
        (
            f"> 本周累计收录 **{len(entries)}** 篇 ｜ "
            f"必读 **{must_count}** 篇"
        ),
        "",
    ]

    # --------------------------------------------------------
    # 本周重点推荐
    # --------------------------------------------------------

    lines.extend(
        [
            "## 本周重点推荐",
            "",
        ]
    )

    top_entries = entries[:10]

    if not top_entries:

        lines.extend(
            [
                "本周暂无新收录文献。",
                "",
            ]
        )

    for index, entry in enumerate(
        top_entries,
        1,
    ):

        item = entry["item"]
        score = entry[
            "scored"
        ].score

        url = _article_url(
            item
        )

        title = (
            f"[{item.title}]({url})"
            if url
            else item.title
        )

        tags = " / ".join(
            entry["research_tags"][:3]
        )

        methods = " / ".join(
            entry["methods"][:5]
        )

        lines.append(
            f"### {index}. {title}"
        )

        lines.append("")

        lines.append(
            f"- **期刊**：{item.journal} ｜ "
            f"**日期**：{item.date or '未知'} ｜ "
            f"**得分**：{score} ｜ "
            f"**等级**：{_tier_name(score, cfg)}"
        )

        if tags:
            lines.append(
                f"- **研究用途**：{tags}"
            )

        if entry["reason"]:
            lines.append(
                f"- **相关原因**："
                f"{entry['reason']}"
            )

        if methods:
            lines.append(
                f"- **方法识别**：{methods}"
            )

        if item.doi:
            lines.append(
                f"- **DOI**："
                f"https://doi.org/{item.doi}"
            )

        lines.append("")

    # --------------------------------------------------------
    # 按研究用途分类
    # --------------------------------------------------------

    lines.extend(
        [
            "---",
            "",
            "## 按研究用途分类",
            "",
        ]
    )

    grouped: dict[str, list[dict]] = (
        defaultdict(list)
    )

    for entry in entries:

        primary_tag = (
            entry["research_tags"][0]
            if entry[
                "research_tags"
            ]
            else "相关文献"
        )

        grouped[
            primary_tag
        ].append(entry)

    group_order = sorted(
        grouped,
        key=lambda tag: (
            -max(
                x["scored"].score
                for x in grouped[tag]
            ),
            tag,
        )
    )

    for tag in group_order:

        bucket = grouped[tag]

        lines.append(
            f"### {tag}（{len(bucket)}）"
        )

        lines.append("")

        for entry in bucket[:10]:

            item = entry["item"]

            url = _article_url(
                item
            )

            title = (
                f"[{item.title}]({url})"
                if url
                else item.title
            )

            lines.append(
                f"- {title} "
                f"— {item.journal} "
                f"（{entry['scored'].score} 分）"
            )

        if len(bucket) > 10:

            lines.append(
                f"- ……另有 "
                f"{len(bucket) - 10} 篇"
            )

        lines.append("")

    # --------------------------------------------------------
    # 方法趋势
    # --------------------------------------------------------

    lines.extend(
        [
            "---",
            "",
            "## 本周方法趋势",
            "",
        ]
    )

    if method_counter:

        for method, count in (
            method_counter.most_common(
                15
            )
        ):

            lines.append(
                f"- **{method}**："
                f"{count} 篇"
            )

    else:

        lines.append(
            "本周文献标题和摘要中"
            "未识别到明确的方法学关键词。"
        )

    lines.append("")

    # --------------------------------------------------------
    # 研究方向统计
    # --------------------------------------------------------

    lines.extend(
        [
            "---",
            "",
            "## 研究方向统计",
            "",
        ]
    )

    for tag, count in (
        tag_counter.most_common()
    ):

        lines.append(
            f"- **{tag}**："
            f"{count} 篇"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            (
                "*由 journal-alert 自动生成 · "
                f"{generated_at}*"
            ),
            "",
        ]
    )

    return "\n".join(lines)


# ============================================================
# Weekly Push
# ============================================================

def build_weekly_push(
    *,
    week_id: str,
    cfg: dict,
    entries: list[dict],
) -> tuple[str, str]:

    project = cfg.get(
        "project",
        {},
    ).get(
        "name",
        "文献雷达",
    )

    must_threshold = int(
        cfg.get(
            "tiers",
            {},
        ).get(
            "must_read",
            15,
        )
    )

    must_count = sum(
        1
        for entry in entries
        if entry[
            "scored"
        ].score
        >= must_threshold
    )

    title = (
        f"{project} {week_id}："
        f"本周 {len(entries)} 篇，"
        f"必读 {must_count} 篇"
    )

    lines = [
        (
            f"本周收录 **{len(entries)}** 篇，"
            f"其中必读 **{must_count}** 篇。"
        ),
        "",
        "**优先阅读：**",
        "",
    ]

    for index, entry in enumerate(
        entries[:5],
        1,
    ):

        item = entry["item"]

        url = _article_url(
            item
        )

        if url:

            head = (
                f"{index}. "
                f"[{item.title}]({url})"
            )

        else:

            head = (
                f"{index}. "
                f"{item.title}"
            )

        lines.append(head)

        if entry[
            "research_tags"
        ]:

            lines.append(
                "　用途："
                + " / ".join(
                    entry[
                        "research_tags"
                    ][:2]
                )
            )

        lines.append("")

    lines.append(
        "完整分类、方法趋势和 Zotero "
        "文献库见仓库 weekly/ 与 zotero/。"
    )

    return (
        title,
        "\n".join(lines),
    )


# ============================================================
# Main
# ============================================================

def run(
    *,
    config_path: str | None = None,
    days: int = 7,
    no_push: bool = False,
) -> int:

    cfg = load_config(
        config_path
    )

    log = setup_logging(
        cfg
    )

    root = Path(
        cfg["_root"]
    )

    weekly_dir = (
        root / "weekly"
    )

    zotero_dir = (
        root / "zotero"
    )

    weekly_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    zotero_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    today = date.today()

    (
        week_id,
        week_start,
        week_end,
    ) = _week_info(
        today
    )

    with Store(
        cfg["state_db"]
    ) as store:

        recent = store.recent_articles(
            days=days
        )

        all_rows = store.all_articles()

    entries = enrich_rows(
        recent
    )

    generated_at = datetime.now().strftime(
        "%Y-%m-%d %H:%M"
    )

    # --------------------------------------------------------
    # Markdown 周报
    # --------------------------------------------------------

    markdown = build_weekly_markdown(
        week_id=week_id,
        start_date=week_start,
        end_date=week_end,
        cfg=cfg,
        entries=entries,
        generated_at=generated_at,
    )

    weekly_path = (
        weekly_dir
        / f"{week_id}.md"
    )

    weekly_path.write_text(
        markdown,
        encoding="utf-8",
        newline="\n",
    )

    log.info(
        "weekly report written: %s",
        weekly_path,
    )

    # --------------------------------------------------------
    # 本周 Zotero BibTeX
    # --------------------------------------------------------

    weekly_bib = build_bibtex(
        recent
    )

    weekly_bib_path = (
        zotero_dir
        / f"{week_id}.bib"
    )

    weekly_bib_path.write_text(
        weekly_bib,
        encoding="utf-8",
        newline="\n",
    )

    log.info(
        "weekly BibTeX written: %s",
        weekly_bib_path,
    )

    # --------------------------------------------------------
    # 累积 Zotero 文献库
    # --------------------------------------------------------

    library_bib = build_bibtex(
        all_rows
    )

    library_path = (
        zotero_dir
        / "library.bib"
    )

    library_path.write_text(
        library_bib,
        encoding="utf-8",
        newline="\n",
    )

    log.info(
        "cumulative BibTeX written: %s",
        library_path,
    )

    # --------------------------------------------------------
    # 微信周报推送
    # --------------------------------------------------------

    if (
        not no_push
        and cfg.get(
            "push",
            {},
        ).get(
            "enabled",
            False,
        )
    ):

        title, body = build_weekly_push(
            week_id=week_id,
            cfg=cfg,
            entries=entries,
        )

        results = push_module.dispatch(
            cfg,
            title,
            body,
            log,
        )

        log.info(
            "weekly push results: %s",
            json.dumps(
                results,
                ensure_ascii=False,
            ),
        )

    log.info(
        "weekly complete: %d recent / %d cumulative articles",
        len(recent),
        len(all_rows),
    )

    return 0


def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Generate weekly literature digest "
            "and Zotero BibTeX library"
        )
    )

    parser.add_argument(
        "--config",
        help="path to config.json",
    )

    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="number of recent days to include",
    )

    parser.add_argument(
        "--no-push",
        action="store_true",
        help="generate files without push notification",
    )

    args = parser.parse_args()

    return run(
        config_path=args.config,
        days=max(
            1,
            args.days,
        ),
        no_push=args.no_push,
    )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
