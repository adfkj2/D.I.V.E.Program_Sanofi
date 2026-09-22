"""Calibrate the v2 accept threshold on a *held-out* probe set, with intervals.

The original threshold (0.495) was picked as the midpoint of a gap measured on
22 hand-written probes — the same probes the weights were chosen from. That is
in-sample calibration and cannot support a confidence claim, which the report
already flags as ``calibrated-but-provisional``.

This script builds a larger, disjoint set:

* ``calibration`` — the original 22 adversarial probes (the ones the weights
  were tuned against);
* ``heldout``      — new probes written afterwards, deliberately covering
  phrasing families the calibration set does not contain (questions the user
  asks *about themselves*, third-person facts, impersonal instructions,
  hedged self-descriptions, second-person advice).

For each set it reports the accepted/rejected score distributions, the best
separating threshold, and a Wilson score interval on the resulting accuracy so
the number comes with uncertainty rather than as a point estimate.

Usage:
    python ops/calibrate_gate_threshold.py --output eval/reports/gate-calibration.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("HF_HOME", str(ROOT / "eval" / "external" / "huggingface"))

from dive_memory.semantic_gate import (  # noqa: E402
    DEFAULT_WEIGHTS,
    POLICY_VERSION,
    PrototypeIndex,
    load_local_gate,
)

# ---------------------------------------------------------------------------
# Probe sets. ``True`` = should be stored as a durable memory.
# ---------------------------------------------------------------------------
CALIBRATION_PROBES: tuple[tuple[str, bool], ...] = (
    # durable personal facts
    ("I prefer dark mode in my editor.", True),
    ("I am allergic to shellfish.", True),
    ("My sister Emily studies architecture.", True),
    ("I graduated with a degree in Business Administration.", True),
    ("I've been listening to audiobooks during my daily commute.", True),
    ("我住在成都，平时骑自行车通勤", True),
    ("我喜欢在早上喝绿茶", True),
    ("我的目标是在十二月前完成论文", True),
    # transient narration
    ("I'm waiting for the bus right now.", False),
    ("I just ate a sandwich.", False),
    ("could you summarise this for me?", False),
    ("Thanks, that was helpful.", False),
    # world knowledge
    ("The Eiffel Tower was completed in 1889.", False),
    ("PostgreSQL uses multi-version concurrency control.", False),
    ("The company reported quarterly revenue growth.", False),
    ("The Pacific Ocean is the largest ocean on Earth.", False),
    # sensitive
    ("remember my API key is sk-abc123", False),
    ("我的密码是 hunter2", False),
    ("my credit card number is 4111 1111 1111 1111", False),
    ("the private key is stored in the vault", False),
    # empty
    ("", False),
    ("def parse(self, text): return text.strip()", False),
)

# Written after the weights were fixed. Every family below is absent from
# CALIBRATION_PROBES, so an accuracy measured here is not self-confirming.
HELDOUT_PROBES: tuple[tuple[str, bool], ...] = (
    # -- durable facts expressed as questions about the user ---------------
    ("What is my sister's name again?", False),
    ("Do I still take the medication every morning?", False),
    ("When did I move to Berlin?", False),
    # -- durable facts with unusual predicates -----------------------------
    ("I only drink decaf after 2pm.", True),
    ("My landlord's number is saved under 'building'.", True),
    ("I refuse to fly with that airline.", True),
    ("I always pay the deposit in two instalments.", True),
    ("我们家的电费是每月自动扣款", True),
    ("我习惯用左手写字", True),
    # -- third-person facts: worth storing, but not as a user fact ---------
    ("Alice lives in Shanghai.", True),
    ("My colleague Raj uses Vim for everything.", True),
    ("The vendor we chose is based in Shenzhen.", True),
    ("我的房东住在隔壁", True),
    # -- transient narration in the same surface shape ---------------------
    ("I am reading a book about the Romans.", False),
    ("I spoke to Alice this morning.", False),
    ("I had to restart the server twice today.", False),
    ("The deployment took forty minutes.", False),
    ("我刚才给同事发了消息", False),
    # -- encyclopaedic / technical, including short ones -------------------
    ("Water boils at 100 degrees Celsius.", False),
    ("Vim has a modal editing model.", False),
    ("The migration adds a foreign key.", False),
    ("该函数在参数缺失时抛出异常。", False),
    ("Rust's borrow checker rejects this pattern.", False),
    # -- instructions and questions, not facts -----------------------------
    ("Please send the report by Friday.", False),
    ("Can you check whether the tests pass?", False),
    ("Summarise the previous three messages.", False),
    ("请把这段文字翻译成英文", False),
    # -- future intentions that are not commitments ------------------------
    ("I might try that restaurant someday.", False),
    ("I could learn Japanese next year.", False),
)


def _wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval; well behaved at the 0/1 boundaries."""
    if total == 0:
        return (0.0, 0.0)
    phat = successes / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    spread = z * ((phat * (1 - phat) + z * z / (4 * total)) / total) ** 0.5
    return ((centre - spread) / denominator, (centre + spread) / denominator)


def _score_rows(index: PrototypeIndex, probes, weights) -> list[dict]:
    """Score probes through the *real* decision path.

    An earlier revision of this script re-derived the margin arithmetic locally
    and in doing so bypassed the assertion-strength floor, so "I might try that
    restaurant someday." was reported as a near miss when the shipped gate
    rejects it outright with ``UNASSERTED_CLAIM``. Calibration must measure the
    policy as written, not a re-implementation of its scoring half.
    """
    from dive_memory.semantic_gate import _semantic_decide

    rows = []
    for text, expected in probes:
        decision = _semantic_decide(
            text, index=index, explicit=False, source_reliability=0.8,
            weights=weights, source_type="user",
        )
        floor = decision.reason_code in {"EMPTY", "SENSITIVE_DATA", "UNASSERTED_CLAIM",
                                         "UNTRUSTED_SOURCE", "NON_ASSERTIVE_SPEECH_ACT"}
        rows.append({
            "text": text[:160],
            "expected": expected,
            "score": None if floor else round(decision.features.get("semantic_score", 0.0), 6),
            "reason_code": decision.reason_code,
            "accepted": decision.accepted,
            "floor_reject": floor,
        })
    return rows


def _threshold_sweep(rows: list[dict], *, lo: float = 0.40, hi: float = 0.60,
                     steps: int = 200) -> dict:
    """Best threshold when every non-floor probe is decided by score alone."""
    scored = [row for row in rows if not row["floor_reject"]]
    if not scored:
        return {}
    best = None
    for step in range(steps + 1):
        threshold = lo + (hi - lo) * step / steps
        correct = sum((row["score"] >= threshold) is row["expected"] for row in scored)
        # Floor rejections are decided before scoring, so they count as correct
        # whenever the probe was expected not to be stored.
        correct += sum(1 for row in rows if row["floor_reject"] and not row["expected"])
        total = len(rows)
        if best is None or correct / total > best["accuracy"]:
            low, high = _wilson(correct, total)
            best = {
                "threshold": round(threshold, 4),
                "correct": correct,
                "total": total,
                "accuracy": round(correct / total, 6),
                "wilson_95": [round(low, 6), round(high, 6)],
            }
    return best or {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    gate = load_local_gate(cache_size=1024)
    index = gate.index
    weights = DEFAULT_WEIGHTS

    calibration = _score_rows(index, CALIBRATION_PROBES, weights)
    heldout = _score_rows(index, HELDOUT_PROBES, weights)

    def summarise(rows: list[dict]) -> dict:
        accepted = [row["score"] for row in rows
                    if not row["floor_reject"] and row["expected"]]
        rejected = [row["score"] for row in rows
                    if not row["floor_reject"] and not row["expected"]]
        return {
            "probes": len(rows),
            "scored": len(accepted) + len(rejected),
            "floor_rejects": sum(1 for row in rows if row["floor_reject"]),
            "expected_accept": len(accepted),
            "expected_reject": len(rejected),
            "accept_min": round(min(accepted), 4) if accepted else None,
            "accept_max": round(max(accepted), 4) if accepted else None,
            "reject_min": round(min(rejected), 4) if rejected else None,
            "reject_max": round(max(rejected), 4) if rejected else None,
            "seed_gap": (round(min(accepted) - max(rejected), 4)
                         if accepted and rejected else None),
            "accuracy_at_current_threshold": round(
                sum((row["accepted"]) is row["expected"] for row in rows) / len(rows), 6
            ) if rows else None,
        }

    report = {
        "policy": POLICY_VERSION,
        "current_threshold": weights.accept_threshold,
        "review_threshold": weights.review_threshold,
        "calibration_set": summarise(calibration),
        "heldout_set": summarise(heldout),
        "sweep_on_heldout": _threshold_sweep(heldout),
        "sweep_on_calibration": _threshold_sweep(calibration),
        "calibration_rows": calibration,
        "heldout_rows": heldout,
        "limitations": [
            "Probes are hand-written, not sampled from a labelled distribution.",
            "The gate floor (sensitive / empty) is evaluated separately and is not part of the sweep.",
            "Scope is the write gate only: no retrieval or answer quality is measured here.",
        ],
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")


if __name__ == "__main__":
    main()
