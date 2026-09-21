import json
from pathlib import Path
import re


ROOT = Path(__file__).parents[1]


def test_phase3_postgres_decision_and_runbook_are_present():
    expected = [
        ROOT / "docs" / "adr" / f"ADR-{number:03d}-{name}.md"
        for number, name in (
            (7, "candidate-extraction-and-write-gate"),
            (8, "memory-conflict-resolution"),
            (9, "embedding-model-selection"),
            (10, "hybrid-retrieval-and-fusion"),
            (11, "context-packing-and-reranking"),
            (12, "postgresql-pgvector"),
        )
    ]
    expected.extend([
        ROOT / "ops" / "postgres" / "README.md",
        ROOT / "docs" / "benchmark" / "postgres-exact-10k-report.md",
    ])
    assert all(path.is_file() for path in expected)


def test_postgres_10k_artifact_is_explicitly_nonsemantic_and_exact():
    path = ROOT / "eval" / "reports" / "postgres-exact-10k-latest.json"
    report = json.loads(path.read_text(encoding="utf-8"))

    assert report["schema_version"] == "dive-postgres-exact-benchmark-v1"
    assert report["backend"] == "postgresql-pgvector"
    assert report["search_mode"] == "exact-cosine-dense-only"
    assert report["memory_count"] == 10_000
    assert report["query_iterations"] == 20
    assert report["embedding"]["quality_claim"] is False
    assert report["trace_backend"] == "postgres-pgvector-exact-cosine"
    assert report["exact_query_top1_rate"] == 1.0
    assert report["retrieve_ms"]["p50"] <= report["retrieve_ms"]["p95"] <= report["retrieve_ms"]["p99"]
    assert report["applied_migrations"] == [
        f"{number:03d}_{name}.sql"
        for number, name in (
            (1, "initial"),
            (2, "outbox_claims"),
            (3, "tombstone_namespace"),
            (4, "candidate_decisions"),
            (5, "memory_transitions"),
            (6, "embedding_generations"),
            (7, "memory_resolution_keys"),
        )
    ]


def test_readme_relative_markdown_links_resolve():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = re.findall(r"\[[^]]+\]\(([^)]+)\)", readme)
    relative = [target for target in targets if "://" not in target and not target.startswith("#")]
    assert relative
    assert all((ROOT / target).exists() for target in relative)
