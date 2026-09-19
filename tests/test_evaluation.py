from dive_memory.evaluation import EvalCase, run_cases
from dive_memory.service import MemoryService
from dive_memory.benchmark import run_load_benchmark


def test_eval_harness_reports_recall_and_abstention():
    service = MemoryService()
    written = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    report = run_cases(service, [
        EvalCase("known", "u1", "绿茶", "绿茶", written["memory_ids"]),
        EvalCase("unknown", "u1", "火星", None, [], should_abstain=True),
    ])
    assert report["cases"] == 2
    assert report["recall"] == 1.0
    assert report["abstention_accuracy"] == 1.0
    assert report["false_memory_rate"] == 0.0
    assert report["provenance_coverage"] == 1.0
    assert report["latency_ms"]["p95"] >= 0


def test_eval_counts_an_answer_to_an_unknown_question_as_false_memory():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    report = run_cases(service, [
        EvalCase("must-abstain", "u1", "我喜欢什么", None, [], should_abstain=True),
    ])
    assert report["false_memory_rate"] == 1.0


def test_local_load_benchmark_reports_quality_and_latency():
    report = run_load_benchmark(memory_count=20, query_iterations=3)
    assert report["storage"] == "sqlite-development"
    assert report["memory_count"] == 20
    assert report["retrieve_ms"]["p95"] >= 0
    assert report["quality"]["false_memory_rate"] == 0.0


def test_local_load_benchmark_clamps_zero_and_negative_sizes():
    report = run_load_benchmark(memory_count=0, query_iterations=-2)
    assert report["memory_count"] == 1
    assert report["query_iterations"] == 1
    assert report["quality"]["recall"] == 1.0
