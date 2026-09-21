"""Smoke: does the v2 gate reach the LongMemEval runner and change formation?

Runs a handful of cases twice (v1 vs v2) so the plumbing is verified before
spending hours on the full 500-case sweep.

Writes a JSON artifact directly so no stdout is needed on Windows.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("HF_HOME", str(ROOT / "eval" / "external" / "huggingface"))

DATASET = ROOT / "eval" / "external" / "longmemeval" / "longmemeval_s_cleaned.json"
MAX_CASES = int(sys.argv[1]) if len(sys.argv) > 1 else 8
OUT = ROOT / "eval" / "reports" / "smoke-v2-retrieval.json"


def main() -> None:
    from dive_memory.longmemeval_benchmark import run_longmemeval_retrieval

    out: dict[str, object] = {}
    for label, gate_name in (("v1", "utility-baseline-v1"), ("v2", "semantic-utility-v2")):
        try:
            gate = None
            if label == "v2":
                from dive_memory.semantic_gate import load_local_gate

                gate = load_local_gate()
            report = run_longmemeval_retrieval(
                DATASET, max_cases=MAX_CASES, limit=10,
                repository_root=ROOT, gate=gate, gate_name=gate_name,
            )
            out[label] = {
                "gate": report["manifest"]["configuration"].get("write_gate"),
                "formation": report["formation"],
                "retrieval_turn": report["retrieval"]["turn_level"],
                "retrieval_session": report["retrieval"]["session_level"],
                "abstention": report["abstention"],
                "errors": report["errors"][:3],
                "duration_seconds": report["duration_seconds"],
            }
        except Exception:
            out[label] = {"error": traceback.format_exc()}
        # Publish after each arm so a crash in v2 still preserves the v1 arm.
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
