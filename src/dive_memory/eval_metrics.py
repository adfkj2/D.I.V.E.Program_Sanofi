from __future__ import annotations

from math import log2
from typing import Any, Iterable, Mapping, Sequence


def _validated_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("k must be a positive integer")
    return k


def _unique_relevant_hits(retrieved: Sequence[str], relevant: set[str], k: int) -> int:
    seen: set[str] = set()
    hits = 0
    for item_id in retrieved[:k]:
        if item_id in relevant and item_id not in seen:
            seen.add(item_id)
            hits += 1
    return hits


def precision_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Return P@k with a fixed ``k`` denominator.

    Duplicate results occupy rank positions but receive relevance credit once.
    This makes duplicate-heavy retrieval visibly worse rather than silently
    collapsing its output before evaluation.
    """

    k = _validated_k(k)
    return _unique_relevant_hits(retrieved, set(relevant), k) / k


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    k = _validated_k(k)
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    return _unique_relevant_hits(retrieved, relevant_set, k) / len(relevant_set)


def reciprocal_rank(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    for rank, item_id in enumerate(retrieved, start=1):
        if item_id in relevant_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevance_grades: Mapping[str, float], k: int) -> float:
    k = _validated_k(k)
    if not relevance_grades:
        return 0.0

    seen: set[str] = set()
    dcg = 0.0
    for rank, item_id in enumerate(retrieved[:k], start=1):
        grade = 0.0 if item_id in seen else float(relevance_grades.get(item_id, 0.0))
        seen.add(item_id)
        if grade > 0.0:
            dcg += (2.0**grade - 1.0) / log2(rank + 1.0)

    ideal = sorted((float(value) for value in relevance_grades.values() if value > 0.0), reverse=True)
    idcg = sum((2.0**grade - 1.0) / log2(rank + 1.0)
               for rank, grade in enumerate(ideal[:k], start=1))
    return dcg / idcg if idcg else 0.0


def classification_metrics(
    gold: Sequence[str], predicted: Sequence[str], labels: Sequence[str]
) -> dict[str, Any]:
    if len(gold) != len(predicted):
        raise ValueError("gold and predicted must have equal lengths")
    if len(set(labels)) != len(labels):
        raise ValueError("labels must be unique")
    label_set = set(labels)
    unknown = (set(gold) | set(predicted)) - label_set
    if unknown:
        raise ValueError(f"unknown labels: {sorted(unknown)!r}")

    per_label: dict[str, dict[str, int | float]] = {}
    for label in labels:
        tp = sum(actual == label and guess == label for actual, guess in zip(gold, predicted))
        support = sum(actual == label for actual in gold)
        predicted_count = sum(guess == label for guess in predicted)
        precision = tp / predicted_count if predicted_count else 0.0
        recall = tp / support if support else 0.0
        f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_label[label] = {
            "support": support,
            "predicted": predicted_count,
            "true_positive": tp,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

    count = len(gold)
    return {
        "cases": count,
        "accuracy": sum(actual == guess for actual, guess in zip(gold, predicted)) / count if count else 0.0,
        "macro_precision": sum(float(row["precision"]) for row in per_label.values()) / len(labels) if labels else 0.0,
        "macro_recall": sum(float(row["recall"]) for row in per_label.values()) / len(labels) if labels else 0.0,
        "macro_f1": sum(float(row["f1"]) for row in per_label.values()) / len(labels) if labels else 0.0,
        "per_label": per_label,
    }


def _aggregate(rows: Sequence[Mapping[str, Any]], ks: tuple[int, ...]) -> dict[str, Any]:
    evaluable = [row for row in rows if row["relevance_grades"]]
    denominator = len(evaluable)

    def average(values: Iterable[float]) -> float:
        return sum(values) / denominator if denominator else 0.0

    precision = {
        str(k): average(precision_at_k(row["retrieved"], row["relevance_grades"], k)
                        for row in evaluable)
        for k in ks
    }
    recall = {
        str(k): average(recall_at_k(row["retrieved"], row["relevance_grades"], k)
                        for row in evaluable)
        for k in ks
    }
    ndcg = {
        str(k): average(ndcg_at_k(row["retrieved"], row["relevance_grades"], k)
                        for row in evaluable)
        for k in ks
    }
    return {
        "cases": len(rows),
        "evaluable_cases": denominator,
        "precision_at_k": precision,
        "recall_at_k": recall,
        "mrr": average(reciprocal_rank(row["retrieved"], row["relevance_grades"])
                       for row in evaluable),
        "ndcg_at_k": ndcg,
    }


def aggregate_ranking_rows(
    rows: Sequence[Mapping[str, Any]], *, ks: Sequence[int] = (1, 5, 10)
) -> dict[str, Any]:
    normalized_ks = tuple(ks)
    if not normalized_ks:
        raise ValueError("ks must not be empty")
    if len(set(normalized_ks)) != len(normalized_ks):
        raise ValueError("ks must be unique")
    for k in normalized_ks:
        _validated_k(k)

    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        if "retrieved" not in row or "relevance_grades" not in row:
            raise ValueError("each row requires retrieved and relevance_grades")
        retrieved = row["retrieved"]
        grades = row["relevance_grades"]
        if not isinstance(retrieved, (list, tuple)) or not isinstance(grades, Mapping):
            raise ValueError("retrieved must be a sequence and relevance_grades a mapping")
        normalized_rows.append({
            "retrieved": list(retrieved),
            "relevance_grades": dict(grades),
            "query_type": str(row.get("query_type", "factual")),
        })

    report = _aggregate(normalized_rows, normalized_ks)
    slice_names = sorted({row["query_type"] for row in normalized_rows})
    report["slices"] = {
        name: _aggregate([row for row in normalized_rows if row["query_type"] == name], normalized_ks)
        for name in slice_names
    }
    return report
