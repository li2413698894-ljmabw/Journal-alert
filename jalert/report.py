"""Markdown report rendering."""

from __future__ import annotations

import json
from datetime import date as _date

from .enrich import enrich
from .score import TIER_TITLES, tier_of

TIER_ORDER = ["must_read", "worth_reading", "other"]


def _fmt_date(value: str) -> str:
    """Elsevier-style feeds may use a future issue date."""
    if not value:
        return "未知"

    if value > _date.today().isoformat():
        return f"{value}（在线预发表）"

    return value


def _link(item) -> str:
    if getattr(item, "url", ""):
        return item.url

    if getattr(item, "doi", ""):
        return f"https://doi.org/{item.doi}"

    return ""


def _doi_link(doi: str) -> str:
    if not doi:
        return "—"

    return f"[{doi}](https://doi.org/{doi})"


def _oa_markdown(
    item,
) -> str:

    pdf_url = getattr(
        item,
        "oa_pdf_url",
        "",
    ) or ""

    oa_url = getattr(
        item,
        "oa_url",
        "",
    ) or ""

    checked = bool(
        getattr(
            item,
            "oa_checked",
            False,
        )
    )

    version = getattr(
        item,
        "oa_version",
        "",
    ) or ""

    host_type = getattr(
        item,
        "oa_host_type",
        "",
    ) or ""

    if pdf_url:

        suffix = []

        if version:
            suffix.append(
                version
            )

        if host_type:
            suffix.append(
                host_type
            )

        extra = (
            " ｜ "
            + " / ".join(
                suffix
            )
            if suffix
            else ""
        )

        return (
            "- **全文**："
            f"✅ [免费 PDF]({pdf_url})"
            f"{extra}"
        )

    if oa_url:

        return (
            "- **全文**："
            f"🟢 [开放全文页面]({oa_url})"
        )

    if checked:

        return (
            "- **全文**："
            "🔒 暂未发现合法开放全文"
        )

    return ""

    return f"[{doi}](https://doi.org/{doi})"


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()

    if len(text) <= limit:
        return text

    cut = text[:limit].rsplit(" ", 1)[0]

    if not cut:
        cut = text[:limit]

    return cut + " …"


def _frontmatter(
    *,
    day: str,
    cfg: dict,
    counts: dict,
    statuses: list[dict],
    fetched_total: int,
    matched_total: int,
    new_count: int,
    listed: int,
    generated_at: str,
) -> list[str]:

    topics = [
        k.get("label", "")
        for k in cfg.get("keywords", [])
    ]

    journals = {
        s["journal"]
        for s in statuses
    }

    failed = [
        s
        for s in statuses
        if not s["ok"]
    ]

    return [
        "---",
        f"date: {day}",
        f"generated: {generated_at}",
        f"window_days: {cfg.get('window', {}).get('days', 3)}",
        f"fetched: {fetched_total}",
        f"matched: {matched_total}",
        f"new_count: {new_count}",
        f"listed: {listed}",
        f"must_read: {counts['must_read']}",
        f"worth_reading: {counts['worth_reading']}",
        f"other: {counts['other']}",
        f"journals: {len(journals)}",
        f"sources_failed: {len(failed)}",
        f"topics: {json.dumps(topics, ensure_ascii=False)}",
        "tags: [journal-alert]",
        "---",
        "",
    ]


def _source_status_section(
    statuses: list[dict],
    mode: str,
) -> list[str]:

    failed = [
        s
        for s in statuses
        if not s["ok"]
    ]

    if mode == "none":
        return []

    if mode == "summary":

        if not statuses:
            return []

        journals = len({
            s["journal"]
            for s in statuses
        })

        if not failed:
            return [
                (
                    f"> ✅ 数据源：{journals} 本刊、"
                    f"{len(statuses)} 条通道全部正常。"
                ),
                "",
            ]

        out = [
            "## 数据源状态",
            "",
            (
                f"> ⚠️ {journals} 本刊中 "
                f"**{len(failed)}** 条通道本次抓取失败，"
                "其余正常；失败不影响其他来源。"
            ),
            "",
            "| 期刊 | 数据源 | 条数 | 说明 |",
            "|---|---|---|---|",
        ]

        for status in failed:

            note = (
                status.get("note") or ""
            ).replace("|", "/")

            out.append(
                f"| {status['journal']} | "
                f"{status['source']} | "
                f"{status['items']} | "
                f"{note} |"
            )

        out.append("")

        return out

    out = [
        "## 数据源状态",
        "",
        "| 期刊 | 数据源 | 状态 | 条数 | 说明 |",
        "|---|---|---|---|---|",
    ]

    for status in statuses:

        state = "✅" if status["ok"] else "❌"

        note = (
            status.get("note") or ""
        ).replace("|", "/")

        out.append(
            f"| {status['journal']} | "
            f"{status['source']} | "
            f"{state} | "
            f"{status['items']} | "
            f"{note} |"
        )

    out.append("")

    if failed:
        out.append(
            f"⚠️ 有 {len(failed)} 个数据源本次抓取失败，"
            "上面表格已标明原因；失败不影响其他来源。"
        )
    else:
        out.append(
            "✅ 全部数据源抓取正常。"
        )

    out.append("")

    return out


def build_markdown(
    *,
    day: str,
    cfg: dict,
    entries: list[dict],
    statuses: list[dict],
    fetched_total: int,
    matched_total: int,
    new_count: int = 0,
    seen_before: int = 0,
    generated_at: str,
    entries_total: int | None = None,
    all_entries: list[dict] | None = None,
) -> str:

    project = cfg.get(
        "project",
        {},
    ).get(
        "name",
        "文献日报",
    )

    counted = (
        all_entries
        if all_entries is not None
        else entries
    )

    counts = {
        tier: 0
        for tier in TIER_ORDER
    }

    for entry in counted:

        tier = tier_of(
            entry["scored"].score,
            cfg.get("tiers", {}),
        )

        counts[tier] += 1

    keyword_labels = " / ".join(
        k.get("label", "")
        for k in cfg.get(
            "keywords",
            [],
        )
    )

    lines: list[str] = []

    if cfg.get(
        "output",
        {},
    ).get(
        "frontmatter",
        True,
    ):

        lines.extend(
            _frontmatter(
                day=day,
                cfg=cfg,
                counts=counts,
                statuses=statuses,
                fetched_total=fetched_total,
                matched_total=matched_total,
                new_count=new_count,
                listed=len(entries),
                generated_at=generated_at,
            )
        )

    lines.append(
        f"# {project} · {day}"
    )

    lines.append("")

    lines.append(
        f"> 本次运行：抓取 **{fetched_total}** 篇 ｜ "
        f"关键词命中 **{matched_total}** 篇 ｜ "
        f"新增 **{new_count}** 篇"
        f"（其中 {seen_before} 篇此前已读过）"
    )

    if (
        entries_total is not None
        and entries_total > len(entries)
    ):

        lines.append(
            f"> 命中 **{entries_total}** 篇，"
            f"按重要度只列出前 **{len(entries)}** 篇"
            f"（其余 {entries_total - len(entries)} 篇"
            "仍留在历史库）"
        )

    else:

        lines.append(
            f"> 本日报累计收录 **{len(entries)}** 篇"
            "（当天多次运行会自动合并，不重复推送）"
        )

    capped = (
        entries_total is not None
        and entries_total > len(entries)
    )

    lines.append(
        f"> 必读 **{counts['must_read']}** ｜ "
        f"值得一读 **{counts['worth_reading']}** ｜ "
        f"其他相关 **{counts['other']}**"
        + (
            "（按全部命中统计）"
            if capped
            else ""
        )
    )

    lines.append(
        f"> 关注方向：{keyword_labels}"
    )

    lines.append(
        f"> 时间窗：近 "
        f"{cfg.get('window', {}).get('days', 3)} 天 ｜ "
        f"生成时间：{generated_at}"
    )

    lines.append("")

    if not entries:

        lines.append(
            "## 今日无新增命中"
        )

        lines.append("")

        lines.append(
            "所有抓取到的文献都已在历史记录中出现过，"
            "或没有文献同时满足关键词与时间窗条件。"
        )

        lines.append("")

    for tier in TIER_ORDER:

        bucket = [
            entry
            for entry in entries
            if tier_of(
                entry["scored"].score,
                cfg.get("tiers", {}),
            )
            == tier
        ]

        if not bucket:
            continue

        lines.append(
            f"## {TIER_TITLES[tier]}（{len(bucket)}）"
        )

        lines.append("")

        for index, entry in enumerate(
            bucket,
            1,
        ):

            item = entry["item"]
            scored = entry["scored"]

            info = enrich(
                item,
                scored,
            )

            research_tags = (
                info.get(
                    "research_tags",
                    [],
                )
                or []
            )

            methods = (
                info.get(
                    "methods",
                    [],
                )
                or []
            )

            reason = (
                info.get(
                    "relevance_reason",
                    "",
                )
                or ""
            )

            url = _link(item)

            heading = (
                f"[{item.title}]({url})"
                if url
                else item.title
            )

            lines.append(
                f"### {index}. {heading}"
            )

            lines.append("")

            meta = [
                f"**期刊**：{item.journal}",
                f"**日期**：{_fmt_date(item.date)}",
            ]

            if item.doi:

                meta.append(
                    f"**DOI**：{_doi_link(item.doi)}"
                )

            lines.append(
                "- " + " ｜ ".join(meta)
            )

            if research_tags:

                lines.append(
                    "- **研究用途**："
                    + " / ".join(
                        research_tags
                    )
                )

            if reason:

                lines.append(
                    f"- **相关原因**：{reason}"
                )

            if methods:

                lines.append(
                    "- **方法识别**："
                    + " / ".join(
                        methods
                    )
                )

            else:

                lines.append(
                    "- **方法识别**："
                    "标题和摘要中暂未识别到明确方法"
                )
            oa_line = _oa_markdown(
                item
            )

            if oa_line:
                lines.append(
                    oa_line
                )

            hits = "、".join(
                (
                    f"{m.label}"
                    f"（"
                    f"{'标题' if m.field_name == 'title' else '摘要'}"
                    f"：{m.term}"
                    f"）"
                )
                for m in scored.matches
            )

            lines.append(
                f"- **匹配**：{hits} ｜ "
                f"**得分**：{scored.score}"
            )

            if item.authors:

                lines.append(
                    f"- **作者**："
                    f"{', '.join(item.authors[:6])}"
                    f"{' 等' if len(item.authors) > 6 else ''}"
                )

            if item.abstract:

                lines.append("")

                lines.append(
                    f"> {_truncate(item.abstract, 700)}"
                )

            lines.append("")

        lines.append("---")

        lines.append("")

    mode = str(
        cfg.get(
            "output",
            {},
        ).get(
            "source_status",
            "full",
        )
    ).lower()

    if mode not in (
        "full",
        "summary",
        "none",
    ):
        mode = "full"

    lines.extend(
        _source_status_section(
            statuses,
            mode,
        )
    )

    lines.append("---")

    lines.append("")

    lines.append(
        f"*由 journal-alert 自动生成 · "
        f"{generated_at} · "
        "历史库 `state/seen.sqlite`*"
    )

    lines.append("")

    return "\n".join(lines)


def build_digest(
    *,
    day: str,
    cfg: dict,
    entries: list[dict],
    fetched_total: int,
    matched_total: int,
    max_items: int,
    max_chars: int,
) -> tuple[str, str]:

    project = cfg.get(
        "project",
        {},
    ).get(
        "name",
        "文献日报",
    )

    must = [
        entry
        for entry in entries
        if tier_of(
            entry["scored"].score,
            cfg.get("tiers", {}),
        )
        == "must_read"
    ]

    must_ids = {
        id(entry)
        for entry in must
    }

    rest = [
        entry
        for entry in entries
        if id(entry) not in must_ids
    ]

    title = (
        f"{project} {day}："
        f"新增 {len(entries)} 篇，"
        f"必读 {len(must)} 篇"
    )

    blocks: list[str] = []

    shown = 0

    for entry in (
        must + rest
    ):

        if shown >= max_items:
            break

        item = entry["item"]
        scored = entry["scored"]

        info = enrich(
            item,
            scored,
        )

        tags = (
            info.get(
                "research_tags",
                [],
            )
            or []
        )

        methods = (
            info.get(
                "methods",
                [],
            )
            or []
        )

        url = _link(item)

        star = (
            "🔥"
            if id(entry) in must_ids
            else "·"
        )

        if url:

            head = (
                f"{star} "
                f"[{_truncate(item.title, 110)}]"
                f"({url})"
            )

        else:

            head = (
                f"{star} "
                f"{_truncate(item.title, 110)}"
            )

        detail_lines = []

        if tags:

            detail_lines.append(
                "用途 "
                + " / ".join(
                    tags[:2]
                )
            )

        if methods:

            detail_lines.append(
                "方法 "
                + " / ".join(
                    methods[:3]
                )
            )

        detail = (
            " · ".join(
                detail_lines
            )
            if detail_lines
            else "相关主题"
        )

        meta = (
            f"　　{item.journal} · "
            f"{item.date}  \n"
            f"　　{detail}"
        )

        blocks.append(
            f"{head}  \n{meta}"
        )

        shown += 1

    lines = [
        (
            f"抓取 {fetched_total} 篇 · "
            f"命中 {matched_total} 篇 · "
            f"新增 **{len(entries)}** 篇"
        )
    ]

    for block in blocks:

        lines.append("")

        lines.append(block)

    if len(entries) > shown:

        lines.append("")

        lines.append(
            f"…… 其余 "
            f"{len(entries) - shown} 篇"
            "见完整日报。"
        )

    body = "\n".join(lines)

    if len(body) > max_chars:

        cut = (
            body[:max_chars]
            .rsplit(
                "\n\n",
                1,
            )[0]
        )

        if not cut:

            cut = (
                body[:max_chars]
                .rsplit(
                    "\n",
                    1,
                )[0]
            )

        body = (
            cut
            + "\n\n……（已截断，完整内容见 Markdown 日报）"
        )

    return title, body
