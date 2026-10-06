"""Research enrichment for journal-alert.

V3 research enrichment:
1. Research-use tags
2. Human-readable relevance explanation
3. Research / modelling method extraction

No external API or LLM is required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _text(item) -> str:
    """Combine title and abstract into normalized searchable text."""
    title = getattr(item, "title", "") or ""
    abstract = getattr(item, "abstract", "") or ""
    return f"{title}\n{abstract}".lower()


def _labels(scored) -> set[str]:
    """Return matched keyword labels from Scored-like object."""
    labels = getattr(scored, "labels", []) or []
    return {str(x) for x in labels if x}


def _has(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


@dataclass(frozen=True)
class ResearchTag:
    label: str
    priority: int


# ============================================================
# 1. 方法识别规则
# ============================================================

METHOD_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (

    (
        "Random Forest",
        (
            r"\brandom forest(?:s)?\b",
            r"\brandom[- ]forest\b",
        ),
    ),

    (
        "XGBoost",
        (
            r"\bxgboost\b",
            r"\bextreme gradient boosting\b",
        ),
    ),

    (
        "LightGBM",
        (
            r"\blightgbm\b",
            r"\blight gradient boosting machine\b",
        ),
    ),

    (
        "CatBoost",
        (
            r"\bcatboost\b",
        ),
    ),

    (
        "SVM/SVR",
        (
            r"\bsupport vector machine\b",
            r"\bsupport vector regression\b",
            r"\bsvm\b",
            r"\bsvr\b",
        ),
    ),

    (
        "神经网络/深度学习",
        (
            r"\bartificial neural network\b",
            r"\bneural network\b",
            r"\bdeep learning\b",
            r"\bconvolutional neural network\b",
            r"\bcnn\b",
            r"\blstm\b",
        ),
    ),

    (
        "Elastic Net",
        (
            r"\belastic net\b",
            r"\belastic[- ]net\b",
        ),
    ),

    (
        "SHAP",
        (
            r"\bshap\b",
            r"\bshapley additive explanations?\b",
            r"\bshapley values?\b",
        ),
    ),

    (
        "PDP",
        (
            r"\bpartial dependence\b",
            r"\bpartial dependence plots?\b",
            r"\bpdp\b",
        ),
    ),

    (
        "特征重要性",
        (
            r"\bfeature importance\b",
            r"\bpermutation importance\b",
        ),
    ),

    (
        "空间交叉验证",
        (
            r"\bspatial cross[- ]validation\b",
            r"\bspatial cv\b",
            r"\bblock cross[- ]validation\b",
        ),
    ),

    (
        "嵌套交叉验证",
        (
            r"\bnested cross[- ]validation\b",
            r"\bnested cv\b",
        ),
    ),

    (
        "外部验证",
        (
            r"\bexternal validation\b",
            r"\bindependent validation\b",
            r"\bout[- ]of[- ]sample validation\b",
        ),
    ),

    (
        "迁移学习/域适应",
        (
            r"\btransfer learning\b",
            r"\bdomain adaptation\b",
            r"\bdomain generalization\b",
            r"\bdomain generalisation\b",
        ),
    ),

    (
        "数字土壤制图",
        (
            r"\bdigital soil mapping\b",
            r"\bpedometrics\b",
            r"\bsoil property mapping\b",
        ),
    ),

    (
        "遥感",
        (
            r"\bremote sensing\b",
            r"\bearth observation\b",
            r"\blandsat\b",
            r"\bsentinel[- ]?2\b",
            r"\bmodis\b",
            r"\bhyperspectral\b",
        ),
    ),

    (
        "Meta分析",
        (
            r"\bmeta[- ]analysis\b",
            r"\bmeta analysis\b",
        ),
    ),

    (
        "结构方程模型",
        (
            r"\bstructural equation model(?:ing|ling)?\b",
            r"\bstructural equation modelling\b",
        ),
    ),

    (
        "方差分析",
        (
            r"\banova\b",
            r"\banalysis of variance\b",
        ),
    ),

    (
        "培养实验",
        (
            r"\bincubation experiment\b",
            r"\blaboratory incubation\b",
            r"\bsoil incubation\b",
            r"\bmicrocosm\b",
        ),
    ),

    (
        "田间试验",
        (
            r"\bfield experiment\b",
            r"\bfield trial\b",
            r"\blong[- ]term experiment\b",
        ),
    ),

    (
        "微生物群落分析",
        (
            r"\bmicrobial community\b",
            r"\bmicrobiome\b",
            r"\b16s rrna\b",
            r"\bits sequencing\b",
            r"\bamplicon sequencing\b",
        ),
    ),

    (
        "稳定同位素",
        (
            r"\bstable isotope\b",
            r"\b15n\b",
            r"\bnitrogen isotope\b",
            r"\bisotopic composition\b",
        ),
    ),
)


def detect_methods(item) -> list[str]:
    """Detect research methods from title + abstract."""
    text = _text(item)

    found: list[str] = []

    for method, patterns in METHOD_PATTERNS:
        if any(_has(text, pattern) for pattern in patterns):
            found.append(method)

    return found


# ============================================================
# 2. 研究用途标签
# ============================================================

def research_tags(item, scored) -> list[str]:
    """Assign papers to active research directions."""

    labels = _labels(scored)
    text = _text(item)

    tags: list[ResearchTag] = []

    freeze = "冻融过程" in labels
    inorganic_n = "无机氮" in labels
    n_cycle = "氮转化" in labels
    total_n = "土壤总氮与氮储量" in labels
    black_soil = "黑土与Mollisol" in labels
    microbial = "土壤微生物" in labels

    ml = bool(
        {
            "机器学习",
            "机器学习算法",
            "可解释机器学习",
        }
        & labels
    )

    validation = bool(
        {
            "模型验证与泛化",
            "迁移学习与域适应",
        }
        & labels
    )

    mapping = "数字土壤制图" in labels
    remote = "遥感" in labels

    # 当前冻融无机氮论文
    if freeze and (inorganic_n or n_cycle):
        tags.append(
            ResearchTag(
                "冻融无机氮论文",
                100,
            )
        )

    elif freeze:
        tags.append(
            ResearchTag(
                "冻融机制参考",
                90,
            )
        )

    # 毕业论文机制分析
    if (
        (freeze and microbial)
        or (microbial and n_cycle)
    ):
        tags.append(
            ResearchTag(
                "毕业论文机制分析",
                85,
            )
        )

    # 土壤总氮机器学习论文
    if total_n and (
        ml
        or validation
        or mapping
        or remote
    ):
        tags.append(
            ResearchTag(
                "土壤总氮机器学习",
                95,
            )
        )

    # 方法学参考
    if (
        validation
        or "可解释机器学习" in labels
        or _has(
            text,
            (
                r"\bspatial cross[- ]validation\b|"
                r"\bnested cross[- ]validation\b|"
                r"\bexternal validation\b|"
                r"\btransfer learning\b|"
                r"\bdomain adaptation\b"
            ),
        )
    ):
        tags.append(
            ResearchTag(
                "方法学参考",
                75,
            )
        )

    # 遥感和数字土壤制图
    if mapping or remote:
        tags.append(
            ResearchTag(
                "遥感/数字土壤制图",
                70,
            )
        )

    # 黑土氮过程
    if black_soil and (
        inorganic_n
        or n_cycle
        or total_n
    ):
        tags.append(
            ResearchTag(
                "黑土氮循环参考",
                80,
            )
        )

    # 兜底标签
    if not tags:

        if (
            inorganic_n
            or n_cycle
            or total_n
        ):
            tags.append(
                ResearchTag(
                    "土壤氮过程参考",
                    50,
                )
            )

        else:
            tags.append(
                ResearchTag(
                    "相关文献",
                    10,
                )
            )

    # 去重并按优先级排序
    best: dict[str, int] = {}

    for tag in tags:
        best[tag.label] = max(
            tag.priority,
            best.get(tag.label, -1),
        )

    result = sorted(
        best.items(),
        key=lambda x: (
            -x[1],
            x[0],
        ),
    )

    return [
        label
        for label, _ in result
    ]


# ============================================================
# 3. 相关性解释
# ============================================================

def relevance_reason(item, scored) -> str:
    """Generate concise Chinese explanation of relevance."""

    labels = _labels(scored)

    if {
        "冻融过程",
        "无机氮",
    } <= labels:

        return (
            "同时命中冻融过程和无机氮，"
            "可直接对照冻融条件下 NO3-N / NH4-N 的响应。"
        )

    if {
        "冻融过程",
        "氮转化",
    } <= labels:

        return (
            "同时命中冻融过程和氮转化，"
            "适合解释矿化、硝化或反硝化等过程机制。"
        )

    if {
        "黑土与Mollisol",
        "无机氮",
    } <= labels:

        return (
            "同时涉及黑土/Mollisol 与无机氮，"
            "可用于东北黑土氮素结果的区域背景比较。"
        )

    if {
        "黑土与Mollisol",
        "氮转化",
    } <= labels:

        return (
            "同时涉及黑土/Mollisol 与氮转化，"
            "可用于黑土氮循环机制讨论。"
        )

    if (
        "土壤总氮与氮储量" in labels
        and {
            "机器学习",
            "机器学习算法",
            "可解释机器学习",
        }
        & labels
    ):

        return (
            "同时涉及土壤总氮和机器学习，"
            "可直接参考预测模型、特征筛选或解释方法。"
        )

    if (
        "土壤总氮与氮储量" in labels
        and "模型验证与泛化" in labels
    ):

        return (
            "同时涉及土壤总氮和模型验证，"
            "可参考外部验证、空间交叉验证或泛化设计。"
        )

    if (
        "数字土壤制图" in labels
        and {
            "机器学习",
            "机器学习算法",
            "可解释机器学习",
        }
        & labels
    ):

        return (
            "同时涉及数字土壤制图和机器学习，"
            "可参考土壤属性空间预测流程。"
        )

    if (
        "遥感" in labels
        and "土壤总氮与氮储量" in labels
    ):

        return (
            "同时涉及遥感和土壤总氮，"
            "可参考遥感变量构建与空间预测。"
        )

    if (
        "土壤微生物" in labels
        and "氮转化" in labels
    ):

        return (
            "同时涉及微生物与氮转化，"
            "可用于解释氮循环的生物学机制。"
        )

    if "冻融过程" in labels:

        return (
            "直接涉及冻融过程，"
            "可作为冻融实验设计、过程解释或讨论部分参考。"
        )

    if "无机氮" in labels:

        return (
            "涉及硝态氮或铵态氮，"
            "可作为无机氮变化规律和讨论部分的补充参考。"
        )

    if "氮转化" in labels:

        return (
            "涉及氮矿化、硝化、反硝化或氮循环过程，"
            "可用于机制讨论。"
        )

    if "土壤总氮与氮储量" in labels:

        return (
            "涉及土壤总氮或氮储量，"
            "可用于土壤氮空间预测与背景讨论。"
        )

    if "数字土壤制图" in labels:

        return (
            "涉及数字土壤制图，"
            "可参考空间预测、环境协变量或验证方案。"
        )

    if "可解释机器学习" in labels:

        return (
            "涉及可解释机器学习，"
            "可参考 SHAP、特征重要性或响应解释方法。"
        )

    return (
        "命中当前文献雷达的相关主题，"
        "可作为补充阅读文献。"
    )


# ============================================================
# 4. 对外统一接口
# ============================================================

def enrich(item, scored) -> dict:
    """Return enrichment information for one paper."""

    return {
        "research_tags": research_tags(
            item,
            scored,
        ),

        "relevance_reason": relevance_reason(
            item,
            scored,
        ),

        "methods": detect_methods(
            item,
        ),
    }
