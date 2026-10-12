"""Portable exact-cache replay kit. Verification needs only the installed package.

Creation reads local historical public benchmark evidence. Verification opens a
zip without extracting it and has no network transport, evaluator or gold data.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dive_memory.application.readers import create_reader
from dive_memory.models import EvidenceState, Memory, MemoryStatus, RetrievalItem
from dive_memory.oracle.reader import ChatCompletion, OracleReaderRequest, render_reader_context
from dive_memory.token_budget import ApproximateTokenCounter


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "latency_ms":
                continue
            if key == "detail" and isinstance(item, str):
                try:
                    item = json.loads(item)
                except ValueError:
                    pass
            result[key] = canonical(item)
        return result
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def restore(row, policy):
    trace = row["trace"]
    text = trace["reader_context_text"]
    offsets = []
    for index, view in enumerate(trace["evidence"]["views"]):
        assert index == view["index"]
        marker = f"[{index}] id={view['memory_id']}; source="
        start = text.find(marker)
        assert start >= 0 and text.find(marker, start + 1) < 0
        body = text.find("\n    ", start)
        assert body >= 0
        offsets.append((start, body + 5, view))
    assert offsets == sorted(offsets, key=lambda item: item[0])
    statuses = {"CURRENT": MemoryStatus.ACTIVE, "SUPERSEDED": MemoryStatus.SUPERSEDED,
                "ARCHIVED": MemoryStatus.ARCHIVED, "REMOVED": MemoryStatus.DELETED}
    items = []
    for i, (_, body, view) in enumerate(offsets):
        end = offsets[i + 1][0] - 1 if i + 1 < len(offsets) else len(text)
        memory = Memory(view["memory_id"], trace["namespace"], "visible_replay",
            EvidenceState.OBSERVATION, text[body:end], {"role": view["role"]},
            status=statuses[view["lifecycle"]], valid_from=view["valid_from"],
            valid_to=view["valid_to"], contradicts_id=view["conflict_group"])
        items.append(RetrievalItem(memory, view["retrieval_score"], channels=view["channels"],
                                   source_refs=view["source_refs"]))
    request = OracleReaderRequest(row["question"], trace["as_of"], trace["as_of"], tuple(items), text,
        token_budget=6000, evidence_omitted=trace["packing"]["omitted"], policy=policy)
    assert render_reader_context(request) == text
    assert ApproximateTokenCounter().count(text) <= 6000
    return request


class Cache:
    provider = "deepseek"

    def __init__(self, read, mode, used):
        self.read, self.mode, self.used = read, mode, used

    def complete(self, **kwargs):
        identity = {"endpoint": "https://api.deepseek.com/chat/completions",
                    "extra_body": self.mode, "request": kwargs}
        key = digest(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        raw = self.read(key)
        self.used.append((key, raw))
        return ChatCompletion(**json.loads(raw))


def replay(rows, read):
    used = []
    normal = Cache(read, {"thinking": {"type": "disabled"}}, used)
    thinking = Cache(read, {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}, used)
    reader = create_reader(normal, thinking, model="deepseek-flash", provider="deepseek", strategy="r40")
    for row in rows:
        actual = reader.generate(restore(row, reader.policy)).to_dict()
        actual["spec"] = reader.spec.to_dict()
        assert canonical(actual) == canonical(row["trace"]["reader"]), row["question_id"]
    return used


def create(output):
    from dive_memory.oracle.loader import load_oracle_dataset
    from dive_memory.oracle.runner import trace_filename

    if output.exists():
        raise SystemExit("Use a fresh bundle path")
    historical = ROOT / "eval/reports/oracle-current-state-review-20261012"
    plan = json.loads((historical / "frozen-plan.json").read_text(encoding="utf-8"))
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    assert dataset.sha256 == plan["dataset_sha256"]
    rows = []
    for case in dataset.cases:
        raw = (historical / "full/traces" / trace_filename(case.question_id)).read_bytes()
        trace = json.loads(raw)
        # Only the public question and already-visible evidence, plus comparison
        # output, travel in the kit. No reference answer or question type.
        kept = {k: trace[k] for k in ("namespace", "as_of", "reader_context_text", "evidence", "reader", "packing")}
        rows.append({"question_id": case.question_id, "question": case.question,
                     "original_trace_sha256": digest(raw), "trace": kept})
    paths = {}
    for directory in sorted((ROOT / "eval/reports").glob("*/*/responses")):
        for path in directory.glob("*.json"):
            paths.setdefault(path.stem, path)
    used = replay(rows, lambda key: paths[key].read_bytes())
    entries = {"requests.json": json.dumps(rows, ensure_ascii=False).encode(),
               "submission.jsonl": (historical / "full/submission.jsonl").read_bytes(),
               "evaluation-summary.json": (historical / "full-evaluation.json").read_bytes()}
    entries.update({f"responses/{key}.json": raw for key, raw in used})
    for path in (historical / "source-snapshot").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            entries["frozen-source/" + path.relative_to(historical / "source-snapshot").as_posix()] = path.read_bytes()
    manifest = {"schema": "dive-portable-r40-replay-v1", "cases": len(rows),
        "kind": "exact_cached_reader_replay_not_fresh_generation_or_official_score",
        "reader_spec": plan["reader_spec"], "dataset_sha256": dataset.sha256,
        "frozen_source_environment_sha256": plan["source_snapshot"]["sha256"],
        "frozen_plan_sha256": digest((historical / "frozen-plan.json").read_bytes()),
        "cache_hits": len(used), "unique_responses": len({key for key, _ in used}),
        "files": {name: digest(data) for name, data in entries.items()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"bundle": output.name, "sha256": digest(output.read_bytes()), "bytes": output.stat().st_size,
            "files": len(entries), "cases": len(rows), "cache_hits": len(used), "new_api_calls": 0}


def verify(bundle):
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for name, expected in manifest["files"].items():
            assert digest(archive.read(name)) == expected, name
        rows = json.loads(archive.read("requests.json"))
        used = replay(rows, lambda key: archive.read(f"responses/{key}.json"))
    return {"passed": True, "cases": len(rows), "cache_hits": len(used), "new_api_calls": 0,
        "bundle_sha256": digest(bundle.read_bytes()), "network_transport_available": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", type=Path)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.create) == bool(args.verify):
        parser.error("choose one of --create or --verify")
    report = create(args.create) if args.create else verify(args.verify)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
