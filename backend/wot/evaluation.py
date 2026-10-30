"""Offline synthetic PFE framework. Never imported by the application runtime.

References are available only to the scorer. A provider receives a question,
frozen generation settings and authorized training context, never a Case object.
The bundled provider is deterministic and makes no network requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol

ARMS = ("B0", "B1", "B2", "D")
BASELINES = ARMS[:3]
ROOT = Path(__file__).resolve().parents[2]
REAL_ORIGINS = {"REAL_USER_MESSAGE", "USER_SELF_REPORT", "USER_CORRECTION"}


def fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalized(text: str) -> str:
    return "".join(re.findall(r"\w+", text.casefold()))


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Evaluation timestamps require an explicit timezone")
    return parsed


def validate_dataset(dataset: dict[str, Any]) -> None:
    """Freeze within-user training/development/holdout by session and time.

    This is a within-person personalization design, not a claim that people in
    train and holdout are disjoint. Never use its results for population inference.
    """
    if dataset.get("classification") != "PUBLIC_SYNTHETIC":
        raise ValueError("This runner accepts public synthetic data only")
    records = dataset.get("records", [])
    if not records:
        raise ValueError("Dataset must contain records")
    seen: set[str] = set()
    sessions: dict[tuple[str, str], str] = {}
    times: dict[str, dict[str, list[datetime]]] = defaultdict(lambda: defaultdict(list))
    questions: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for record in records:
        required = {"id", "kind", "owner_id", "session_id", "split", "occurred_at"}
        if not required <= record.keys():
            raise ValueError("Dataset record is missing lineage fields")
        if record["id"] in seen:
            raise ValueError("Duplicate record ID")
        seen.add(record["id"])
        split = record["split"]
        if split not in {"train", "dev", "holdout"}:
            raise ValueError("Unknown dataset split")
        key = record["owner_id"], record["session_id"]
        if key in sessions and sessions[key] != split:
            raise ValueError("One conversation session crosses dataset splits")
        sessions[key] = split
        times[record["owner_id"]][split].append(timestamp(record["occurred_at"]))
        if record["kind"] == "memory":
            if split != "train" or record.get("source_origin") not in REAL_ORIGINS:
                raise ValueError("Only real-source training records may enter contexts")
            if record.get("subject_id") != record["owner_id"]:
                raise ValueError("Training evidence must belong to its user")
            if record.get("consent_purpose") != "PFE_TRAINING":
                raise ValueError("Training evidence needs an explicit PFE grant")
            if not {"content", "domain", "choice", "reasons"} <= record.keys():
                raise ValueError("Training memory is incomplete")
        elif record["kind"] == "case":
            if split not in {"dev", "holdout"}:
                raise ValueError("Evaluation questions must be separated from training")
            if not {"question", "options", "reference", "domain"} <= record.keys():
                raise ValueError("Evaluation case is incomplete")
            reference = record["reference"]
            if reference.get("choice") not in [*record["options"], "ABSTAIN"]:
                raise ValueError("Reference choice is outside the frozen options")
            if not isinstance(reference.get("reasons"), list):
                raise ValueError("Reference reason rubric must be a list")
            question = normalized(record["question"])
            for prior_split, prior_question in questions[record["owner_id"]]:
                if prior_split != split and SequenceMatcher(None, question, prior_question).ratio() >= 0.9:
                    raise ValueError("Near-duplicate question crosses development/holdout")
            questions[record["owner_id"]].append((split, question))
        else:
            raise ValueError("Unknown record kind")
    for owner, splits in times.items():
        if not splits.get("train") or not splits.get("holdout"):
            raise ValueError(f"User {owner} needs training and holdout sessions")
        ordered = [splits[s] for s in ("train", "dev", "holdout") if splits.get(s)]
        if any(max(a) >= min(b) for a, b in zip(ordered, ordered[1:])):
            raise ValueError("Training/development/holdout time windows overlap")


@dataclass(frozen=True)
class GenerationRequest:
    arm: str
    case_id: str
    owner_id: str
    question: str
    options: tuple[str, ...]
    context_json: str
    context_refs: tuple[str, ...]
    provider: str
    model: str
    temperature: float
    max_output_tokens: int
    max_context_chars: int
    prompt_version: str


@dataclass(frozen=True)
class Prediction:
    choice: str
    reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()


class EvaluationProvider(Protocol):
    def generate(self, request: GenerationRequest) -> Prediction | None: ...


class SyntheticMockProvider:
    """A transparent choice-from-context fixture, not a language model.

    All arms use the exact same generator: choose the latest supplied evidence
    whose choice is an allowed answer. Different results must come from the
    supplied context, never an arm name or reference answer.
    """

    def generate(self, request: GenerationRequest) -> Prediction:
        entries = json.loads(request.context_json)["entries"]
        candidates = [entry for entry in entries if entry["choice"] in request.options]
        if not candidates:
            return Prediction("ABSTAIN", ())
        chosen = max(candidates, key=lambda entry: (entry["occurred_at"], entry["id"]))
        return Prediction(chosen["choice"], tuple(chosen["reasons"]), (chosen["id"],))


def _terms(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def build_request(
    arm: str, case: dict[str, Any], dataset: dict[str, Any], protocol: dict[str, Any]
) -> GenerationRequest:
    if arm not in ARMS:
        raise ValueError("Unknown evaluation arm")
    # Only these public case fields are passed on. The reference stays here.
    memories = [
        row for row in dataset["records"]
        if row["kind"] == "memory" and row["split"] == "train"
        and row["owner_id"] == case["owner_id"]
        and row.get("subject_id") == case["owner_id"]
        and row.get("source_origin") in REAL_ORIGINS
        and row.get("consent_purpose") == "PFE_TRAINING"
        and timestamp(row["occurred_at"]) < timestamp(case["occurred_at"])
    ]
    memories.sort(key=lambda row: (row["occurred_at"], row["id"]))
    if arm == "B0":
        selected = []
    elif arm == "B1":
        # Frozen static biography: the first training self-description.
        selected = memories[:1]
    elif arm == "B2":
        # Ordinary lexical RAG, with no structured domain filtering.
        query_terms = _terms(case["question"])
        selected = sorted(
            memories,
            key=lambda row: (-len(query_terms & _terms(row["content"])), row["id"]),
        )[: protocol["retrieval"]["rag_top_k"]]
    else:
        # Structured persona fixture: domain-filtered real evidence with lineage.
        # This is NOT a measured implementation of the production DIVE engine.
        selected = [row for row in memories if row["domain"] == case["domain"]]
    fields = ("id", "content", "choice", "reasons", "occurred_at", "domain", "source_origin")
    entries: list[dict[str, Any]] = []
    limit = protocol["generation"]["max_context_chars"]
    # Most recent evidence is retained first when the common budget is reached.
    for row in sorted(selected, key=lambda row: (row["occurred_at"], row["id"]), reverse=True):
        entry = {field: row[field] for field in fields}
        candidate = json.dumps({"entries": [*entries, entry]}, ensure_ascii=False, sort_keys=True)
        if len(candidate) <= limit:
            entries.append(entry)
    config = protocol["generation"]
    return GenerationRequest(
        arm, case["id"], case["owner_id"], case["question"], tuple(case["options"]),
        json.dumps({"entries": entries}, ensure_ascii=False, sort_keys=True),
        tuple(entry["id"] for entry in entries), config["provider"], config["model"],
        config["temperature"], config["max_output_tokens"], limit, config["prompt_version"],
    )


def score_prediction(prediction: Prediction, reference: dict[str, Any]) -> dict[str, float | None]:
    choice = float(prediction.choice == reference["choice"])
    expected = reference["reasons"]
    supplied = " ".join(prediction.reasons).casefold()
    reason = sum(phrase.casefold() in supplied for phrase in expected) / len(expected) if expected else None
    return {
        "choice_match": choice,
        "core_reason_coverage": reason,
        "appropriate_abstention": choice if reference["choice"] == "ABSTAIN" else None,
        "style_human_rating": None,
        "interpersonal_human_rating": None,
        "primary_score": (choice + reason) / 2 if reason is not None else choice,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def clustered_mean(rows: list[dict[str, Any]], value_key: str) -> float:
    by_user: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        by_user[row["owner_id"]][row["session_id"]].append(row[value_key])
    return _mean([_mean([_mean(v) for v in sessions.values()]) for sessions in by_user.values()])


def cluster_bootstrap(
    rows: list[dict[str, Any]], *, seed: int, iterations: int
) -> dict[str, Any]:
    """Hierarchical paired bootstrap: users, then their conversation sessions.

    A session retains all its items; items are never sampled independently.
    Equal user and session weighting is frozen in the protocol.
    """
    if not rows:
        return {"estimate": None, "ci95": None, "users": 0, "sessions": 0, "iterations": 0}
    groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        groups[row["owner_id"]][row["session_id"]].append(row["difference"])
    user_ids = sorted(groups)
    rng = random.Random(seed)
    draws = []
    for _ in range(iterations):
        user_means = []
        for user in rng.choices(user_ids, k=len(user_ids)):
            session_ids = sorted(groups[user])
            sampled = rng.choices(session_ids, k=len(session_ids))
            user_means.append(_mean([_mean(groups[user][session]) for session in sampled]))
        draws.append(_mean(user_means))
    draws.sort()
    ci = [draws[math.floor((iterations - 1) * q)] for q in (0.025, 0.975)] if len(user_ids) >= 2 else None
    return {
        "estimate": clustered_mean(rows, "difference"), "ci95": ci,
        "users": len(user_ids), "sessions": sum(len(sessions) for sessions in groups.values()),
        "iterations": iterations, "seed": seed,
        "unit": "hierarchical user then session; all items in a session retained",
        "interpretation": "synthetic pipeline check only; no population or persona-fidelity inference",
    }


def run_benchmark(
    dataset: dict[str, Any], protocol: dict[str, Any], provider: EvaluationProvider | None = None
) -> dict[str, Any]:
    validate_dataset(dataset)
    if protocol.get("classification") != "SYNTHETIC_FRAMEWORK_VALIDATION":
        raise ValueError("Protocol must explicitly identify synthetic framework validation")
    if protocol.get("baselines") != list(ARMS):
        raise ValueError("Protocol must freeze all four arms")
    if protocol["generation"]["provider"] != "synthetic_mock":
        raise ValueError("This runner cannot call external model providers")
    if protocol["generation"]["max_context_chars"] < 100 or protocol["generation"]["max_output_tokens"] < 1:
        raise ValueError("Generation budgets must be positive")
    if protocol["bootstrap"]["iterations"] < 2 or protocol["retrieval"]["rag_top_k"] < 1:
        raise ValueError("Bootstrap/retrieval configuration is invalid")
    provider = provider or SyntheticMockProvider()
    frozen_dataset_hash = fingerprint(dataset)
    frozen_protocol_hash = fingerprint(protocol)
    cases = sorted(
        [row for row in dataset["records"] if row["kind"] == "case" and row["split"] == "holdout"],
        key=lambda row: row["id"],
    )
    order = [(case, arm) for case in cases for arm in ARMS]
    random.Random(protocol["seed"]).shuffle(order)
    ledger: list[dict[str, Any]] = []
    for index, (case, arm) in enumerate(order):
        request = build_request(arm, case, dataset, protocol)
        item: dict[str, Any] = {
            "call_index": index, "case_id": case["id"], "owner_id": case["owner_id"],
            "session_id": case["session_id"], "arm": arm,
            "request_hash": fingerprint(asdict(request)), "context_refs": list(request.context_refs),
            "context_characters": len(request.context_json), "provider": request.provider,
            "model": request.model, "cost_cny": 0.0,
            "usage_source": "mock fixture; tokens not measured and no network request",
        }
        try:
            prediction = provider.generate(request)
            if prediction is None:
                item.update(status="MISSING", scores=None, prediction=None)
            elif not isinstance(prediction, Prediction):
                raise ValueError("Provider did not return a typed prediction")
            elif prediction.choice not in (*request.options, "ABSTAIN"):
                raise ValueError("Provider returned an answer outside the frozen options")
            elif not set(prediction.evidence_refs) <= set(request.context_refs):
                raise ValueError("Provider fabricated or accessed a private evidence reference")
            else:
                item.update(status="COMPLETED", prediction=asdict(prediction), scores=score_prediction(prediction, case["reference"]))
        except Exception as error:
            # Do not write provider exception messages or raw prompts to the ledger.
            item.update(status="FAILED", error_type=type(error).__name__, scores=None, prediction=None)
        ledger.append(item)
    by_case: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in ledger:
        by_case[item["case_id"]][item["arm"]] = item
    complete_ids = [case["id"] for case in cases if all(by_case[case["id"]][arm]["status"] == "COMPLETED" for arm in ARMS)]
    arm_summary = {}
    for arm in ARMS:
        rows = [item for item in ledger if item["arm"] == arm]
        valid = [dict(item, primary=item["scores"]["primary_score"]) for item in rows if item["status"] == "COMPLETED"]
        arm_summary[arm] = {
            "expected": len(cases), "completed": len(valid),
            "missing": sum(row["status"] == "MISSING" for row in rows),
            "failed": sum(row["status"] == "FAILED" for row in rows),
            "observed_primary_mean": clustered_mean(valid, "primary") if valid else None,
        }
    matched: dict[str, float] = {}
    for arm in ARMS:
        rows = [dict(by_case[case_id][arm], primary=by_case[case_id][arm]["scores"]["primary_score"]) for case_id in complete_ids]
        if rows:
            matched[arm] = clustered_mean(rows, "primary")
    strongest = max(BASELINES, key=lambda arm: matched[arm]) if matched else None
    differences = []
    for case in cases:
        if case["id"] not in complete_ids:
            continue
        arms = by_case[case["id"]]
        differences.append({
            "case_id": case["id"], "owner_id": case["owner_id"], "session_id": case["session_id"],
            "difference": arms["D"]["scores"]["primary_score"] - arms[strongest]["scores"]["primary_score"],
        })
    if fingerprint(dataset) != frozen_dataset_hash or fingerprint(protocol) != frozen_protocol_hash:
        raise ValueError("Frozen evaluation inputs changed during the run")
    return {
        "classification": "SYNTHETIC_FRAMEWORK_VALIDATION",
        "persona_fidelity_validated": False, "domain_tested": False,
        "dataset_hash": frozen_dataset_hash, "protocol_hash": frozen_protocol_hash,
        "generation_hash": fingerprint(protocol["generation"]), "seed": protocol["seed"],
        "model": protocol["generation"], "arms": arm_summary,
        "paired": {
            "expected_cases": len(cases), "complete_four_arm_cases": len(complete_ids),
            "excluded_case_ids": [case["id"] for case in cases if case["id"] not in complete_ids],
            "matched_arm_means": matched, "strongest_baseline": strongest,
            "baseline_selection": "highest aggregate primary score on the complete four-arm cohort; ties B0, B1, B2",
            "differences": differences,
            "bootstrap": cluster_bootstrap(differences, seed=protocol["seed"], iterations=protocol["bootstrap"]["iterations"]),
        },
        "cost": {"provider_calls": len(ledger), "network_calls": 0, "actual_cny": 0.0},
        "ledger": ledger,
        "limitations": [
            "Public synthetic fixtures and a deterministic mock cannot establish persona fidelity or growth.",
            "D is a structured-context fixture, not a measured production DIVE retrieval experiment.",
            "Reason matching is a transparent phrase rubric, not an independent human judgment.",
            "Style/interpersonal ratings are unmeasured; no combined personality-accuracy percentage is produced.",
            "Missing/failed outputs are excluded from paired estimates and remain visible in denominators.",
            "No private reference files are mounted or loaded by this runner or the application.",
            "Persona-swap, multiround continuation and blinded human ratings remain future protocol extensions.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline public-synthetic PFE framework (zero API cost)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    # Fixed public paths: arbitrary --dataset/--private paths are intentionally absent.
    dataset = json.loads((ROOT / "eval/public_synthetic/sample.json").read_text(encoding="utf-8"))
    protocol = json.loads((ROOT / "eval/protocols/eval_protocol_v0.1.json").read_text(encoding="utf-8"))
    report = run_benchmark(dataset, protocol)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Synthetic framework validation: {report['paired']['complete_four_arm_cases']}/{report['paired']['expected_cases']} paired cases; external calls 0; cost CNY 0.00")
    print("Persona fidelity: unvalidated. No DOMAIN_TESTED status can be awarded.")
    return 0 if all(row["status"] == "COMPLETED" for row in report["ledger"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
