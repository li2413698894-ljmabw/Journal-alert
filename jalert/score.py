"""Keyword scoring: transparent, explainable relevance ranking.

V2 adds two ideas on top of the original keyword score:
1. Cross-topic bonuses for combinations that are especially relevant.
2. A weak-only cap so broad topics cannot enter the report by themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TITLE_MULTIPLIER = 3
ABSTRACT_MULTIPLIER = 1

# 如果一篇文章只命中下面这些宽泛主题，
# 无论标题命中多少，最终最高只给 4 分。
# 当前 config.json 的最低收录阈值是 5，因此不会进入日报。
WEAK_ONLY_CAP = 4

WEAK_ONLY_LABELS = frozenset(
    {
        "黑土与Mollisol",
        "土壤水分与土层",
        "土壤微生物",
        "机器学习",
        "机器学习算法",
        "可解释机器学习",
        "模型验证与泛化",
        "迁移学习与域适应",
        "遥感",
    }
)

# 高价值研究主题组合。
# 格式：
# 名称, 必须同时命中的关键词组, 额外得分
COMBO_RULES: tuple[tuple[str, frozenset[str], int], ...] = (
    (
        "冻融 + 无机氮",
        frozenset({"冻融过程", "无机氮"}),
        8,
    ),
    (
        "冻融 + 氮转化",
        frozenset({"冻融过程", "氮转化"}),
        7,
    ),
    (
        "冻融 + 黑土",
        frozenset({"冻融过程", "黑土与Mollisol"}),
        4,
    ),
    (
        "冻融 + 土壤水分",
        frozenset({"冻融过程", "土壤水分与土层"}),
        3,
    ),
    (
        "黑土 + 无机氮",
        frozenset({"黑土与Mollisol", "无机氮"}),
        5,
    ),
    (
        "黑土 + 氮转化",
        frozenset({"黑土与Mollisol", "氮转化"}),
        4,
    ),
    (
        "黑土 + 土壤总氮",
        frozenset({"黑土与Mollisol", "土壤总氮与氮储量"}),
        5,
    ),
    (
        "土壤总氮 + 机器学习",
        frozenset({"土壤总氮与氮储量", "机器学习"}),
        6,
    ),
    (
        "土壤总氮 + 机器学习算法",
        frozenset({"土壤总氮与氮储量", "机器学习算法"}),
        5,
    ),
    (
        "土壤总氮 + 可解释机器学习",
        frozenset({"土壤总氮与氮储量", "可解释机器学习"}),
        6,
    ),
    (
        "数字土壤制图 + 机器学习",
        frozenset({"数字土壤制图", "机器学习"}),
        5,
    ),
    (
        "数字土壤制图 + 机器学习算法",
        frozenset({"数字土壤制图", "机器学习算法"}),
        4,
    ),
    (
        "模型验证 + 土壤总氮",
        frozenset({"模型验证与泛化", "土壤总氮与氮储量"}),
        4,
    ),
    (
        "模型验证 + 数字土壤制图",
        frozenset({"模型验证与泛化", "数字土壤制图"}),
        4,
    ),
    (
        "迁移学习 + 土壤总氮",
        frozenset({"迁移学习与域适应", "土壤总氮与氮储量"}),
        5,
    ),
    (
        "遥感 + 土壤总氮",
        frozenset({"遥感", "土壤总氮与氮储量"}),
        4,
    ),
    (
        "遥感 + 数字土壤制图",
        frozenset({"遥感", "数字土壤制图"}),
        4,
    ),
    (
        "土壤微生物 + 氮转化",
        frozenset({"土壤微生物", "氮转化"}),
        4,
    ),
    (
        "土壤水分 + 无机氮",
        frozenset({"土壤水分与土层", "无机氮"}),
        3,
    ),
)


def _pattern(term: str) -> re.Pattern:
    """Build a case-insensitive, alphanumeric-boundary matcher for term."""
    escaped = re.escape(term.strip())
    return re.compile(
        r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])",
        re.IGNORECASE,
    )


@dataclass
class Match:
    label: str
    term: str
    field_name: str
    points: int


@dataclass
class Scored:
    score: int = 0
    matches: list[Match] = field(default_factory=list)
    excluded: bool = False
    exclude_term: str = ""

    @property
    def labels(self) -> list[str]:
        seen: list[str] = []
        for match in self.matches:
            if match.label not in seen:
                seen.append(match.label)
        return seen

    @property
    def title_hits(self) -> list[str]:
        return [
            m.label
            for m in self.matches
            if m.field_name == "title"
        ]


class Scorer:
    def __init__(
        self,
        keywords: list[dict],
        exclude_terms: list[str],
        exclude_doi_prefixes: list[str],
    ):
        self.groups = []

        for entry in keywords:
            label = (
                entry.get("label")
                or entry.get("term")
                or "keyword"
            )

            weight = int(entry.get("weight", 3))

            terms = (
                entry.get("terms")
                or ([entry["term"]] if entry.get("term") else [])
            )

            compiled = [
                (term, _pattern(term))
                for term in terms
                if term and term.strip()
            ]

            if compiled:
                self.groups.append(
                    (label, weight, compiled)
                )

        self.excludes = [
            (term, _pattern(term))
            for term in exclude_terms
            if term and term.strip()
        ]

        self.exclude_doi_prefixes = [
            prefix.lower()
            for prefix in exclude_doi_prefixes
            if prefix
        ]

    def score(self, item) -> Scored:
        title = item.title or ""
        abstract = item.abstract or ""

        result = Scored()

        # DOI 排除规则
        for prefix in self.exclude_doi_prefixes:
            if (item.doi or "").lower().startswith(prefix):
                result.excluded = True
                result.exclude_term = f"doi:{prefix}"
                return result

        # 文本排除规则
        haystack = f"{title} {abstract}"

        for term, pattern in self.excludes:
            if pattern.search(haystack):
                result.excluded = True
                result.exclude_term = term
                return result

        # 基础关键词评分
        for label, weight, terms in self.groups:
            hit = None

            # 优先检查标题
            for term, pattern in terms:
                if pattern.search(title):
                    hit = (
                        term,
                        "title",
                        weight * TITLE_MULTIPLIER,
                    )
                    break

            # 标题没命中，再检查摘要
            if hit is None:
                for term, pattern in terms:
                    if pattern.search(abstract):
                        hit = (
                            term,
                            "abstract",
                            weight * ABSTRACT_MULTIPLIER,
                        )
                        break

            if hit is not None:
                term, field_name, points = hit

                result.matches.append(
                    Match(
                        label=label,
                        term=term,
                        field_name=field_name,
                        points=points,
                    )
                )

                result.score += points

        labels = set(result.labels)

        # --------------------------------------------------
        # V2：研究主题组合额外加分
        # --------------------------------------------------
        for _, required_labels, bonus in COMBO_RULES:
            if required_labels.issubset(labels):
                result.score += bonus

        # --------------------------------------------------
        # V2：宽泛主题单独命中时降级
        # --------------------------------------------------
        if labels and labels.issubset(WEAK_ONLY_LABELS):
            result.score = min(
                result.score,
                WEAK_ONLY_CAP,
            )

        return result


def tier_of(score: int, tiers: dict) -> str:
    if score >= int(tiers.get("must_read", 12)):
        return "must_read"

    if score >= int(tiers.get("worth_reading", 6)):
        return "worth_reading"

    return "other"


TIER_TITLES = {
    "must_read": "必读",
    "worth_reading": "值得一读",
    "other": "其他相关",
}
