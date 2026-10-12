"""Zero-network parity of the independent factory against frozen R40 evidence.

This audit adapter may import historical audit helpers; the installed runtime
must not. It never relaxes or calls R40's frozen-source guard on new source.
Only latency is excluded from reader output equality.
"""

import argparse
import json
from hashlib import sha256
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ("src", "ops", "audit_tools")]
import oracle_current_state_experiment as history
from oracle_refusal_gated_experiment import ModeClient
from dive_memory.application.readers import create_reader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Use a new audit output path; old results are immutable")
    frozen = history.OUTPUT
    plan = json.loads((frozen / "frozen-plan.json").read_text(encoding="utf-8"))
    meter = history.cache_meter(frozen, "delivery-parity-no-network")
    reader = create_reader(ModeClient(meter, meter.normal_transport),
        ModeClient(meter, meter.thinking_transport), provider="deepseek",
        model="deepseek-flash", strategy="r40")
    assert reader.spec.to_dict() == plan["reader_spec"]
    dataset = history.e.load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    assert dataset.sha256 == plan["dataset_sha256"]
    _, records = history.e._load_run(frozen / "full")
    checks = []
    for case in dataset.cases:
        saved = records[case.question_id]
        request = history.e.restore_visible_request(case.question, saved)
        outcome = reader.generate(request).to_dict()
        outcome["spec"] = reader.spec.to_dict()
        assert history.canonical(outcome) == history.canonical(saved["reader"]), case.question_id
        checks.append({"question_id": case.question_id, "equivalent": True})
    assert meter.summary()["attempts"] == 0
    report = {"kind": "exact_cache_reader_factory_parity_not_new_accuracy",
        "cases": len(checks), "equivalent": len(checks), "new_api_calls": 0,
        "cache_hits": meter.hits, "excludes": ["latency_ms"],
        "reader_spec": reader.spec.to_dict(), "dataset_sha256": dataset.sha256,
        "factory_sha256": sha256((ROOT / "src/dive_memory/application/readers.py").read_bytes()).hexdigest(),
        "frozen_submission_sha256": sha256((frozen / "full/submission.jsonl").read_bytes()).hexdigest(),
        "checks": checks}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "checks"}))


if __name__ == "__main__":
    main()
