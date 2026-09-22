"""Run only the v2 arm of the smoke test and persist any failure trace."""
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
OUT = ROOT / "eval" / "reports" / "smoke-v2-only.json"


def main() -> None:
    payload: dict[str, object] = {}
    try:
        from dive_memory.semantic_gate import POLICY_VERSION, load_local_gate
        from dive_memory.longmemeval_benchmark import run_longmemeval_retrieval

        gate = load_local_gate(cache_size=1024, encode_batch_size=8, max_sequence_length=512)
        payload["index_built"] = True
        report = run_longmemeval_retrieval(
            DATASET, max_cases=MAX_CASES, limit=10,
            repository_root=ROOT, gate=gate, gate_name=POLICY_VERSION,
        )
        payload["formation"] = report["formation"]
        payload["retrieval_turn"] = report["retrieval"]["turn_level"]
        payload["retrieval_session"] = report["retrieval"]["session_level"]
        payload["errors"] = report["errors"][:3]
        payload["duration_seconds"] = report["duration_seconds"]
    except BaseException:
        payload["traceback"] = traceback.format_exc()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
