"""Offline gate-formation probe (read-only, no service, no DB).

Counts how many gold-evidence turns would pass the write gate, so we can
compare gate policies on the *same* 896 evidence turns without re-running the
full (very slow) LongMemEval retrieval benchmark.

Usage:
    python ops/gate_formation_probe.py --policy keyword
    python ops/gate_formation_probe.py --policy keyword --all-turns
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dive_memory.gate import decide  # noqa: E402
from dive_memory.eval_adapters.longmemeval import LongMemEvalAdapter  # noqa: E402

DATASET = ROOT / "eval" / "external" / "longmemeval" / "longmemeval_s_cleaned.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--policy", default="keyword")
    parser.add_argument("--all-turns", action="store_true")
    parser.add_argument("--dump-rejects", default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    cases = LongMemEvalAdapter.load(args.dataset)

    evidence_total = 0
    evidence_accepted = 0
    evidence_reasons: Counter[str] = Counter()
    all_total = 0
    all_accepted = 0
    all_reasons: Counter[str] = Counter()
    rejects: list[dict[str, str]] = []
    evidence_rejects: list[dict[str, str]] = []

    for case in cases:
        for turn in case.turns:
            text = turn.content
            if not text.strip():
                continue
            decision = decide(text, explicit=False)
            all_total += 1
            if decision.accepted:
                all_accepted += 1
            else:
                all_reasons[decision.reason_code] += 1
            if turn.has_answer:
                evidence_total += 1
                if decision.accepted:
                    evidence_accepted += 1
                else:
                    evidence_reasons[decision.reason_code] += 1
                    if len(evidence_rejects) < 40:
                        evidence_rejects.append({
                            "question_id": case.question_id,
                            "ref": turn.ref,
                            "reason_code": decision.reason_code,
                            "text": text[:300],
                        })
            if args.dump_rejects and not decision.accepted and len(rejects) < 400:
                rejects.append({
                    "question_id": case.question_id,
                    "ref": turn.ref,
                    "reason_code": decision.reason_code,
                    "text": text[:300],
                })

    report = {
        "policy": args.policy,
        "dataset": args.dataset,
        "evidence_turns": evidence_total,
        "evidence_accepted": evidence_accepted,
        "evidence_formation_coverage": (
            evidence_accepted / evidence_total if evidence_total else 0.0
        ),
        "evidence_reason_codes": dict(evidence_reasons),
        "all_turns": all_total,
        "all_accepted": all_accepted,
        "all_accept_rate": all_accepted / all_total if all_total else 0.0,
        "all_reason_codes": dict(all_reasons),
        "evidence_reject_samples": evidence_rejects,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if args.dump_rejects:
        Path(args.dump_rejects).write_text(
            json.dumps(rejects, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"evidence {evidence_accepted}/{evidence_total} = "
          f"{report['evidence_formation_coverage']:.6f}")
    print(f"all      {all_accepted}/{all_total} = {report['all_accept_rate']:.6f}")


if __name__ == "__main__":
    main()
