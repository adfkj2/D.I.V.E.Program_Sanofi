from dive_memory.evaluation import EvalCase, run_cases
from dive_memory.service import MemoryService


def test_eval_harness_reports_recall_and_abstention():
    service = MemoryService()
    written = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    report = run_cases(service, [
        EvalCase("known", "u1", "绿茶", "绿茶", written["memory_ids"]),
        EvalCase("unknown", "u1", "火星", None, [], should_abstain=True),
    ])
    assert report["cases"] == 2
    assert report["recall"] == 0.5
    assert report["abstention_accuracy"] == 1.0
