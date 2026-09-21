"""Measure the cost of the v2 gate per turn, and the benefit of batching.

The runner calls the gate once per turn, and each call encodes exactly one
string. On CPU that leaves the model's batch dimension idle. This probes the
ratio so the optimisation is justified by a number rather than by intuition.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("HF_HOME", str(ROOT / "eval" / "external" / "huggingface"))

OUT = ROOT / "eval" / "reports" / "gate-batch-probe.json"


def main() -> None:
    from dive_memory.semantic_gate import load_local_gate

    payload: dict[str, object] = {}
    gate = load_local_gate()
    encoder = gate.index.encoder

    texts = [
        "I prefer dark mode in my editor.",
        "I live in Berlin and commute by bicycle.",
        "The Eiffel Tower was completed in 1889.",
        "I just ate a sandwich for lunch.",
    ] * 16  # 64 strings, enough to amortise warm-up

    started = time.perf_counter()
    for text in texts:
        encoder.encode([text], normalize_embeddings=True)
    payload["per_string_seconds"] = (time.perf_counter() - started) / len(texts)
    payload["strings"] = len(texts)

    for batch in (8, 16, 32, 64):
        started = time.perf_counter()
        for start in range(0, len(texts), batch):
            encoder.encode(texts[start:start + batch], normalize_embeddings=True)
        elapsed = time.perf_counter() - started
        payload[f"batch_{batch}_seconds"] = elapsed
        payload[f"batch_{batch}_per_string_seconds"] = elapsed / len(texts)

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
