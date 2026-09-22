import json

from dive_memory.answering import AnswerStatus, EvidenceCitation, GroundedAnswerDecision
from dive_memory.longmemeval_benchmark import run_longmemeval_retrieval


def test_longmemeval_retrieval_records_formation_failures_without_gold_leakage(tmp_path):
    dataset = [
        {
            "question_id": "known",
            "question": "What does the user prefer?",
            "answer": "tea",
            "question_type": "single-session-user",
            "question_date": "2025/01/02 (Thu) 12:00",
            "haystack_session_ids": ["s1"],
            "haystack_dates": ["2025/01/01 (Wed) 12:00"],
            "haystack_sessions": [[
                {"role": "user", "content": "I prefer tea.", "has_answer": True},
                {"role": "assistant", "content": "Noted."},
            ]],
            "answer_session_ids": ["s1"],
        },
        {
            "question_id": "missing_abs",
            "question": "What is the user's passport number?",
            "answer": "The history does not say.",
            "question_type": "single-session-user",
            "question_date": "2025/01/02 (Thu) 12:00",
            "haystack_session_ids": ["s2"],
            "haystack_dates": ["2025/01/01 (Wed) 12:00"],
            "haystack_sessions": [[{"role": "user", "content": "We discussed tea."}]],
            "answer_session_ids": ["s2"],
        },
    ]
    path = tmp_path / "official.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")

    report = run_longmemeval_retrieval(path, repository_root=tmp_path)

    assert report["status"] == "PARTIAL"
    assert report["official_scope"]["retrieval_stage"] == "COMPLETED"
    assert report["official_scope"]["official_qa_judge"] == "NOT_COMPLETED"
    assert report["retrieval"]["answerable_cases"] == 1
    assert report["retrieval"]["session_level"]["ndcg_at_10"] <= 1.0
    assert report["formation"]["gold_evidence_turns"] == 1
    assert report["rows"][0]["question_id"] == "known"
    assert "answer" not in report["rows"][0]
    assert report["rows"][0]["predicted_abstain"] is None
    assert report["abstention"]["status"] == "NOT_EVALUATED"
    assert report["abstention"]["accuracy"] is None


def test_longmemeval_reader_receives_no_gold_and_reports_real_abstention_metrics(tmp_path):
    dataset = [
        {
            "question_id": "known",
            "question": "What does the user prefer?",
            "answer": "tea",
            "question_type": "single-session-user",
            "question_date": "2025/01/02 (Thu) 12:00",
            "haystack_session_ids": ["s1"],
            "haystack_dates": ["2025/01/01 (Wed) 12:00"],
            "haystack_sessions": [[
                {"role": "user", "content": "I prefer tea.", "has_answer": True},
            ]],
            "answer_session_ids": ["s1"],
        },
        {
            "question_id": "negative_abs",
            "question": "What is the user's hamster called?",
            "answer": "The history does not say.",
            "question_type": "single-session-user",
            "question_date": "2025/01/02 (Thu) 12:00",
            "haystack_session_ids": ["s2"],
            "haystack_dates": ["2025/01/01 (Wed) 12:00"],
            "haystack_sessions": [[{
                "role": "user",
                "content": "My cat's name is Luna and I always buy cat food.",
            }]],
            "answer_session_ids": ["s2"],
        },
    ]
    path = tmp_path / "official.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")

    class SpyReader:
        policy_version = "spy-reader-v1"

        def __init__(self):
            self.calls = []

        def answer(self, question, items, *, as_of=None):
            self.calls.append({"question": question, "items": items, "as_of": as_of})
            if "hamster" in question:
                return GroundedAnswerDecision(
                    AnswerStatus.ABSTAIN, None, "ENTITY_NOT_SUPPORTED",
                    missing_requirements=("hamster name",), policy_version=self.policy_version,
                )
            item = items[0]
            return GroundedAnswerDecision(
                AnswerStatus.ANSWER, "tea", "SUPPORTED",
                (EvidenceCitation(item.memory.id, item.memory.content, tuple(item.source_refs)),),
                policy_version=self.policy_version,
            )

    reader = SpyReader()
    report = run_longmemeval_retrieval(path, repository_root=tmp_path, reader=reader)

    assert len(reader.calls) == 2
    assert set(reader.calls[0]) == {"question", "items", "as_of"}
    assert report["abstention"]["status"] == "EVALUATED"
    assert report["abstention"]["precision"] == 1.0
    assert report["abstention"]["recall"] == 1.0
    assert report["abstention"]["false_abstention_rate"] == 0.0
    assert report["official_scope"]["official_qa_judge"] == "NOT_COMPLETED"
