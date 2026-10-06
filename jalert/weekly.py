"""Weekly research digest and Zotero BibTeX exporter.

V3.1:
1. Read recent literature from state/seen.sqlite.
2. Re-score every article with the CURRENT config.json + score.py.
3. Build weekly Markdown research digest.
4. Export weekly and cumulative Zotero BibTeX.
5. Push weekly summary through existing push channels.

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

from . import push as push_module
from .config import load_config
from .enrich import enrich
from .score import Scorer, tier_of
from .state import Store


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


def _article_from_row(row: dict):
    return SimpleNamespace(
        uid=row.get("uid", "") or "",
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


def _article_url(item) -> str:
    if getattr(item, "url", ""):
        return item.url

    if getattr(item, "doi", ""):
        return f"https://doi.org/{item.doi}"

    return ""


def _week_info(
    today: date,
) -> tuple[str, date, date]:

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

    return week_id, monday, sunday


def _tier_name(
    score: int,
    cfg: dict,
) -> str:

    tier = tier_of(
        score,
        cfg.get("tiers", {}),
    )

    return {
        "must_read": "必读",
        "worth_reading": "值得一读",
        "other": "其他相关",
    }.get(
        tier,
        "其他相关",
    )


# ============================================================
# 当前规则重新评分
# ============================================================

def rescore_rows(
    rows: list[dict],
    cfg: dict,
) -> list[dict]:

    """Re-score historical articles using CURRENT scoring rules."""

    scorer = Scorer(
        cfg.get("keywords", []),
        cfg.get("exclude_terms", []),
        cfg.get(
            "exclude_doi_prefixes",
            [],
        ),
    )

    min_score = int(
        cfg.get(
            "tiers",
            {},
        ).get(
            "min_score",
            5,
        )
    )

    result: list[dict] = []

    for row in rows:

        item = _article_from_row(
            row
        )

        scored = scorer.score(
            item
        )

        # 如果按照当前规则已不再达到最低收录标准，
        # 就不进入当前周报推荐。
        if (
            scored.excluded
            or scored.score < min_score
        ):
            continue

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
                "old_score": int(
                    row.get("score", 0)
                    or 0
                ),
            }
        )

    result.sort(
        key=lambda x: (
            {
                "must_read": 0,
                "worth_reading": 1,
                "other": 2,
            }.get(
                tier_of(
                    x["scored"].score,
                    cfg.get(
                        "tiers",
                        {},
                    ),
                ),
                3,
            ),
            -x["scored"].score,
            x["item"].journal,
            x["item"].title,
        )
    )

    return result


# ============================================================
# BibTeX
# ============================================================

def _bib_escape(
    value: str,
) -> str:

    value = str(
        value or ""
    )

    replacements = (
        ("\\", r"\\"),
        ("{", r"\{"),
        ("}", r"\}"),
        ("&", r"\&"),
        ("%", r"\%"),
        ("#", r"\#"),
        ("_", r"\_"),
    )

    for old, new in replacements:
        value = value.replace(
            old,
            new,
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
        str(
            row.get("pub_date")
            or ""
        )[:4]
        or "ND"
    )

    title = str(
        row.get("title")
        or ""
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

    base = re.sub(
        r"[^A-Za-z0-9]+",
        "",
        f"{surname}{year}{keyword}",
    )

    if not base:
        base = "Article"

    key = base
    counter = 2

    while key in used:
        key = (
            f"{base}{counter}"
        )
        counter += 1

    used.add(key)

    return key


def build_bibtex(
    rows: list[dict],
) -> str:

    used: set[str] = set()
    seen_identity: set[str] = set()
    blocks: list[str] = []

    for row in rows:

        doi = (
            row.get("doi")
            or ""
        ).strip().lower()

        uid = (
            row.get("uid")
            or ""
        ).strip()

        identity = (
            f"doi:{doi}"
            if doi
            else uid
        )

        if identity in seen_identity:
            continue

        seen_identity.add(
            identity
        )

        key = _citation_key(
            row,
            used,
        )

        title = _bib_escape(
            row.get(
                "title",
                "",
            )
        )

        journal = _bib_escape(
            row.get(
                "journal",
                "",
            )
        )

        url = _bib_escape(
            row.get(
                "url",
                "",
            )
        )

        pub_date = str(
            row.get(
                "pub_date",
                "",
            )
            or ""
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
                f"  author = "
                f"{{{author_text}}}"
            )

        if journal:
            fields.append(
                f"  journal = "
                f"{{{journal}}}"
            )

        if year:
            fields.append(
                f"  year = "
                f"{{{year}}}"
            )

        if doi:
            fields.append(
                f"  doi = "
                f"{{{_bib_escape(doi)}}}"
            )

        if url:
            fields.append(
                f"  url = "
                f"{{{url}}}"
            )

        blocks.append(
            f"@article{{{key},\n"
            + ",\n".join(fields)
            + "\n}"
        )

    if not blocks:
        return ""

    return (
        "\n\n".join(blocks)
        + "\n"
    )


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
    raw_count: int,
    generated_at: str,
) -> str:

    project = cfg.get(
        "project",
        {},
    ).get(
        "name",
        "文献雷达",
    )

    counts = {
        "must_read": 0,
        "worth_reading": 0,
        "other": 0,
    }

    for entry in entries:

        tier = tier_of(
            entry["scored"].score,
            cfg.get(
                "tiers",
                {},
            ),
        )

        counts[tier] += 1

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
        f"historical_articles: {raw_count}",
        f"articles: {len(entries)}",
        f"must_read: {counts['must_read']}",
        f"worth_reading: {counts['worth_reading']}",
        f"other: {counts['other']}",
        "rescored: true",
        "tags: [journal-alert, weekly-review]",
        "---",
        "",
        f"# {project} · {week_id} 周报",
        "",
        (
            f"> 周期：**"
            f"{start_date.isoformat()} — "
            f"{end_date.isoformat()}**"
        ),
        (
            f"> 历史库近 7 天共 **{raw_count}** 篇 ｜ "
            f"按当前规则重新筛选后 **{len(entries)}** 篇"
        ),
        (
            f"> 必读 **{counts['must_read']}** ｜ "
            f"值得一读 **{counts['worth_reading']}** ｜ "
            f"其他相关 **{counts['other']}**"
        ),
        (
            "> 注：周报使用当前 "
            "`config.json + score.py` "
            "重新评分，不沿用历史数据库旧分数。"
        ),
        "",
        "## 本周重点推荐",
        "",
    ]

    top_entries = entries[:10]

    if not top_entries:

        lines.extend(
            [
                "本周按当前规则暂无推荐文献。",
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

        old_score = entry[
            "old_score"
        ]

        url = _article_url(
            item
        )

        title = (
            f"[{item.title}]({url})"
            if url
            else item.title
        )

        tags = " / ".join(
            entry[
                "research_tags"
            ][:3]
        )

        methods = " / ".join(
            entry[
                "methods"
            ][:5]
        )

        lines.append(
            f"### {index}. {title}"
        )

        lines.append("")

        lines.append(
            f"- **期刊**：{item.journal} ｜ "
            f"**日期**：{item.date or '未知'} ｜ "
            f"**当前得分**：{score} ｜ "
            f"**等级**："
            f"{_tier_name(score, cfg)}"
        )

        if old_score != score:

            lines.append(
                f"- **历史得分**：{old_score} → "
                f"**当前规则得分**：{score}"
            )

        if tags:

            lines.append(
                f"- **研究用途**："
                f"{tags}"
            )

        if entry["reason"]:

            lines.append(
                f"- **相关原因**："
                f"{entry['reason']}"
            )

        if methods:

            lines.append(
                f"- **方法识别**："
                f"{methods}"
            )

        if item.doi:

            lines.append(
                "- **DOI**："
                f"https://doi.org/"
                f"{item.doi}"
            )

        lines.append("")

    lines.extend(
        [
            "---",
            "",
            "## 按研究用途分类",
            "",
        ]
    )

    grouped: dict[
        str,
        list[dict],
    ] = defaultdict(list)

    for entry in entries:

        primary_tag = (
            entry[
                "research_tags"
            ][0]
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

        bucket = grouped[
            tag
        ]

        lines.append(
            f"### {tag}（{len(bucket)}）"
        )

        lines.append("")

        for entry in bucket[:10]:

            item = entry[
                "item"
            ]

            url = _article_url(
                item
            )

            title = (
                f"[{item.title}]({url})"
                if url
                else item.title
            )

            lines.append(
                f"- {title} — "
                f"{item.journal} "
                f"（{entry['scored'].score} 分）"
            )

        if len(bucket) > 10:

            lines.append(
                f"- ……另有 "
                f"{len(bucket) - 10} 篇"
            )

        lines.append("")

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
            "暂未识别到明确的方法学关键词。"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 研究方向统计",
            "",
        ]
    )

    if tag_counter:

        for tag, count in (
            tag_counter.most_common()
        ):

            lines.append(
                f"- **{tag}**："
                f"{count} 篇"
            )

    else:

        lines.append(
            "暂无研究用途统计。"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            (
                "*由 journal-alert 自动生成 · "
                f"{generated_at} · "
                "周报采用当前评分规则重新计算*"
            ),
            "",
        ]
    )

    return "\n".join(lines)


# ============================================================
# Weekly push
# ============================================================

def build_weekly_push(
    *,
    week_id: str,
    cfg: dict,
    entries: list[dict],
    raw_count: int,
) -> tuple[str, str]:

    project = cfg.get(
        "project",
        {},
    ).get(
        "name",
        "文献雷达",
    )

    must_count = sum(
        1
        for entry in entries
        if tier_of(
            entry[
                "scored"
            ].score,
            cfg.get(
                "tiers",
                {},
            ),
        )
        == "must_read"
    )

    title = (
        f"{project} {week_id}："
        f"精选 {len(entries)} 篇，"
        f"必读 {must_count} 篇"
    )

    lines = [
        (
            f"历史库近 7 天 **{raw_count}** 篇，"
            f"按当前规则重新筛选后 "
            f"**{len(entries)}** 篇。"
        ),
        "",
        "**优先阅读：**",
        "",
    ]

    for index, entry in enumerate(
        entries[:5],
        1,
    ):

        item = entry[
            "item"
        ]

        url = _article_url(
            item
        )

        if url:

            lines.append(
                f"{index}. "
                f"[{item.title}]({url})"
            )

        else:

            lines.append(
                f"{index}. "
                f"{item.title}"
            )

        tags = entry[
            "research_tags"
        ]

        if tags:

            lines.append(
                "　用途："
                + " / ".join(
                    tags[:2]
                )
            )

        lines.append(
            "　当前得分："
            f"{entry['scored'].score}"
        )

        lines.append("")

    lines.append(
        "完整周报、方法趋势和 Zotero "
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

        recent_rows = (
            store.recent_articles(
                days=days
            )
        )

        all_rows = (
            store.all_articles()
        )

    # 关键变化：
    # 周报使用当前规则重新评分。
    entries = rescore_rows(
        recent_rows,
        cfg,
    )

    generated_at = (
        datetime.now().strftime(
            "%Y-%m-%d %H:%M"
        )
    )

    markdown = build_weekly_markdown(
        week_id=week_id,
        start_date=week_start,
        end_date=week_end,
        cfg=cfg,
        entries=entries,
        raw_count=len(
            recent_rows
        ),
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
    # Zotero 本周精准导出
    # --------------------------------------------------------
    # entries 已经是按照“当前 config.json + score.py”
    # 重新评分并筛选后的本周文献。
    # 因此只导出当前仍然相关的文献。

    weekly_selected_rows = [
        entry["row"]
        for entry in entries
    ]

    weekly_bib = build_bibtex(
        weekly_selected_rows
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
        "weekly BibTeX written: %s (%d selected articles)",
        weekly_bib_path,
        len(weekly_selected_rows),
    )

    # --------------------------------------------------------
    # Zotero 累积文献库精准导出
    # --------------------------------------------------------
    # seen.sqlite 保留全部历史记录，
    # 但 library.bib 只输出按“当前规则”仍然相关的文献。

    library_entries = rescore_rows(
        all_rows,
        cfg,
    )

    library_selected_rows = [
        entry["row"]
        for entry in library_entries
    ]

    library_bib = build_bibtex(
        library_selected_rows
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
        "cumulative BibTeX written: %s (%d selected / %d historical)",
        library_path,
        len(library_selected_rows),
        len(all_rows),
    )

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
            raw_count=len(
                recent_rows
            ),
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
        (
            "weekly complete: "
            "%d weekly historical / "
            "%d weekly selected / "
            "%d library selected / "
            "%d library historical"
        ),
        len(recent_rows),
        len(entries),
        len(library_selected_rows),
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
