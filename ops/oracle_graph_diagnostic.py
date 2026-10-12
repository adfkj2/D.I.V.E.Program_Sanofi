"""Offline paired probe of the existing two-hop relation table, no graph engine.

The forced arm measures an upper bound on what the existing query router misses.
Question wording is unchanged; labels enter only evaluate() after runtime ends.
Coverage gains alone cannot establish that a question requires graph reasoning.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "ops"))
from oracle_retrieval_ablation import (
    DEFAULT_MODEL, DEFAULT_REVISION, ARM_CHANNELS, model_provider, make_gate,
    sanitize_runtime_case, runtime_identity, RuntimeOracleCase,
)
from dive_memory.oracle.loader import load_oracle_dataset, select_cases
from dive_memory.oracle.pipeline import ingest_case, _after, packing_content_caps
from dive_memory.oracle.runner import atomic_write_json, trace_filename
from dive_memory.oracle.evidence import build_evidence_views, render_evidence_block
from dive_memory.context import pack_context
from dive_memory.retrieval import RetrievalConfig, scoring_policy
from dive_memory.service import MemoryService


def runtime(case: RuntimeOracleCase, gate, embedder):
    if not isinstance(case, RuntimeOracleCase):
        raise TypeError("sanitized runtime input required")
    service = MemoryService(gate=gate, embedder=embedder, deterministic_ids=True)
    try:
        cutoff = case.visibility_cutoff_iso
        formation = ingest_case(service, case, gate=gate,
                                turns=tuple(t for t in case.chronological_turns if not _after(t, case)))
        arms = {}
        for name in ("hybrid", "forced_two_hop"):
            channels = ARM_CHANNELS["hybrid"] | ({"multi_hop"} if name == "forced_two_hop" else set())
            result = service.retrieve(
                case.namespace, case.question, limit=10, as_of=cutoff, observed_as_of=cutoff,
                intent="multi_hop" if name == "forced_two_hop" else None,
                config=RetrievalConfig(frozenset(channels), scoring=scoring_policy("balanced")),
            )
            def render(items):
                return render_evidence_block(build_evidence_views(items, question=case.question, question_date=cutoff))
            packed = pack_context(
                result.items, 1500, current_only=result.plan["temporal_mode"] not in {"historical", "earliest", "all"},
                content_caps=packing_content_caps(scoring_policy("balanced")), render_for_budget=render,
            )
            arms[name] = {
                "plan": result.plan,
                "returned_memory_ids": [i.memory.id for i in result.items],
                "returned_source_refs": [i.source_refs for i in result.items],
                "packed_memory_ids": [i.memory.id for i in packed.items],
                "context_tokens": packed.estimated_tokens,
                "retrieval_trace": result.trace,
            }
        edges = [dict(r) for r in service.store.db.execute(
            "SELECT r.id,s.canonical_name AS subject,r.predicate,o.canonical_name AS object,"
            "r.source_event_id,r.valid_from,r.valid_to,mr.memory_id FROM relations r "
            "JOIN entities s ON s.id=r.subject_entity_id JOIN entities o ON o.id=r.object_entity_id "
            "JOIN memory_relations mr ON mr.relation_id=r.id WHERE r.namespace=? ORDER BY r.id",
            (case.namespace,),
        )]
        return {"question_id": case.question_id, "formation": formation, "arms": arms, "edges": edges}
    finally:
        service.store.close()


def evaluate(cases, traces):
    rows = []
    for case in cases:
        trace = traces[case.question_id]
        refs = () if case.should_abstain else case.evidence_refs
        events = [trace["formation"]["turn_events"].get(ref) for ref in refs]
        row = {"question_id": case.question_id, "question_type": case.question_type,
               "gold_turns": len(refs), "formed": sum(trace["formation"]["turn_memory_counts"].get(r, 0) > 0 for r in refs),
               "arms": {}}
        for name, arm in trace["arms"].items():
            returned = {e for group in arm["returned_source_refs"] for e in group}
            packed = {e for mid, group in zip(arm["returned_memory_ids"], arm["returned_source_refs"], strict=True)
                      if mid in arm["packed_memory_ids"] for e in group}
            row["arms"][name] = {"retrieved": sum(e in returned for e in events if e),
                                 "packed": sum(e in packed for e in events if e)}
        row["all_formed_but_incomplete_baseline"] = bool(refs) and row["formed"] == len(refs) and row["arms"]["hybrid"]["packed"] < len(refs)
        rows.append(row)
    movements = {"gained": [], "lost": []}
    for row in rows:
        delta = row["arms"]["forced_two_hop"]["packed"] - row["arms"]["hybrid"]["packed"]
        if delta:
            movements["gained" if delta > 0 else "lost"].append({"question_id": row["question_id"], "delta": delta})
    edges = [edge for trace in traces.values() for edge in trace["edges"]]
    return {"schema_version": "oracle-graph-diagnostic-v1", "api_calls": 0, "is_accuracy": False,
            "official_score": "NOT_EVALUATED", "cases": len(rows),
            "router_multi_hop_cases": sum(t["arms"]["hybrid"]["plan"]["intent"] == "multi_hop" for t in traces.values()),
            "eligible_diagnostic_cases": sum(r["all_formed_but_incomplete_baseline"] for r in rows),
            "stored_relation_rows": len(edges), "user_hub_relation_rows": sum(e["subject"].startswith("user:") for e in edges),
            "arms": {name: {level: sum(r["arms"][name][level] for r in rows) for level in ("retrieved", "packed")}
                     for name in ("hybrid", "forced_two_hop")},
            "packed_movements": movements, "per_case": rows,
            "interpretation": "Existing-table probe with forced routing. A gain is not proof of necessary multi-hop reasoning; inspect source paths. No new graph engine is justified by this test alone."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise SystemExit("choose an empty output directory")
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    ids = (ROOT / "eval/reports/oracle-document-ablation-50.ids").read_text().splitlines()
    cases = select_cases(dataset.cases, question_ids=ids)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "traces").mkdir()
    atomic_write_json(args.output / "manifest.json", {
        "dataset_sha256": dataset.sha256, "selected_ids": ids, "runtime_identity": runtime_identity(),
        "model": DEFAULT_MODEL, "revision": DEFAULT_REVISION, "device": args.device,
        "top_k": 10, "evidence_budget": 1500, "forced_routing_diagnostic_only": True,
    })
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, args.device)
    gate, _ = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
                       document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    traces = {}
    for index, case in enumerate(cases, 1):
        trace = runtime(sanitize_runtime_case(case), gate, provider)
        traces[case.question_id] = trace
        atomic_write_json(args.output / "traces" / trace_filename(case.question_id), trace)
        if index % 10 == 0:
            print(f"{index}/{len(cases)} completed", flush=True)
    report = evaluate(cases, traces)
    report["dataset_sha256"] = dataset.sha256
    atomic_write_json(args.output / "report.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "per_case"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
