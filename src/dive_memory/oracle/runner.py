"""Oracle runner: durable, resumable, reproducible 500-question execution.

Durability model
----------------
The previous full-corpus sweep kept everything in ``:memory:`` and wrote once at
the end, so an interruption discarded the whole run -- ``docs/benchmark/
longmemeval-500case-profiling.md`` records that a terminated run could only be
reported as "abandoned", never as a partial result. This runner inverts that:

* each finished question is written to ``traces/<id>.json`` through
  ``os.replace`` on a temporary file in the same directory, so a torn write is
  impossible and an interrupted run leaves a complete prefix;
* ``submission.jsonl`` is regenerated atomically from the traces on every
  question, so the submission is never behind the traces;
* ``manifest.json`` is written *before* the first question, so an interrupted
  run still carries its dataset hash, configuration hash and reader spec;
* ``--resume`` refuses to continue when the dataset, configuration, gate,
  reader spec or question selection differ from the artefact on disk, because
  merging two different configurations into one submission is exactly how a
  benchmark result becomes unreproducible.

See ``docs/oracle/02-runner-architecture.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence

from ..eval_adapters.longmemeval import LongMemEvalCase
from ..eval_manifest import build_run_manifest, code_snapshot
from ..retrieval import SCORING_POLICIES
from .contracts import (
    CANONICAL_ABSTENTION_HYPOTHESIS,
    ORACLE_ABSTENTION_CASES,
    ORACLE_DATASET_LICENSE,
    ORACLE_DATASET_NAME,
    ORACLE_EXPECTED_CASES,
    SUBMISSION_FIELDS,
    OracleResumeConflictError,
)
from .loader import OracleDataset, load_oracle_dataset, select_cases, sha256_file
from .pipeline import CaseResult, PipelineConfig, run_case, summarize_record
from .reader import READER_POLICIES, ReaderPolicy, ReaderSpec, reader_policy

SCHEMA_VERSION = "dive-oracle-run-v1"
SUBMISSION_SCHEMA_VERSION = "dive-oracle-submission-v1"

#: Policy names the CLI accepts, kept in one place so the argument parser and
#: the resolver cannot drift apart.
READER_POLICY_NAMES: tuple[str, ...] = tuple(sorted(READER_POLICIES))

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


# --------------------------------------------------------------------------- #
# Atomic filesystem primitives
# --------------------------------------------------------------------------- #


def atomic_write_text(path: str | Path, text: str) -> str:
    """Write ``text`` to ``path`` atomically, returning the file's SHA-256.

    The temporary file is created in the *same* directory as the target so the
    final ``os.replace`` stays on one volume, which is what makes it atomic on
    Windows as well as POSIX.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write_json(path: str | Path, payload: Any) -> str:
    return atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def trace_filename(question_id: str) -> str:
    """A deterministic, Windows-safe file name for one question's trace."""

    safe = _UNSAFE.sub("_", question_id)
    if safe != question_id or not safe or len(safe) > 100:
        digest = hashlib.sha1(question_id.encode("utf-8")).hexdigest()[:10]
        safe = f"{safe[:60]}-{digest}"
    return f"{safe}.json"


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def _latency(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"samples": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "samples": len(values),
        "mean": sum(values) / len(values),
        "p50": median(values),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values),
    }


def environment_report() -> dict[str, Any]:
    """Record the machine a run happened on. Unknown values stay ``UNKNOWN``."""

    report: dict[str, Any] = {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "UNKNOWN",
        "cpu_count": os.cpu_count(),
        "torch": "UNKNOWN",
        "cuda_available": "UNKNOWN",
        "cuda_device": "UNKNOWN",
    }
    try:  # optional: the core package has no torch dependency
        import torch  # type: ignore[import-not-found]

        report["torch"] = str(getattr(torch, "__version__", "UNKNOWN"))
        cuda_available = bool(torch.cuda.is_available())
        report["cuda_available"] = cuda_available
        if cuda_available:
            report["cuda_device"] = str(torch.cuda.get_device_name(0))
    except Exception:  # noqa: BLE001 - absent torch is a normal state
        pass
    return report


@dataclass(frozen=True, slots=True)
class OracleRunPaths:
    root: Path
    traces: Path
    submission: Path
    cases: Path
    failures: Path
    manifest: Path
    summary: Path

    @classmethod
    def under(cls, root: str | Path) -> "OracleRunPaths":
        base = Path(root)
        return cls(
            root=base,
            traces=base / "traces",
            submission=base / "submission.jsonl",
            cases=base / "cases.jsonl",
            failures=base / "failures.json",
            manifest=base / "manifest.json",
            summary=base / "summary.json",
        )


@dataclass(slots=True)
class OracleRunner:
    dataset: OracleDataset
    output_dir: Path
    reader: Any
    gate: Any | None = None
    #: The store's embedding provider. ``None`` means the offline hash stub the
    #: v1 artefacts were produced with; a semantic checkpoint is passed in only
    #: when the run is explicitly asking for one, so the choice is always
    #: visible in the manifest rather than being a default.
    embedder: Any | None = None
    embedder_name: str = "deterministic-test-provider (non-semantic)"
    pipeline_config: PipelineConfig = field(default_factory=PipelineConfig)
    gate_name: str = "utility-baseline-v1"
    selection: Sequence[LongMemEvalCase] = field(default_factory=tuple)
    selection_meta: Mapping[str, Any] = field(default_factory=dict)
    on_error: str = "abstain"
    run_id: str | None = None
    progress: Callable[[Mapping[str, Any]], None] | None = None
    #: Derived in ``__post_init__``; declared so the slotted dataclass has a
    #: place to store it.
    paths: "OracleRunPaths" = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.on_error not in {"abstain", "skip"}:
            raise ValueError("on_error must be 'abstain' or 'skip'")
        actual_reader_policy = getattr(self.reader, "policy", None)
        if isinstance(actual_reader_policy, ReaderPolicy) \
                and actual_reader_policy != self.pipeline_config.policy():
            raise ValueError("reader policy differs from pipeline policy")
        admission = getattr(getattr(self.gate, "document_admission", None), "mode", None)
        if admission == "all" and self.pipeline_config.scoring_policy_name == "legacy":
            raise ValueError(
                "document admission 'all' with legacy scoring can crowd out short evidence; "
                "use balanced scoring or turn document admission off"
            )
        self.output_dir = Path(self.output_dir)
        self.paths = OracleRunPaths.under(self.output_dir)
        if not self.selection:
            self.selection = self.dataset.cases
        if self.run_id is None:
            self.run_id = f"oracle-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    # -- identity ---------------------------------------------------------- #

    def reader_spec(self) -> ReaderSpec | None:
        spec = getattr(self.reader, "spec", None)
        return spec if isinstance(spec, ReaderSpec) else None

    def configuration(self) -> dict[str, Any]:
        spec = self.reader_spec()
        return {
            "adapter": "longmemeval-adapter-v2",
            "code_snapshot": code_snapshot(Path(__file__).resolve().parents[3]),
            "oracle_pipeline": self.pipeline_config.to_dict(),
            "gate": self.gate_name,
            "gate_runtime": {
                "max_sequence_length": getattr(self.gate, "max_sequence_length", None),
                "encode_batch_size": getattr(self.gate, "encode_batch_size", None),
                "device": _gate_device(self.gate),
                "encoder": getattr(getattr(self.gate, "index", None), "encoder_name", None),
            },
            # The dense channel's provider is a *different* encoder from the
            # write gate's. Recording it separately is what makes "the dense
            # channel was non-semantic" checkable from the artefact instead of
            # being inferred from the gate.
            "embedder": self.embedder_name,
            # Whether a long non-trusted deliverable may become a document
            # memory. It changes what the writer accepts, so it belongs in the
            # configuration hash -- and therefore in the resume guard.
            "document_admission": (
                self.gate.document_admission.to_dict()
                if getattr(self.gate, "document_admission", None) is not None
                else None
            ),
            "reader_spec": spec.to_dict() if spec else {
                "provider": getattr(self.reader, "provider", type(self.reader).__name__),
                "model": "UNKNOWN",
            },
            "reader_fingerprint": spec.fingerprint() if spec else _stable_hash(
                {"reader": type(self.reader).__name__}
            ),
            "reader_policy": self.pipeline_config.policy().to_dict(),
            "reader_policy_fingerprint": self.pipeline_config.policy().fingerprint(),
            "scoring_policy": self.pipeline_config.scoring().to_dict(),
            "scoring_policy_fingerprint": self.pipeline_config.scoring().fingerprint(),
            "on_error_policy": self.on_error,
            "abstention_hypothesis": CANONICAL_ABSTENTION_HYPOTHESIS,
            "dataset": {
                "name": self.dataset.declared_name,
                "sha256": self.dataset.sha256,
                "cases": len(self.dataset.cases),
            },
            "selection": dict(self.selection_meta),
            "gold_visibility": (
                "question/answer/answer_session_ids/has_answer/question_type are evaluator-only; "
                "the writer sees role/content/session/time and the reader sees question, "
                "question_date and the budget-limited retrieved context"
            ),
        }

    def resume_key(self) -> dict[str, Any]:
        configuration = self.configuration()
        return {
            "configuration_sha256": _stable_hash(configuration),
            "dataset_sha256": self.dataset.sha256,
            "dataset_cases": len(self.dataset.cases),
            "reader_fingerprint": configuration["reader_fingerprint"],
            "gate": self.gate_name,
            "selection_sha256": _stable_hash([case.question_id for case in self.selection]),
            "prompt_version": getattr(self.reader_spec(), "prompt_version", None),
        }

    # -- main loop --------------------------------------------------------- #

    def run(self, *, resume: bool = False, force: bool = False) -> dict[str, Any]:
        self.paths.traces.mkdir(parents=True, exist_ok=True)
        prior_manifest = self._read_json(self.paths.manifest)
        if prior_manifest is not None and not force:
            self._assert_resume_compatible(prior_manifest, resume=resume)
        elif prior_manifest is None and not force:
            existing = list(self.paths.traces.glob("*.json"))
            if existing:
                raise OracleResumeConflictError(
                    f"{self.paths.traces} already holds {len(existing)} traces but there is no "
                    "manifest.json to verify them against; pass --force to overwrite the directory"
                )

        started = datetime.now(timezone.utc)
        configuration = self.configuration()
        resumed = self._load_traces() if resume else {}
        manifest = self._build_manifest(configuration, started)
        atomic_write_json(self.paths.manifest, manifest)

        records: dict[str, dict[str, Any]] = dict(resumed)
        failures: list[dict[str, Any]] = []
        skipped = 0
        completed = 0
        for index, case in enumerate(self.selection, start=1):
            if case.question_id in records:
                skipped += 1
                continue
            result: CaseResult = run_case(
                case,
                reader=self.reader,
                gate=self.gate,
                embedder=self.embedder,
                config=self.pipeline_config,
                gate_name=self.gate_name,
            )
            record = dict(result.record)
            records[case.question_id] = record
            atomic_write_json(self.paths.traces / trace_filename(case.question_id), record)
            if result.ok and record.get("status") == "COMPLETED":
                completed += 1
            elif record.get("failure"):
                failures.append(dict(record["failure"]))
            self._write_submission(records)
            if self.progress is not None:
                self.progress({"index": index, "total": len(self.selection),
                               "question_id": case.question_id,
                               "status": record.get("status")})

        finished = datetime.now(timezone.utc)
        ordered_records = [records[case.question_id] for case in self.selection
                           if case.question_id in records]
        # Checkpointed failures remain failures after resume. Build the final
        # inventory from all saved records, in selection order, not only the
        # cases executed by this process.
        failures = [dict(record["failure"]) for record in ordered_records if record.get("failure")]
        summary = self._build_summary(ordered_records, failures, skipped, started, finished)
        atomic_write_text(self.paths.cases,
                          "".join(json.dumps(summarize_record(record), ensure_ascii=False) + "\n"
                                  for record in ordered_records))
        atomic_write_json(self.paths.failures, failures)
        submission_stats = self._write_submission(records)
        summary["submission"] = submission_stats
        atomic_write_json(self.paths.summary, summary)
        manifest["finished_at"] = finished.isoformat()
        manifest["duration_seconds"] = (finished - started).total_seconds()
        manifest["status"] = summary["status"]
        manifest["artifacts"] = self._artifact_hashes(submission_stats)
        atomic_write_json(self.paths.manifest, manifest)
        return {**summary, "manifest": manifest, "paths": {
            "root": str(self.paths.root),
            "submission": str(self.paths.submission),
            "traces": str(self.paths.traces),
            "manifest": str(self.paths.manifest),
            "summary": str(self.paths.summary),
            "failures": str(self.paths.failures),
            "cases": str(self.paths.cases),
        }}

    # -- helpers ----------------------------------------------------------- #

    def _assert_resume_compatible(self, prior: Mapping[str, Any], *, resume: bool) -> None:
        target = self.resume_key()
        previous = dict(prior.get("resume_key") or {})
        if not resume:
            raise OracleResumeConflictError(
                f"{self.paths.root} already holds a run ({prior.get('run_id')}); pass --resume to "
                "continue it or --force to start over in place"
            )
        changed = {key: {"previous": previous.get(key), "requested": value}
                   for key, value in target.items() if previous.get(key) != value}
        if changed:
            raise OracleResumeConflictError(
                "refusing to resume: the requested run differs from the artefact on disk in "
                f"{sorted(changed)!r}. Resuming across a configuration change would merge two "
                "different experiments into one submission."
            )

    def _load_traces(self) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        selected = {case.question_id for case in self.selection}
        for path in sorted(self.paths.traces.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            question_id = record.get("question_id")
            if isinstance(question_id, str) and question_id in selected:
                records[question_id] = record
        return records

    def _submission_lines(self, records: Mapping[str, Mapping[str, Any]]) -> tuple[list[str], int]:
        """Build the official 2-field JSONL, plus the error-fallback count."""

        lines: list[str] = []
        fallbacks = 0
        for case in self.selection:
            record = records.get(case.question_id)
            if record is None:
                continue
            hypothesis = record.get("hypothesis")
            if hypothesis is None:
                if self.on_error != "abstain":
                    continue
                hypothesis = CANONICAL_ABSTENTION_HYPOTHESIS
                fallbacks += 1
            line = {"question_id": case.question_id, "hypothesis": str(hypothesis)}
            # The submission contract is exactly two fields. Asserting it here
            # means a future trace field can never leak into the scored file.
            if set(line) != SUBMISSION_FIELDS:
                raise AssertionError("submission line must carry exactly question_id and hypothesis")
            lines.append(json.dumps(line, ensure_ascii=False))
        return lines, fallbacks

    def _write_submission(self, records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        lines, fallbacks = self._submission_lines(records)
        text = "".join(line + "\n" for line in lines)
        digest = atomic_write_text(self.paths.submission, text)
        return {
            "path": str(self.paths.submission),
            "lines": len(lines),
            "sha256": digest,
            "error_fallback_lines": fallbacks,
            "expected_lines": len(self.selection),
        }

    def _build_manifest(self, configuration: Mapping[str, Any], started: datetime) -> dict[str, Any]:
        base = build_run_manifest(
            run_id=self.run_id or "oracle",
            dataset_name=ORACLE_DATASET_NAME,
            dataset_version=f"sha256-{self.dataset.sha256[:12]}",
            dataset_path=self.dataset.path,
            configuration=configuration,
            repository_root=Path(__file__).resolve().parents[3],
        ).to_dict()
        spec = self.reader_spec()
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": "RUNNING",
            "started_at": started.isoformat(),
            "finished_at": None,
            "duration_seconds": None,
            "dataset": {
                "name": ORACLE_DATASET_NAME,
                "declared_name": self.dataset.declared_name,
                "version": f"sha256-{self.dataset.sha256[:12]}",
                "source": "LongMemEval (arXiv:2410.10813), local copy",
                "license": ORACLE_DATASET_LICENSE,
                "path": str(self.dataset.path),
                "sha256": self.dataset.sha256,
                "size_bytes": self.dataset.size_bytes,
                "cases": len(self.dataset.cases),
                "expected_cases": ORACLE_EXPECTED_CASES,
                "abstention_cases": len(self.dataset.abstention_ids),
                "expected_abstention_cases": ORACLE_ABSTENTION_CASES,
                "question_type_counts": self.dataset.question_type_counts,
            },
            "configuration": base["configuration"],
            "configuration_sha256": base["configuration_sha256"],
            "resume_key": self.resume_key(),
            "reader": spec.to_dict() if spec else {"provider": "UNKNOWN", "model": "UNKNOWN"},
            "versions": {
                "adapter": "longmemeval-adapter-v2",
                "extractor": "HeuristicExtractionProvider/heuristic-v1",
                "gate": self.gate_name,
                "resolver": "RelationshipResolver",
                # The store's dense provider, not the write gate's encoder. They
                # are separate objects and a report that conflates them cannot
                # tell a weak retriever from a weak reader.
                "embedding": self.embedder_name,
                "reader": spec.model if spec else "UNKNOWN",
                "prompt": getattr(spec, "prompt_version", "UNKNOWN"),
                "extraction_prompt": "candidate-extraction-v1 (heuristic provider)",
            },
            "environment": {**base["environment"], **environment_report()},
            "git": base["git"],
            "seed": self.selection_meta.get("seed"),
            "selection": dict(self.selection_meta),
            "command": " ".join(sys.argv),
            "artifacts": {},
            "known_limitations": list(KNOWN_LIMITATIONS),
        }

    def _build_summary(
        self,
        records: Sequence[Mapping[str, Any]],
        failures: Sequence[Mapping[str, Any]],
        skipped: int,
        started: datetime,
        finished: datetime,
    ) -> dict[str, Any]:
        statuses: dict[str, int] = {}
        reader_statuses: dict[str, int] = {}
        origins: dict[str, int] = {}
        stage_failures: dict[str, int] = {}
        for record in records:
            statuses[str(record.get("status"))] = statuses.get(str(record.get("status")), 0) + 1
            reader_status = str((record.get("reader") or {}).get("status"))
            reader_statuses[reader_status] = reader_statuses.get(reader_status, 0) + 1
            origin = str(record.get("hypothesis_origin"))
            origins[origin] = origins.get(origin, 0) + 1
            stage = (record.get("failure") or {}).get("stage")
            if stage:
                stage_failures[str(stage)] = stage_failures.get(str(stage), 0) + 1

        def latencies(key: str) -> list[float]:
            values: list[float] = []
            for record in records:
                timing = record.get("timing_ms") or {}
                value = timing.get(key)
                if isinstance(value, (int, float)):
                    values.append(float(value))
            return values

        def sum_tokens(key: str) -> int | None:
            total = 0
            seen = False
            for record in records:
                value = (record.get("tokens") or {}).get(key)
                if isinstance(value, int):
                    total += value
                    seen = True
            return total if seen else None

        return {
            "schema_version": SUBMISSION_SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": "COMPLETED" if len(records) == len(self.selection) and not failures
                      else ("PARTIAL" if records else "FAILED"),
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": (finished - started).total_seconds(),
            "cases_selected": len(self.selection),
            "cases_recorded": len(records),
            "cases_skipped_from_checkpoint": skipped,
            "successful_cases": statuses.get("COMPLETED", 0),
            "degraded_cases": statuses.get("DEGRADED", 0),
            "failed_cases": statuses.get("FAILED", 0),
            "successful": statuses.get("COMPLETED", 0),
            "failed": statuses.get("FAILED", 0),
            "timeouts": sum(1 for record in records
                            if (record.get("reader") or {}).get("status") == "TIMEOUT"),
            "status_counts": statuses,
            "reader_status_counts": reader_statuses,
            "hypothesis_origin_counts": origins,
            "failure_stage_counts": stage_failures,
            "failures": list(failures),
            "latency_ms": {
                "ingest": _latency(latencies("ingest")),
                "retrieve": _latency(latencies("retrieve")),
                "pack": _latency(latencies("pack")),
                "read": _latency(latencies("read")),
                "end_to_end": _latency(latencies("total")),
            },
            "tokens": {
                "reader_input_total": sum_tokens("reader_input"),
                "reader_output_total": sum_tokens("reader_output"),
                "reader_usage_source": sorted({
                    str((record.get("tokens") or {}).get("reader_usage_source"))
                    for record in records
                }),
            },
            "calls": {
                "retrieval": sum(int((record.get("calls") or {}).get("retrieval") or 0)
                                 for record in records),
                "reader": sum(int((record.get("calls") or {}).get("reader") or 0)
                              for record in records),
                "embedding": sum(int((record.get("calls") or {}).get("embedding") or 0)
                                 for record in records) if any(
                    (record.get("calls") or {}).get("embedding") is not None for record in records
                ) else None,
            },
            "self_reported_accuracy": "NOT_EVALUATED",
            "official_score": "NOT_EVALUATED",
            "note": (
                "This summary measures throughput and pipeline health only. Overall Accuracy "
                "requires the project group's official evaluate_qa.py and judge; see "
                "dive_memory.oracle.evaluate."
            ),
        }

    def _artifact_hashes(self, submission_stats: Mapping[str, Any]) -> dict[str, Any]:
        traces = sorted(self.paths.traces.glob("*.json"))
        manifest = {
            "submission_jsonl": {
                "path": str(self.paths.submission),
                "sha256": submission_stats.get("sha256"),
                "lines": submission_stats.get("lines"),
            },
            "cases_jsonl": {"path": str(self.paths.cases)},
            "failures_json": {"path": str(self.paths.failures)},
            "traces_dir": {
                "path": str(self.paths.traces),
                "count": len(traces),
                "sha256_of_name_list": _stable_hash([path.name for path in traces]),
            },
        }
        for key, path in (("cases_jsonl", self.paths.cases),
                          ("failures_json", self.paths.failures)):
            if path.exists():
                manifest[key]["sha256"] = sha256_file(path)
        return manifest

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


KNOWN_LIMITATIONS: tuple[str, ...] = (
    "Overall Accuracy is NOT computed here. The 40-point Benchmark requires the project group's "
    "official evaluate_qa.py and judge model against the same reference answers.",
    "The default deterministic reader is a pipeline probe, not a model; any score derived from it "
    "is self-reported and non-official.",
    "Retrieval uses the repository's formed-memory index, so a question whose evidence turn the "
    "write gate rejected can never be answered -- the failure belongs to the formation stage.",
    "A dirty working tree means the commit in the manifest does not fully describe the code that "
    "produced the artifact; a full 500-question result must come from a clean commit.",
    "Embedding call counts are UNKNOWN unless the gate exposes a counter; the encoder is shared "
    "across cases and its cache makes a call count input-dependent.",
)


def _gate_device(gate: Any | None) -> str | None:
    encoder = getattr(getattr(gate, "index", None), "encoder", None)
    device = getattr(encoder, "device", None)
    return str(device) if device is not None else None


# --------------------------------------------------------------------------- #
# Reader construction (the only place a provider is chosen)
# --------------------------------------------------------------------------- #


def build_reader(args: argparse.Namespace) -> Any:
    from .providers import OpenAICompatibleChatClient, ScriptedChatClient
    from .reader import GroundedChatReader

    policy: ReaderPolicy = reader_policy(getattr(args, "reader_policy", "evidence_first"))
    from .calculating_reader import CalculatingReader
    from .indexed_reader import IndexedEvidenceReader
    reader_class = (CalculatingReader if policy.name == "cited_calculation" else
                    IndexedEvidenceReader if policy.name == "indexed_evidence" else GroundedChatReader)

    if args.reader == "deterministic":
        from .reader import DeterministicReader

        return DeterministicReader()
    if args.reader == "scripted":
        from .reader import ReaderSpec as _Spec

        responses: list[str] = []
        if args.scripted_file:
            responses = [line for line in Path(args.scripted_file).read_text(encoding="utf-8").splitlines()
                         if line.strip()]
        client = ScriptedChatClient(responses=responses)
        return reader_class(client, _Spec(
            provider="scripted", model="scripted-chat-v1", revision=client.revision,
            prompt_version=policy.prompt_version,
            temperature=0.0, max_tokens=args.max_tokens, timeout_seconds=args.timeout,
        ), policy=policy)
    if args.reader == "openai":
        if not args.model or not args.endpoint:
            raise SystemExit("--reader openai requires --model and --endpoint")
        key = os.environ.get(args.api_key_env or "")
        if not key:
            raise SystemExit(
                f"--reader openai requires an API key in ${args.api_key_env}; the key is read "
                "from the environment so it never lands in the manifest or a shell history"
            )
        client = OpenAICompatibleChatClient(endpoint=args.endpoint, api_key=key,
                                            provider=args.provider_name or "openai-compatible",
                                            extra_body=_parse_extra_body(args.extra_body))
        return reader_class(client, ReaderSpec(
            provider=args.provider_name or "openai-compatible",
            model=args.model, revision=args.model_revision,
            prompt_version=policy.prompt_version,
            temperature=args.temperature, max_tokens=args.max_tokens,
            timeout_seconds=args.timeout,
        ), policy=policy)
    raise SystemExit(f"unknown reader: {args.reader!r}")


def build_embedder(args: argparse.Namespace) -> tuple[Any | None, str]:
    """The store's embedding provider, plus the name to record in the manifest.

    The default is the offline hash stub, which is what every published v1
    artefact was produced with. ``--embedder local`` swaps in the pinned
    sentence-transformers checkpoint from the local cache; it is opt-in because
    it turns a secondless run into a minutes-long one and because the choice has
    to appear in the manifest rather than in a default.
    """

    if getattr(args, "embedder", "deterministic") != "local":
        return None, "deterministic-test-provider (non-semantic)"
    from ..embeddings import LocalSentenceTransformerEmbeddingProvider

    device = args.embedding_device or (None if args.device == "cpu" else args.device)
    provider = LocalSentenceTransformerEmbeddingProvider.load(
        model=args.embedding_model,
        revision=args.embedding_revision,
        dimensions=_EMBEDDING_DIMENSIONS.get(args.embedding_model, 1024),
        device=device,
    )
    return provider, f"{provider.provider_name}:{provider.model}@{provider.revision}"


#: Published output widths, so a checkpoint swap cannot silently produce a
#: vector whose length disagrees with the store's metadata.
_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "BAAI/bge-m3": 1024,
    "intfloat/multilingual-e5-large-instruct": 1024,
    "Alibaba-NLP/gte-multilingual-base": 768,
}


def _parse_extra_body(raw: str | None) -> dict[str, Any]:
    """Parse an ``--extra-body`` JSON string into a request-body fragment.

    Provider-neutral injection point: DeepSeek needs ``{"thinking": {"type":
    "disabled"}}`` to return its answer in ``message.content`` instead of the
    default reasoning channel; other OpenAI-compatible providers may need other
    extra fields. An empty or ``null`` value yields an empty fragment.
    """

    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--extra-body is not valid JSON: {exc}") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise SystemExit("--extra-body must be a JSON object")
    return payload


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dive_memory.oracle.runner",
        description="Run the LongMemEval Oracle answer pipeline and write an official JSONL.",
    )
    parser.add_argument("--dataset", required=True,
                        help="path to longmemeval_oracle.json")
    parser.add_argument("--output", required=True, help="run directory")
    parser.add_argument("--reader", choices=("deterministic", "scripted", "openai"),
                        default="deterministic")
    parser.add_argument("--scripted-file", help="JSONL of reader responses (reader=scripted)")
    parser.add_argument("--endpoint", help="chat-completions URL (reader=openai)")
    parser.add_argument("--model", help="reader model id (reader=openai)")
    parser.add_argument("--model-revision", default=None)
    parser.add_argument("--provider-name", default=None)
    parser.add_argument("--api-key-env", default="DIVE_READER_API_KEY",
                        help="name of the environment variable holding the API key")
    parser.add_argument("--extra-body", default=None,
                        help="JSON object merged into the request body (e.g. DeepSeek "
                             "'{\"thinking\":{\"type\":\"disabled\"}}')")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--reader-policy", choices=tuple(READER_POLICY_NAMES),
                        default="evidence_first",
                        help="reading strategy; 'strict' reproduces the v1 behaviour exactly")
    parser.add_argument("--scoring-policy", choices=tuple(sorted(SCORING_POLICIES)),
                        default="balanced",
                        help="retrieval re-rank; 'legacy' reproduces the historical "
                             "formula exactly")
    parser.add_argument("--limit", type=int, default=10, help="retrieval top-k")
    parser.add_argument("--token-budget", type=int, default=1500)
    parser.add_argument("--episodic-retention", action="store_true",
                        help="selectively retain specific user episodes as observations")
    parser.add_argument("--narrative-retention", action="store_true",
                        help="retain bounded original context around admitted personal material")
    parser.add_argument("--source-diversity", action="store_true",
                        help="retrieve a wider pool and prioritize distinct source events")
    parser.add_argument("--narrative-clause-anchors", action="store_true",
                        help="retain independent factual clauses in mixed narrative observations")
    parser.add_argument("--controlled-document-limit", type=int, default=2)
    parser.add_argument("--controlled-document-token-budget", type=int, default=600)
    parser.add_argument("--controlled-document-char-budget", type=int, default=None)
    parser.add_argument("--expand-document-spans", action="store_true")
    parser.add_argument("--semantic-retrieval-only", action="store_true",
                        help="disable BM25 in the main lane; requires --embedder local")
    parser.add_argument("--evidence-recovery", action="store_true",
                        help="one bounded local recall round for missing compound-query sides")
    parser.add_argument("--document-selection", choices=("prefix", "query_excerpt"),
                        default="prefix",
                        help="how a capped document is shown to the reader; query_excerpt "
                             "selects a source-verbatim passage and is opt-in")
    parser.add_argument("--document-chunk-search", choices=("off", "all", "assistant_cued", "controlled_supplement"),
                        default="off",
                        help="opt-in document-passage retrieval channel; assistant_cued "
                             "routes only from visible question wording")
    parser.add_argument("--gate", choices=("v1", "semantic"), default="v1")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--gate-cache", type=int, default=1024)
    parser.add_argument("--gate-batch-size", type=int, default=8)
    parser.add_argument("--document-admission", choices=("off", "all"), default="all",
                        help="whether a long non-trusted deliverable (an assistant's "
                             "table/plan/recommendation) may become a document memory. "
                             "'off' reproduces the pre-document behaviour exactly; "
                             "'all' is the measured default and its cost is reported")
    parser.add_argument("--document-min-chars", type=int, default=200)
    parser.add_argument("--document-max-chars", type=int, default=8000)
    parser.add_argument("--embedder", choices=("deterministic", "local"), default="deterministic",
                        help="'deterministic' is the offline hash stub; 'local' uses the pinned "
                             "sentence-transformers checkpoint from the local cache")
    parser.add_argument("--embedding-model", default="BAAI/bge-m3")
    parser.add_argument("--embedding-revision", default="5617a9f61b028005a4858fdac845db406aefb181")
    parser.add_argument("--embedding-device", default=None,
                        help="device for the local embedder (defaults to --device)")
    parser.add_argument("--max-cases", type=int, default=None,
                        help="take the first N questions of the (possibly selected) list")
    parser.add_argument("--stratified", type=int, default=None,
                        help="question-type-stratified pilot size")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--ids-file", default=None,
                        help="file with one question_id per line")
    parser.add_argument("--on-error", choices=("abstain", "skip"), default="abstain")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="start over in place, discarding the existing manifest guard")
    parser.add_argument("--allow-partial", action="store_true",
                        help="permit a dataset smaller than the official 500 questions")
    return parser


def document_admission_policy(args: argparse.Namespace) -> Any:
    """The document-admission policy the CLI asked for.

    Built here rather than inside ``load_gate`` so the policy is a named value
    the caller can reason about, and so ``--gate v1`` (which never constructs a
    ``SemanticGate``) still parses the same arguments.
    """

    from ..semantic_gate import DocumentAdmissionPolicy

    return DocumentAdmissionPolicy(
        mode=getattr(args, "document_admission", "all"),
        min_chars=max(1, int(getattr(args, "document_min_chars", 200))),
        max_chars=max(1, int(getattr(args, "document_max_chars", 8000))),
    )


def load_gate(args: argparse.Namespace) -> tuple[Any | None, str]:
    if args.gate != "semantic":
        return None, "utility-baseline-v1"
    from ..semantic_gate import POLICY_VERSION, load_local_gate

    gate = load_local_gate(
        cache_size=args.gate_cache,
        encode_batch_size=max(1, args.gate_batch_size),
        device=None if args.device == "cpu" else args.device,
        document_admission=document_admission_policy(args),
    )
    return gate, POLICY_VERSION


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    expected = None if args.allow_partial else ORACLE_EXPECTED_CASES
    dataset = load_oracle_dataset(args.dataset, expected_cases=expected)
    ids = None
    if args.ids_file:
        ids = [line.strip() for line in Path(args.ids_file).read_text(encoding="utf-8").splitlines()
               if line.strip()]
    selection = select_cases(
        dataset.cases, question_ids=ids, max_cases=args.max_cases,
        stratified_size=args.stratified, seed=args.seed,
    )
    reader = build_reader(args)
    gate, gate_name = load_gate(args)
    embedder, embedder_name = build_embedder(args)
    selection_meta = {
        "mode": ("ids_file" if ids else ("stratified" if args.stratified else
                                         ("max_cases" if args.max_cases else "full"))),
        "seed": args.seed,
        "requested_size": len(selection),
        "stratified": args.stratified,
        "max_cases": args.max_cases,
        "ids_file": args.ids_file,
    }
    runner = OracleRunner(
        dataset=dataset,
        output_dir=Path(args.output),
        reader=reader,
        gate=gate,
        embedder=embedder,
        embedder_name=embedder_name,
        pipeline_config=PipelineConfig(limit=max(1, args.limit),
                                       token_budget=max(0, args.token_budget),
                                       reader_policy_name=args.reader_policy,
                                       scoring_policy_name=args.scoring_policy,
                                       document_selection_name=args.document_selection,
                                       document_chunk_search_name=args.document_chunk_search,
                                       episodic_retention=args.episodic_retention,
                                       semantic_retrieval_only=args.semantic_retrieval_only,
                                       evidence_recovery=args.evidence_recovery,
                                       narrative_retention=args.narrative_retention,
                                       narrative_clause_anchors=args.narrative_clause_anchors,
                                       controlled_document_limit=args.controlled_document_limit,
                                       controlled_document_token_budget=args.controlled_document_token_budget,
                                       controlled_document_char_budget=args.controlled_document_char_budget,
                                       expand_document_spans=args.expand_document_spans,
                                       source_diversity=args.source_diversity),
        gate_name=gate_name,
        selection=selection,
        selection_meta=selection_meta,
        on_error=args.on_error,
        progress=_cli_progress,
    )
    started = perf_counter()
    try:
        summary = runner.run(resume=args.resume, force=args.force)
    except OracleResumeConflictError as exc:
        raise SystemExit(f"resume refused: {exc}") from exc
    elapsed = perf_counter() - started
    print(json.dumps({
        "run_id": summary["run_id"],
        "status": summary["status"],
        "cases_selected": summary["cases_selected"],
        "cases_recorded": summary["cases_recorded"],
        "submission_lines": summary["submission"]["lines"],
        "submission_sha256": summary["submission"]["sha256"],
        "error_fallback_lines": summary["submission"]["error_fallback_lines"],
        "wall_clock_seconds": round(elapsed, 1),
        "overall_accuracy": "NOT_EVALUATED",
    }, ensure_ascii=False, indent=2))


def _cli_progress(event: Mapping[str, Any]) -> None:
    total = event.get("total") or 0
    index = event.get("index") or 0
    if index == 1 or index % 25 == 0 or index == total:
        print(f"[{index}/{total}] {event.get('question_id')} -> {event.get('status')}",
              file=sys.stderr, flush=True)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
