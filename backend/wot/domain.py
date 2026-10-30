"""Server-owned provenance and deterministic policy; model output grants no rights."""
from enum import Enum


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class SourceOrigin(str, Enum):
    REAL_USER_MESSAGE = "REAL_USER_MESSAGE"
    USER_SELF_REPORT = "USER_SELF_REPORT"
    USER_CORRECTION = "USER_CORRECTION"
    USER_CHOSEN_DIRECTIVE = "USER_CHOSEN_DIRECTIVE"
    AUTHORIZED_IMPORT = "AUTHORIZED_IMPORT"
    EXTERNAL_SKILL_DATA = "EXTERNAL_SKILL_DATA"
    MODEL_INFERENCE = "MODEL_INFERENCE"
    SIMULATED_DIALOGUE = "SIMULATED_DIALOGUE"
    SIMULATION_FEEDBACK = "SIMULATION_FEEDBACK"
    EVALUATION_PRIVATE = "EVALUATION_PRIVATE"


REAL_ORIGINS = frozenset({
    SourceOrigin.REAL_USER_MESSAGE, SourceOrigin.USER_SELF_REPORT,
    SourceOrigin.USER_CORRECTION, SourceOrigin.AUTHORIZED_IMPORT,
})


def allow_evidence(actor: str, owner: str, subject: str, origin: SourceOrigin,
                   purpose: str, granted: bool = False) -> None:
    if owner != subject or actor not in {"HUMAN", "PERSONA"}:
        raise DomainError("没有访问这项个人证据的权限", 403)
    if origin not in REAL_ORIGINS:
        raise DomainError("此来源不能作为真实用户证据", 403)
    if purpose not in {"ADMIT", "MIRROR", "SIMULATION"}:
        raise DomainError("没有为这项用途授权", 403)
    if purpose == "ADMIT" and actor != "HUMAN":
        raise DomainError("仅本人可以批准真实记忆", 403)
    if purpose == "SIMULATION" and not granted:
        raise DomainError("模拟尚未获准使用私人证据", 403)


def classify(text: str) -> str:
    """Conservative display routing, never a psychological inference."""
    if any(s in text for s in ("我希望", "我想成为", "我想更", "我愿意成为", "我打算")):
        return "ASPIRATION"
    if any(s in text for s in ("他说", "她说", "朋友说", "假如", "假设", "小说", "故事里")):
        return "NEEDS_REVIEW"
    if any(s in text for s in ("我认为自己", "我觉得自己", "我是一个", "我喜欢", "我不喜欢")):
        return "SELF_REPORT"
    return "OBSERVATION"
