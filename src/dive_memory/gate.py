from __future__ import annotations

from dataclasses import dataclass


EPHEMERAL_MARKERS = ("今天晚上", "现在临时", "just for now", "今晚", "稍后")
EXPLICIT_MARKERS = ("记住", "请记住", "remember", "不要忘记", "keep in mind")


@dataclass(slots=True)
class GateDecision:
    accepted: bool
    importance: float
    confidence: float
    salience: float
    durability: str
    reason: str


def decide(text: str, *, explicit: bool = False, source_reliability: float = 0.8) -> GateDecision:
    normalized = text.strip().lower()
    if not normalized:
        return GateDecision(False, 0, 0, 0, "ephemeral", "empty")
    requested = explicit or any(marker in normalized for marker in EXPLICIT_MARKERS)
    ephemeral = any(marker in normalized for marker in EPHEMERAL_MARKERS)
    durable_signal = any(k in normalized for k in ("我喜欢", "我偏好", "我住在", "我使用", "我的目标", "always", "prefer"))
    importance = 0.9 if requested else (0.65 if durable_signal else 0.25)
    if ephemeral and not requested:
        return GateDecision(False, importance, source_reliability, 0.35, "ephemeral", "temporary intent")
    if importance < 0.45:
        return GateDecision(False, importance, source_reliability, 0.3, "ephemeral", "low future utility")
    durability = "permanent" if requested else ("long_term" if durable_signal else "medium_term")
    return GateDecision(True, importance, source_reliability, min(1.0, importance + 0.1), durability, "accepted by write gate")
