"""No-paid paired retrieval test: real semantic path is opt-in, gold stays evaluator-side."""

from __future__ import annotations

import json
import importlib.util
import sys
import types
from dataclasses import asdict
from pathlib import Path

import pytest

from dive_memory.eval_adapters.longmemeval import LongMemEvalCase, LongMemEvalTurn
from dive_memory.oracle.contracts import find_evaluator_only_fields
from dive_memory.oracle.runner import trace_filename

_SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "oracle_retrieval_ablation.py"
_SPEC = importlib.util.spec_from_file_location("oracle_retrieval_ablation", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
ablation = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ablation
_SPEC.loader.exec_module(ablation)

ARM_CHANNELS = ablation.ARM_CHANNELS
_arm_hits = ablation._arm_hits
evaluate_traces = ablation.evaluate_traces
exact_two_sided_sign_test = ablation.exact_two_sided_sign_test
model_provider = ablation.model_provider
runtime_case = ablation.runtime_case
sanitize_runtime_case = ablation.sanitize_runtime_case


class RecordingSemanticEmbedder:
    model = "test-semantic"
    revision = "test-pinned"
    dimensions = 4
    semantic_similarity = True
    provider_name = "test-semantic"

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        return [1.0, 0.0, 0.0, 0.0] if "tea" in text.casefold() else [0.0, 1.0, 0.0, 0.0]


def _case(*, answer: str = "GOLD_ANSWER_NEVER_TO_RUNTIME") -> LongMemEvalCase:
    return LongMemEvalCase(
        question_id="sample-1",
        question="What drink did I ask you to remember?",
        answer=answer,
        question_type="single-session-user",
        question_date="2024-01-03T00:00:00+00:00",
        turns=(LongMemEvalTurn(
            session_id="session-1", session_occurrence=0, turn_index=0,
            role="user", content="Please remember that my favorite drink is tea.",
            occurred_at="2024-01-01T00:00:00+00:00", has_answer=True,
        ),),
        evidence_session_ids=("session-1",),
    )


def test_three_arms_share_formation_and_never_embed_gold() -> None:
    case = _case()
    encoder = RecordingSemanticEmbedder()
    runtime_input = sanitize_runtime_case(case)
    assert find_evaluator_only_fields(asdict(runtime_input)) == []
    assert not hasattr(runtime_input, "answer")
    assert not hasattr(runtime_input, "question_type")
    assert not hasattr(runtime_input, "evidence_refs")
    assert all(not hasattr(turn, "has_answer") for turn in runtime_input.chronological_turns)
    with pytest.raises(TypeError, match="sanitized RuntimeOracleCase"):
        runtime_case(case, gate=None, embedder=encoder, limit=10,
                     token_budget=1500, scoring_name="balanced")
    trace = runtime_case(runtime_input, gate=None, embedder=encoder, limit=10,
                         token_budget=1500, scoring_name="balanced")
    assert trace["status"] == "COMPLETED"
    assert set(trace["arms"]) == set(ARM_CHANNELS)
    assert "dense" not in trace["arms"]["lexical"]["executed_channels"]
    assert "dense" in trace["arms"]["semantic"]["executed_channels"]
    assert "bm25" not in trace["arms"]["semantic"]["executed_channels"]
    assert "dense" in trace["arms"]["hybrid"]["executed_channels"]
    assert "bm25" in trace["arms"]["hybrid"]["executed_channels"]
    assert trace["arms"]["semantic"]["dense_metadata"]["model"] == encoder.model
    assert trace["formation"]["turn_memory_counts"][case.evidence_refs[0]] >= 1
    assert all(case.answer not in text for text in encoder.texts)
    assert case.answer not in json.dumps(trace)
    for name in ARM_CHANNELS:
        hits = _arm_hits(case, trace, name)
        assert hits["formed"] >= hits["retrieved"] >= hits["packed"]
        assert trace["arms"][name]["estimated_tokens"] <= 1500


def test_coverage_is_posthoc_and_reports_case_level_regressions(tmp_path: Path) -> None:
    case = _case()
    ref = case.evidence_refs[0]
    formation = {"turn_memory_counts": {ref: 1}, "turn_events": {ref: "evt-a"}}

    def arm(memory_id: str, event_id: str, packed: bool) -> dict:
        return {
            "returned_memory_ids": [memory_id],
            "returned_source_refs": [[event_id]],
            "packed_memory_ids": [memory_id] if packed else [],
            "packed_count": int(packed), "estimated_tokens": 42,
        }

    trace = {"status": "COMPLETED", "formation": formation,
             "arms": {"lexical": arm("mem-a", "evt-a", True),
                      "semantic": arm("mem-b", "evt-b", False),
                      "hybrid": arm("mem-a", "evt-a", True)}}
    (tmp_path / trace_filename(case.question_id)).write_text(json.dumps(trace), encoding="utf-8")
    report = evaluate_traces([case], tmp_path)
    assert report["is_accuracy"] is False
    assert report["arms"]["lexical"]["overall"]["packed"] == 1
    assert report["arms"]["lexical"]["overall"]["all_gold_packed_cases"] == 1
    assert report["arms"]["semantic"]["overall"]["all_gold_packed_cases"] == 0
    assert report["arms"]["semantic"]["overall"]["packed"] == 0
    assert report["paired"]["semantic"]["overall_delta"]["packed"] == -1
    assert report["paired"]["semantic"]["case_movements"]["packed"]["lost"] == [
        {"question_id": case.question_id, "delta": -1}
    ]
    semantic_exact = report["paired"]["semantic"]["exact_paired"]
    assert semantic_exact["turn_count_sign_test"]["packed"] == {
        "candidate_better": 0,
        "lexical_better": 1,
        "ties": 0,
        "discordant_pairs": 1,
        "exact_two_sided_p_value": 1.0,
    }
    assert semantic_exact["all_gold_case_mcnemar"]["packed"]["lexical_only_complete"] == 1
    assert semantic_exact["all_gold_case_mcnemar"]["packed"]["candidate_only_complete"] == 0
    assert semantic_exact["all_gold_case_mcnemar"]["packed"]["exact_mcnemar_two_sided_p_value"] == 1.0


def test_exact_sign_test_matches_small_paired_pilot() -> None:
    assert exact_two_sided_sign_test(4, 2) == pytest.approx(0.6875)
    assert exact_two_sided_sign_test(4, 3) == 1.0
    assert exact_two_sided_sign_test(0, 0) is None


def test_runtime_identity_records_reproducibility_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ablation, "_git_identity", lambda: {
        "commit": "abc123", "dirty": True, "status_entry_count": 2,
    })
    ablation.force_offline_model_mode()
    identity = ablation.runtime_identity()
    assert identity["git"] == {"commit": "abc123", "dirty": True, "status_entry_count": 2}
    assert identity["python"]["version"]
    assert identity["script_sha256"]
    assert set(identity["dependencies"]) == set(ablation.CRITICAL_DISTRIBUTIONS)
    assert identity["offline_environment"] == {
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    }


def test_selection_identity_records_method_and_seed(tmp_path: Path) -> None:
    args = ablation.build_parser().parse_args([
        "--output", str(tmp_path / "out"), "--stratified", "50", "--seed", "17",
    ])
    identity = ablation.selection_identity(args, None)
    assert identity == {
        "method": "stratified",
        "seed": 17,
        "stratified_size": 50,
        "max_cases": None,
        "ids_file": None,
        "ids_file_sha256": None,
    }


def test_uncached_model_fails_closed_without_hash_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ablation, "cached_model_path", lambda _m, _r: None)
    with pytest.raises(RuntimeError, match="no download was attempted"):
        model_provider("BAAI/bge-m3", "pinned", "cpu")


def test_model_loader_uses_local_path_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def sentence_transformer(path: str, **kwargs: object) -> object:
        seen.update({"path": path, **kwargs})
        return object()

    monkeypatch.setattr(ablation, "cached_model_path", lambda _m, _r: tmp_path)
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(
        SentenceTransformer=sentence_transformer,
    ))
    provider = model_provider("BAAI/bge-m3", "pinned", "cpu")
    assert provider.model == "BAAI/bge-m3"
    assert seen["path"] == str(tmp_path)
    assert seen["local_files_only"] is True
