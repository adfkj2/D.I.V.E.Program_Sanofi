"""Portable paired state-mechanism experiment; not natural-language QA accuracy.

Fixtures provide explicit keys, roles, dates and replacement links. Arms never
receive expected answers. No language model or paid provider is constructed.
"""

import argparse
import json
from collections import Counter
from pathlib import Path
from time import perf_counter

from .application.records import ProjectRecord
from .application.runtime import MemoryApplication
from .evaluation_bundle import digest, write_json
from .temporal import parse_instant

ARMS = (
    "memory",
    "without_updates",
    "without_source_gate",
    "without_forgetting",
    "event_scan",
    "rolling_summary",
    "lexical_top_one",
)


def load_fixtures(path: Path) -> list[dict]:
    stories = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not stories or len({s["id"] for s in stories}) != len(stories):
        raise ValueError("fixtures must contain unique, nonempty stories")
    for story in stories:
        events, tasks = story["events"], story["tasks"]
        if not events or not tasks or len({e["id"] for e in events}) != len(events):
            raise ValueError("events must be nonempty with unique IDs")
        if len({t["id"] for t in tasks}) != len(tasks):
            raise ValueError("task IDs must be unique within each story")
        seen = set()
        for event in events:
            if parse_instant(event["at"]) is None:
                raise ValueError("event timestamp is required")
            if "delete" in event and event["delete"] not in seen:
                raise ValueError("delete must reference a prior event")
            if event.get("replaces") and event["replaces"] not in seen:
                raise ValueError("replacement must reference a prior event")
            if "delete" not in event:
                ProjectRecord(
                    event["key"], event["value"], disposition=event["disposition"]
                )
                if event["role"] not in {"user", "assistant", "system"}:
                    raise ValueError("unsupported role")
            seen.add(event["id"])
        for task in tasks:
            if parse_instant(task["at"]) is None or not task["key"]:
                raise ValueError("task timestamp and key required")
            if task["expected"] is not None and not isinstance(task["expected"], str):
                raise ValueError("expected value must be a string or null")
    return stories


def build_memory(events: list[dict], db: Path, arm: str) -> None:
    app = MemoryApplication(str(db))
    ids = {}
    try:
        for event in events:
            if "delete" in event:
                if arm != "without_forgetting":
                    app.forget("experiment", ids[event["delete"]])
                continue
            record = ProjectRecord(
                event["key"],
                event["value"],
                disposition=event["disposition"],
                session=event["id"],
            )
            if event["role"] != "user" and arm != "without_source_gate":
                result = app.service.ingest(
                    "experiment",
                    record.encode(),
                    explicit=True,
                    source_type=event["role"],
                    observed_at=event["at"],
                    idempotency_key=event["id"],
                )
            else:
                result = app.record(
                    "experiment",
                    record,
                    idempotency_key=event["id"],
                    observed_at=event["at"],
                    replaces=ids.get(event.get("replaces"))
                    if arm != "without_updates"
                    else None,
                )
            if result["memory_ids"]:
                ids[event["id"]] = result["memory_ids"][0]
    finally:
        app.close()


def baseline(events: list[dict], key: str, at: str, arm: str):
    # Erasure applies even to historical questions, matching the service contract.
    deleted = {e["delete"] for e in events if "delete" in e}
    rows = [e for e in events if "delete" not in e and e["id"] not in deleted]
    if arm == "lexical_top_one":
        rows = [
            e
            for e in rows
            if e["key"] == key and parse_instant(e["at"]) <= parse_instant(at)
        ]
        rows.sort(key=lambda e: parse_instant(e["at"]), reverse=True)
        return rows[0]["value"] if rows else None
    rows = [e for e in rows if e["role"] == "user" and e["disposition"] == "confirmed"]
    if arm == "rolling_summary":
        latest = {
            e["key"]: e for e in sorted(rows, key=lambda e: parse_instant(e["at"]))
        }
        return latest[key]["value"] if key in latest else None
    rows = [e for e in rows if parse_instant(e["at"]) <= parse_instant(at)]
    closed = {e["replaces"] for e in rows if e.get("replaces")}
    values = {e["value"] for e in rows if e["id"] not in closed and e["key"] == key}
    return next(iter(values)) if len(values) == 1 else None


def summarize(results: list[dict], expected_tasks: int) -> dict:
    counts = {}
    by_arm = {}
    for arm in ARMS:
        rows = [r for r in results if r["arm"] == arm]
        keyed = {(r["story"], r["task"]): r for r in rows}
        if len(rows) != expected_tasks or len(keyed) != expected_tasks:
            raise ValueError(
                "missing or duplicate experiment rows; denominator cannot shrink"
            )
        by_arm[arm] = keyed
        counts[arm] = {
            "correct": sum(r["correct"] for r in rows),
            "total": len(rows),
            "errors": sum(r["status"] == "ERROR" for r in rows),
        }
    pairs = {}
    for arm in ARMS[1:]:
        if set(by_arm[arm]) != set(by_arm["memory"]):
            raise ValueError("arms must evaluate exactly the same tasks")
        outcomes = Counter()
        for key, memory in by_arm["memory"].items():
            other = by_arm[arm][key]
            label = (
                ("both_correct" if other["correct"] else "memory_only")
                if memory["correct"]
                else ("other_only" if other["correct"] else "both_wrong")
            )
            outcomes[label] += 1
        pairs[arm] = {
            key: outcomes[key]
            for key in ("both_correct", "memory_only", "other_only", "both_wrong")
        }
    return {"arms": counts, "paired_vs_memory": pairs}


def run(fixtures: Path, output: Path) -> dict:
    stories = load_fixtures(fixtures)
    fixture_hash = digest(fixtures)
    output.mkdir(parents=True, exist_ok=False)
    (output / "fixtures.jsonl").write_bytes(fixtures.read_bytes())
    if digest(output / "fixtures.jsonl") != fixture_hash:
        raise ValueError("fixtures changed during copy")
    source_paths = sorted(Path(__file__).parent.rglob("*.py"))
    sources = {
        str(p.relative_to(Path(__file__).parent)): digest(p) for p in source_paths
    }
    write_json(output / "source-manifest.json", {"files": sources})
    plan = {
        "kind": "paired_structured_state_development",
        "blind": False,
        "model_calls": 0,
        "fixture_sha256": fixture_hash,
        "arms": list(ARMS),
        "reader": "exact confirmed field lookup; no LLM",
        "budget": "all arms see all supplied events; no token limit or NLP claim",
        "memory_restart": "close after ingestion; reopen before tasks",
        "expected_tasks_per_arm": sum(len(s["tasks"]) for s in stories),
        "limitations": [
            "Developer-authored structured fixtures; no independent generalization evidence.",
            "Source admission ablation is not all selective-writing mechanisms.",
            "Rolling field summary and lexical top-one are simple proxies, not LLM summarization or model RAG.",
            "Event scan implements the same explicit update/role/deletion contract; a tie proves no superiority.",
            "Latency excludes annotation and user time; backend timings are not directly comparable.",
        ],
    }
    write_json(output / "plan.json", plan)
    results, sizes = [], []
    for index, story in enumerate(stories):
        for arm in ARMS:
            app, setup_error = None, None
            started = perf_counter()
            try:
                if arm in ARMS[:4]:
                    db = output / f"state-{index}-{arm}.db"
                    build_memory(story["events"], db, arm)
                    app = MemoryApplication(str(db))
                    sizes.append(
                        {
                            "story": story["id"],
                            "arm": arm,
                            "memory_count": len(app.snapshot("experiment")["timeline"]),
                            "database_bytes": db.stat().st_size,
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - failed arm remains in the denominator
                setup_error = type(exc).__name__
            setup_ms = (perf_counter() - started) * 1000
            try:
                for task in story["tasks"]:
                    actual, error = None, setup_error
                    started = perf_counter()
                    try:
                        if error is None:
                            actual = (
                                app.lookup("experiment", task["key"], as_of=task["at"])[
                                    "value"
                                ]
                                if app
                                else baseline(
                                    story["events"], task["key"], task["at"], arm
                                )
                            )
                    except Exception as exc:  # noqa: BLE001 - record, never silently drop a failed task
                        error = type(exc).__name__
                    results.append(
                        {
                            "story": story["id"],
                            "task": task["id"],
                            "family": story["family"],
                            "arm": arm,
                            "actual": actual,
                            "expected": task["expected"],
                            "correct": error is None and actual == task["expected"],
                            "status": "ERROR" if error else "COMPLETED",
                            "error_type": error,
                            "setup_ms": setup_ms,
                            "query_ms": (perf_counter() - started) * 1000,
                        }
                    )
                    write_json(output / "results.json", {"rows": results})
            finally:
                if app:
                    app.close()
    summary = {
        **plan,
        **summarize(results, plan["expected_tasks_per_arm"]),
        "storage": sizes,
    }
    final_sources = {
        str(p.relative_to(Path(__file__).parent)): digest(p)
        for p in sorted(Path(__file__).parent.rglob("*.py"))
    }
    if final_sources != sources:
        raise ValueError("source changed during experiment")
    write_json(
        output / "failures.json", {"rows": [r for r in results if not r["correct"]]}
    )
    write_json(output / "summary.json", summary)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args.fixtures, args.output)
    print(
        json.dumps(
            {"arms": result["arms"], "paired_vs_memory": result["paired_vs_memory"]}
        )
    )
    return 1 if any(a["errors"] for a in result["arms"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
