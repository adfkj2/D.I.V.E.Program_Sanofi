from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(slots=True)
class QueryPlan:
    intent: str
    temporal_mode: str
    as_of: str | None
    channels: list[str]
    limit: int
    token_budget: int


def plan_query(query: str, *, limit: int = 8, token_budget: int = 1500) -> QueryPlan:
    text = query.lower()
    if any(token in text for token in ("第一次", "first", "最早")):
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
    channels = ["dense", "bm25", "metadata"]
    if intent in {"timeline", "historical", "current"}:
        channels.append("temporal")
    if intent in {"procedure", "semantic"}:
        channels.append("relation")
    return QueryPlan(intent, temporal, None, channels, max(1, min(limit, 100)), max(128, token_budget))
