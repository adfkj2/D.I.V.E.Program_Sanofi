"""Persistent deterministic-provider engineering timings, not semantic accuracy."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import statistics
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dive_memory.application.records import ProjectRecord
from dive_memory.application.runtime import MemoryApplication


def distribution(values):
    ordered = sorted(values)
    return {"n": len(values), "p50_ms": statistics.median(values),
            "p95_ms": ordered[min(len(ordered)-1, int(len(ordered)*0.95))]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--reuse-data", type=Path, help="Retest queries on the same persistent input; skip ingestion")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"started_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
        "platform": platform.platform(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
        "provider": "deterministic hash embedding; no model/network calls",
        "workload": "unique explicit records in one project, SQLite file, serialized writes, 30 distinct queries",
        "timing": "wall clock; includes persistence; not human task time; no concurrent load", "runs": []}
    for size in args.sizes:
        db = (args.reuse_data or args.output) / f"size-{size}.db"
        if args.reuse_data and not db.is_file():
            raise FileNotFoundError(db)
        app = MemoryApplication(str(db))
        samples = []
        started = perf_counter()
        for i in range(0 if args.reuse_data else size):
            at = perf_counter()
            app.record("perf", ProjectRecord(f"experiment-{i}", f"adopted threshold {i % 101}", "result"),
                idempotency_key=f"event-{i}", observed_at="2026-01-01")
            samples.append((perf_counter() - at) * 1000)
        ingest_seconds = None if args.reuse_data else perf_counter() - started
        app.close()
        app = MemoryApplication(str(db))
        queries = []
        stages = {"retrieve": [], "pack": [], "total": []}
        for i in range(30):
            start = perf_counter()
            trace = app.answer("perf", f"experiment-{(i * 337) % size} adopted threshold")
            queries.append((perf_counter() - start) * 1000)
            for stage in stages:
                stages[stage].append(trace["timing_ms"][stage])
            assert trace["status"] == "EVIDENCE_ONLY" and trace["estimated_tokens"] <= 1500
        app.close()
        report["runs"].append({"events": size, "ingest_seconds": ingest_seconds,
            "ingest": distribution(samples) if samples else None, "query": distribution(queries),
            "reused_input": bool(args.reuse_data),
            "stages": {k: distribution(v) for k, v in stages.items()}, "database_bytes": db.stat().st_size})
        (args.output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report["runs"][-1]), flush=True)


if __name__ == "__main__":
    main()
