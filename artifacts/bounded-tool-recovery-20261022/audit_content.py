"""Read-only source-chain audit of R62 content failures; never generates answers."""

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--repository", type=Path, default=Path.cwd())
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = args.repository.resolve()
sys.path.insert(0, str(root / "src"))
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.runner import trace_filename

base = root / "eval/reports/scoped-evidence-20261020"
priorities = base / "full-final-v2/improvement-priorities.json"
verdict_path = base / "full-final-v2/verdicts.json"
dataset_path = root / "eval/external/longmemeval/longmemeval_oracle.json"
inputs = [priorities, verdict_path, dataset_path]
ids = json.loads(priorities.read_text(encoding="utf-8"))["content_error_ids"]
verdicts = {r["question_id"]: r for r in json.loads(verdict_path.read_text(encoding="utf-8"))["results"]}
cases = load_oracle_dataset(dataset_path).by_id()
assert len(ids) == len(set(ids)) == 40
header = re.compile(r"^\[(\d+)\] id=([^;\n]+); source=([^;\n]+); valid=[^\n]+\n    ", re.M)


def passages(turn):
    context = turn["context"]
    matches = list(header.finditer(context))
    assert [m.group(2) for m in matches] == turn["visible_memory_ids"]
    return [
        (set(m.group(3).split(",")), context[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(context)].rstrip())
        for i, m in enumerate(matches)
    ]


def refs_in(blocks):
    return set().union(*(refs for refs, _ in blocks))


def normalized(text):
    return " ".join(text.split())


rows = []
private = []
for qid in ids:
    case = cases[qid]
    verdict = verdicts[qid]
    assert verdict["correct"] is False and verdict["status"] in {"ANSWER", "ABSTAIN"}
    paths = [base / d / "run/traces" / trace_filename(qid) for d in ("full", "full-continuation")]
    paths = [p for p in paths if p.exists()]
    assert len(paths) == 1
    path = paths[0]
    inputs.append(path)
    trace = json.loads(path.read_text(encoding="utf-8"))
    assert trace["reader"]["status"] == verdict["status"]
    turns = json.loads(trace["reader"]["detail"])["turns"]
    blocks = [passages(t) for t in turns]
    events = trace["formation"]["turn_events"]
    counts = trace["formation"]["turn_memory_counts"]
    retrieved = set().union(*map(set, trace["retrieval"]["returned_source_refs"]))
    by_ref = {t.ref: t for t in case.turns}
    source_rows = []
    for ref in case.evidence_refs:
        event = events[ref]
        source_rows.append({
            "turn_ref": ref,
            "role": by_ref[ref].role,
            "formed_memories": counts.get(ref, 0),
            "initial_retrieval": event in retrieved,
            "initial_context": event in refs_in(blocks[0]),
            "any_context": any(event in refs_in(b) for b in blocks),
            "final_context": event in refs_in(blocks[-1]),
            "full_turn_in_final_passage": any(event in refs and normalized(by_ref[ref].content) in normalized(text) for refs, text in blocks[-1]),
        })
    if case.should_abstain:
        group = "unanswerable_expected_refusal"
    elif not source_rows:
        group = "no_annotated_turns"
    elif any(r["formed_memories"] == 0 for r in source_rows):
        group = "annotated_source_not_formed"
    elif not all(r["final_context"] for r in source_rows):
        group = "formed_source_not_visible_in_final"
    elif not all(r["full_turn_in_final_passage"] for r in source_rows):
        group = "sources_visible_but_full_turn_text_partial"
    else:
        group = "full_annotated_turns_visible"
    rows.append({
        "question_id": qid,
        "category": "unanswerable" if case.should_abstain else case.question_type,
        "status": verdict["status"],
        "group": group,
        "trace": path.relative_to(root).as_posix(),
        "trace_sha256": sha256(path.read_bytes()).hexdigest(),
        "reader_calls": len(turns),
        "tools_attempted": [t["action"]["action"] for t in turns if "action" in t],
        "sources": source_rows,
    })
    private.append({
        "question_id": qid, "group": group, "question": case.question,
        "hypothesis": trace["hypothesis"], "reference": case.answer,
        "missing": trace["reader"]["missing_requirements"],
        "annotated_turns": [{"ref": ref, "content": by_ref[ref].content} for ref in case.evidence_refs],
        "final_context": turns[-1]["context"],
    })

report = {
    "scope": "Post-run metadata audit only. Full-turn visibility is not semantic sufficiency; partial text is not proof that necessary spans were lost. No accuracy claims or verdict changes.",
    "new_model_requests": 0, "cost_cny": "0", "benchmark_accuracy": None,
    "cases": len(rows), "statuses": dict(Counter(r["status"] for r in rows)),
    "groups": dict(Counter(r["group"] for r in rows)),
    "answerable_cases_with_initial_retrieval_gap": sum(r["category"] != "unanswerable" and any(not s["initial_retrieval"] for s in r["sources"]) for r in rows),
    "answerable_cases_with_final_source_gap": sum(r["category"] != "unanswerable" and any(not s["final_context"] for s in r["sources"]) for r in rows),
    "answerable_cases_with_source_lost_after_initial_context": sum(r["category"] != "unanswerable" and any(s["initial_context"] and not s["final_context"] for s in r["sources"]) for r in rows),
    "input_sha256": {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest() for p in inputs},
    "rows": rows,
}
with args.output.open("x", encoding="utf-8", newline="\n") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
    f.write("\n")
with args.output.with_name("private-review.json").open("x", encoding="utf-8", newline="\n") as f:
    json.dump(private, f, ensure_ascii=False, indent=2)
    f.write("\n")
print(json.dumps({k: v for k, v in report.items() if k not in {"input_sha256", "rows"}}, ensure_ascii=False))
