"""False-memory / safety regression harness for the write gate.

The suite in ``eval/datasets/false_memory_v1/cases.json`` labels, per event,
whether a memory *should* have been formed. That is exactly a gate oracle, so
this harness evaluates gate policies directly on those events instead of
running the full service pipeline.

Two things are reported separately, because they answer different questions:

* **oracle agreement** — does the gate's accept/reject match
  ``memory_expected``? This is the end-to-end safety number.
* **floor integrity** — for events the suite marks as attacks (sensitive
  data, untrusted sources, injection), did the gate refuse them? This must
  never regress when recall is relaxed.

Usage:
    python ops/false_memory_gate_eval.py --policy v1 --output eval/reports/false-memory-v1.json
    python ops/false_memory_gate_eval.py --policy v2 --output eval/reports/false-memory-v2.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dive_memory.gate import decide as decide_v1  # noqa: E402

DATASET = ROOT / "eval" / "datasets" / "false_memory_v1" / "cases.json"

# Categories whose events are adversarial by construction: a policy that
# accepts these has a defect regardless of any recall gain.
ATTACK_CATEGORIES = frozenset({
    "extraction_hallucination",
    "inference_promotion",
    "incorrect_merge",
    "incorrect_supersede",
    "entity_confusion",
    "temporal_confusion",
    "generated_response_feedback",
    "summary_distortion",
})


def _load_cases() -> list[dict]:
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    return list(payload["cases"])


def _v2_decide_factory():
    from dive_memory.semantic_gate import load_local_gate

    gate = load_local_gate()
    return gate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", choices=("v1", "v2"), required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    decide = decide_v1 if args.policy == "v1" else _v2_decide_factory()
    supports_source = args.policy == "v2"
    cases = _load_cases()

    total_events = 0
    agree = 0
    by_category: dict[str, dict[str, int]] = defaultdict(lambda: {"events": 0, "agree": 0})
    attack_events = 0
    attack_refused = 0
    false_accepts: list[dict] = []
    false_rejects: list[dict] = []
    # The oracle has two independent fields and they mean different things:
    #   memory_expected    -- should *any* memory be formed from this event
    #   user_fact_allowed  -- may it become a fact about the *current user*
    # Conflating them mis-scores third-party statements such as
    # "Alice lives in Shanghai; do not treat this as my residence", which
    # should form an entity memory but must never enter the user profile.
    # The gate governs user-fact admission, so we report both axes.
    user_fact_events = 0
    user_fact_correct = 0

    for case in cases:
        category = case.get("category", "unknown")
        for event in case.get("events", []):
            oracle = event.get("oracle")
            if not isinstance(oracle, dict) or "memory_expected" not in oracle:
                continue
            if event.get("operation") not in (None, "ingest"):
                continue
            text = str(event.get("text", ""))
            explicit = bool(event.get("explicit", False))
            source_type = event.get("source_type")
            expected = bool(oracle["memory_expected"])
            user_fact_allowed = oracle.get("user_fact_allowed")
            # A gate acceptance means "this may become a user fact".
            gate_accepts_user_fact = decision_accepts = None
            if supports_source:
                decision = decide(text, explicit=explicit, source_type=source_type)
            else:
                decision = decide(text, explicit=explicit)
            gate_accepts_user_fact = decision.accepted

            total_events += 1
            ok = decision.accepted is expected
            agree += int(ok)
            by_category[category]["events"] += 1
            by_category[category]["agree"] += int(ok)

            if user_fact_allowed is not None:
                user_fact_events += 1
                user_fact_correct += int(gate_accepts_user_fact is bool(user_fact_allowed))

            if category in ATTACK_CATEGORIES:
                attack_events += 1
                attack_refused += int(not decision.accepted)

            row = {
                "case_id": case["case_id"], "category": category,
                "source_type": source_type,
                "memory_expected": expected,
                "user_fact_allowed": user_fact_allowed,
                "reason_code": decision.reason_code, "text": text[:200],
            }
            if not ok and decision.accepted and not expected:
                false_accepts.append(row)
            elif not ok and not decision.accepted and expected:
                false_rejects.append(row)

    report = {
        "policy": args.policy,
        "source_aware": supports_source,
        "dataset": str(DATASET),
        "events_evaluated": total_events,
        "oracle_agreement": agree / total_events if total_events else 0.0,
        "oracle_agrees": agree,
        "by_category": {key: value for key, value in sorted(by_category.items())},
        "floor_integrity": {
            "attack_events": attack_events,
            "attack_refused": attack_refused,
            "attack_refusal_rate": attack_refused / attack_events if attack_events else 0.0,
        },
        "false_accept_count": len(false_accepts),
        "false_reject_count": len(false_rejects),
        "false_accepts": false_accepts,
        "false_rejects": false_rejects,
        "user_fact_axis": {
            "events": user_fact_events,
            "correct": user_fact_correct,
            "accuracy": user_fact_correct / user_fact_events if user_fact_events else 0.0,
            "note": "Gate acceptance is compared against oracle.user_fact_allowed.",
        },
        "notes": [
            "The suite's oracle is evaluated at the gate, not through the full service.",
            "memory_expected covers any memory; user_fact_allowed covers the user profile.",
            "false_accepts are the safety-critical direction.",
            "The v1 policy receives no source_type, so provenance-only cases are "
            "expected to fail for v1 and are the specific gap v2 closes.",
        ],
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"policy={args.policy} events={total_events} agreement={report['oracle_agreement']:.4f} "
          f"user_fact={report['user_fact_axis']['accuracy']:.4f} "
          f"attack_refusal={report['floor_integrity']['attack_refusal_rate']:.4f} "
          f"false_accepts={len(false_accepts)} false_rejects={len(false_rejects)}")


if __name__ == "__main__":
    main()
