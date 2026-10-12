"""Zero-API paired Oracle ablation for document selection and controlled rescue.

The four arms share exactly one formation pass and one in-memory store per
question::

    N   prefix / chunk search off
    E   query excerpt / chunk search off
    EC  query excerpt / chunk search all
    ER  query excerpt / protected facts + bounded chunk-only document rescue

All arms use the semantic write gate from one pinned, local BGE-M3 checkpoint,
the deterministic (non-semantic) retrieval embedder, balanced re-ranking,
top-k 10, and a 1,500-token v3 annotated-evidence budget.  No answer model or
judge is called.

Runtime traces are deliberately answer-free.  Only after every selected case
has a complete four-arm trace does :func:`evaluate_traces` read question type
and gold evidence-turn references.  Its formed/retrieved/packed measurements
are evidence-reach diagnostics, not answer accuracy.  Oracle does not provide
gold character spans, so a packed evidence turn does not prove that the
answer-bearing sentence is inside the recorded visible document span.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from typing import Any, Mapping, Sequence


# Set the hard no-network boundary before any optional Hugging Face import.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "ops"))
from oracle_retrieval_ablation import RuntimeOracleCase, sanitize_runtime_case, runtime_identity

from dive_memory.context import PackedContext, pack_context  # noqa: E402
from dive_memory.eval_manifest import code_snapshot
from dive_memory.embeddings import DeterministicEmbeddingProvider  # noqa: E402
from dive_memory.models import DOCUMENT_PREDICATE, RetrievalItem  # noqa: E402
from dive_memory.oracle.contracts import ORACLE_EXPECTED_CASES, assert_writer_visible  # noqa: E402
from dive_memory.oracle.evidence import build_evidence_views, render_evidence_block  # noqa: E402
from dive_memory.oracle.loader import load_oracle_dataset, select_cases  # noqa: E402
from dive_memory.oracle.pipeline import (  # noqa: E402
    CONTEXT_BUDGET_VERSION,
    _after,
    ingest_case,
    packing_content_caps,
)
from dive_memory.oracle.reader import reader_policy  # noqa: E402
from dive_memory.oracle.runner import atomic_write_json, trace_filename  # noqa: E402
from dive_memory.retrieval import RetrievalConfig, scoring_policy  # noqa: E402
from dive_memory.semantic_gate import (  # noqa: E402
    POLICY_VERSION,
    DocumentAdmissionPolicy,
    load_local_gate,
)
from dive_memory.service import MemoryService  # noqa: E402
from dive_memory.token_budget import ApproximateTokenCounter  # noqa: E402


SCHEMA_VERSION = "dive-oracle-document-ablation-v3"
DEFAULT_DATASET = ROOT / "eval/external/longmemeval/longmemeval_oracle.json"
DEFAULT_IDS = ROOT / "eval/reports/oracle-document-ablation-50.ids"
GATE_MODEL = "BAAI/bge-m3"
GATE_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
GATE_DEVICE = "cpu"
GATE_CACHE_SIZE = 8192
GATE_BATCH_SIZE = 8
GATE_MAX_SEQUENCE_LENGTH = 512
DOCUMENT_MIN_CHARS = 200
DOCUMENT_MAX_CHARS = 8000
SCORING_POLICY = "balanced"
TOP_K = 10
TOKEN_BUDGET = 1500
CONTROLLED_DOCUMENT_LIMIT = 2
CONTROLLED_DOCUMENT_TOKEN_BUDGET = 600
READER_POLICY = "evidence_first"
LEVELS = ("formed", "retrieved", "packed")

ARM_CONFIG: dict[str, dict[str, Any]] = {
    "N": {"document_selection": "prefix", "document_chunk_search": "off"},
    "E": {"document_selection": "query_excerpt", "document_chunk_search": "off"},
    "EC": {"document_selection": "query_excerpt", "document_chunk_search": "all"},
    "ER": {
        "document_selection": "query_excerpt",
        "document_chunk_search": "controlled_supplement",
        "max_document_candidates": CONTROLLED_DOCUMENT_LIMIT,
        "document_token_budget": CONTROLLED_DOCUMENT_TOKEN_BUDGET,
        "protect_baseline_non_documents": True,
    },
}
STANDARD_ARMS = ("N", "E", "EC")


def cached_model_path(model: str = GATE_MODEL, revision: str = GATE_REVISION) -> Path | None:
    """Return a pinned local snapshot or ``None`` without attempting a download."""

    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None
    try:
        found = try_to_load_from_cache(model, "modules.json", revision=revision)
    except Exception:  # noqa: BLE001 - a cache miss is reported by the caller
        return None
    if isinstance(found, str) and Path(found).is_file():
        return Path(found).parent
    return None


def load_fixed_gate() -> Any:
    """Load the one allowed gate configuration, locally and fail-closed."""

    if cached_model_path() is None:
        raise RuntimeError(
            f"pinned local checkpoint {GATE_MODEL}@{GATE_REVISION} is not cached; "
            "no network request was attempted"
        )
    return load_local_gate(
        model=GATE_MODEL,
        revision=GATE_REVISION,
        device=GATE_DEVICE,
        cache_size=GATE_CACHE_SIZE,
        encode_batch_size=GATE_BATCH_SIZE,
        max_sequence_length=GATE_MAX_SEQUENCE_LENGTH,
        document_admission=DocumentAdmissionPolicy(
            mode="all", min_chars=DOCUMENT_MIN_CHARS, max_chars=DOCUMENT_MAX_CHARS,
        ),
    )


def _retrieval_config(arm_name: str) -> RetrievalConfig:
    arm = ARM_CONFIG[arm_name]
    return RetrievalConfig.full(
        include_multi_hop=False,
        include_document_chunks=arm["document_chunk_search"] == "all",
        name=f"oracle-document-{arm_name}",
        scoring=scoring_policy(SCORING_POLICY),
    )


from dive_memory.oracle.controlled_documents import (
    _document_supplement_config, _controlled_candidates, _controlled_pack, _is_document,
)


def _visible_spans(packed: Any) -> list[dict[str, Any]]:
    """Describe the exact source substring available to the annotated reader."""

    truncations = {
        str(row["memory_id"]): row
        for row in packed.decisions
        if row.get("decision") == "TRUNCATE"
    }
    rows: list[dict[str, Any]] = []
    for original, visible in zip(packed.items, packed.visible_items, strict=True):
        source = original.memory.content
        shown = visible.memory.content
        decision = truncations.get(original.memory.id)
        if decision:
            start = int(decision["visible_start"])
            end = int(decision["visible_end"])
            selection_source = str(decision["selection_source"])
        else:
            start, end, selection_source = 0, len(source), "full"
        if source[start:end] != shown:
            raise RuntimeError(f"visible span does not reconstruct memory {original.memory.id}")
        predicate = str((original.memory.structured_content or {}).get("predicate", ""))
        rows.append({
            "memory_id": original.memory.id,
            "predicate": predicate,
            "is_document": predicate == DOCUMENT_PREDICATE,
            "source_refs": list(original.source_refs),
            "channels": list(original.channels),
            "original_chars": len(source),
            "visible_start": start,
            "visible_end": end,
            "visible_chars": len(shown),
            "selection_source": selection_source,
            "visible_sha256": hashlib.sha256(shown.encode("utf-8")).hexdigest(),
        })
    return rows


def runtime_case(
    case: Any,
    *,
    gate: Any,
    embedder: Any | None = None,
    limit: int = TOP_K,
    token_budget: int = TOKEN_BUDGET,
) -> dict[str, Any]:
    """Form once, then produce N/E/EC/ER traces from writer-visible fields only.

    This function must not read ``answer``, ``question_type``,
    ``evidence_refs`` or ``should_abstain``.  Those belong exclusively to the
    post-hoc evaluator.
    """

    if not isinstance(case, RuntimeOracleCase):
        case = sanitize_runtime_case(case)
    cutoff = case.visibility_cutoff_iso
    turns = tuple(turn for turn in case.chronological_turns if not _after(turn, case))
    retrieval_embedder = embedder or DeterministicEmbeddingProvider()
    service = MemoryService(gate=gate, embedder=retrieval_embedder, deterministic_ids=True)
    total_started = perf_counter()
    try:
        formation_started = perf_counter()
        formation = ingest_case(service, case, turns=turns, gate=gate)
        formation_ms = (perf_counter() - formation_started) * 1000
        arms: dict[str, Any] = {}
        for arm_name in STANDARD_ARMS:
            arm_config = ARM_CONFIG[arm_name]
            arm_started = perf_counter()
            retrieve_started = perf_counter()
            result = service.retrieve(
                case.namespace,
                case.question,
                limit=max(1, int(limit)),
                as_of=cutoff,
                observed_as_of=cutoff,
                config=_retrieval_config(arm_name),
            )
            retrieve_ms = (perf_counter() - retrieve_started) * 1000

            pack_started = perf_counter()
            current_only = result.plan.get("temporal_mode") not in {"historical", "earliest", "all"}
            budget_policy = reader_policy(READER_POLICY)

            def render_for_budget(visible_items: Sequence[Any]) -> str:
                views = build_evidence_views(
                    visible_items,
                    question=case.question,
                    question_date=cutoff,
                )
                return render_evidence_block(views)

            chunk_spans = {
                str(candidate["memory_id"]): tuple(candidate["document_chunk_span"])
                for candidate in result.trace.get("candidates", ())
                if candidate.get("selected") and candidate.get("document_chunk_span")
            }
            packed = pack_context(
                result.items,
                max(0, int(token_budget)),
                current_only=current_only,
                content_caps=packing_content_caps(scoring_policy(SCORING_POLICY)),
                question=case.question,
                document_selection=arm_config["document_selection"],
                document_spans=chunk_spans,
                render_for_budget=render_for_budget if budget_policy.annotate_evidence else None,
            )
            pack_ms = (perf_counter() - pack_started) * 1000
            reader_context = render_for_budget(packed.visible_items)
            if reader_context != packed.text:
                raise RuntimeError("reader-visible annotated evidence differs from budgeted context")
            if packed.estimated_tokens > token_budget:
                raise RuntimeError("reader-visible annotated evidence exceeds the configured budget")

            candidate_chunk_spans = [
                {
                    "memory_id": str(candidate["memory_id"]),
                    "start": int(candidate["document_chunk_span"][0]),
                    "end": int(candidate["document_chunk_span"][1]),
                }
                for candidate in result.trace.get("candidates", ())
                if candidate.get("selected") and candidate.get("document_chunk_span")
            ]
            arms[arm_name] = {
                **arm_config,
                "retrieval_config": _retrieval_config(arm_name).to_dict(),
                "executed_channels": result.trace.get("executed_channels", []),
                "returned_memory_ids": [item.memory.id for item in result.items],
                "returned_source_refs": [list(item.source_refs) for item in result.items],
                "retrieved_document_chunk_spans": candidate_chunk_spans,
                "packed_memory_ids": [item.memory.id for item in packed.items],
                "packed_count": len(packed.items),
                "omitted": packed.omitted,
                "packing_decisions": packed.decisions,
                "visible_spans": _visible_spans(packed),
                "reader_context_text": reader_context,
                "reader_context_sha256": hashlib.sha256(reader_context.encode("utf-8")).hexdigest(),
                "context_tokens": packed.estimated_tokens,
                "token_counter": packed.token_counter,
                "token_counter_exact": not packed.token_count_degraded,
                "timing_ms": {
                    "retrieve": retrieve_ms,
                    "pack": pack_ms,
                    "arm_total": (perf_counter() - arm_started) * 1000,
                },
            }

        # ER keeps the baseline RRF untouched. A separate chunk-only lookup may
        # nominate documents, but its scores never enter the baseline fusion.
        er_started = perf_counter()
        retrieve_started = perf_counter()
        er_baseline = service.retrieve(
            case.namespace,
            case.question,
            limit=max(1, int(limit)),
            as_of=cutoff,
            observed_as_of=cutoff,
            config=_retrieval_config("E"),
        )
        er_supplement = service.retrieve(
            case.namespace,
            case.question,
            limit=max(20, int(limit) * 2),
            as_of=cutoff,
            observed_as_of=cutoff,
            config=_document_supplement_config(),
        )
        er_retrieve_ms = (perf_counter() - retrieve_started) * 1000
        non_documents, documents, candidate_policy = _controlled_candidates(
            er_baseline.items,
            er_supplement.items,
            max_documents=CONTROLLED_DOCUMENT_LIMIT,
            limit=limit,
        )
        if [item.memory.id for item in non_documents] != [
            item.memory.id for item in er_baseline.items if not _is_document(item)
        ]:
            raise RuntimeError("ER changed baseline non-document order")
        supplement_spans = {
            str(candidate["memory_id"]): tuple(candidate["document_chunk_span"])
            for candidate in er_supplement.trace.get("candidates", ())
            if candidate.get("selected") and candidate.get("document_chunk_span")
        }
        er_pack_started = perf_counter()
        er_current_only = er_baseline.plan.get("temporal_mode") not in {
            "historical", "earliest", "all",
        }
        er_packed, packing_policy = _controlled_pack(
            non_documents,
            documents,
            question=case.question,
            question_date=cutoff,
            current_only=er_current_only,
            document_spans=supplement_spans,
            token_budget=max(0, int(token_budget)),
            document_token_budget=CONTROLLED_DOCUMENT_TOKEN_BUDGET,
        )
        er_pack_ms = (perf_counter() - er_pack_started) * 1000

        def render_er(visible_items: Sequence[RetrievalItem]) -> str:
            return render_evidence_block(build_evidence_views(
                visible_items, question=case.question, question_date=cutoff,
            ))

        er_context = render_er(er_packed.visible_items)
        if er_context != er_packed.text:
            raise RuntimeError("ER reader context differs from its budgeted evidence")
        er_candidates = [*non_documents, *documents]
        er_document_spans = [
            {"memory_id": item.memory.id,
             "start": int(supplement_spans[item.memory.id][0]),
             "end": int(supplement_spans[item.memory.id][1])}
            for item in documents if item.memory.id in supplement_spans
        ]
        er_config = ARM_CONFIG["ER"]
        arms["ER"] = {
            **er_config,
            "retrieval_config": {
                "baseline": _retrieval_config("E").to_dict(),
                "supplement": _document_supplement_config().to_dict(),
                "fusion": "none_between_lanes",
            },
            "executed_channels": er_baseline.trace.get("executed_channels", []),
            "supplement_executed_channels": er_supplement.trace.get("executed_channels", []),
            "returned_memory_ids": [item.memory.id for item in er_candidates],
            "returned_source_refs": [list(item.source_refs) for item in er_candidates],
            "retrieved_document_chunk_spans": er_document_spans,
            "packed_memory_ids": [item.memory.id for item in er_packed.items],
            "packed_count": len(er_packed.items),
            "omitted": er_packed.omitted,
            "packing_decisions": er_packed.decisions,
            "visible_spans": _visible_spans(er_packed),
            "reader_context_text": er_context,
            "reader_context_sha256": hashlib.sha256(er_context.encode("utf-8")).hexdigest(),
            "context_tokens": er_packed.estimated_tokens,
            "token_counter": er_packed.token_counter,
            "token_counter_exact": not er_packed.token_count_degraded,
            "controlled_document_policy": {**candidate_policy, **packing_policy},
            "timing_ms": {
                "retrieve": er_retrieve_ms,
                "pack": er_pack_ms,
                "arm_total": (perf_counter() - er_started) * 1000,
            },
        }

        # N and E intentionally share the exact retrieval policy.  Failing here
        # prevents a packing experiment from silently becoming a retrieval one.
        for field in ("returned_memory_ids", "returned_source_refs", "executed_channels"):
            if arms["N"][field] != arms["E"][field]:
                raise RuntimeError(f"N/E retrieval differs in {field}")
        baseline_non_document_ids = [
            item.memory.id for item in er_baseline.items if not _is_document(item)
        ]
        if arms["ER"]["returned_memory_ids"][:len(baseline_non_document_ids)] != baseline_non_document_ids:
            raise RuntimeError("ER did not preserve every baseline non-document candidate")
        er_documents = [span for span in arms["ER"]["visible_spans"] if span["is_document"]]
        if len(er_documents) > CONTROLLED_DOCUMENT_LIMIT:
            raise RuntimeError("ER packed too many document fragments")
        return {
            "schema_version": SCHEMA_VERSION,
            "question_id": case.question_id,
            "status": "COMPLETED",
            "formation": formation,
            "formation_timing_ms": formation_ms,
            "arms": arms,
            "total_timing_ms": (perf_counter() - total_started) * 1000,
        }
    finally:
        service.store.close()


def _arm_hits(case: Any, trace: Mapping[str, Any], arm_name: str) -> dict[str, int]:
    """Map evaluator gold turn refs to formed, returned and packed sources."""

    formation = trace["formation"]
    arm = trace["arms"][arm_name]
    packed_ids = set(arm["packed_memory_ids"])
    retrieved_sources: set[str] = set()
    packed_sources: set[str] = set()
    for memory_id, refs in zip(
        arm["returned_memory_ids"], arm["returned_source_refs"], strict=True,
    ):
        retrieved_sources.update(str(ref) for ref in refs)
        if memory_id in packed_ids:
            packed_sources.update(str(ref) for ref in refs)
    hits = {level: 0 for level in LEVELS}
    # Abstention rows contain distractor sessions, not a positive evidence
    # target. Counting any dataset flag on those rows as recall would reward a
    # system for retrieving material from which it should refuse to answer.
    evidence_refs = () if case.should_abstain else case.evidence_refs
    for ref in evidence_refs:
        event_id = formation["turn_events"].get(ref)
        hits["formed"] += int(int(formation["turn_memory_counts"].get(ref, 0)) > 0)
        hits["retrieved"] += int(bool(event_id) and event_id in retrieved_sources)
        hits["packed"] += int(bool(event_id) and event_id in packed_sources)
    return hits


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _timing_summary(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "mean": mean(values) if values else None,
        "median": median(values) if values else None,
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def evaluate_traces(cases: Sequence[Any], trace_dir: Path) -> dict[str, Any]:
    """Read gold only after runtime traces exist and report evidence reach."""

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    failures: list[dict[str, str]] = []
    for case in cases:
        path = trace_dir / trace_filename(case.question_id)
        if not path.exists():
            missing.append(case.question_id)
            continue
        trace = json.loads(path.read_text(encoding="utf-8"))
        if trace.get("status") != "COMPLETED" or set(trace.get("arms", {})) != set(ARM_CONFIG):
            failures.append({
                "question_id": case.question_id,
                "error": str(trace.get("error", "incomplete four-arm trace")),
            })
            continue
        row: dict[str, Any] = {
            "question_id": case.question_id,
            "question_type": case.question_type,
            "should_abstain": case.should_abstain,
            "gold_turns": 0 if case.should_abstain else len(case.evidence_refs),
            "formation_timing_ms": trace.get("formation_timing_ms"),
            "total_timing_ms": trace.get("total_timing_ms"),
            "arms": {},
        }
        for arm_name in ARM_CONFIG:
            arm = trace["arms"][arm_name]
            row["arms"][arm_name] = {
                **_arm_hits(case, trace, arm_name),
                "retrieved_items": len(arm["returned_memory_ids"]),
                "returned_memory_ids": arm["returned_memory_ids"],
                "packed_items": int(arm["packed_count"]),
                "context_tokens": int(arm["context_tokens"]),
                "reader_context_sha256": arm["reader_context_sha256"],
                "packed_memory_ids": arm["packed_memory_ids"],
                "visible_spans": arm["visible_spans"],
                "controlled_document_policy": arm.get("controlled_document_policy"),
                "timing_ms": arm["timing_ms"],
            }
        rows.append(row)

    by_arm: dict[str, Any] = {}
    for arm_name in ARM_CONFIG:
        by_type: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "cases": 0,
                "positive_target_cases": 0,
                "gold_turns": 0,
                **{level: 0 for level in LEVELS},
                "packed_items": 0,
                "packed_document_items": 0,
                "context_tokens": [],
                "arm_total_ms": [],
            }
        )
        for row in rows:
            bucket = by_type[row["question_type"]]
            bucket["cases"] += 1
            bucket["positive_target_cases"] += int(row["gold_turns"] > 0)
            bucket["gold_turns"] += row["gold_turns"]
            arm = row["arms"][arm_name]
            for level in LEVELS:
                bucket[level] += arm[level]
            bucket["packed_items"] += arm["packed_items"]
            bucket["packed_document_items"] += sum(
                bool(span["is_document"]) for span in arm["visible_spans"]
            )
            bucket["context_tokens"].append(arm["context_tokens"])
            bucket["arm_total_ms"].append(arm["timing_ms"]["arm_total"])

        serialised_types: dict[str, Any] = {}
        for question_type, bucket in sorted(by_type.items()):
            gold_turns = bucket["gold_turns"]
            serialised_types[question_type] = {
                key: value for key, value in bucket.items()
                if key not in {"context_tokens", "arm_total_ms"}
            }
            for level in LEVELS:
                serialised_types[question_type][f"{level}_rate"] = (
                    bucket[level] / gold_turns if gold_turns else None
                )
            serialised_types[question_type]["packed_document_share"] = (
                bucket["packed_document_items"] / bucket["packed_items"]
                if bucket["packed_items"] else None
            )
            serialised_types[question_type]["context_tokens"] = _timing_summary(
                bucket["context_tokens"]
            )
            serialised_types[question_type]["arm_total_ms"] = _timing_summary(
                bucket["arm_total_ms"]
            )

        overall_gold = sum(row["gold_turns"] for row in rows)
        totals = {level: sum(row["arms"][arm_name][level] for row in rows) for level in LEVELS}
        packed_items = sum(row["arms"][arm_name]["packed_items"] for row in rows)
        packed_documents = sum(
            bool(span["is_document"])
            for row in rows for span in row["arms"][arm_name]["visible_spans"]
        )
        by_arm[arm_name] = {
            "configuration": ARM_CONFIG[arm_name],
            "overall": {
                "cases": len(rows),
                "positive_target_cases": sum(row["gold_turns"] > 0 for row in rows),
                "abstention_cases": sum(row["should_abstain"] for row in rows),
                "gold_turns": overall_gold,
                **totals,
                "packed_items": packed_items,
                "packed_document_items": packed_documents,
                "packed_document_share": packed_documents / packed_items if packed_items else None,
                **{f"{level}_rate": totals[level] / overall_gold if overall_gold else None
                   for level in LEVELS},
                "context_tokens": _timing_summary(
                    [row["arms"][arm_name]["context_tokens"] for row in rows]
                ),
                "arm_total_ms": _timing_summary(
                    [row["arms"][arm_name]["timing_ms"]["arm_total"] for row in rows]
                ),
            },
            "by_question_type": serialised_types,
        }
        if arm_name == "ER":
            controls = [row["arms"][arm_name]["controlled_document_policy"] for row in rows]
            by_arm[arm_name]["controlled_budget_audit"] = {
                "document_candidate_limit": CONTROLLED_DOCUMENT_LIMIT,
                "document_token_budget": CONTROLLED_DOCUMENT_TOKEN_BUDGET,
                "max_document_candidates_observed": max(
                    (int(control["document_candidates"]) for control in controls), default=0,
                ),
                "max_documents_packed_observed": max(
                    (int(control["documents_packed"]) for control in controls), default=0,
                ),
                "document_context_tokens": _timing_summary([
                    int(control["document_context_tokens"]) for control in controls
                ]),
                "candidate_limit_violations": sum(
                    int(control["document_candidates"]) > CONTROLLED_DOCUMENT_LIMIT
                    for control in controls
                ),
                "packed_limit_violations": sum(
                    int(control["documents_packed"]) > CONTROLLED_DOCUMENT_LIMIT
                    for control in controls
                ),
                "document_budget_violations": sum(
                    int(control["document_context_tokens"]) > CONTROLLED_DOCUMENT_TOKEN_BUDGET
                    for control in controls
                ),
            }

    paired: dict[str, Any] = {}
    for baseline, candidate in (
        ("N", "E"), ("E", "EC"), ("N", "EC"),
        ("E", "ER"), ("N", "ER"), ("EC", "ER"),
    ):
        movements = {level: {"gained": [], "lost": [], "unchanged": 0} for level in LEVELS}
        by_type: dict[str, dict[str, Any]] = defaultdict(
            lambda: {level: {"gained_cases": 0, "lost_cases": 0, "net_gold_turns": 0}
                     for level in LEVELS}
        )
        retrieval_changed: list[str] = []
        packing_changed: list[str] = []
        context_changed: list[str] = []
        visible_span_changed: list[str] = []
        for row in rows:
            before, after = row["arms"][baseline], row["arms"][candidate]
            for level in LEVELS:
                delta = after[level] - before[level]
                bucket = by_type[row["question_type"]][level]
                bucket["net_gold_turns"] += delta
                if delta > 0:
                    bucket["gained_cases"] += 1
                    movements[level]["gained"].append({"question_id": row["question_id"], "delta": delta})
                elif delta < 0:
                    bucket["lost_cases"] += 1
                    movements[level]["lost"].append({"question_id": row["question_id"], "delta": delta})
                else:
                    movements[level]["unchanged"] += 1
            if before["returned_memory_ids"] != after["returned_memory_ids"]:
                retrieval_changed.append(row["question_id"])
            if before["packed_memory_ids"] != after["packed_memory_ids"]:
                packing_changed.append(row["question_id"])
            if before["reader_context_sha256"] != after["reader_context_sha256"]:
                context_changed.append(row["question_id"])
            if before["visible_spans"] != after["visible_spans"]:
                visible_span_changed.append(row["question_id"])
        paired[f"{candidate}_vs_{baseline}"] = {
            "baseline": baseline,
            "candidate": candidate,
            "gold_turn_delta": {
                level: by_arm[candidate]["overall"][level] - by_arm[baseline]["overall"][level]
                for level in LEVELS
            },
            "case_movements": movements,
            "by_question_type": dict(sorted(by_type.items())),
            "retrieval_changed_cases": retrieval_changed,
            "packing_changed_cases": packing_changed,
            "context_changed_cases": context_changed,
            "visible_span_changed_cases": visible_span_changed,
        }

    formation_times = [float(row["formation_timing_ms"]) for row in rows
                       if isinstance(row.get("formation_timing_ms"), (int, float))]
    total_times = [float(row["total_timing_ms"]) for row in rows
                   if isinstance(row.get("total_timing_ms"), (int, float))]
    return {
        "schema_version": SCHEMA_VERSION,
        "is_accuracy": False,
        "official_score": "NOT_EVALUATED",
        "api_calls": 0,
        "cases_selected": len(cases),
        "cases_completed": len(rows),
        "question_type_counts": dict(sorted(Counter(row["question_type"] for row in rows).items())),
        "missing_traces": missing,
        "failures": failures,
        "timing_ms": {
            "formation_once_per_case": _timing_summary(formation_times),
            "four_arm_total_per_case": _timing_summary(total_times),
        },
        "arms": by_arm,
        "paired": paired,
        "per_case": rows,
        "interpretation": (
            "Formed/retrieved/packed counts measure gold evidence-turn reach after all four "
            "answer-free runtime artifacts were produced. They are not answer accuracy. Oracle "
            "abstention rows have no positive recall target and therefore contribute zero gold turns. "
            "Oracle has no gold character offsets: a packed turn can still expose the wrong passage, "
            "so visible_start/visible_end and reader_context_text must be audited separately. "
            "Token counts cover the actual v3 annotated evidence block using the project's "
            "approximate counter; they exclude prompt instructions because no reader was called."
        ),
    }


def _identity(dataset_sha256: str, selected_ids: Sequence[str]) -> dict[str, Any]:
    policy = reader_policy(READER_POLICY)
    embedder = DeterministicEmbeddingProvider()
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_sha256": dataset_sha256,
        "code_snapshot": code_snapshot(ROOT),
        "selected_question_ids": list(selected_ids),
        "semantic_gate": {
            "policy": POLICY_VERSION,
            "model": GATE_MODEL,
            "revision": GATE_REVISION,
            "device": GATE_DEVICE,
            "cache_size": GATE_CACHE_SIZE,
            "batch_size": GATE_BATCH_SIZE,
            "max_sequence_length": GATE_MAX_SEQUENCE_LENGTH,
            "document_admission": {
                "mode": "all",
                "min_chars": DOCUMENT_MIN_CHARS,
                "max_chars": DOCUMENT_MAX_CHARS,
            },
        },
        "retrieval_embedder": {
            "provider": embedder.provider_name,
            "model": embedder.model,
            "revision": embedder.revision,
            "dimensions": embedder.dimensions,
            "semantic": embedder.semantic_similarity,
        },
        "shared": {
            "formation_passes_per_case": 1,
            "scoring_policy": SCORING_POLICY,
            "top_k": TOP_K,
            "token_budget": TOKEN_BUDGET,
            "reader_policy": policy.name,
            "reader_prompt_version": policy.prompt_version,
            "context_budget_version": CONTEXT_BUDGET_VERSION,
        },
        "arms": ARM_CONFIG,
        "network": "forced-offline",
        "api_calls": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--ids-file", type=Path, default=DEFAULT_IDS)
    parser.add_argument("--max-cases", type=int, help="head slice of the fixed ID list (smoke only)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate dataset, fixed IDs and local cache; write nothing")
    parser.add_argument("--resume", action="store_true",
                        help="continue only an identical saved selection/configuration")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    expected = None if args.allow_partial else ORACLE_EXPECTED_CASES
    dataset = load_oracle_dataset(args.dataset, expected_cases=expected)
    if not args.ids_file.is_file():
        raise SystemExit(f"fixed ID file does not exist: {args.ids_file}")
    ids = [line.strip() for line in args.ids_file.read_text(encoding="utf-8").splitlines()
           if line.strip()]
    if len(ids) != len(set(ids)):
        raise SystemExit("fixed ID file contains duplicate question IDs")
    selection = select_cases(dataset.cases, question_ids=ids, max_cases=args.max_cases)
    if not selection:
        raise SystemExit("selection contains no cases")
    identity = _identity(dataset.sha256, [case.question_id for case in selection])
    fingerprint = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    checkpoint = cached_model_path()
    if args.dry_run:
        print(json.dumps({
            "dataset_sha256": dataset.sha256,
            "cases_selected": len(selection),
            "selected_question_ids": [case.question_id for case in selection],
            "configuration_sha256": fingerprint,
            "local_checkpoint": str(checkpoint) if checkpoint else None,
            "cache_manifest_present": checkpoint is not None,
            "network": "forced-offline",
            "api_calls": 0,
            "note": "No model was loaded, no memory was formed, and no output was written.",
        }, ensure_ascii=False, indent=2))
        return 0 if checkpoint else 2
    if checkpoint is None:
        raise SystemExit(
            f"pinned local checkpoint {GATE_MODEL}@{GATE_REVISION} is unavailable; "
            "no download was attempted"
        )

    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    trace_dir = args.output / "traces"
    if args.resume:
        if not manifest_path.is_file():
            raise SystemExit("cannot resume: manifest.json is missing")
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        if prior.get("configuration_sha256") != fingerprint:
            raise SystemExit("cannot resume: dataset, selection or configuration changed")
    elif any(args.output.iterdir()):
        raise SystemExit("output directory is not empty; choose a new directory or use --resume")
    trace_dir.mkdir(exist_ok=True)
    atomic_write_json(manifest_path, {
        "configuration_sha256": fingerprint,
        "configuration": identity,
        "local_checkpoint": str(checkpoint),
        "gold_evaluation_phase": "after_all_three_arm_runtime_traces",
    })

    try:
        gate = load_fixed_gate()
    except Exception as exc:  # noqa: BLE001 - setup failure must be actionable
        raise SystemExit(f"fixed semantic gate setup failed: {type(exc).__name__}: {exc}") from exc
    embedder = DeterministicEmbeddingProvider()
    for index, case in enumerate(selection, 1):
        path = trace_dir / trace_filename(case.question_id)
        if args.resume and path.is_file():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("status") == "COMPLETED" and set(saved.get("arms", {})) == set(ARM_CONFIG):
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        try:
            trace = runtime_case(case, gate=gate, embedder=embedder)
        except Exception as exc:  # noqa: BLE001 - preserve failure and continue the fixed set
            trace = {
                "schema_version": SCHEMA_VERSION,
                "question_id": case.question_id,
                "status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}"[:2000],
            }
        atomic_write_json(path, trace)
        if index == 1 or index % 10 == 0 or index == len(selection):
            print(f"[{index}/{len(selection)}] {case.question_id}: {trace['status']}", flush=True)

    # Evaluation is intentionally a separate pass after every N/E/EC artifact
    # has either completed or recorded a failure.
    report = evaluate_traces(selection, trace_dir)
    report["dataset_sha256"] = dataset.sha256
    report["configuration_sha256"] = fingerprint
    report["fixed_ids_sha256"] = hashlib.sha256(
        args.ids_file.read_bytes()
    ).hexdigest()
    atomic_write_json(args.output / "report.json", report)
    print(json.dumps({
        "cases_completed": report["cases_completed"],
        "failures": len(report["failures"]),
        "missing_traces": len(report["missing_traces"]),
        "packed_gold_turns": {
            arm: report["arms"][arm]["overall"]["packed"] for arm in ARM_CONFIG
        },
        "context_changed": {
            name: len(values["context_changed_cases"])
            for name, values in report["paired"].items()
        },
        "is_accuracy": False,
        "official_score": "NOT_EVALUATED",
        "report": str(args.output / "report.json"),
    }, ensure_ascii=False, indent=2))
    return 0 if not report["failures"] and not report["missing_traces"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
