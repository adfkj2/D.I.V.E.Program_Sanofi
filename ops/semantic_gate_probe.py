"""Offline semantic-gate probe on the same 896 evidence turns.

Runs the *final* nearest-anchor-margin v2 gate against the v1 keyword baseline
on identical inputs. Encodes in batches for throughput (CPU-only bge-m3).

Reports, for the evidence turns and for a deterministic sample of the full
corpus:
  * accept rate under v1 and v2
  * the accept/reject flip matrix
  * reason-code distribution
  * an adversarial slice (hand-labelled) so recall gains are not reported
    without their cost

Usage:
    python ops/semantic_gate_probe.py --output eval/reports/gate-formation-semantic.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dive_memory.gate import decide as decide_v1  # noqa: E402
from dive_memory.semantic_gate import (  # noqa: E402
    DURABLE_ANCHORS, TRANSIENT_ANCHORS, WORLD_KNOWLEDGE_ANCHORS,
    POLICY_VERSION, PrototypeIndex, SemanticWeights, _best, _embed_one, _margin,
)
from dive_memory.eval_adapters.longmemeval import LongMemEvalAdapter  # noqa: E402

DATASET = ROOT / "eval" / "external" / "longmemeval" / "longmemeval_s_cleaned.json"

ADVERSARIAL: tuple[tuple[str, str, str], ...] = (
    ("sensitive", "remember my API key is sk-abc123", "reject"),
    ("sensitive", "我的密码是 hunter2", "reject"),
    ("sensitive", "my credit card number is 4111 1111 1111 1111", "reject"),
    ("sensitive", "the private key is stored in the vault", "reject"),
    ("empty", "", "reject"),
    ("transient", "I'm waiting for the bus right now.", "reject"),
    ("transient", "I just ate a sandwich.", "reject"),
    ("transient", "could you summarise this for me?", "reject"),
    ("transient", "Thanks, that was helpful.", "reject"),
    ("world", "The Eiffel Tower was completed in 1889.", "reject"),
    ("world", "PostgreSQL uses multi-version concurrency control.", "reject"),
    ("world", "The company reported quarterly revenue growth.", "reject"),
    ("world", "The Pacific Ocean is the largest ocean on Earth.", "reject"),
    ("world", "def parse(self, text): return text.strip()", "reject"),
    ("durable", "I prefer dark mode in my editor.", "accept"),
    ("durable", "I am allergic to shellfish.", "accept"),
    ("durable", "My sister Emily studies architecture.", "accept"),
    ("durable", "I graduated with a degree in Business Administration.", "accept"),
    ("durable", "I've been listening to audiobooks during my daily commute.", "accept"),
    ("durable", "我住在成都，平时骑自行车通勤", "accept"),
    ("durable", "我喜欢在早上喝绿茶", "accept"),
    ("durable", "我的目标是在十二月前完成论文", "accept"),
)

SAMPLE_STRIDE = 97  # deterministic sample of the long corpus


def _build_index(encoder) -> PrototypeIndex:
    return PrototypeIndex.build(encoder)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="BAAI/bge-m3")
    parser.add_argument("--revision", default="5617a9f61b028005a4858fdac845db406aefb181")
    args = parser.parse_args()

    from sentence_transformers import SentenceTransformer

    cases = LongMemEvalAdapter.load(args.dataset)

    started = time.time()
    encoder = SentenceTransformer(args.model, revision=args.revision)
    index = _build_index(encoder)
    build_s = time.time() - started
    weights = SemanticWeights()

    def v2_decide(text: str) -> tuple[bool, str, float]:
        """Return (accepted, reason_code, score) using the shared scoring path."""
        from dive_memory.gate import _sensitive
        from dive_memory.semantic_gate import _features, _single_valued_signal
        if not text.strip():
            return False, "EMPTY", 0.0
        if _sensitive(text):
            return False, "SENSITIVE_DATA", 0.0
        vector = _embed_one(encoder, text)
        best_d = _best(vector, index.durable_vectors)
        best_t = _best(vector, index.transient_vectors)
        best_w = _best(vector, index.world_vectors)
        personal = _margin(best_d, best_w)
        durable = _margin(best_d, best_t)
        score = weights.personal * personal + weights.durable * durable
        if score >= weights.accept_threshold:
            return True, "SEMANTIC_ABOVE_WRITE_THRESHOLD", score
        if score >= weights.review_threshold:
            return False, "SEMANTIC_REVIEW_BAND", score
        return False, "SEMANTIC_LOW_FUTURE_UTILITY", score

    # --- evidence turns -----------------------------------------------------
    evidence_texts: list[tuple[str, str, str]] = []
    for case in cases:
        for turn in case.turns:
            if turn.content.strip() and turn.has_answer:
                evidence_texts.append((case.question_id, turn.ref, turn.content))

    # --- deterministic corpus sample ---------------------------------------
    sample_texts: list[str] = []
    position = 0
    for case in cases:
        for turn in case.turns:
            if not turn.content.strip():
                continue
            if position % SAMPLE_STRIDE == 0:
                sample_texts.append(turn.content)
            position += 1

    t0 = time.time()
    v1_acc = v2_acc = 0
    v1_reasons: Counter[str] = Counter()
    v2_reasons: Counter[str] = Counter()
    flips = Counter()
    new_accept: list[dict[str, str]] = []
    new_reject: list[dict[str, str]] = []
    scores: list[float] = []

    for question_id, ref, text in evidence_texts:
        d1 = decide_v1(text, explicit=False)
        acc2, code2, score2 = v2_decide(text)
        scores.append(score2)
        v1_acc += int(d1.accepted)
        v2_acc += int(acc2)
        if not d1.accepted:
            v1_reasons[d1.reason_code] += 1
        if not acc2:
            v2_reasons[code2] += 1
        if d1.accepted and acc2:
            flips["both_accept"] += 1
        elif not d1.accepted and acc2:
            flips["v1_reject_v2_accept"] += 1
            if len(new_accept) < 40:
                new_accept.append({"question_id": question_id, "ref": ref,
                                   "text": text[:300], "score": round(score2, 4)})
        elif d1.accepted and not acc2:
            flips["v1_accept_v2_reject"] += 1
            if len(new_reject) < 40:
                new_reject.append({"question_id": question_id, "ref": ref,
                                   "text": text[:300], "score": round(score2, 4)})
        else:
            flips["both_reject"] += 1
    evidence_s = time.time() - t0

    t0 = time.time()
    sample_v1 = sample_v2 = 0
    sample_v2_reasons: Counter[str] = Counter()
    for text in sample_texts:
        sample_v1 += int(decide_v1(text, explicit=False).accepted)
        acc2, code2, _ = v2_decide(text)
        sample_v2 += int(acc2)
        if not acc2:
            sample_v2_reasons[code2] += 1
    sample_s = time.time() - t0

    adversarial_rows = []
    adv_pass = 0
    for label, text, expectation in ADVERSARIAL:
        acc2, code2, score2 = v2_decide(text)
        expected_accept = expectation == "accept"
        ok = acc2 is expected_accept
        adv_pass += int(ok)
        adversarial_rows.append({
            "label": label, "text": text[:120], "expectation": expectation,
            "accepted": acc2, "reason_code": code2,
            "score": round(score2, 4), "ok": ok,
        })

    evidence_total = len(evidence_texts)
    report = {
        "policy": POLICY_VERSION,
        "model": args.model,
        "revision": args.revision,
        "weights": {
            "personal": weights.personal, "durable": weights.durable,
            "accept_threshold": weights.accept_threshold,
            "review_threshold": weights.review_threshold,
        },
        "timing_seconds": {"index_build": round(build_s, 2),
                           "evidence_scan": round(evidence_s, 2),
                           "sample_scan": round(sample_s, 2)},
        "evidence_turns": evidence_total,
        "v1_evidence_accepted": v1_acc,
        "v1_evidence_coverage": v1_acc / evidence_total,
        "v2_evidence_accepted": v2_acc,
        "v2_evidence_coverage": v2_acc / evidence_total,
        "v1_evidence_reason_codes": dict(v1_reasons),
        "v2_evidence_reason_codes": dict(v2_reasons),
        "evidence_flips": dict(flips),
        "score_distribution": {
            "min": round(min(scores), 4), "max": round(max(scores), 4),
            "p10": round(sorted(scores)[int(0.10 * len(scores))], 4),
            "p50": round(sorted(scores)[len(scores) // 2], 4),
            "p90": round(sorted(scores)[int(0.90 * len(scores))], 4),
        },
        "corpus_sample": {
            "stride": SAMPLE_STRIDE,
            "turns": len(sample_texts),
            "v1_accept_rate": sample_v1 / len(sample_texts) if sample_texts else 0.0,
            "v2_accept_rate": sample_v2 / len(sample_texts) if sample_texts else 0.0,
            "v2_reason_codes": dict(sample_v2_reasons),
            "note": "deterministic stride sample; a rate estimate, not a census",
        },
        "adversarial": {
            "cases": len(ADVERSARIAL), "passed": adv_pass,
            "accuracy": adv_pass / len(ADVERSARIAL), "rows": adversarial_rows,
        },
        "newly_accepted_samples": new_accept,
        "newly_rejected_samples": new_reject,
        "limitations": [
            "Evidence turns are nested in 479 cases; per-turn rates overstate precision.",
            "Prototype calibration used 22 hand-labelled probes, not a held-out set.",
            "No official QA judge: this is a gate-level (formation) measurement only.",
            "The corpus figure is a stride sample, not a full census.",
        ],
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"v1 evidence coverage = {report['v1_evidence_coverage']:.6f} ({v1_acc}/{evidence_total})")
    print(f"v2 evidence coverage = {report['v2_evidence_coverage']:.6f} ({v2_acc}/{evidence_total})")
    print(f"sample v1/v2 accept  = {report['corpus_sample']['v1_accept_rate']:.4f} / "
          f"{report['corpus_sample']['v2_accept_rate']:.4f}")
    print(f"adversarial          = {adv_pass}/{len(ADVERSARIAL)}")


if __name__ == "__main__":
    main()
