"""Structured state mechanics and transparent baselines; no model accuracy claim."""

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dive_memory.application.records import ProjectRecord
from dive_memory.application.runtime import MemoryApplication
from project_stories import stories


def baseline(story, task, arm):
    events = story["events"]
    deleted = {e["delete"] for e in events if "delete" in e}
    rows = [e for e in events if "delete" not in e and e["id"] not in deleted]
    if arm == "recent_session":
        latest = max(e["at"] for e in rows)
        rows = [e for e in rows if e["at"] == latest]
    # Lexical top-one is a retrieval proxy, not an LLM RAG evaluation.
    if arm == "lexical_top_one":
        rows = [e for e in rows if e["key"] == task["key"] and e["at"] <= task["at"]]
        rows.sort(key=lambda e: e["at"], reverse=True)
        return (rows[0]["value"], [rows[0]["id"]]) if rows else (None, [])
    rows = [e for e in rows if e["role"] == "user" and e["disposition"] == "confirmed"]
    if arm == "rolling_summary":
        latest = {e["key"]: e for e in sorted(rows, key=lambda e: e["at"])}
        row = latest.get(task["key"])
        return (row["value"], [row["id"]]) if row else (None, [])
    rows = [e for e in rows if e["at"] <= task["at"]]
    closed = {e["replaces"] for e in rows if e.get("replaces")}
    rows = [e for e in rows if e["id"] not in closed and e["key"] == task["key"]]
    values = {e["value"] for e in rows}
    return (next(iter(values)), [e["id"] for e in rows]) if len(values) == 1 else (None, [])


def execute(story, *, updates=True, provenance=True):
    app = MemoryApplication()
    ids = {}
    for e in story["events"]:
        if "delete" in e:
            app.forget(story["id"], ids[e["delete"]])
            continue
        record = ProjectRecord(e["key"], e["value"], disposition=e["disposition"], session=e["id"])
        if e["role"] != "user" and provenance:
            result = app.service.ingest(story["id"], record.encode(), explicit=True,
                source_type=e["role"], observed_at=e["at"], idempotency_key=e["id"])
        else:
            result = app.record(story["id"], record, idempotency_key=e["id"], observed_at=e["at"],
                replaces=ids.get(e.get("replaces")) if updates else None)
        if result["memory_ids"]:
            ids[e["id"]] = result["memory_ids"][0]
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    fixture = list(stories())
    encoded = "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in fixture)
    (args.output / "stories.jsonl").write_text(encoded, encoding="utf-8")
    results = []
    for story in fixture:
        apps = {"memory": execute(story), "without_updates": execute(story, updates=False),
                "without_source_gate": execute(story, provenance=False)}
        try:
            for task in story["tasks"]:
                for arm in (*apps, "recent_session", "rolling_summary", "lexical_top_one", "event_scan"):
                    start = perf_counter()
                    if arm in apps:
                        answer = apps[arm].lookup(story["id"], task["key"], as_of=task["at"])
                        value, sources = answer["value"], [m["id"] for m in answer["sources"]]
                    else:
                        value, sources = baseline(story, task, arm)
                    results.append({"story": story["id"], "family": story["family"], "language": story["language"],
                        "task": task["id"], "arm": arm, "expected": task["expected"], "actual": value,
                        "correct": value == task["expected"], "sources": sources,
                        "elapsed_ms": (perf_counter() - start) * 1000})
        finally:
            for app in apps.values():
                app.close()
    counts = defaultdict(Counter)
    for row in results:
        counts[row["arm"]]["cases"] += 1
        counts[row["arm"]]["correct"] += row["correct"]
    report = {"kind": "structured_state_development_validation", "stories": len(fixture),
        "tasks_per_story": 4, "template_families": 6, "blind": False, "model_calls": 0,
        "fixture_sha256": sha256(encoded.encode()).hexdigest(),
        "arms": {k: dict(v) for k, v in counts.items()},
        "caveats": ["Schema and exact keys are supplied by the fixture, not extracted by an LLM.",
            "30 domains use six repeated mechanism templates; no claim of 30 independent linguistic patterns.",
            "Lexical top-one is not a full model-backed RAG comparison.",
            "Event scan is a strong simple baseline; matching it does not establish product value.",
            "Timing excludes user reading, annotation and maintenance; it is not recovery time.",
            "No reader-review ablation: no language model was run."],
        "by_family": {}, "by_record_language": {}}
    for field, target in (("family", "by_family"), ("language", "by_record_language")):
        for group in sorted({r[field] for r in results}):
            report[target][group] = {arm: {"correct": sum(r["correct"] for r in results if r[field] == group and r["arm"] == arm),
                "cases": sum(1 for r in results if r[field] == group and r["arm"] == arm)} for arm in counts}
    for filename, data in (("summary.json", report), ("results.json", results),
                           ("failures.json", [r for r in results if not r["correct"]])):
        (args.output / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["arms"]))


if __name__ == "__main__":
    main()
