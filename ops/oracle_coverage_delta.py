"""Delta between two gold-evidence coverage reports (an ablation comparison).

Why this exists
---------------
``oracle_formation_coverage`` answers "how much gold evidence reached the
reader?" for *one* run. An ablation needs the difference between two runs, and
the difference has to be auditable: enabling document admission adds long
memories that compete for the same context budget, so a change that raises
``formed`` can still lower ``packed``. A single net number would hide that.

So this reports the delta at three levels *and* the case-level direction, which
is what separates "it helped" from "it moved the loss around":

* per-level deltas overall and per question type;
* how many cases gained, lost or kept each level;
* every case that lost any level, listed individually.

That last list is the point. A regression that is invisible in the aggregate is
still a regression, and this project's rule is that the cost of a change gets
stated with the change -- not discovered later.

Inputs are two reports from ``oracle_formation_coverage`` (schema v2). Nothing
here recomputes coverage or reads traces; it only compares published numbers, so
it cannot disagree with them.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "dive-oracle-coverage-delta-v1"

LEVELS = ("formed", "retrieved", "packed")


def _rate(value: int, gold: int) -> float | None:
    return (value / gold) if gold else None


def _delta(level_bucket: Mapping[str, Any], base_bucket: Mapping[str, Any]) -> dict[str, Any]:
    gold = int(level_bucket.get("gold_turns") or 0)
    base_gold = int(base_bucket.get("gold_turns") or 0)
    entry: dict[str, Any] = {"gold_turns": gold, "baseline_gold_turns": base_gold}
    for level in LEVELS:
        after = int(level_bucket.get(level) or 0)
        before = int(base_bucket.get(level) or 0)
        after_rate = _rate(after, gold)
        before_rate = _rate(before, base_gold)
        entry[level] = {
            "baseline": before,
            "candidate": after,
            "delta": after - before,
            "baseline_rate": before_rate,
            "candidate_rate": after_rate,
            "rate_delta": (
                after_rate - before_rate
                if after_rate is not None and before_rate is not None
                else None
            ),
        }
    return entry


def compare(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    baseline_label: str,
    candidate_label: str,
) -> dict[str, Any]:
    base_cases = {row["question_id"]: row for row in baseline.get("per_case") or ()}
    cand_cases = {row["question_id"]: row for row in candidate.get("per_case") or ()}

    directions: dict[str, dict[str, int]] = {
        level: {"gained": 0, "lost": 0, "unchanged": 0} for level in LEVELS
    }
    gained_cases: list[dict[str, Any]] = []
    regressed_cases: list[dict[str, Any]] = []
    only_baseline: list[str] = []
    only_candidate: list[str] = []

    for question_id in sorted(set(base_cases) | set(cand_cases)):
        before = base_cases.get(question_id)
        after = cand_cases.get(question_id)
        if before is None:
            only_candidate.append(question_id)
            continue
        if after is None:
            only_baseline.append(question_id)
            continue
        per_level: dict[str, dict[str, int]] = {}
        for level in LEVELS:
            b = int(before.get(level) or 0)
            a = int(after.get(level) or 0)
            per_level[level] = {"baseline": b, "candidate": a, "delta": a - b}
            if a > b:
                directions[level]["gained"] += 1
            elif a < b:
                directions[level]["lost"] += 1
            else:
                directions[level]["unchanged"] += 1
        row = {
            "question_id": question_id,
            "question_type": after.get("question_type") or before.get("question_type"),
            "gold_turns": after.get("gold_turns") or before.get("gold_turns"),
            **per_level,
        }
        if any(per_level[level]["delta"] > 0 for level in LEVELS):
            gained_cases.append(row)
        if any(per_level[level]["delta"] < 0 for level in LEVELS):
            regressed_cases.append(row)

    by_type: dict[str, dict[str, Any]] = {}
    for name in sorted(set(baseline.get("by_question_type") or {})
                       | set(candidate.get("by_question_type") or {})):
        by_type[name] = _delta(
            (candidate.get("by_question_type") or {}).get(name, {}),
            (baseline.get("by_question_type") or {}).get(name, {}),
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_label": baseline_label,
        "candidate_label": candidate_label,
        "baseline_traces_dir": baseline.get("traces_dir"),
        "candidate_traces_dir": candidate.get("traces_dir"),
        "overall": _delta(candidate.get("overall") or {}, baseline.get("overall") or {}),
        "by_question_type": by_type,
        "case_level": {
            "compared": len(set(base_cases) & set(cand_cases)),
            "only_in_baseline": only_baseline,
            "only_in_candidate": only_candidate,
            "directions": directions,
        },
        "gained_cases": gained_cases,
        "regressed_cases": regressed_cases,
        "interpretation": {
            "is_accuracy": False,
            "levels_are_nested": True,
            "note": (
                "Coverage is an upper bound on the answer stage. A level can rise "
                "while a later level falls (a longer memory is formed and retrieved "
                "but no longer fits the context budget), which is why all three "
                "levels and the case-level direction are reported together."
            ),
        },
    }


def summarize(report: Mapping[str, Any]) -> str:
    lines = [
        f"baseline : {report['baseline_label']}",
        f"candidate: {report['candidate_label']}",
        "",
        f"{'level':<12}{'before':>8}{'after':>8}{'delta':>8}"
        f"{'rate_before':>13}{'rate_after':>12}{'rate_delta':>12}",
    ]
    for level in LEVELS:
        row = report["overall"][level]
        fmt = lambda v: "n/a" if v is None else f"{v:.4f}"  # noqa: E731
        lines.append(
            f"{level:<12}{row['baseline']:>8}{row['candidate']:>8}{row['delta']:>+8}"
            f"{fmt(row['baseline_rate']):>13}{fmt(row['candidate_rate']):>12}"
            f"{fmt(row['rate_delta']):>12}"
        )

    lines.append("")
    lines.append(f"{'question_type':<28}{'gold':>6}{'formed':>8}{'d':>6}"
                 f"{'retr':>8}{'d':>6}{'packed':>8}{'d':>6}")
    for name, row in report["by_question_type"].items():
        lines.append(
            f"{name:<28}{row['gold_turns']:>6}"
            f"{row['formed']['candidate']:>8}{row['formed']['delta']:>+6}"
            f"{row['retrieved']['candidate']:>8}{row['retrieved']['delta']:>+6}"
            f"{row['packed']['candidate']:>8}{row['packed']['delta']:>+6}"
        )

    case_level = report["case_level"]
    lines.append("")
    lines.append(f"cases compared: {case_level['compared']}"
                 f"   only-in-baseline: {len(case_level['only_in_baseline'])}"
                 f"   only-in-candidate: {len(case_level['only_in_candidate'])}")
    for level in LEVELS:
        bucket = case_level["directions"][level]
        lines.append(f"  {level:<10} gained {bucket['gained']:>4}"
                     f"   lost {bucket['lost']:>4}   unchanged {bucket['unchanged']:>4}")
    lines.append(f"  cases that lost any level: {len(report['regressed_cases'])}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python ops/oracle_coverage_delta.py",
        description="Compare two Oracle gold-evidence coverage reports.",
    )
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline-label", default="baseline")
    parser.add_argument("--candidate-label", default="candidate")
    args = parser.parse_args(argv)

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    for label, report in (("baseline", baseline), ("candidate", candidate)):
        if not report.get("schema_version", "").startswith("dive-oracle-coverage"):
            raise SystemExit(f"{label} is not a coverage report: {args}")
    report = compare(
        baseline, candidate,
        baseline_label=args.baseline_label, candidate_label=args.candidate_label,
    )
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(summarize(report))
    print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
