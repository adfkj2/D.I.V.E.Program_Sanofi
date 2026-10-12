"""The ten quality properties the Oracle v2 work has to hold, as tests.

Each test names the requirement it encodes. They are deliberately end-to-end
wherever an end-to-end check is possible -- a property that only holds inside a
helper is not a property of the system -- and every one of them is offline and
deterministic: no model weights, no network, no API key.

1.  a newer single-valued fact supersedes the older one
2.  evidence is described in temporal order, with the span computed
3.  two preferences accumulate instead of overwriting, and the prompt says so
4.  an answer assembled from several memories is an answer, not an abstention
5.  competing scalar values are reported as a conflict; documents are not
6.  usable evidence triggers one recheck, without forcing an unsupported answer
7.  genuinely absent evidence abstains, and the reader is not called at all
8.  a long non-trusted deliverable becomes a document memory, and only then
9.  the re-rank prefers the newer of two otherwise identical memories
10. gold never reaches the writer, the prompt or the recorded reader context
"""

from __future__ import annotations

import json

import pytest

from dive_memory.eval_adapters.longmemeval import LongMemEvalCase, LongMemEvalTurn
from dive_memory.extraction import extract_candidates
from dive_memory.gate import GateAction, GateDecision
from dive_memory.models import (
    DOCUMENT_ASSERTION,
    DOCUMENT_PREDICATE,
    EvidenceState,
    Memory,
    MemoryStatus,
    RetrievalItem,
)
from dive_memory.oracle.contracts import (
    WRITER_VISIBLE_FIELDS,
    assert_writer_visible,
    find_evaluator_only_fields,
)
from dive_memory.oracle.evidence import (
    EvidenceState as EvidenceAssessmentState,
    assess_evidence,
    build_evidence_views,
    build_temporal_scaffold,
    render_evidence_block,
)
from dive_memory.oracle.pipeline import PipelineConfig, run_case
from dive_memory.oracle.providers import ScriptedChatClient
from dive_memory.oracle.reader import (
    DeterministicReader,
    GroundedChatReader,
    OracleReaderRequest,
    PROMPT_TOP_LEVEL_KEYS,
    ReaderPolicy,
    ReaderSpec,
    build_reader_prompt,
    invoke_reader,
    reader_outcome,
)
from dive_memory.retrieval import ScoringPolicy
from dive_memory.semantic_gate import DocumentAdmissionPolicy, admit_document
from dive_memory.service import MemoryService
from dive_memory.store import _scoring_quality

LONG_ASSISTANT_DELIVERABLE = (
    "Thank you for the agent names. Here is the shift rotation sheet for the GM "
    "social media agents with 7 agents, 4 shifts and 2 days off for every agent.\n\n"
    "| Agent | Monday | Tuesday |\n| Admon | 8am-4pm | off |\n| Sara | off | 8am-4pm |\n\n"
    "Let me know if you would like the weekend rows spelled out separately."
)

#: Deliberately longer than ``min_chars`` so the size gate cannot mask the
#: safety floor the test is actually about: a secret is refused because it is a
#: secret, not because it is short.
LONG_DELIVERABLE_WITH_SECRET = (
    "Here is the plan for the migration. We will move the ingestion service onto "
    "the new cluster over the weekend, and the retrieval read-replicas will follow "
    "on Monday morning so the index rebuild can finish without downtime. Please "
    "review the checklist before Friday and tell me which window you prefer.\n"
    "The api_key is sk-abcdefghijklmnop and it rotates every thirty days, so keep "
    "it somewhere safe.\n"
)

GOLD_SENTINEL = "GOLDANSWER-SENTINEL-9931"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _memory(
    memory_id: str,
    content: str,
    *,
    structured: dict | None = None,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    valid_from: str | None = "2024-01-01T00:00:00+00:00",
    valid_to: str | None = None,
    importance: float = 0.6,
    confidence: float = 0.8,
    observed_at: str | None = "2024-01-01T00:00:00+00:00",
) -> Memory:
    return Memory(
        id=memory_id,
        namespace="ns",
        kind="semantic_fact",
        evidence_state=EvidenceState.FACT,
        content=content,
        structured_content=structured or {},
        status=status,
        importance=importance,
        confidence=confidence,
        valid_from=valid_from,
        valid_to=valid_to,
        observed_at=observed_at,
    )


def _item(memory: Memory, score: float = 1.0, channels=("bm25",), refs=("s1:0",)) -> RetrievalItem:
    return RetrievalItem(memory, score, list(channels), list(refs))


def _request(
    question: str,
    question_date: str | None,
    items,
    *,
    as_of: str | None = None,
    token_budget: int = 1500,
    policy: ReaderPolicy | None = None,
) -> OracleReaderRequest:
    """Build the only object a reader is allowed to see for one question.

    Equivalent to what ``pipeline.run_case`` assembles -- question, its date,
    the packed context -- without pulling the whole pipeline in.
    """

    items = tuple(items)
    context = "\n".join(
        f"[{item.memory.id}; source={','.join(item.source_refs) or 'unknown'}] {item.memory.content}"
        for item in items
    )
    return OracleReaderRequest(
        question=question,
        question_date=question_date,
        as_of=as_of if as_of is not None else question_date,
        items=items,
        context_text=context,
        token_budget=token_budget,
        policy=policy,
    )


def _turn(session_id: str, index: int, role: str, content: str, occurred_at: str, *,
          has_answer: bool = False, haystack_index: int = 0) -> LongMemEvalTurn:
    return LongMemEvalTurn(
        session_id=session_id,
        session_occurrence=1,
        turn_index=index,
        role=role,
        content=content,
        occurred_at=occurred_at,
        has_answer=has_answer,
        haystack_index=haystack_index,
    )


# --------------------------------------------------------------------------- #
# 1. A newer single-valued fact supersedes the older one
# --------------------------------------------------------------------------- #


def test_01_new_single_valued_fact_supersedes_the_old_one():
    service = MemoryService()
    try:
        namespace = "residence"
        service.ingest(namespace, "我住在成都", observed_at="2024-01-01T00:00:00+00:00")
        service.ingest(namespace, "我住在上海", observed_at="2024-06-01T00:00:00+00:00")

        active = service.store.active_memories(namespace)
        assert len(active) == 1, "a single-valued predicate must leave one live memory"
        assert active[0].structured_content["predicate"] == "residence"
        assert active[0].structured_content["value"] == "上海"

        history = service.list_memories(namespace, limit=100)
        superseded = [memory for memory in history
                      if memory.status is MemoryStatus.SUPERSEDED]
        assert superseded, (
            "the displaced residence must be retained as SUPERSEDED history, not deleted"
        )
        assert superseded[0].valid_to is not None, "supersession must close the validity window"
        assert superseded[0].structured_content["value"] == "成都"
    finally:
        service.store.close()


# --------------------------------------------------------------------------- #
# 2. Evidence is described in temporal order and the span is computed
# --------------------------------------------------------------------------- #


def test_02_evidence_is_temporally_ordered_with_a_computed_span():
    older = _item(_memory("m-old", "user lives in Chengdu",
                          valid_from="2023-01-01T00:00:00+00:00"))
    newer = _item(_memory("m-new", "user lives in Shanghai",
                          valid_from="2024-01-01T00:00:00+00:00"))
    question_date = "2024-03-01T00:00:00+00:00"

    views = build_evidence_views([newer, older], question="Where does the user live?",
                                 question_date=question_date)
    by_id = {view.memory_id: view for view in views}
    assert by_id["m-old"].days_before_question == 425
    assert by_id["m-new"].days_before_question == 60

    scaffold = build_temporal_scaffold("Where does the user live?", question_date, views)
    assert scaffold.earliest_evidence_date == "2023-01-01T00:00:00+00:00"
    assert scaffold.latest_evidence_date == "2024-01-01T00:00:00+00:00"
    assert scaffold.span_days == 365
    assert scaffold.undated_views == 0
    assert scaffold.current_views == 2

    block = render_evidence_block(views)
    assert "days_before_question=425" in block
    assert "user lives in Chengdu" in block, "the memory text must stay verbatim"


# --------------------------------------------------------------------------- #
# 3. Preferences accumulate, and the prompt knows it is a preference question
# --------------------------------------------------------------------------- #


def test_03_preferences_accumulate_and_the_prompt_aggregates_them():
    service = MemoryService()
    try:
        namespace = "prefs"
        service.ingest(namespace, "我喜欢绿茶", observed_at="2024-01-01T00:00:00+00:00")
        service.ingest(namespace, "我喜欢咖啡", observed_at="2024-02-01T00:00:00+00:00")

        active = service.store.active_memories(namespace)
        predicates = {memory.structured_content["predicate"] for memory in active}
        assert predicates == {"preference"}
        assert len(active) == 2, "preference is multi-valued: both statements must coexist"

        items = [_item(memory) for memory in active]
        views = build_evidence_views(
            items, question="What do I like to drink?",
            question_date="2024-03-01T00:00:00+00:00",
        )
        assert all(view.is_current for view in views)
        assert assess_evidence(views).state is not EvidenceAssessmentState.NO_EVIDENCE

        prompt = build_reader_prompt(
            _request("What do I like to drink?", "2024-03-01T00:00:00+00:00", items),
            policy=ReaderPolicy.evidence_first(),
        )
        assert prompt["answer_kind"] == "preference"
        assert "Combine every preference" in prompt["temporal_scaffold"]["instruction"]
    finally:
        service.store.close()


# --------------------------------------------------------------------------- #
# 4. An answer assembled from several memories is an answer
# --------------------------------------------------------------------------- #


def test_04_answer_synthesised_from_several_memories_is_not_an_abstention():
    first = _memory("m-1", "The train left Chengdu at 08:00 on Monday.")
    second = _memory("m-2", "The train arrived in Chongqing at 10:30 on Monday.")
    items = [_item(first, refs=("s1:0",)), _item(second, refs=("s2:0",))]
    client = ScriptedChatClient(responses=[json.dumps({
        "status": "ANSWER",
        "answer": "Two and a half hours.",
        "reason_code": "SUPPORTED_BY_MULTIPLE_EVIDENCE",
        "citations": [
            {"memory_id": "m-1", "quote": "The train left Chengdu at 08:00 on Monday.",
             "source_refs": ["s1:0"]},
            {"memory_id": "m-2", "quote": "The train arrived in Chongqing at 10:30 on Monday.",
             "source_refs": ["s2:0"]},
        ],
        "missing_requirements": [],
    })])
    reader = GroundedChatReader(
        client,
        ReaderSpec(provider="scripted", model="scripted-chat-v1"),
        policy=ReaderPolicy.evidence_first(),
    )
    request = _request("How long was the train journey?", "2024-01-02T00:00:00+00:00", items)
    outcome = reader_outcome(reader, request)

    assert outcome.hypothesis == "Two and a half hours."
    assert outcome.reason_code == "SUPPORTED_BY_MULTIPLE_EVIDENCE"
    assert len(outcome.citations) == 2
    assert outcome.origin is not None and outcome.origin.value == "READER"


# --------------------------------------------------------------------------- #
# 5. Competing scalars conflict; documents do not
# --------------------------------------------------------------------------- #


def test_05_competing_scalar_values_conflict_but_documents_do_not():
    chengdu = _item(_memory("m-cd", "user lives in Chengdu",
                            structured={"subject": "user", "predicate": "residence",
                                        "value": "成都", "normalized_value": "成都"}))
    shanghai = _item(_memory("m-sh", "user lives in Shanghai",
                             structured={"subject": "user", "predicate": "residence",
                                         "value": "上海", "normalized_value": "上海"}))
    views = build_evidence_views([chengdu, shanghai], question="Where does the user live?",
                                 question_date="2024-06-01T00:00:00+00:00")
    assessment = assess_evidence(views)
    assert assessment.state is EvidenceAssessmentState.CONFLICTING_EVIDENCE
    assert assessment.conflicting == {"user:residence": ["m-cd", "m-sh"]}

    # Negative control: two long documents are not two competing values. Without
    # this, every pair of formed statements would read as a contradiction.
    doc_a = _item(_memory("m-doc-a", "Here is the sheet for Monday. | Admon | 8am |",
                          structured={"subject": "assistant",
                                      "predicate": DOCUMENT_PREDICATE,
                                      "assertion": DOCUMENT_ASSERTION}))
    doc_b = _item(_memory("m-doc-b", "Here is the sheet for Tuesday. | Sara | 9am |",
                          structured={"subject": "assistant",
                                      "predicate": DOCUMENT_PREDICATE,
                                      "assertion": DOCUMENT_ASSERTION}))
    document_views = build_evidence_views(
        [doc_a, doc_b], question="What was the sheet?", question_date="2024-06-01T00:00:00+00:00"
    )
    assert assess_evidence(document_views).state is not (
        EvidenceAssessmentState.CONFLICTING_EVIDENCE
    )


# --------------------------------------------------------------------------- #
# 6. Usable evidence triggers one recheck, without forcing an unsupported answer
# --------------------------------------------------------------------------- #


def _refusal_then_answers() -> ScriptedChatClient:
    return ScriptedChatClient(responses=[
        json.dumps({"status": "ABSTAIN", "answer": None,
                    "reason_code": "NO_RELEVANT_EVIDENCE",
                    "citations": [], "missing_requirements": ["the exact date"]}),
        json.dumps({"status": "ANSWER", "answer": "2024-03-05",
                    "reason_code": "SUPPORTED_BY_SINGLE_EVIDENCE",
                    "citations": [{"memory_id": "m-date",
                                   "quote": "The renewal is due on 2024-03-05.",
                                   "source_refs": ["s1:0"]}],
                    "missing_requirements": []}),
    ])


def test_06_sufficient_evidence_forbids_abstention_and_the_refusal_is_retried():
    items = [_item(_memory("m-date", "The renewal is due on 2024-03-05."))]
    client = _refusal_then_answers()
    reader = GroundedChatReader(
        client, ReaderSpec(provider="scripted", model="scripted-chat-v1"),
        policy=ReaderPolicy.evidence_first(),
    )
    request = _request("When is the renewal due?", "2024-03-10T00:00:00+00:00", items)
    outcome = reader_outcome(reader, request)

    assert outcome.status.value == "ANSWER", "a refusal with usable evidence must be re-asked"
    assert outcome.hypothesis == "2024-03-05"
    assert outcome.calls == 2, "exactly one retry, not a retry loop"
    retry_prompt = json.loads(client.prompts[1].split("\n", 1)[1])
    assert "otherwise abstain" in retry_prompt["previous_attempt"]
    first_prompt = json.loads(client.prompts[0].split("\n", 1)[1])
    assert "requested value" in first_prompt["abstain_policy"]

    # The strict policy keeps the original behaviour: the first refusal stands.
    strict_reader = GroundedChatReader(
        _refusal_then_answers(), ReaderSpec(provider="scripted", model="scripted-chat-v1"),
        policy=ReaderPolicy.strict(),
    )
    strict_outcome = reader_outcome(
        strict_reader, _request("When is the renewal due?", "2024-03-10T00:00:00+00:00", items)
    )
    assert strict_outcome.status.value == "ABSTAIN"
    assert strict_outcome.calls == 1, "strict must not retry"


def test_malformed_reader_output_is_retried_once_with_accounting():
    items = [_item(_memory("m-date", "The renewal is due on 2024-03-05."))]
    valid = json.dumps({
        "status": "ANSWER", "answer": "2024-03-05",
        "reason_code": "SUPPORTED_BY_SINGLE_EVIDENCE",
        "citations": [{"memory_id": "m-date",
                       "quote": "The renewal is due on 2024-03-05.",
                       "source_refs": ["s1:0"]}],
        "missing_requirements": [],
    })
    client = ScriptedChatClient(
        responses=["not JSON", valid], input_tokens_per_call=11,
        output_tokens_per_call=7,
    )
    reader = GroundedChatReader(
        client, ReaderSpec(provider="scripted", model="scripted-chat-v1"),
        policy=ReaderPolicy.evidence_first(),
    )
    outcome = reader_outcome(
        reader, _request("When is the renewal due?", "2024-03-10T00:00:00+00:00", items)
    )
    assert outcome.status.value == "ANSWER"
    assert outcome.calls == 2
    assert outcome.usage.input_tokens == 22 and outcome.usage.output_tokens == 14
    assert len(outcome.attempt_errors) == 1
    assert outcome.attempt_errors[0]["reason_code"] == "UNSUPPORTED_READER_OUTPUT"
    assert "previous_attempt" in client.prompts[1]


def test_measured_v2_policy_remains_selectable_without_v3_changes():
    item = _item(_memory("m-date", "The renewal is due on 2024-03-05."))
    request = _request("When is the renewal due?", "2024-03-10T00:00:00+00:00", [item])
    policy = ReaderPolicy.evidence_first_v2()
    prompt = build_reader_prompt(request, policy=policy)
    assert policy.prompt_version == "oracle-grounded-reader-v2"
    assert "An abstention with usable evidence" in prompt["abstain_policy"]
    assert "requested value" not in prompt["abstain_policy"]
    assert policy.max_format_retries == 0


# --------------------------------------------------------------------------- #
# 7. Genuinely absent evidence abstains, without calling the reader
# --------------------------------------------------------------------------- #


def test_07_absent_evidence_abstains_without_calling_the_reader():
    silent = ScriptedChatClient(responses=[])
    decision = invoke_reader(silent, "What is my passport number?", [], as_of=None)
    assert decision.status.value == "ABSTAIN"
    assert decision.reason_code == "NO_RETRIEVAL_CANDIDATES"
    assert silent.prompts == [], "the reader must not be consulted when nothing was retrieved"

    views = build_evidence_views([], question="What is my passport number?",
                                 question_date="2024-01-01T00:00:00+00:00")
    assessment = assess_evidence(views)
    assert assessment.state is EvidenceAssessmentState.NO_EVIDENCE
    assert assessment.strong_views == 0

    # A considered refusal is a different fact from a missing retrieval, and the
    # outcome keeps them apart. Two identical refusals: the retry is bounded at
    # one, so the second is what the reason code has to describe.
    refusal = json.dumps({
        "status": "ABSTAIN", "answer": None, "reason_code": "NO_RELEVANT_EVIDENCE",
        "citations": [], "missing_requirements": ["passport number"],
    })
    refusing_client = ScriptedChatClient(responses=[refusal, refusal])
    refusing = GroundedChatReader(
        refusing_client, ReaderSpec(provider="scripted", model="scripted-chat-v1"),
        policy=ReaderPolicy.evidence_first(),
    )
    item = _item(_memory("m-other", "The user prefers window seats."))
    outcome = reader_outcome(
        refusing, _request("What is my passport number?", "2024-01-01T00:00:00+00:00", [item])
    )
    assert outcome.status.value == "ABSTAIN"
    assert outcome.calls == 2, "one initial attempt plus exactly one retry"
    assert outcome.reason_code.startswith("RETRY_EXHAUSTED_"), (
        "a refusal that survives the retry must say so in its reason code"
    )
    assert outcome.origin.value == "READER_ABSTENTION"


# --------------------------------------------------------------------------- #
# 8. A long non-trusted deliverable becomes a document memory -- and only then
# --------------------------------------------------------------------------- #


class _DenyingGate:
    """A gate that refuses every turn, but exposes document admission.

    The refusal reason is ``SEMANTIC_LOW_FUTURE_UTILITY`` on purpose: that code
    is outside the assertion-recovery set, which is exactly the path where an
    earlier version returned early and silently skipped document admission.
    """

    supports_source_type = True
    segment_assertions = True

    def __init__(self, policy: DocumentAdmissionPolicy) -> None:
        self.document_admission = policy

    def __call__(self, text, *, explicit=False, source_reliability=0.8, source_type=None):
        return GateDecision(False, 0.0, source_reliability, 0.2, "ephemeral",
                            "denied by the test gate", GateAction.SKIP,
                            "SEMANTIC_LOW_FUTURE_UTILITY", {}, "test-gate")

    def admit_document(self, text, *, source_type=None):
        return admit_document(text, source_type=source_type, policy=self.document_admission)


def test_08_document_admission_is_gated_on_source_length_and_shape():
    policy = DocumentAdmissionPolicy(mode="all", min_chars=200)

    accepted = admit_document(LONG_ASSISTANT_DELIVERABLE, source_type="assistant", policy=policy)
    assert accepted.accepted
    assert accepted.reason_code == "DOCUMENT_FROM_NON_TRUSTED_SOURCE"
    assert accepted.durability == "long_term"

    off = admit_document(LONG_ASSISTANT_DELIVERABLE, source_type="assistant",
                         policy=DocumentAdmissionPolicy(mode="off"))
    assert off.reason_code == "DOCUMENT_ADMISSION_DISABLED"

    # Only a delegated source. A user turn keeps the semantic score, and an
    # untrusted page keeps the v1 refusal.
    assert admit_document(LONG_ASSISTANT_DELIVERABLE, source_type="user",
                          policy=policy).reason_code == "DOCUMENT_SOURCE_NOT_CONDITIONAL"
    assert admit_document(LONG_ASSISTANT_DELIVERABLE, source_type="untrusted_web",
                          policy=policy).reason_code == "DOCUMENT_SOURCE_NOT_CONDITIONAL"
    # Too short.
    assert admit_document("Sure, here is a short note.", source_type="assistant",
                          policy=policy).reason_code == "DOCUMENT_TOO_SHORT"
    # Long, but one sentence is a remark, not a deliverable.
    remark = "You should definitely consider booking the earlier train " * 8
    assert len(remark) > 200, "the shape rule must be what refuses this, not the size rule"
    assert admit_document(remark, source_type="assistant",
                          policy=policy).reason_code == "DOCUMENT_NOT_DELIVERABLE_SHAPE"
    # The safety floor is never overridden by an admission policy.
    assert len(LONG_DELIVERABLE_WITH_SECRET) > 200
    assert admit_document(LONG_DELIVERABLE_WITH_SECRET, source_type="assistant",
                          policy=policy).reason_code == "SENSITIVE_DATA"


def test_08b_admitted_deliverable_produces_a_scalar_free_document_candidate():
    gate = _DenyingGate(DocumentAdmissionPolicy(mode="all", min_chars=200))
    produced = extract_candidates(LONG_ASSISTANT_DELIVERABLE, explicit=False,
                                  gate=gate, source_type="assistant")

    assert len(produced) == 1, (
        "a refused turn with an admissible deliverable must still produce a memory"
    )
    candidate = produced[0]
    assert candidate.structured_content["predicate"] == DOCUMENT_PREDICATE
    assert candidate.structured_content["assertion"] == DOCUMENT_ASSERTION
    assert candidate.structured_content["subject"] == "assistant"
    assert "value" not in candidate.structured_content
    assert "normalized_value" not in candidate.structured_content, (
        "a document must occupy no scalar slot, or it becomes an overwrite target"
    )
    assert candidate.evidence_state == "OBSERVATION"

    # Same turn, admission off: the refused turn produces nothing, as before.
    closed = _DenyingGate(DocumentAdmissionPolicy(mode="off"))
    assert extract_candidates(LONG_ASSISTANT_DELIVERABLE, explicit=False,
                              gate=closed, source_type="assistant") == []


# --------------------------------------------------------------------------- #
# 9. The re-rank prefers the newer of two otherwise identical memories
# --------------------------------------------------------------------------- #


def test_09_scoring_policy_prefers_the_newer_fact():
    old = _memory("m-old", "user lives in Chengdu", valid_from="2022-01-01T00:00:00+00:00")
    new = _memory("m-new", "user lives in Shanghai", valid_from="2024-06-01T00:00:00+00:00")
    candidate = {"dense": 0.5, "bm25": 0.5}
    as_of = "2024-07-01T00:00:00+00:00"

    legacy = ScoringPolicy.legacy()
    assert _scoring_quality(old, candidate, legacy, as_of) == pytest.approx(
        _scoring_quality(new, candidate, legacy, as_of)
    ), "the historical formula must be time-blind, or every published number changes"

    balanced = ScoringPolicy.balanced()
    assert _scoring_quality(new, candidate, balanced, as_of) > _scoring_quality(
        old, candidate, balanced, as_of
    )

    # A superseded memory falls behind a current one at any age.
    superseded = _memory("m-sup", "user lives in Chengdu",
                         valid_from="2024-06-01T00:00:00+00:00",
                         status=MemoryStatus.SUPERSEDED)
    assert _scoring_quality(superseded, candidate, balanced, as_of) < _scoring_quality(
        new, candidate, balanced, as_of
    )

    # A document is damped but never zeroed, so it stays visible.
    document = _memory("m-doc", LONG_ASSISTANT_DELIVERABLE,
                       structured={"subject": "assistant", "predicate": DOCUMENT_PREDICATE,
                                   "assertion": DOCUMENT_ASSERTION},
                       valid_from="2024-06-01T00:00:00+00:00")
    assert _scoring_quality(document, candidate, balanced, as_of) > 0.0


# --------------------------------------------------------------------------- #
# 10. Gold never reaches the writer, the prompt or the recorded context
# --------------------------------------------------------------------------- #


def _gold_case() -> LongMemEvalCase:
    """A real case whose answer is absent from its own transcript.

    The answer must not appear in the transcript: a sentinel that is *also* the
    memory content proves nothing, because then it legitimately belongs in the
    retrieval context.
    """

    turns = (
        _turn("s1", 0, "user",
              "Please remember that my golden retriever is named Biscuit.",
              "2024-05-01T10:00:00Z", has_answer=True, haystack_index=0),
        _turn("s1", 1, "assistant", "That is wonderful news about Biscuit.",
              "2024-05-01T10:00:00Z", haystack_index=1),
    )
    return LongMemEvalCase(
        question_id="q-gold",
        question="What is the name of the golden retriever I adopted?",
        answer=GOLD_SENTINEL,
        question_type="single-session-user",
        question_date="2024-05-10T10:00:00Z",
        turns=turns,
        evidence_session_ids=("s1",),
    )


def test_10_gold_never_reaches_the_writer_or_the_reader_prompt():
    case = _gold_case()
    rows = list(case.system_input())
    assert_writer_visible(rows)
    for row in rows:
        assert set(row) == set(WRITER_VISIBLE_FIELDS)

    items = [_item(_memory("m-1", "The adoption paper names the dog Biscuit.",
                           structured={"subject": "user", "predicate": "statement"}))]
    prompt = build_reader_prompt(
        _request(case.question, case.question_date_iso, items),
        policy=ReaderPolicy.evidence_first(),
    )

    assert set(prompt) <= PROMPT_TOP_LEVEL_KEYS
    serialized = json.dumps(prompt, ensure_ascii=False)
    assert GOLD_SENTINEL not in serialized
    assert case.question_type not in serialized
    assert find_evaluator_only_fields(
        {key: value for key, value in prompt.items() if key != "output_schema"}
    ) == []

    # The recorded reader context must be the annotated evidence, and neither
    # the trace nor the context may have picked up a gold field on the way.
    result = run_case(case, reader=DeterministicReader(), config=PipelineConfig())
    record = result.record
    assert result.ok
    assert GOLD_SENTINEL not in json.dumps(record, ensure_ascii=False, default=str)
    assert not find_evaluator_only_fields(record)
    assert case.answer not in record["reader_context_text"]
    assert "Biscuit" in record["reader_context_text"]
    assert "Biscuit" in (result.hypothesis or "")
