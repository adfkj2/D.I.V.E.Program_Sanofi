"""The N/E/EC/ER document experiment is paired, offline and answer-free."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest

from dive_memory.embeddings import DeterministicEmbeddingProvider
from dive_memory.eval_adapters.longmemeval import LongMemEvalCase, LongMemEvalTurn
from dive_memory.gate import GateAction, GateDecision
from dive_memory.models import (
    DOCUMENT_ASSERTION,
    DOCUMENT_PREDICATE,
    EvidenceState,
    Memory,
    RetrievalItem,
)
from dive_memory.oracle.runner import trace_filename
from dive_memory.semantic_gate import DocumentAdmissionPolicy, admit_document


_SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "oracle_document_ablation.py"
_SPEC = importlib.util.spec_from_file_location("oracle_document_ablation", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
ablation = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ablation)


class _DenyingDocumentGate:
    """Refuse scalar facts but admit a long assistant deliverable as a document."""

    supports_source_type = True
    segment_assertions = True

    def __init__(self) -> None:
        self.document_admission = DocumentAdmissionPolicy(mode="all", min_chars=200)
        self.calls = 0
        self.document_calls = 0

    def __call__(self, text, *, explicit=False, source_reliability=0.8, source_type=None):
        self.calls += 1
        return GateDecision(
            False, 0.0, source_reliability, 0.2, "ephemeral", "denied by test gate",
            GateAction.SKIP, "SEMANTIC_LOW_FUTURE_UTILITY", {}, "test-gate",
        )

    def admit_document(self, text, *, source_type=None):
        self.document_calls += 1
        return admit_document(text, source_type=source_type, policy=self.document_admission)


def _case() -> LongMemEvalCase:
    document = (
        "# Telescope plan\n\n"
        + "Opening logistics, weather checks, route details, and ordinary equipment. " * 28
        + "\n\n## Final battery recommendation\n\n"
        + "Use the cobalt battery for the telescope during the overnight observation."
    )
    return LongMemEvalCase(
        question_id="document-ablation-case",
        question="Which cobalt battery did you recommend for the telescope?",
        answer="GOLD-SENTINEL-NEVER-RUNTIME",
        question_type="single-session-assistant",
        question_date="2025-01-02T00:00:00+00:00",
        turns=(LongMemEvalTurn(
            session_id="session-1", session_occurrence=1, turn_index=0,
            role="assistant", content=document,
            occurred_at="2025-01-01T00:00:00+00:00", has_answer=True,
        ),),
        evidence_session_ids=("session-1",),
    )


def _retrieval_item(memory_id: str, *, document: bool) -> RetrievalItem:
    content = (
        "# Plan\n\n" + "Background material. " * 80
        + "\n\nUse the cobalt battery for the telescope."
        if document else f"Short durable fact {memory_id}."
    )
    structured = (
        {"subject": "assistant", "predicate": DOCUMENT_PREDICATE,
         "assertion": DOCUMENT_ASSERTION, "role": "assistant"}
        if document else {"subject": "user", "predicate": "statement", "value": content}
    )
    memory = Memory(
        memory_id, "ns", "episode", EvidenceState.OBSERVATION,
        content, structured, source_event_ids=[f"evt-{memory_id}"],
        observed_at="2025-01-01T00:00:00+00:00",
        valid_from="2025-01-01T00:00:00+00:00",
    )
    return RetrievalItem(memory, 1.0, ["bm25"], [f"evt-{memory_id}"])


def test_controlled_lane_preserves_fact_order_and_caps_document_nominations() -> None:
    fact_a, fact_b = (_retrieval_item(name, document=False) for name in ("fact-a", "fact-b"))
    doc_a, doc_b, doc_c = (
        _retrieval_item(name, document=True) for name in ("doc-a", "doc-b", "doc-c")
    )
    non_documents, documents, policy = ablation._controlled_candidates(
        [doc_a, fact_a, fact_b, doc_b],
        [doc_c, doc_b, doc_a],
    )

    assert [item.memory.id for item in non_documents] == ["fact-a", "fact-b"]
    assert [item.memory.id for item in documents] == ["doc-a", "doc-c"]
    assert policy["protected_non_document_ids"] == ["fact-a", "fact-b"]
    assert policy["max_document_candidates"] == 2

    packed, control = ablation._controlled_pack(
        non_documents,
        documents,
        question="Which cobalt battery powers the telescope?",
        question_date="2025-01-02T00:00:00+00:00",
        current_only=True,
        document_spans={},
    )
    packed_ids = [item.memory.id for item in packed.items]
    assert packed_ids[:2] == ["fact-a", "fact-b"]
    assert control["document_candidates"] == 2
    assert control["documents_packed"] <= 2
    assert control["document_context_tokens"] <= 600
    assert packed.estimated_tokens <= 1500


def test_controlled_documents_cannot_expand_top_k():
    facts = [_retrieval_item(f"fact-{i}", document=False) for i in range(10)]
    doc = _retrieval_item("doc", document=True)
    kept, documents, _ = ablation._controlled_candidates(facts, [doc], limit=10)
    assert kept == facts
    assert documents == []
    _, documents, _ = ablation._controlled_candidates(facts[:9], [doc], limit=10)
    assert documents == [doc]
    _, documents, _ = ablation._controlled_candidates([], [doc], max_documents=0)
    assert documents == []


def test_runtime_forms_once_and_only_document_switches_differ() -> None:
    case = _case()
    gate = _DenyingDocumentGate()
    trace = ablation.runtime_case(
        case,
        gate=gate,
        embedder=DeterministicEmbeddingProvider(),
    )

    assert trace["status"] == "COMPLETED"
    assert set(trace["arms"]) == {"N", "E", "EC", "ER"}
    assert gate.calls == 1
    assert gate.document_calls == 1
    assert trace["formation"]["events"] == 1
    assert trace["formation"]["memories_formed"] == 1

    normal, excerpt, chunked, controlled = (
        trace["arms"][name] for name in ("N", "E", "EC", "ER")
    )
    assert normal["returned_memory_ids"] == excerpt["returned_memory_ids"]
    assert normal["returned_source_refs"] == excerpt["returned_source_refs"]
    assert "document_chunk" not in normal["executed_channels"]
    assert "document_chunk" not in excerpt["executed_channels"]
    assert "document_chunk" in chunked["executed_channels"]
    assert "document_chunk" not in controlled["executed_channels"]
    assert controlled["supplement_executed_channels"] == ["document_chunk", "temporal"]

    n_span = next(row for row in normal["visible_spans"] if row["is_document"])
    e_span = next(row for row in excerpt["visible_spans"] if row["is_document"])
    ec_span = next(row for row in chunked["visible_spans"] if row["is_document"])
    er_span = next(row for row in controlled["visible_spans"] if row["is_document"])
    assert n_span["selection_source"] == "prefix"
    assert n_span["visible_start"] == 0
    assert e_span["selection_source"] == "query_overlap"
    assert e_span["visible_start"] > 0
    assert ec_span["selection_source"] == "indexed_chunk"
    assert er_span["selection_source"] == "indexed_chunk"
    assert "cobalt battery" not in normal["reader_context_text"].casefold()
    assert "cobalt battery" in excerpt["reader_context_text"].casefold()
    assert "cobalt battery" in chunked["reader_context_text"].casefold()
    assert "cobalt battery" in controlled["reader_context_text"].casefold()
    assert all(trace["arms"][name]["context_tokens"] <= 1500
               for name in ("N", "E", "EC", "ER"))
    control = controlled["controlled_document_policy"]
    assert control["document_candidates"] <= 2
    assert control["documents_packed"] <= 2
    assert control["document_context_tokens"] <= 600

    runtime_json = json.dumps(trace, ensure_ascii=False)
    assert case.answer not in runtime_json
    assert case.question_type not in runtime_json


def test_gold_evaluation_is_posthoc_and_explicitly_not_accuracy(tmp_path: Path) -> None:
    case = _case()
    trace = ablation.runtime_case(case, gate=_DenyingDocumentGate())
    path = tmp_path / trace_filename(case.question_id)
    path.write_text(json.dumps(trace), encoding="utf-8")

    assert case.answer not in path.read_text(encoding="utf-8")
    report = ablation.evaluate_traces([case], tmp_path)

    assert report["is_accuracy"] is False
    assert report["official_score"] == "NOT_EVALUATED"
    assert report["cases_completed"] == 1
    for arm in ("N", "E", "EC", "ER"):
        assert report["arms"][arm]["overall"]["formed"] == 1
        assert report["arms"][arm]["overall"]["retrieved"] == 1
        assert report["arms"][arm]["overall"]["packed"] == 1
        assert report["per_case"][0]["arms"][arm]["visible_spans"]
        assert report["arms"][arm]["overall"]["packed_document_items"] >= 1
    assert report["paired"]["E_vs_N"]["retrieval_changed_cases"] == []
    assert report["paired"]["E_vs_N"]["context_changed_cases"] == [case.question_id]
    assert report["arms"]["ER"]["controlled_budget_audit"]["document_budget_violations"] == 0


def test_abstention_distractors_are_not_positive_recall_targets(tmp_path: Path) -> None:
    case = replace(
        _case(), question_id="document-ablation-case_abs", answer="unknown",
    )
    trace = ablation.runtime_case(case, gate=_DenyingDocumentGate())
    (tmp_path / trace_filename(case.question_id)).write_text(
        json.dumps(trace), encoding="utf-8",
    )

    report = ablation.evaluate_traces([case], tmp_path)

    assert report["per_case"][0]["should_abstain"] is True
    assert report["per_case"][0]["gold_turns"] == 0
    for arm in ("N", "E", "EC", "ER"):
        overall = report["arms"][arm]["overall"]
        assert overall["gold_turns"] == 0
        assert overall["formed"] == overall["retrieved"] == overall["packed"] == 0


def test_fixed_gate_fails_closed_when_checkpoint_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ablation, "cached_model_path", lambda: None)
    with pytest.raises(RuntimeError, match="no network request was attempted"):
        ablation.load_fixed_gate()


def test_identity_discloses_hash_embedder_and_actual_v3_budget() -> None:
    identity = ablation._identity("dataset-sha", ["q1"])
    assert identity["network"] == "forced-offline"
    assert identity["api_calls"] == 0
    assert identity["retrieval_embedder"]["semantic"] is False
    assert identity["shared"]["formation_passes_per_case"] == 1
    assert identity["shared"]["scoring_policy"] == "balanced"
    assert identity["shared"]["top_k"] == 10
    assert identity["shared"]["token_budget"] == 1500
    assert identity["shared"]["reader_prompt_version"] == "oracle-grounded-reader-v3"
    assert identity["arms"] == {
        "N": {"document_selection": "prefix", "document_chunk_search": "off"},
        "E": {"document_selection": "query_excerpt", "document_chunk_search": "off"},
        "EC": {"document_selection": "query_excerpt", "document_chunk_search": "all"},
        "ER": {
            "document_selection": "query_excerpt",
            "document_chunk_search": "controlled_supplement",
            "max_document_candidates": 2,
            "document_token_budget": 600,
            "protect_baseline_non_documents": True,
        },
    }


def test_controlled_runner_matches_ablation_visible_packet():
    from dive_memory.oracle.pipeline import PipelineConfig, run_case
    from dive_memory.oracle.reader import DeterministicReader
    case = _case()
    trace = ablation.runtime_case(case, gate=_DenyingDocumentGate())
    config = PipelineConfig(scoring_policy_name="balanced", document_selection_name="query_excerpt",
                            document_chunk_search_name="controlled_supplement")
    outcome = run_case(case, reader=DeterministicReader(), gate=_DenyingDocumentGate(), config=config)
    assert outcome.ok, outcome.record
    assert outcome.record["reader_context_text"] == trace["arms"]["ER"]["reader_context_text"]
    from dataclasses import replace
    baseline = run_case(case, reader=DeterministicReader(), gate=_DenyingDocumentGate(),
                        config=replace(config, reader_policy_name="evidence_first_v2"))
    assert baseline.ok
    assert baseline.record["reader_context_text"] == outcome.record["reader_context_text"]
