import json

import pytest

from dive_memory.eval_adapters.longmemeval import (
    LongMemEvalAdapter,
    LongMemEvalSchemaError,
)
from dive_memory.eval_datasets import load_internal_evolution_suite


def _sample():
    return [
        {
            "question_id": "q-known",
            "question": "Where does the user live?",
            "answer": "Shanghai",
            "question_type": "knowledge-update",
            "question_date": "2025-01-03T00:00:00+00:00",
            "haystack_session_ids": ["s1", "s2"],
            "haystack_dates": ["2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00"],
            "haystack_sessions": [
                [{"role": "user", "content": "I live in Chengdu."}],
                [
                    {"role": "user", "content": "I moved to Shanghai.", "has_answer": True},
                    {"role": "assistant", "content": "Got it."},
                ],
            ],
            "answer_session_ids": ["s2"],
        },
        {
            "question_id": "q-abstain",
            "question": "What is the user's passport number?",
            "answer": "abstain",
            "question_type": "abstention",
            "question_date": "2025-01-03T00:00:00+00:00",
            "haystack_session_ids": ["s3"],
            "haystack_dates": ["2025-01-01T00:00:00+00:00"],
            "haystack_sessions": [[{"role": "user", "content": "I enjoy tea."}]],
            "answer_session_ids": [],
        },
    ]


def test_longmemeval_adapter_separates_system_visible_data_from_gold(tmp_path):
    path = tmp_path / "longmemeval.json"
    path.write_text(json.dumps(_sample()), encoding="utf-8")

    cases = LongMemEvalAdapter.load(path)
    known = cases[0]

    assert known.evidence_refs == ("s2:0",)
    assert known.evidence_session_ids == ("s2",)
    assert list(known.system_input()) == [
        {
            "namespace": "longmemeval:q-known",
            "session_id": "s1",
            "turn_id": "s1:0",
            "role": "user",
            "content": "I live in Chengdu.",
            "occurred_at": "2025-01-01T00:00:00+00:00",
        },
        {
            "namespace": "longmemeval:q-known",
            "session_id": "s2",
            "turn_id": "s2:0",
            "role": "user",
            "content": "I moved to Shanghai.",
            "occurred_at": "2025-01-02T00:00:00+00:00",
        },
        {
            "namespace": "longmemeval:q-known",
            "session_id": "s2",
            "turn_id": "s2:1",
            "role": "assistant",
            "content": "Got it.",
            "occurred_at": "2025-01-02T00:00:00+00:00",
        },
    ]
    # Gold answer text, answer session ids, and has_answer never enter writer input.
    assert all(set(row) == {"namespace", "session_id", "turn_id", "role", "content", "occurred_at"}
               for row in known.system_input())


def test_longmemeval_adapter_builds_retrieval_cases_and_excludes_abstention_gold(tmp_path):
    path = tmp_path / "longmemeval.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in _sample()), encoding="utf-8")
    known, abstain = LongMemEvalAdapter.load(path)

    retrieval = known.to_eval_case({"s2:0": ["mem-shanghai"]})
    negative = abstain.to_eval_case({})

    assert retrieval.expected_memory_ids == ["mem-shanghai"]
    assert retrieval.query_type == "knowledge-update"
    assert not retrieval.should_abstain
    assert negative.expected_memory_ids == []
    assert negative.should_abstain


def test_longmemeval_adapter_accepts_official_numeric_answers_and_date_format(tmp_path):
    numeric = _sample()[0]
    numeric["question_id"] = "temporal-number"
    numeric["answer"] = 3
    numeric["question_date"] = "2025/01/03 (Fri) 00:00"
    numeric["haystack_dates"] = [
        "2025/01/01 (Wed) 00:00",
        "2025/01/02 (Thu) 00:00",
    ]
    abstain = _sample()[0]
    abstain["question_id"] = "official-style_abs"
    abstain["question_type"] = "temporal-reasoning"
    abstain["answer"] = "The supplied history is insufficient."
    path = tmp_path / "official-shapes.json"
    path.write_text(json.dumps([numeric, abstain]), encoding="utf-8")

    parsed_numeric, parsed_abstain = LongMemEvalAdapter.load(path)

    assert parsed_numeric.answer == "3"
    assert not parsed_numeric.should_abstain
    assert parsed_abstain.should_abstain
    assert parsed_abstain.to_eval_case({"s2:0": ["would-leak-gold"]}).expected_memory_ids == []


def test_longmemeval_official_dates_use_day_precision_but_reject_later_days(tmp_path):
    same_day = _sample()[0]
    same_day["question_date"] = "2025/01/03 (Fri) 08:00"
    same_day["haystack_dates"] = [
        "2025/01/02 (Thu) 22:00",
        "2025/01/03 (Fri) 21:00",
    ]
    path = tmp_path / "official-date.json"
    path.write_text(json.dumps([same_day]), encoding="utf-8")
    assert len(LongMemEvalAdapter.load(path)) == 1

    same_day["haystack_dates"][1] = "2025/01/04 (Sat) 00:01"
    path.write_text(json.dumps([same_day]), encoding="utf-8")
    with pytest.raises(LongMemEvalSchemaError, match="future"):
        LongMemEvalAdapter.load(path)


def test_longmemeval_adapter_disambiguates_official_duplicate_session_ids(tmp_path):
    row = _sample()[0]
    row["haystack_session_ids"] = ["duplicate", "duplicate"]
    row["answer_session_ids"] = []
    path = tmp_path / "duplicates.json"
    path.write_text(json.dumps([row]), encoding="utf-8")

    case = LongMemEvalAdapter.load(path)[0]

    assert case.turns[0].ref == "duplicate:0"
    assert case.turns[1].ref == "duplicate#2:0"
    assert len({turn.ref for turn in case.turns}) == len(case.turns)


def test_longmemeval_adapter_preserves_official_empty_turns_without_gold(tmp_path):
    row = _sample()[0]
    row["haystack_sessions"][0][0]["content"] = ""
    path = tmp_path / "empty-turn.json"
    path.write_text(json.dumps([row]), encoding="utf-8")

    case = LongMemEvalAdapter.load(path)[0]

    assert case.turns[0].content == ""
    assert not case.turns[0].has_answer


def test_longmemeval_adapter_rejects_misaligned_or_future_sessions(tmp_path):
    invalid = _sample()[0]
    invalid["haystack_dates"] = ["2025-01-01T00:00:00+00:00"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([invalid]), encoding="utf-8")
    with pytest.raises(LongMemEvalSchemaError, match="aligned"):
        LongMemEvalAdapter.load(path)

    future = _sample()[0]
    future["haystack_dates"][1] = "2025-02-01T00:00:00+00:00"
    path.write_text(json.dumps([future]), encoding="utf-8")
    with pytest.raises(LongMemEvalSchemaError, match="future"):
        LongMemEvalAdapter.load(path)


def test_internal_evolution_suite_covers_relationship_matrix_bilingually():
    suite = load_internal_evolution_suite()
    expected = {
        "unrelated", "duplicate", "reinforcement", "refinement", "correction",
        "temporal_update", "contradiction", "supersession",
    }

    assert suite.schema_version == "dive-internal-evolution-v1"
    for relationship in expected:
        matching = [case for case in suite.cases if case.relationship == relationship]
        assert {case.language for case in matching} == {"zh", "en"}
        assert all(case.source_event_ids for case in matching)
    assert {case.relationship for case in suite.cases} == expected
