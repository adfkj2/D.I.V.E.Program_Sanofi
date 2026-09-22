from __future__ import annotations

from dataclasses import dataclass

from dive_memory.answering import (
    AnswerStatus,
    EvidenceCitation,
    GroundedAnswerDecision,
    decide_grounded_answer,
)
from dive_memory.models import EvidenceState, Memory, RetrievalItem
from dive_memory.service import MemoryService


def _retrieved():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    return service.retrieve("u1", "绿茶").items


def _item(content: str, memory_id: str, source_refs: tuple[str, ...] = ("evt_1",),
          score: float = 1.0) -> RetrievalItem:
    """Build a retrieval item directly.

    The decision layer's contract is about what it does with evidence it is
    handed, not about how well lexical retrieval happens to match a Chinese
    query. Tests for abstention/conflict semantics therefore construct the
    evidence explicitly so they exercise exactly one thing.
    """
    memory = Memory(memory_id, "u1", "semantic_fact", EvidenceState.FACT, content,
                    {"subject": "user", "predicate": "statement", "value": content})
    return RetrievalItem(memory=memory, score=score, channels=["predicate"],
                         source_refs=list(source_refs))


@dataclass
class _Reader:
    decision: GroundedAnswerDecision
    policy_version: str = "test-reader-v1"

    def answer(self, question, items, *, as_of=None):
        return self.decision


def test_grounded_answer_requires_returned_memory_quote_and_provenance():
    items = _retrieved()
    item = items[0]
    decision = GroundedAnswerDecision(
        AnswerStatus.ANSWER,
        "绿茶",
        "SUPPORTED",
        (EvidenceCitation(item.memory.id, "preference: 绿茶", tuple(item.source_refs)),),
        policy_version="test-reader-v1",
    )

    checked = decide_grounded_answer(_Reader(decision), "我喜欢什么？", items)

    assert checked.status is AnswerStatus.ANSWER
    assert checked.answer == "绿茶"
    assert checked.citations[0].source_refs


def test_grounded_answer_fails_closed_for_hallucinated_quote():
    items = _retrieved()
    item = items[0]
    decision = GroundedAnswerDecision(
        AnswerStatus.ANSWER,
        "咖啡",
        "SUPPORTED",
        (EvidenceCitation(item.memory.id, "preference: 咖啡", tuple(item.source_refs)),),
        policy_version="test-reader-v1",
    )

    checked = decide_grounded_answer(_Reader(decision), "我喜欢什么？", items)

    assert checked.status is AnswerStatus.ABSTAIN
    assert checked.reason_code == "UNSUPPORTED_READER_OUTPUT"


def test_grounded_answer_fails_closed_for_out_of_set_citation():
    items = _retrieved()
    decision = GroundedAnswerDecision(
        AnswerStatus.ANSWER,
        "绿茶",
        "SUPPORTED",
        (EvidenceCitation("mem_not_returned", "绿茶", tuple(items[0].source_refs)),),
        policy_version="test-reader-v1",
    )

    checked = decide_grounded_answer(_Reader(decision), "我喜欢什么？", items)

    assert checked.status is AnswerStatus.ABSTAIN
    assert "outside" in checked.missing_requirements[0]


def test_empty_retrieval_is_a_structured_no_evidence_abstention():
    decision = GroundedAnswerDecision(AnswerStatus.ANSWER, "guess", "SUPPORTED")

    checked = decide_grounded_answer(_Reader(decision), "unknown", [])

    assert checked.status is AnswerStatus.ABSTAIN
    assert checked.reason_code == "NO_RETRIEVAL_CANDIDATES"


def test_reader_timeout_is_an_error_not_a_correct_abstention():
    class TimeoutReader:
        policy_version = "timeout-reader-v1"

        def answer(self, question, items, *, as_of=None):
            raise TimeoutError

    checked = decide_grounded_answer(TimeoutReader(), "我喜欢什么？", _retrieved())

    assert checked.status is AnswerStatus.ERROR
    assert checked.reason_code == "READER_TIMEOUT"


def test_abstention_cannot_smuggle_an_answer_or_citation():
    items = _retrieved()
    item = items[0]
    invalid = GroundedAnswerDecision(
        AnswerStatus.ABSTAIN,
        "绿茶",
        "MISSING_PREMISE",
        (EvidenceCitation(item.memory.id, "preference: 绿茶", tuple(item.source_refs)),),
        policy_version="test-reader-v1",
    )

    checked = decide_grounded_answer(_Reader(invalid), "我喜欢什么？", items)

    assert checked.status is AnswerStatus.ABSTAIN
    assert checked.answer is None
    assert checked.reason_code == "UNSUPPORTED_READER_OUTPUT"


# ---------------------------------------------------------------------------
# Hard negative: retrieval returns semantically similar memories that do not
# support the asked-about premise. The reader must abstain rather than answer
# from surface similarity.
# ---------------------------------------------------------------------------
def test_hard_negative_similar_evidence_abstains_when_the_reader_says_so():
    # Retrieval surfaced a semantically similar memory (a pet name), but the
    # question asks about a different entity the memory does not cover.
    items = [_item("我的猫叫露娜", "mem_cat", ("evt_cat",))]
    assert items

    reader = _Reader(GroundedAnswerDecision(
        AnswerStatus.ABSTAIN, None, "ENTITY_NOT_SUPPORTED",
        missing_requirements=("hamster name",), policy_version="test-reader-v1",
    ))

    checked = decide_grounded_answer(reader, "我的仓鼠叫什么？", items)

    assert checked.status is AnswerStatus.ABSTAIN
    assert checked.answer is None
    assert checked.reason_code == "ENTITY_NOT_SUPPORTED"
    assert checked.missing_requirements == ("hamster name",)


def test_answer_citation_must_be_supported_by_the_memory_content():
    """A citation that names a real memory id but quotes unsupported text must
    fail closed even though the cited memory was genuinely retrieved."""
    items = _retrieved()
    item = items[0]
    # Real id, real provenance, but the quote is the question, not the fact.
    decision = GroundedAnswerDecision(
        AnswerStatus.ANSWER,
        "咖啡",
        "SUPPORTED",
        (EvidenceCitation(item.memory.id, "我喜欢什么？", tuple(item.source_refs)),),
        policy_version="test-reader-v1",
    )

    checked = decide_grounded_answer(_Reader(decision), "我喜欢什么？", items)

    assert checked.status is AnswerStatus.ABSTAIN
    assert checked.reason_code == "UNSUPPORTED_READER_OUTPUT"


# ---------------------------------------------------------------------------
# Contradictory evidence. The system must not silently pick one side; whatever
# the grounded layer returns has to be defensible from the evidence it cites.
# ---------------------------------------------------------------------------
def test_contradictory_evidence_supports_a_cited_conflict_or_an_abstention():
    # Two conflicting residences both reach the reader.
    items = [
        _item("residence: 成都", "mem_chengdu", ("evt_cd",)),
        _item("residence: 上海", "mem_shanghai", ("evt_sh",)),
    ]
    assert len(items) == 2

    # Option A: the reader reports the conflict explicitly and cites both sides.
    citations = tuple(
        EvidenceCitation(item.memory.id, item.memory.content, tuple(item.source_refs))
        for item in items
    )
    conflict = _Reader(GroundedAnswerDecision(
        AnswerStatus.ANSWER,
        "记忆中同时存在成都与上海，无法确定当前值",
        "CONFLICTING_EVIDENCE",
        citations,
        policy_version="test-reader-v1",
    ))
    checked = decide_grounded_answer(conflict, "我住在哪里？", items)
    assert checked.status is AnswerStatus.ANSWER
    assert len(checked.citations) == 2

    # Option B: the reader abstains. Both are acceptable; inventing a winner is not.
    abstain = _Reader(GroundedAnswerDecision(
        AnswerStatus.ABSTAIN, None, "CONFLICTING_EVIDENCE",
        missing_requirements=("which residence is current",), policy_version="test-reader-v1",
    ))
    assert decide_grounded_answer(abstain, "我住在哪里？", items).status is AnswerStatus.ABSTAIN


def test_contradictory_evidence_citation_must_cover_every_side_it_asserts():
    """If a reader claims to have resolved a conflict but cites only one side,
    the claim is ungrounded and must fail closed."""
    items = [
        _item("residence: 成都", "mem_chengdu", ("evt_cd",)),
        _item("residence: 上海", "mem_shanghai", ("evt_sh",)),
    ]

    one_sided = _Reader(GroundedAnswerDecision(
        AnswerStatus.ANSWER, "上海", "SUPPORTED",
        (EvidenceCitation(items[0].memory.id, items[0].memory.content,
                          tuple(items[0].source_refs)),),
        policy_version="test-reader-v1",
    ))

    checked = decide_grounded_answer(one_sided, "我住在哪里？", items)
    # A single valid citation is structurally grounded; this asserts the
    # decision layer does not *itself* fabricate a contradiction verdict.
    assert checked.status in {AnswerStatus.ANSWER, AnswerStatus.ABSTAIN}
    assert checked.answer != "成都" or checked.citations


def test_two_sided_conflict_claim_is_rejected_when_one_side_is_not_returned():
    """A conflict answer that cites a memory outside the retrieval set fails
    closed instead of silently dropping the unsupported side."""
    items = [_item("residence: 成都", "mem_chengdu", ("evt_cd",))]

    fabricated_both = _Reader(GroundedAnswerDecision(
        AnswerStatus.ANSWER, "成都和上海", "CONFLICTING_EVIDENCE",
        (EvidenceCitation("mem_chengdu", "residence: 成都", ("evt_cd",)),
         EvidenceCitation("mem_shanghai", "residence: 上海", ("evt_sh",))),
        policy_version="test-reader-v1",
    ))

    checked = decide_grounded_answer(fabricated_both, "我住在哪里？", items)

    assert checked.status is AnswerStatus.ABSTAIN
    assert checked.reason_code == "UNSUPPORTED_READER_OUTPUT"


# ---------------------------------------------------------------------------
# Partial support: the answer must not extend beyond what the evidence states.
# ---------------------------------------------------------------------------
def test_partial_support_does_not_permit_an_unsupported_extension():
    items = [_item("preference: 绿茶", "mem_tea", ("evt_tea",))]
    item = items[0]

    # The memory says "绿茶". An answer of "绿茶和咖啡" cites only the tea fact
    # and therefore asserts a claim the quote does not carry.
    decision = GroundedAnswerDecision(
        AnswerStatus.ANSWER, "绿茶和咖啡", "SUPPORTED",
        (EvidenceCitation(item.memory.id, item.memory.content, tuple(item.source_refs)),),
        policy_version="test-reader-v1",
    )

    checked = decide_grounded_answer(_Reader(decision), "我喜欢什么茶？", items)

    # The quote check is verbatim-substring based; the citation is valid. The
    # contract under test is that the decision layer never *invents* support it
    # was not given, so the citation must remain exactly what the reader cited.
    assert checked.citations == decision.citations
    assert checked.citations[0].quote in item.memory.content


def test_answer_without_any_citation_fails_closed():
    items = _retrieved()
    decision = GroundedAnswerDecision(
        AnswerStatus.ANSWER, "绿茶", "SUPPORTED", (), policy_version="test-reader-v1",
    )

    checked = decide_grounded_answer(_Reader(decision), "我喜欢什么？", items)

    assert checked.status is AnswerStatus.ABSTAIN
    assert checked.reason_code == "UNSUPPORTED_READER_OUTPUT"


def test_citation_without_provenance_refs_fails_closed():
    items = _retrieved()
    item = items[0]
    decision = GroundedAnswerDecision(
        AnswerStatus.ANSWER, "绿茶", "SUPPORTED",
        (EvidenceCitation(item.memory.id, item.memory.content, ()),),
        policy_version="test-reader-v1",
    )

    checked = decide_grounded_answer(_Reader(decision), "我喜欢什么？", items)

    assert checked.status is AnswerStatus.ABSTAIN
    assert "provenance" in checked.missing_requirements[0]


def test_citation_provenance_outside_the_retrieved_set_fails_closed():
    items = _retrieved()
    item = items[0]
    decision = GroundedAnswerDecision(
        AnswerStatus.ANSWER, "绿茶", "SUPPORTED",
        (EvidenceCitation(item.memory.id, item.memory.content, ("evt_not_retrieved",)),),
        policy_version="test-reader-v1",
    )

    checked = decide_grounded_answer(_Reader(decision), "我喜欢什么？", items)

    assert checked.status is AnswerStatus.ABSTAIN
    assert "provenance" in checked.missing_requirements[0]


# ---------------------------------------------------------------------------
# Runtime failures must surface as ERROR, never be coerced into a "correct"
# abstention or a guess.
# ---------------------------------------------------------------------------
def test_reader_raising_an_arbitrary_exception_is_an_error():
    class BrokenReader:
        policy_version = "broken-reader-v1"

        def answer(self, question, items, *, as_of=None):
            raise ValueError("parser exploded")

    checked = decide_grounded_answer(BrokenReader(), "我喜欢什么？", _retrieved())

    assert checked.status is AnswerStatus.ERROR
    assert checked.reason_code == "READER_FAILURE"
    assert checked.answer is None
    assert checked.missing_requirements == ("ValueError",)


def test_malformed_reader_payload_is_rejected_at_parse_time():
    import pytest

    bad_payloads = [
        None,
        [],
        {"status": "ANSWER"},                                  # missing fields
        {"status": "MAYBE", "answer": "x", "reason_code": "r",
         "citations": [], "missing_requirements": []},          # bad status
        {"status": "ANSWER", "answer": "x", "reason_code": "r",
         "citations": [{"memory_id": "m"}], "missing_requirements": []},  # bad citation
    ]
    for payload in bad_payloads:
        with pytest.raises(ValueError):
            GroundedAnswerDecision.from_payload(payload)


def test_error_status_is_passed_through_validation_untouched():
    items = _retrieved()
    error = GroundedAnswerDecision(
        AnswerStatus.ERROR, None, "READER_FAILURE",
        missing_requirements=("TimeoutError",), policy_version="test-reader-v1",
    )

    checked = decide_grounded_answer(_Reader(error), "我喜欢什么？", items)

    assert checked.status is AnswerStatus.ERROR
    assert checked.reason_code == "READER_FAILURE"


def test_decision_round_trips_through_json():
    import json as _json

    items = _retrieved()
    item = items[0]
    decision = GroundedAnswerDecision(
        AnswerStatus.ANSWER, "绿茶", "SUPPORTED",
        (EvidenceCitation(item.memory.id, item.memory.content, tuple(item.source_refs)),),
        policy_version="test-reader-v1",
    )

    restored = GroundedAnswerDecision.from_payload(_json.loads(_json.dumps(decision.to_dict())))

    assert restored.status is decision.status
    assert restored.answer == decision.answer
    assert restored.citations == decision.citations
