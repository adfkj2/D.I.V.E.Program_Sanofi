"""Paired, no-API Oracle retrieval ablation with a *real local* dense encoder.

Three arms share one formation pass per question, the same memory store, query,
scoring policy, top-k and context budget.  Only BM25/dense channel membership
changes. Predicate, temporal, entity and relation channels stay enabled in all
arms, so this is the incremental value of lexical/semantic retrieval *within*
the current Oracle stack, not a standalone BM25-vs-vector search benchmark.

Gold answer/session labels are used only by ``evaluate_traces`` after all
retrieval and packing has completed.  No reader is invoked; evidence reach is
an upper bound, never answer accuracy.  The process forces Hugging Face offline
mode and refuses to silently substitute a deterministic hash embedding.

Example (from repository root)::

    python ops/oracle_retrieval_ablation.py --stratified 12 --output eval/reports/retrieval-pilot --dry-run
    python ops/oracle_retrieval_ablation.py --stratified 12 --output eval/reports/retrieval-pilot
    python ops/oracle_retrieval_ablation.py --stratified 12 --output eval/reports/retrieval-pilot --resume
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dive_memory.context import pack_context  # noqa: E402
from dive_memory.eval_manifest import code_snapshot
from dive_memory.oracle.evidence import build_evidence_views, render_evidence_block
from dive_memory.embeddings import LocalSentenceTransformerEmbeddingProvider  # noqa: E402
from dive_memory.eval_adapters.longmemeval import parse_instant  # noqa: E402
from dive_memory.oracle.contracts import ORACLE_EXPECTED_CASES, assert_writer_visible  # noqa: E402
from dive_memory.oracle.loader import load_oracle_dataset, select_cases  # noqa: E402
from dive_memory.oracle.pipeline import _after, ingest_case, packing_content_caps  # noqa: E402
from dive_memory.oracle.runner import atomic_write_json, trace_filename  # noqa: E402
from dive_memory.retrieval import RetrievalConfig, scoring_policy  # noqa: E402
from dive_memory.semantic_gate import DocumentAdmissionPolicy, POLICY_VERSION, SemanticGate  # noqa: E402
from dive_memory.service import MemoryService  # noqa: E402

SCHEMA_VERSION = "dive-oracle-retrieval-ablation-v3"
DEFAULT_DATASET = ROOT / "eval/external/longmemeval/longmemeval_oracle.json"
DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
ARM_CHANNELS: dict[str, frozenset[str]] = {
    "lexical": frozenset({"bm25", "predicate", "temporal", "entity", "relation"}),
    "semantic": frozenset({"dense", "predicate", "temporal", "entity", "relation"}),
    "hybrid": frozenset({"bm25", "dense", "predicate", "temporal", "entity", "relation"}),
}
LEVELS = ("formed", "retrieved", "packed")
CRITICAL_DISTRIBUTIONS = (
    "dive-memory",
    "sentence-transformers",
    "transformers",
    "huggingface-hub",
    "torch",
    "numpy",
)


@dataclass(frozen=True, slots=True)
class RuntimeOracleTurn:
    """Gold-free turn DTO admitted to formation and retrieval."""

    session_id: str
    turn_id: str
    role: str
    content: str
    occurred_at: str
    source_order: int

    @property
    def ref(self) -> str:
        return self.turn_id

    @property
    def occurred_at_iso(self) -> str:
        return parse_instant(self.occurred_at).isoformat()


@dataclass(frozen=True, slots=True)
class RuntimeOracleCase:
    """Only the fields the runtime is permitted to observe."""

    question_id: str
    namespace: str
    question: str
    visibility_cutoff_iso: str
    chronological_turns: tuple[RuntimeOracleTurn, ...]


def sanitize_runtime_case(case: Any) -> RuntimeOracleCase:
    """Copy an evaluator case into a slots-only DTO with no gold-bearing fields."""

    rows = list(case.system_input())
    assert_writer_visible(rows)
    question_id = str(case.question_id)
    namespace = f"longmemeval:{question_id}"
    turns: list[RuntimeOracleTurn] = []
    seen_turn_ids: set[str] = set()
    for source_order, row in enumerate(rows):
        if row["namespace"] != namespace:
            raise ValueError(f"runtime namespace mismatch for {question_id}")
        turn_id = str(row["turn_id"])
        if turn_id in seen_turn_ids:
            raise ValueError(f"duplicate runtime turn id {turn_id!r}")
        seen_turn_ids.add(turn_id)
        turns.append(RuntimeOracleTurn(
            session_id=str(row["session_id"]),
            turn_id=turn_id,
            role=str(row["role"]),
            content=str(row["content"]),
            occurred_at=str(row["occurred_at"]),
            source_order=source_order,
        ))
    turns.sort(key=lambda turn: (parse_instant(turn.occurred_at), turn.source_order))
    return RuntimeOracleCase(
        question_id=question_id,
        namespace=namespace,
        question=str(case.question),
        visibility_cutoff_iso=str(case.visibility_cutoff_iso),
        chronological_turns=tuple(turns),
    )


def force_offline_model_mode() -> None:
    """Set the two model-runtime offline switches before any model cache access."""

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _git_identity() -> dict[str, Any]:
    def run(*args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", "-c", f"safe.directory={ROOT.as_posix()}", *args], cwd=ROOT, check=False, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    head = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--untracked-files=normal")
    commit = head.stdout.strip() if head is not None and head.returncode == 0 else None
    entries = status.stdout.splitlines() if status is not None and status.returncode == 0 else None
    return {
        "commit": commit,
        "dirty": bool(entries) if entries is not None else None,
        "status_entry_count": len(entries) if entries is not None else None,
    }


def runtime_identity() -> dict[str, Any]:
    """Capture code/runtime provenance without importing optional model packages."""

    versions: dict[str, str | None] = {}
    for name in CRITICAL_DISTRIBUTIONS:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(b"".join(
            path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes()
            for folder in (ROOT / "src", ROOT / "ops")
            for path in sorted(folder.rglob("*.py"))
        )).hexdigest(),
        "git": _git_identity(),
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "dependencies": versions,
        "offline_environment": {
            "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE"),
            "TRANSFORMERS_OFFLINE": os.environ.get("TRANSFORMERS_OFFLINE"),
        },
    }


def selection_identity(args: argparse.Namespace, ids_file_sha256: str | None) -> dict[str, Any]:
    methods: list[str] = []
    if args.ids_file:
        methods.append("ids_file")
    if args.stratified is not None:
        methods.append("stratified")
    if args.max_cases is not None:
        methods.append("max_cases")
    return {
        "method": "+".join(methods) if methods else "all_cases",
        "seed": args.seed,
        "stratified_size": args.stratified,
        "max_cases": args.max_cases,
        "ids_file": str(args.ids_file.resolve()) if args.ids_file else None,
        "ids_file_sha256": ids_file_sha256,
    }


def exact_two_sided_sign_test(candidate_better: int, lexical_better: int) -> float | None:
    """Exact binomial sign test over discordant question-level pairs."""

    discordant = int(candidate_better) + int(lexical_better)
    if discordant == 0:
        return None
    tail = sum(math.comb(discordant, value) for value in range(min(candidate_better, lexical_better) + 1))
    return min(1.0, 2.0 * tail / (2 ** discordant))


def cached_model_path(model: str, revision: str) -> Path | None:
    """Read only: find the pinned checkpoint's sentence-transformers manifest."""

    # huggingface_hub reads these at import time; set them *before* importing it.
    force_offline_model_mode()
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None
    try:
        found = try_to_load_from_cache(model, "modules.json", revision=revision)
    except Exception:  # noqa: BLE001 - cache lookup failure is simply unavailable
        return None
    if isinstance(found, str) and Path(found).is_file():
        return Path(found).parent
    return None


def model_provider(model: str, revision: str, device: str) -> LocalSentenceTransformerEmbeddingProvider:
    """Load a pinned local checkpoint without permitting a network fallback."""

    force_offline_model_mode()
    if not revision or revision == "unpinned":
        raise RuntimeError("a pinned embedding revision is required")
    checkpoint = cached_model_path(model, revision)
    if checkpoint is None:
        raise RuntimeError(
            f"pinned local checkpoint {model}@{revision} is not cached; "
            "no download was attempted. Provision it separately, then rerun."
        )
    dimensions = {"BAAI/bge-m3": 1024, "intfloat/multilingual-e5-large-instruct": 1024,
                  "Alibaba-NLP/gte-multilingual-base": 768}.get(model)
    if dimensions is None:
        raise RuntimeError(f"unknown model dimensions for {model!r}; add a checked width first")
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError("install sentence-transformers in the optional model environment") from exc
    # Loading by repo id can issue a HEAD request for optional adapter files,
    # even when the weights are cached. A local snapshot path plus this explicit
    # flag is a hard no-network boundary; any missing file fails locally.
    encoder = SentenceTransformer(
        str(checkpoint), device=None if device == "auto" else device,
        local_files_only=True,
    )
    provider = LocalSentenceTransformerEmbeddingProvider(
        encoder, model=model, revision=revision, dimensions=dimensions,
    )
    if not provider.semantic_similarity:
        raise RuntimeError("the requested provider is not a semantic encoder")
    return provider


def make_gate(provider: LocalSentenceTransformerEmbeddingProvider, args: argparse.Namespace) -> tuple[Any, str]:
    """Reuse the same encoder for writing and retrieval; do not load it twice."""

    if args.gate == "v1":
        if args.document_admission != "off":
            raise ValueError("--gate v1 requires --document-admission off")
        return None, "utility-baseline-v1"
    policy = DocumentAdmissionPolicy(
        mode=args.document_admission,
        min_chars=args.document_min_chars,
        max_chars=args.document_max_chars,
    )
    return SemanticGate(
        provider.encoder, cache_size=args.gate_cache,
        encode_batch_size=args.gate_batch_size, max_sequence_length=512,
        document_admission=policy,
    ), POLICY_VERSION


def runtime_case(case: RuntimeOracleCase, *, gate: Any, embedder: Any, limit: int,
                 token_budget: int, scoring_name: str) -> dict[str, Any]:
    """Produce three arm traces from a gold-free runtime DTO.

    Full evaluator cases are rejected even though they are structurally similar;
    this makes the gold boundary an enforced type check rather than a convention.
    """

    if not isinstance(case, RuntimeOracleCase):
        raise TypeError("runtime_case requires a sanitized RuntimeOracleCase")
    cutoff = case.visibility_cutoff_iso
    turns = tuple(turn for turn in case.chronological_turns if not _after(turn, case))
    service = MemoryService(gate=gate, embedder=embedder, deterministic_ids=True)
    try:
        formation = ingest_case(service, case, turns=turns, gate=gate)
        policy = scoring_policy(scoring_name)
        arms: dict[str, Any] = {}
        for name, channels in ARM_CHANNELS.items():
            config = RetrievalConfig(channels, scoring=policy, name=f"oracle-{name}")
            result = service.retrieve(
                case.namespace, case.question, limit=limit,
                as_of=cutoff, observed_as_of=cutoff, config=config,
            )
            if "dense" in channels:
                dense = result.trace.get("channels", {}).get("dense", {})
                if dense.get("degraded") or dense.get("model") != embedder.model:
                    raise RuntimeError(f"{name} arm did not use the requested semantic model")
            current_only = result.plan.get("temporal_mode") not in {"historical", "earliest", "all"}
            def render(items):
                return render_evidence_block(build_evidence_views(
                    items, question=case.question, question_date=cutoff,
                ))
            packed = pack_context(
                result.items, token_budget, current_only=current_only,
                content_caps=packing_content_caps(policy),
                render_for_budget=render,
            )
            arms[name] = {
                "channels": sorted(channels),
                "executed_channels": result.trace.get("executed_channels", []),
                "dense_metadata": result.trace.get("channels", {}).get("dense"),
                "returned_memory_ids": [item.memory.id for item in result.items],
                "returned_source_refs": [list(item.source_refs) for item in result.items],
                "packed_memory_ids": [item.memory.id for item in packed.items],
                "packed_count": len(packed.items),
                "omitted": packed.omitted,
                "estimated_tokens": packed.estimated_tokens,
                "token_counter": packed.token_counter,
                "packing_decisions": packed.decisions,
                "context_sha256": hashlib.sha256(packed.text.encode("utf-8")).hexdigest(),
                "reader_context_text": packed.text,
                "context_budget_version": "reader-visible-evidence-v2",
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "question_id": case.question_id,
            "status": "COMPLETED",
            "formation": formation,
            "arms": arms,
        }
    finally:
        service.store.close()


def _arm_hits(case: Any, trace: Mapping[str, Any], arm_name: str) -> dict[str, int]:
    """Evaluator-side mapping: gold turn ref -> formed event -> returned/packed."""

    formation = trace["formation"]
    arm = trace["arms"][arm_name]
    returned_ids = arm["returned_memory_ids"]
    returned_refs = arm["returned_source_refs"]
    packed_ids = set(arm["packed_memory_ids"])
    all_returned: set[str] = set()
    all_packed: set[str] = set()
    for memory_id, refs in zip(returned_ids, returned_refs, strict=True):
        all_returned.update(refs)
        if memory_id in packed_ids:
            all_packed.update(refs)
    counts = formation["turn_memory_counts"]
    events = formation["turn_events"]
    hits = {name: 0 for name in LEVELS}
    for ref in case.evidence_refs:
        event_id = events.get(ref)
        hits["formed"] += int(int(counts.get(ref, 0)) > 0)
        hits["retrieved"] += int(bool(event_id) and event_id in all_returned)
        hits["packed"] += int(bool(event_id) and event_id in all_packed)
    return hits


def evaluate_traces(cases: Sequence[Any], trace_dir: Path) -> dict[str, Any]:
    """Score evidence reach after runtime traces exist; never invoke a model."""

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    failures: list[dict[str, str]] = []
    for case in cases:
        path = trace_dir / trace_filename(case.question_id)
        if not path.exists():
            missing.append(case.question_id)
            continue
        trace = json.loads(path.read_text(encoding="utf-8"))
        if trace.get("status") != "COMPLETED" or set(trace.get("arms", {})) != set(ARM_CHANNELS):
            failures.append({"question_id": case.question_id, "error": str(trace.get("error", "incomplete"))})
            continue
        row: dict[str, Any] = {
            "question_id": case.question_id,
            "question_type": case.question_type,
            "should_abstain": case.should_abstain,
            "gold_turns": len(case.evidence_refs),
            "arms": {},
        }
        for name in ARM_CHANNELS:
            arm = trace["arms"][name]
            hits = _arm_hits(case, trace, name)
            complete = {
                f"all_gold_{level}": int(row["gold_turns"] > 0 and hits[level] == row["gold_turns"])
                for level in LEVELS
            }
            row["arms"][name] = {
                **hits,
                **complete,
                "retrieved_items": len(arm["returned_memory_ids"]),
                "packed_items": arm["packed_count"],
                "context_tokens": arm["estimated_tokens"],
            }
        rows.append(row)

    aggregates: dict[str, Any] = {}
    answerable = [row for row in rows if not row["should_abstain"]]
    for name in ARM_CHANNELS:
        by_type: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "cases": 0,
                "gold_turns": 0,
                **{level: 0 for level in LEVELS},
                **{f"all_gold_{level}_cases": 0 for level in LEVELS},
            }
        )
        overall = {
            "cases": len(answerable),
            "gold_turns": 0,
            **{level: 0 for level in LEVELS},
            **{f"all_gold_{level}_cases": 0 for level in LEVELS},
            "context_tokens": 0,
            "packed_items": 0,
        }
        for row in answerable:
            b = by_type[row["question_type"]]
            b["cases"] += 1
            b["gold_turns"] += row["gold_turns"]
            overall["gold_turns"] += row["gold_turns"]
            arm = row["arms"][name]
            for level in LEVELS:
                b[level] += arm[level]
                overall[level] += arm[level]
                b[f"all_gold_{level}_cases"] += arm[f"all_gold_{level}"]
                overall[f"all_gold_{level}_cases"] += arm[f"all_gold_{level}"]
            overall["context_tokens"] += arm["context_tokens"]
            overall["packed_items"] += arm["packed_items"]
        for bucket in [overall, *by_type.values()]:
            for level in LEVELS:
                bucket[f"{level}_rate"] = bucket[level] / bucket["gold_turns"] if bucket["gold_turns"] else None
                bucket[f"all_gold_{level}_case_rate"] = (
                    bucket[f"all_gold_{level}_cases"] / bucket["cases"] if bucket["cases"] else None
                )
        overall["mean_context_tokens"] = overall.pop("context_tokens") / len(answerable) if answerable else None
        overall["mean_packed_items"] = overall.pop("packed_items") / len(answerable) if answerable else None
        aggregates[name] = {"overall": overall, "by_question_type": dict(sorted(by_type.items()))}

    paired: dict[str, Any] = {}
    for candidate in ("semantic", "hybrid"):
        by_type: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: {level: {"gained": 0, "lost": 0, "unchanged": 0} for level in LEVELS}
        )
        movements = {level: {"gained": [], "lost": []} for level in LEVELS}
        for row in answerable:
            for level in LEVELS:
                delta = row["arms"][candidate][level] - row["arms"]["lexical"][level]
                direction = "gained" if delta > 0 else "lost" if delta < 0 else "unchanged"
                by_type[row["question_type"]][level][direction] += 1
                if delta:
                    movements[level][direction].append({"question_id": row["question_id"], "delta": delta})
        sign_tests: dict[str, Any] = {}
        all_gold_tests: dict[str, Any] = {}
        for level in LEVELS:
            candidate_better = len(movements[level]["gained"])
            lexical_better = len(movements[level]["lost"])
            sign_tests[level] = {
                "candidate_better": candidate_better,
                "lexical_better": lexical_better,
                "ties": len(answerable) - candidate_better - lexical_better,
                "discordant_pairs": candidate_better + lexical_better,
                "exact_two_sided_p_value": exact_two_sided_sign_test(candidate_better, lexical_better),
            }
            both_hit = candidate_only = lexical_only = neither_hit = 0
            for row in answerable:
                lexical_hit = bool(row["arms"]["lexical"][f"all_gold_{level}"])
                candidate_hit = bool(row["arms"][candidate][f"all_gold_{level}"])
                if lexical_hit and candidate_hit:
                    both_hit += 1
                elif candidate_hit:
                    candidate_only += 1
                elif lexical_hit:
                    lexical_only += 1
                else:
                    neither_hit += 1
            all_gold_tests[level] = {
                "both_complete": both_hit,
                "candidate_only_complete": candidate_only,
                "lexical_only_complete": lexical_only,
                "neither_complete": neither_hit,
                "discordant_pairs": candidate_only + lexical_only,
                "candidate_case_rate": aggregates[candidate]["overall"][f"all_gold_{level}_case_rate"],
                "lexical_case_rate": aggregates["lexical"]["overall"][f"all_gold_{level}_case_rate"],
                "case_rate_difference": (
                    aggregates[candidate]["overall"][f"all_gold_{level}_case_rate"]
                    - aggregates["lexical"]["overall"][f"all_gold_{level}_case_rate"]
                ),
                "exact_mcnemar_two_sided_p_value": exact_two_sided_sign_test(
                    candidate_only, lexical_only,
                ),
            }
        paired[candidate] = {
            "versus": "lexical",
            "overall_delta": {level: aggregates[candidate]["overall"][level]
                              - aggregates["lexical"]["overall"][level] for level in LEVELS},
            "by_question_type": dict(sorted(by_type.items())),
            "case_movements": movements,
            "exact_paired": {
                "unit": "question",
                "turn_count_sign_test": sign_tests,
                "all_gold_case_mcnemar": all_gold_tests,
            },
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "is_accuracy": False,
        "interpretation": (
            "Turn-source formed/retrieved/packed coverage is an upper bound on answerability, "
            "not answer accuracy. A packed document turn may still have its answer outside "
            "the selected text span. Abstention cases have no positive gold recall target. "
            "Exact paired p-values use the question as the unit, are descriptive coverage "
            "tests, and do not establish answer-quality superiority."
        ),
        "cases_selected": len(cases),
        "cases_completed": len(rows),
        "answerable_cases": len(answerable),
        "abstention_cases": len(rows) - len(answerable),
        "missing_traces": missing,
        "failures": failures,
        "arms": aggregates,
        "paired": paired,
        "per_case": rows,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, required=True, help="new run directory")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--gate", choices=("semantic", "v1"), default="semantic")
    parser.add_argument("--document-admission", choices=("off", "all"), default="all")
    parser.add_argument("--document-min-chars", type=int, default=200)
    parser.add_argument("--document-max-chars", type=int, default=8000)
    parser.add_argument("--gate-cache", type=int, default=1024)
    parser.add_argument("--gate-batch-size", type=int, default=8)
    parser.add_argument("--scoring-policy", choices=("legacy", "balanced"), default="balanced")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--token-budget", type=int, default=1500)
    parser.add_argument("--stratified", type=int)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--ids-file", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="validate inputs/cache; load no model or memories")
    parser.add_argument("--resume", action="store_true", help="continue only an identical saved configuration")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    force_offline_model_mode()
    if args.limit <= 0 or args.token_budget < 0 or args.gate_cache < 0 or args.gate_batch_size <= 0:
        raise SystemExit("limit/batch size must be positive; token budget/cache must be non-negative")
    if args.gate == "v1" and args.document_admission != "off":
        raise SystemExit("--gate v1 requires --document-admission off")
    dataset = load_oracle_dataset(args.dataset, expected_cases=None if args.allow_partial else ORACLE_EXPECTED_CASES)
    ids = None
    ids_file_sha256 = None
    if args.ids_file:
        ids_bytes = args.ids_file.read_bytes()
        ids_file_sha256 = hashlib.sha256(ids_bytes).hexdigest()
        ids = [line.strip() for line in ids_bytes.decode("utf-8").splitlines() if line.strip()]
    selection = select_cases(dataset.cases, question_ids=ids, max_cases=args.max_cases,
                             stratified_size=args.stratified, seed=args.seed)
    if not selection:
        raise SystemExit("selection contains no cases")
    identity = {
        "schema_version": SCHEMA_VERSION,
        "code_snapshot": code_snapshot(ROOT),
        "dataset_sha256": dataset.sha256,
        "dataset_path": str(args.dataset.resolve()),
        "dataset_expected_cases": None if args.allow_partial else ORACLE_EXPECTED_CASES,
        "selected_question_ids": [case.question_id for case in selection],
        "selection": selection_identity(args, ids_file_sha256),
        "model": args.model, "revision": args.revision, "device": args.device,
        "model_local_files_only": True,
        "model_network_policy": "offline-cache-only",
        "gate": args.gate, "document_admission": args.document_admission,
        "encoder_max_sequence_length": 512 if args.gate == "semantic" else "model_default",
        "document_min_chars": args.document_min_chars,
        "document_max_chars": args.document_max_chars,
        "gate_cache": args.gate_cache, "gate_batch_size": args.gate_batch_size,
        "scoring_policy": args.scoring_policy, "limit": args.limit,
        "token_budget": args.token_budget,
        "arms": {name: sorted(channels) for name, channels in ARM_CHANNELS.items()},
    }
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    run_identity = runtime_identity()
    checkpoint = cached_model_path(args.model, args.revision)
    if args.dry_run:
        print(json.dumps({"dataset_sha256": dataset.sha256, "cases_selected": len(selection),
                          "config_sha256": fingerprint,
                          "selection": identity["selection"],
                          "runtime_identity": run_identity,
                          "model_revision": args.revision,
                          "model_local_files_only": True,
                          "local_checkpoint": str(checkpoint) if checkpoint else None,
                          "cache_manifest_present": checkpoint is not None,
                          "note": "Cache manifest only; model files are not fully validated. "
                                  "No model loaded, API called, memory formed, or output written."}, indent=2))
        return 0 if checkpoint else 2
    if checkpoint is None:
        raise SystemExit("pinned model is not in the local cache; no network download was attempted")
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    trace_dir = args.output / "traces"
    if args.resume:
        if not manifest_path.exists():
            raise SystemExit("cannot resume: manifest.json is missing")
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        if prior.get("config_sha256") != fingerprint:
            raise SystemExit("cannot resume: configuration, dataset or selection changed")
    elif any(args.output.iterdir()):
        raise SystemExit("output directory is not empty; choose a new directory or use --resume")
    trace_dir.mkdir(exist_ok=True)
    atomic_write_json(manifest_path, {
        "config_sha256": fingerprint,
        "configuration": identity,
        "runtime_identity": run_identity,
        "semantic_provider_required": True,
        "network_policy": {
            "external_api_calls": 0,
            "huggingface_offline": True,
            "local_files_only": True,
        },
        "api_calls": 0,
    })
    try:
        provider = model_provider(args.model, args.revision, args.device)
        gate, gate_name = make_gate(provider, args)
    except Exception as exc:  # noqa: BLE001 - setup is actionable, not a silent fallback
        raise SystemExit(f"local model setup failed: {type(exc).__name__}: {exc}") from exc
    for index, case in enumerate(selection, 1):
        path = trace_dir / trace_filename(case.question_id)
        if args.resume and path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("status") == "COMPLETED" and set(saved.get("arms", {})) == set(ARM_CHANNELS):
                    continue
            except (OSError, json.JSONDecodeError):
                pass  # A failed/torn trace is safe to recompute under the same manifest.
        try:
            runtime_input = sanitize_runtime_case(case)
            trace = runtime_case(runtime_input, gate=gate, embedder=provider, limit=args.limit,
                                 token_budget=args.token_budget, scoring_name=args.scoring_policy)
        except Exception as exc:  # noqa: BLE001 - preserve a per-case failure, continue the sweep
            trace = {"schema_version": SCHEMA_VERSION, "question_id": case.question_id,
                     "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"[:2000]}
        atomic_write_json(path, trace)
        if index == 1 or index % 25 == 0 or index == len(selection):
            print(f"[{index}/{len(selection)}] {case.question_id}: {trace['status']}", flush=True)
    report = evaluate_traces(selection, trace_dir)
    report["dataset_sha256"] = dataset.sha256
    report["config_sha256"] = fingerprint
    report["configuration"] = identity
    report["runtime_identity"] = run_identity
    report["model"] = {
        "name": args.model,
        "revision": args.revision,
        "checkpoint": str(checkpoint),
        "local_files_only": True,
        "network_policy": "offline-cache-only",
    }
    report["gate"] = gate_name
    report["network_policy"] = {
        "external_api_calls": 0,
        "huggingface_offline": True,
        "local_files_only": True,
    }
    report["api_calls"] = 0
    atomic_write_json(args.output / "report.json", report)
    print(json.dumps({"cases_completed": report["cases_completed"], "failures": len(report["failures"]),
                      "gold_packed": {name: report["arms"][name]["overall"]["packed"]
                                       for name in ARM_CHANNELS},
                      "all_gold_packed_cases": {
                          name: report["arms"][name]["overall"]["all_gold_packed_cases"]
                          for name in ARM_CHANNELS
                      },
                      "is_accuracy": False, "report": str(args.output / "report.json")},
                     ensure_ascii=False, indent=2))
    return 0 if not report["failures"] and not report["missing_traces"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
