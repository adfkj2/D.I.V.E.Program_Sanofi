from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import lexical
from .temporal import find_period


@dataclass(slots=True)
class QueryPlan:
    intent: str
    temporal_mode: str
    as_of: str | None
    channels: list[str]
    limit: int
    token_budget: int
    predicates: list[str] = field(default_factory=list)


# Keywords that reveal *which* attribute a question is about. This is what makes
# "我现在住在哪里" reachable at all: the question and the stored fact
# ("residence: 上海") share almost no characters, so lexical and hash-vector
# similarity both score it near zero.
PREDICATE_HINTS: tuple[tuple[str, str], ...] = (
    ("住", "residence"),
    ("居", "residence"),
    ("live", "residence"),
    ("residence", "residence"),
    ("喜欢", "preference"),
    ("偏好", "preference"),
    ("prefer", "preference"),
    ("like", "preference"),
    ("使用", "primary_tool"),
    ("工具", "primary_tool"),
    ("use", "primary_tool"),
    ("tool", "primary_tool"),
    ("目标", "goal"),
    ("goal", "goal"),
)


def _hint_present(hint: str, text: str) -> bool:
    """Match ASCII hints on word boundaries so "because" is not "use"."""
    if hint.isascii():
        return re.search(rf"(?<![a-z]){re.escape(hint)}(?![a-z])", text) is not None
    return hint in text


# Functional vocabulary that says nothing about *which subject* a question
# concerns: pronouns, time words, question words, units and generic nouns.
_FILLER_WORDS = frozenset({
    "我", "我的", "我们", "自己", "你", "您",
    "现在", "目前", "如今", "当前", "最近", "以前", "曾经", "过去", "历史", "最早",
    "第一次", "什么时候", "时间线", "今天", "昨天", "明天", "时候",
    "是", "什么", "啥", "哪", "哪里", "哪儿", "怎么", "怎样", "如何", "为什么",
    "吗", "呢", "的", "了", "在", "有", "和", "与", "个", "些", "一下",
    "请", "告诉", "帮我", "想知道", "记得", "还", "都", "会", "要", "想", "知道",
    "年", "月", "日", "号", "城市", "地方", "地点", "国家", "区域", "名字",
})


def _residual_subject(query: str) -> str:
    """CJK left over after removing functional vocabulary and explicit periods.

    Leftover characters mean the question names a subject the caller never
    registered ("火星住哪里"). The namespace itself *is* the subject here, so a
    predicate lookup must not fire for those questions — otherwise an unknown
    subject gets answered with the user's own fact, which is exactly the
    "以相似但不同实体填空" failure docs/06 forbids.
    """
    text = query.lower()
    period = find_period(query)
    if period:
        text = text.replace(period.lower(), " ")
    vocabulary = sorted(_FILLER_WORDS | {hint for hint, _ in PREDICATE_HINTS}, key=len, reverse=True)
    for word in vocabulary:
        text = text.replace(word, " ")
    return "".join(lexical.CJK_RUN.findall(text))


def query_predicates(query: str) -> list[str]:
    """Predicates the question is explicitly about, in declaration order."""
    if _residual_subject(query):
        return []
    text = query.lower()
    # The CJK residual check above cannot see an English named subject. Only
    # enable an English predicate hint when the question is explicitly scoped
    # to the caller; otherwise "Where does Alice live?" would be answered with
    # the current user's residence.
    has_ascii_hint = any(hint.isascii() and _hint_present(hint, text) for hint, _ in PREDICATE_HINTS)
    has_ascii_text = re.search(r"[a-z]", text) is not None
    caller_scoped = re.search(r"\b(?:i|me|my|mine|we|us|our|ours)\b", text) is not None or any(
        pronoun in text for pronoun in ("我", "我的", "我们", "自己")
    )
    if has_ascii_text and has_ascii_hint and not caller_scoped:
        return []
    found: list[str] = []
    for hint, predicate in PREDICATE_HINTS:
        if predicate not in found and _hint_present(hint, text):
            found.append(predicate)
    return found


def plan_query(query: str, *, limit: int = 8, token_budget: int = 1500) -> QueryPlan:
    text = query.lower()
    if any(token in text for token in ("多跳", "关联", "关系", "related", "connected", "multi-hop")):
        intent, temporal = "multi_hop", "any"
    elif any(token in text for token in ("第一次", "first", "最早")):
        intent, temporal = "timeline", "earliest"
    elif any(token in text for token in ("最近一次", "latest", "most recent")):
        intent, temporal = "timeline", "latest"
    elif any(token in text for token in ("以前", "曾经", "历史", "过去", "before", "previous")):
        intent, temporal = "historical", "historical"
    elif any(token in text for token in ("现在", "目前", "如今", "current", "now")):
        intent, temporal = "current", "current"
    elif any(token in text for token in ("时间线", "什么时候", "timeline", "when")):
        intent, temporal = "timeline", "all"
    elif any(token in text for token in ("怎么做", "步骤", "如何", "how")):
        intent, temporal = "procedure", "current"
    else:
        intent, temporal = "semantic", "any"
    # An explicit period ("2025年我住在哪") is an as-of question. Without this
    # the planner advertised a temporal mode but never produced the filter the
    # retrieval layer needs, so every historical question ran as "current".
    as_of = find_period(query)
    if as_of is not None and temporal == "any":
        temporal = "historical"
    channels = ["dense", "bm25", "metadata"]
    if intent in {"timeline", "historical", "current"}:
        channels.append("temporal")
    if intent in {"procedure", "semantic", "multi_hop"}:
        channels.append("relation")
    return QueryPlan(intent, temporal, as_of, channels, max(1, min(limit, 100)),
                     max(0, min(token_budget, 100_000)), query_predicates(query))
