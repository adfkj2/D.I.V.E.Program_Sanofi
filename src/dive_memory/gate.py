from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re


EPHEMERAL_MARKERS = ("今天晚上", "现在临时", "just for now", "今晚", "稍后")
EXPLICIT_MARKERS = ("记住", "请记住", "remember", "不要忘记", "keep in mind")
SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:password|passwd|api[_ -]?key|access[_ -]?token|private[_ -]?key)\b", re.I),
    re.compile(r"(?:密码|口令|私钥|身份证号?|信用卡号?)"),
    # Value-shaped detectors. The label-based patterns above require the *word*
    # ("credit card number"); the adversarial probe showed a bare card-like
    # digit sequence passing because no label was present. These patterns key
    # on the value's own shape so a mislabelled or unlabelled secret is still
    # caught.
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),                      # 13-19 digit runs under 20, 20, 22, 28
    re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b"),                  # grouped card format (4-4-4-4)
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{3,}\b"),   # token-shaped secrets
    re.compile(r"\bsk-[A-Za-z0-9-]{6,}\b"),                     # provider key prefixes
    re.compile(r"\b\d{17}[\dXx]\b"),                            # Chinese ID number
)


POLICY_V1 = "utility-baseline-v1"
POLICY_SEMANTIC = "semantic-utility-v2.1"


@dataclass(slots=True)
class GateDecision:
    accepted: bool
    importance: float
    confidence: float
    salience: float
    durability: str
    reason: str
    action: "GateAction" = field(default_factory=lambda: GateAction.WRITE)
    reason_code: str = "UTILITY_ABOVE_WRITE_THRESHOLD"
    features: dict[str, float] = field(default_factory=dict)
    policy_version: str = POLICY_V1


class GateAction(StrEnum):
    WRITE = "WRITE"
    SKIP = "SKIP"
    REVIEW = "REVIEW"


def _features(text: str, *, explicit: bool, requested: bool, ephemeral: bool,
              source_reliability: float, importance: float, sensitive: bool) -> dict[str, float]:
    words = [word for word in re.split(r"\s+|(?<=[，。,.!?])", text.strip()) if word]
    specificity = min(1.0, max(0.2, len(text.strip()) / 80.0))
    return {
        "future_usefulness": importance,
        "durability": 0.2 if ephemeral else (1.0 if requested else 0.65),
        "specificity": specificity,
        "novelty": 0.5,  # related-memory comparison occurs later in the pipeline
        "importance": importance,
        "source_confidence": max(0.0, min(1.0, source_reliability)),
        "redundancy": 0.0,  # filled by the resolver when related memories exist
        "sensitivity_risk": 1.0 if sensitive else 0.0,
        "temporal_relevance": 0.4 if ephemeral else 0.8,
        "explicit_remember_request": 1.0 if explicit or requested else 0.0,
        "token_specificity": min(1.0, len(words) / 12.0),
    }


def _sensitive(text: str) -> bool:
    """Sensitive-data detector shared by every gate policy.

    Kept as a module-level helper so the semantic policy can enforce the same
    safety floor without duplicating the pattern table.
    """
    return any(pattern.search(text.lower()) for pattern in SENSITIVE_PATTERNS)


def decide(text: str, *, explicit: bool = False, source_reliability: float = 0.8) -> GateDecision:
    normalized = text.strip().lower()
    if not normalized:
        return GateDecision(False, 0, 0, 0, "ephemeral", "empty", GateAction.SKIP,
                            "EMPTY", _features("", explicit=explicit, requested=False, ephemeral=True,
                                               source_reliability=source_reliability, importance=0.0,
                                               sensitive=False))
    sensitive = any(pattern.search(normalized) for pattern in SENSITIVE_PATTERNS)
    if sensitive:
        # The local MVP has no encrypted sensitive vault, so sensitive secrets
        # are rejected even when the caller explicitly asks to remember them.
        return GateDecision(False, 1.0, source_reliability, 1.0, "ephemeral", "sensitive data rejected",
                            GateAction.SKIP, "SENSITIVE_DATA",
                            _features(text, explicit=explicit, requested=True, ephemeral=False,
                                      source_reliability=source_reliability, importance=1.0, sensitive=True))
    requested = explicit or any(marker in normalized for marker in EXPLICIT_MARKERS)
    ephemeral = any(marker in normalized for marker in EPHEMERAL_MARKERS)
    durable_signal = any(k in normalized for k in ("我喜欢", "我偏好", "我住在", "我使用", "我的目标", "always", "prefer"))
    importance = 0.9 if requested else (0.65 if durable_signal else 0.25)
    features = _features(text, explicit=explicit, requested=requested, ephemeral=ephemeral,
                         source_reliability=source_reliability, importance=importance, sensitive=False)
    if ephemeral and not requested:
        return GateDecision(False, importance, source_reliability, 0.35, "ephemeral", "temporary intent",
                            GateAction.SKIP, "TEMPORARY_INTENT", features)
    if importance < 0.45:
        action = GateAction.REVIEW if 0.35 <= importance < 0.45 and source_reliability >= 0.5 else GateAction.SKIP
        return GateDecision(False, importance, source_reliability, 0.3, "ephemeral", "low future utility",
                            action, "UTILITY_REVIEW" if action is GateAction.REVIEW else "LOW_FUTURE_UTILITY",
                            features)
    durability = "permanent" if requested else ("long_term" if durable_signal else "medium_term")
    return GateDecision(True, importance, source_reliability, min(1.0, importance + 0.1), durability,
                        "accepted by write gate", GateAction.WRITE, "UTILITY_ABOVE_WRITE_THRESHOLD", features)
