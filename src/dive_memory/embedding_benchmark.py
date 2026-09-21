from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gc
import json
import math
import os
from pathlib import Path
import platform
import re
from statistics import median
from time import perf_counter
from typing import Any, Iterable, Sequence

from .eval_manifest import build_run_manifest
from .eval_metrics import aggregate_ranking_rows
from .ids import stable_vector
from .lexical import cjk_bigrams


MODEL_SPECS: dict[str, dict[str, Any]] = {
    "bge-m3": {
        "repo_id": "BAAI/bge-m3",
        "revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "dimensions": 1024,
        "query_instruction": None,
        "trust_remote_code": False,
    },
    "multilingual-e5-large-instruct": {
        "repo_id": "intfloat/multilingual-e5-large-instruct",
        "revision": "274baa43b0e13e37fafa6428dbc7938e62e5c439",
        "dimensions": 1024,
        "query_instruction": "Given a user memory query, retrieve relevant passages that answer the query",
        "trust_remote_code": False,
    },
    "gte-multilingual-base": {
        "repo_id": "Alibaba-NLP/gte-multilingual-base",
        "revision": "9bbca17d9273fd0d03d5725c7a4b0f6b45142062",
        "dimensions": 768,
        "query_instruction": None,
        "trust_remote_code": True,
        "remote_code_repo": "Alibaba-NLP/new-impl",
        "remote_code_revision": "40ced75c3017eb27626c9d4ea981bde21a2662f4",
    },
}


def load_dataset(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "dive-embedding-memory-v1":
        raise ValueError("unsupported embedding dataset schema")
    documents = payload.get("documents")
    queries = payload.get("queries")
    if not isinstance(documents, list) or not isinstance(queries, list) or not documents or not queries:
        raise ValueError("embedding dataset requires non-empty documents and queries")
    document_ids = [row.get("id") for row in documents]
    query_ids = [row.get("id") for row in queries]
    if any(not isinstance(value, str) or not value for value in document_ids + query_ids):
        raise ValueError("document and query ids must be non-empty strings")
    if len(document_ids) != len(set(document_ids)) or len(query_ids) != len(set(query_ids)):
        raise ValueError("document and query ids must be unique")
    known = set(document_ids)
    categories = set()
    for row in documents:
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError("documents require non-empty text")
        categories.add(row.get("category"))
    for row in queries:
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError("queries require non-empty text")
        relevant = row.get("relevant_ids")
        if not isinstance(relevant, list) or not relevant or any(item not in known for item in relevant):
            raise ValueError("queries require known relevant_ids")
        categories.add(row.get("category"))
    expected = {
        "semantic_paraphrase", "preference_retrieval", "old_episodic_recall",
        "temporal_change", "conflicting_facts", "near_duplicate_memory",
        "entity_ambiguity", "long_tail_facts",
    }
    if set(categories) != expected:
        raise ValueError(f"dataset categories must be exactly {sorted(expected)!r}")
    return payload


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def _latency(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "samples": len(values),
        "p50": median(values) if values else 0.0,
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "mean": sum(values) / len(values) if values else 0.0,
    }


def _quality_report(
    documents: Sequence[dict[str, Any]],
    queries: Sequence[dict[str, Any]],
    scores: Sequence[Sequence[float]],
) -> dict[str, Any]:
    document_ids = [row["id"] for row in documents]
    ranking_rows: list[dict[str, Any]] = []
    per_query: list[dict[str, Any]] = []
    for query, query_scores in zip(queries, scores):
        order = sorted(range(len(document_ids)), key=lambda index: (-float(query_scores[index]), document_ids[index]))
        ranked = [document_ids[index] for index in order]
        ranking_rows.append({
            "retrieved": ranked,
            "relevance_grades": {item: 1.0 for item in query["relevant_ids"]},
            "query_type": query["category"],
        })
        per_query.append({
            "query_id": query["id"],
            "category": query["category"],
            "relevant_ids": query["relevant_ids"],
            "top10": [
                {"document_id": document_ids[index], "score": float(query_scores[index])}
                for index in order[:10]
            ],
        })
    metrics = aggregate_ranking_rows(ranking_rows, ks=(1, 5, 10))
    metrics["hit_rate_at_k"] = dict(metrics["recall_at_k"])
    metrics["temporal_retrieval_accuracy_at_1"] = metrics["slices"]["temporal_change"]["recall_at_k"]["1"]
    metrics["contradiction_retrieval_accuracy_at_1"] = metrics["slices"]["conflicting_facts"]["recall_at_k"]["1"]
    return {"metrics": metrics, "per_query": per_query}


def _dot_scores(query_vectors: Sequence[Sequence[float]], document_vectors: Sequence[Sequence[float]]) -> list[list[float]]:
    return [
        [sum(float(left) * float(right) for left, right in zip(query, document)) for document in document_vectors]
        for query in query_vectors
    ]


def _normalise(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(value) * float(value) for value in vector)) or 1.0
    return [float(value) / norm for value in vector]


def _run_hash_baseline(documents: Sequence[dict[str, Any]], queries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    dimensions = 96
    started = perf_counter()
    doc_vectors = [_normalise(stable_vector(row["text"], dimensions=dimensions)) for row in documents]
    document_seconds = perf_counter() - started
    latencies: list[float] = []
    query_vectors: list[list[float]] = []
    for row in queries:
        query_started = perf_counter()
        query_vectors.append(_normalise(stable_vector(row["text"], dimensions=dimensions)))
        latencies.append((perf_counter() - query_started) * 1000)
    result = _quality_report(documents, queries, _dot_scores(query_vectors, doc_vectors))
    return {
        "status": "COMPLETED",
        "provider": "deterministic-test",
        "model": "deterministic-sha256-v1",
        "revision": "v1",
        "semantic": False,
        "dimensions": dimensions,
        "model_load_seconds": 0.0,
        "embedding_throughput_per_second": len(documents) / document_seconds if document_seconds else 0.0,
        "query_embedding_latency_ms": _latency(latencies),
        "document_embedding_seconds": document_seconds,
        "embedding_storage_bytes_float32": len(documents) * dimensions * 4,
        "model_storage_bytes": 0,
        "cpu_rss_bytes_after_load": None,
        "cpu_rss_bytes_peak_observed": None,
        "gpu_peak_bytes": None,
        **result,
    }


def _tokens(text: str) -> list[str]:
    lowered = text.casefold()
    latin = re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", lowered)
    return latin + cjk_bigrams(lowered)


def _run_lexical_baseline(documents: Sequence[dict[str, Any]], queries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    tokenized = [_tokens(row["text"]) for row in documents]
    document_frequency = Counter(token for tokens in tokenized for token in set(tokens))
    average_length = sum(map(len, tokenized)) / len(tokenized)
    scores: list[list[float]] = []
    latencies: list[float] = []
    for query in queries:
        started = perf_counter()
        query_tokens = set(_tokens(query["text"]))
        row_scores: list[float] = []
        for tokens in tokenized:
            frequencies = Counter(tokens)
            score = 0.0
            for token in query_tokens:
                frequency = frequencies[token]
                if not frequency:
                    continue
                inverse = math.log(1.0 + (len(documents) - document_frequency[token] + 0.5) /
                                   (document_frequency[token] + 0.5))
                denominator = frequency + 1.5 * (1.0 - 0.75 + 0.75 * len(tokens) / (average_length or 1.0))
                score += inverse * frequency * 2.5 / denominator
            row_scores.append(score)
        latencies.append((perf_counter() - started) * 1000)
        scores.append(row_scores)
    result = _quality_report(documents, queries, scores)
    return {
        "status": "COMPLETED",
        "provider": "project-lexical-reference",
        "model": "bm25-word-cjk-bigram-v1",
        "revision": "v1",
        "semantic": False,
        "dimensions": None,
        "model_load_seconds": 0.0,
        "embedding_throughput_per_second": None,
        "query_embedding_latency_ms": _latency(latencies),
        "document_embedding_seconds": None,
        "embedding_storage_bytes_float32": 0,
        "model_storage_bytes": 0,
        "cpu_rss_bytes_after_load": None,
        "cpu_rss_bytes_peak_observed": None,
        "gpu_peak_bytes": None,
        **result,
    }


def _directory_size(path: str | Path) -> int:
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())


def _query_text(spec: dict[str, Any], text: str) -> str:
    instruction = spec["query_instruction"]
    return f"Instruct: {instruction}\nQuery: {text}" if instruction else text


def _run_sentence_transformer(
    key: str,
    documents: Sequence[dict[str, Any]],
    queries: Sequence[dict[str, Any]],
    *,
    device: str,
    batch_size: int,
) -> dict[str, Any]:
    import psutil  # type: ignore[import-not-found]
    import torch  # type: ignore[import-not-found]
    from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
    from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

    spec = MODEL_SPECS[key]
    process = psutil.Process()
    rss_before_load = process.memory_info().rss
    load_started = perf_counter()
    snapshot = snapshot_download(
        repo_id=spec["repo_id"],
        revision=spec["revision"],
        allow_patterns=[
            "*.json", "*.txt", "*.model", "*.py", "*.safetensors",
            "*.safetensors.index.json", "pytorch_model.bin", "*.pt",
        ],
        ignore_patterns=["onnx/**", "openvino/**", "imgs/**", "*.onnx", "*.onnx_data"],
    )
    model_kwargs = None
    if spec.get("remote_code_revision"):
        model_kwargs = {"code_revision": spec["remote_code_revision"]}
    model = SentenceTransformer(
        snapshot,
        device=device,
        trust_remote_code=spec["trust_remote_code"],
        model_kwargs=model_kwargs,
    )
    load_seconds = perf_counter() - load_started
    dimension_method = getattr(model, "get_embedding_dimension", None)
    if not callable(dimension_method):
        dimension_method = model.get_sentence_embedding_dimension
    dimensions = int(dimension_method())
    if dimensions != spec["dimensions"]:
        raise ValueError(f"{key} expected {spec['dimensions']} dimensions, got {dimensions}")
    if device.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats()
    rss_after_load = process.memory_info().rss

    query_texts = [_query_text(spec, row["text"]) for row in queries]
    document_texts = [row["text"] for row in documents]
    model.encode(query_texts[:2], batch_size=2, normalize_embeddings=True, show_progress_bar=False)

    document_started = perf_counter()
    document_vectors = model.encode(
        document_texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    document_seconds = perf_counter() - document_started
    rss_after_documents = process.memory_info().rss

    query_started = perf_counter()
    query_vectors = model.encode(
        query_texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    query_batch_seconds = perf_counter() - query_started
    query_latencies: list[float] = []
    for text in query_texts:
        started = perf_counter()
        model.encode([text], batch_size=1, normalize_embeddings=True,
                     show_progress_bar=False, convert_to_numpy=True)
        query_latencies.append((perf_counter() - started) * 1000)
    rss_peak_observed = max(rss_after_load, rss_after_documents, process.memory_info().rss)
    scores = query_vectors @ document_vectors.T
    quality = _quality_report(documents, queries, scores.tolist())
    result = {
        "status": "COMPLETED",
        "provider": "sentence-transformers",
        "model": spec["repo_id"],
        "revision": spec["revision"],
        "semantic": True,
        "query_instruction": spec["query_instruction"],
        "dimensions": dimensions,
        "device": device,
        "dtype": str(document_vectors.dtype),
        "model_load_seconds": load_seconds,
        "embedding_throughput_per_second": len(documents) / document_seconds if document_seconds else 0.0,
        "query_batch_throughput_per_second": len(queries) / query_batch_seconds if query_batch_seconds else 0.0,
        "query_embedding_latency_ms": _latency(query_latencies),
        "document_embedding_seconds": document_seconds,
        "embedding_storage_bytes_float32": len(documents) * dimensions * 4,
        "model_storage_bytes": _directory_size(snapshot),
        "cpu_rss_bytes_after_load": rss_after_load,
        "cpu_rss_bytes_peak_observed": rss_peak_observed,
        "cpu_rss_bytes_before_load": rss_before_load,
        "cpu_rss_bytes_peak_increment": max(0, rss_peak_observed - rss_before_load),
        "gpu_peak_bytes": int(torch.cuda.max_memory_allocated()) if device.startswith("cuda") else None,
        **quality,
    }
    del model, document_vectors, query_vectors, scores
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return result


def run_embedding_comparison(
    dataset_path: str | Path,
    *,
    models: Sequence[str],
    device: str,
    batch_size: int,
    repository_root: str | Path = ".",
) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    documents = dataset["documents"]
    queries = dataset["queries"]
    started = datetime.now(timezone.utc)
    results: dict[str, Any] = {
        "deterministic-sha256-v1": _run_hash_baseline(documents, queries),
        "bm25-word-cjk-bigram-v1": _run_lexical_baseline(documents, queries),
    }
    failures: list[dict[str, str]] = []
    for model in models:
        try:
            results[model] = _run_sentence_transformer(
                model, documents, queries, device=device, batch_size=batch_size,
            )
        except Exception as exc:
            results[model] = {
                "status": "FAILED",
                "model": MODEL_SPECS[model]["repo_id"],
                "revision": MODEL_SPECS[model]["revision"],
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            failures.append({"model": model, "error_type": type(exc).__name__, "error": str(exc)})
    finished = datetime.now(timezone.utc)
    configuration = {
        "models": list(models),
        "model_specs": {key: MODEL_SPECS[key] for key in models},
        "device": device,
        "batch_size": batch_size,
        "normalization": "L2",
        "similarity": "exact cosine via normalized dot product",
        "corpus_shared_across_models": True,
        "top_k": [1, 5, 10],
    }
    manifest = build_run_manifest(
        run_id=f"embedding-comparison-{started.strftime('%Y%m%dT%H%M%SZ')}",
        dataset_name="dive-embedding-memory-v1",
        dataset_version="1",
        dataset_path=dataset_path,
        configuration=configuration,
        repository_root=repository_root,
    ).to_dict()
    runtime: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "hf_endpoint": os.environ.get("HF_ENDPOINT"),
    }
    try:
        import psutil  # type: ignore[import-not-found]
        import sentence_transformers  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]
        import transformers  # type: ignore[import-not-found]

        runtime.update({
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "sentence_transformers": sentence_transformers.__version__,
            "torch_cuda_build": torch.version.cuda,
            "cuda_available": torch.cuda.is_available(),
            "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "logical_cpu_count": psutil.cpu_count(logical=True),
            "physical_cpu_count": psutil.cpu_count(logical=False),
            "system_memory_bytes": psutil.virtual_memory().total,
        })
    except ImportError:
        runtime["model_runtime"] = "not installed"
    return {
        "schema_version": "dive-embedding-comparison-v1",
        "status": "COMPLETED" if not failures else "PARTIAL",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "manifest": manifest,
        "dataset": {
            "documents": len(documents),
            "queries": len(queries),
            "categories": dict(Counter(row["category"] for row in queries)),
            "language_scope": dataset["language_scope"],
        },
        "runtime": runtime,
        "results": results,
        "failures": failures,
        "limitations": [
            "Project-owned controlled set: 32 queries and 48 documents; quality estimates have high uncertainty.",
            "Exact in-memory cosine ranking measures model retrieval quality, not PostgreSQL latency.",
            "Temporal/provenance words are present in documents; no separate metadata filter is applied.",
            "CPU/GPU numbers describe this process and model runtime only, not a production server.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare real embedding models on the D.I.V.E memory workload")
    parser.add_argument("dataset")
    parser.add_argument("--output", required=True)
    parser.add_argument("--models", nargs="+", choices=sorted(MODEL_SPECS), default=list(MODEL_SPECS))
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    report = run_embedding_comparison(
        args.dataset,
        models=args.models,
        device=args.device,
        batch_size=max(1, args.batch_size),
        repository_root=Path.cwd(),
    )
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
