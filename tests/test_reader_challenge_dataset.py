"""Public handwritten fixtures are development checks, with explicit provenance."""

import json
from collections import Counter
from pathlib import Path

import pytest

from dive_memory.temporal import parse_instant

PATH = Path(__file__).parents[1] / "eval/datasets/reader_challenge_v1/cases.json"
DATASET = json.loads(PATH.read_text(encoding="utf-8"))


def test_dataset_exposure_and_synthetic_origin_are_explicit():
    assert DATASET["schema_version"] == "reader-challenge-v1"
    assert DATASET["dataset_kind"] == "handwritten_development_fixtures"
    assert DATASET["independent_holdout"] is False
    assert DATASET["contains_real_personal_data"] is False
    assert DATASET["derived_from_benchmark"] is False
    assert "not_memory_formation_or_official_benchmark" in DATASET["scope"]


def test_eight_challenge_families_have_two_variants_in_each_language():
    cases = DATASET["cases"]
    assert len(cases) == 32
    assert len({case["id"] for case in cases}) == len(cases)
    categories = Counter(case["category"] for case in cases)
    assert categories == {
        "identity": 4,
        "missing_comparison": 4,
        "updated_total": 4,
        "preference_advice": 4,
        "plan_vs_completion": 4,
        "complete_list": 4,
        "event_chronology": 4,
        "source_injection": 4,
    }
    for category in categories:
        assert Counter(
            case["language"] for case in cases if case["category"] == category
        ) == {"zh": 2, "en": 2}


def test_every_case_has_a_distinct_question_date_and_source_input():
    inputs = [
        json.dumps(
            {key: case[key] for key in ("question", "date", "sources")},
            ensure_ascii=False,
            sort_keys=True,
        )
        for case in DATASET["cases"]
    ]
    assert len(set(inputs)) == len(inputs)


@pytest.mark.parametrize("case", DATASET["cases"], ids=lambda case: case["id"])
def test_fixture_sources_and_expected_contract_remain_separate_and_referential(case):
    assert isinstance(case["question"], str) and case["question"].strip()
    when = parse_instant(case["date"])
    assert when is not None
    sources = case["sources"]
    source_ids = [source["id"] for source in sources]
    assert len(source_ids) == len(set(source_ids)) >= 2
    for source in sources:
        assert set(source) == {"id", "role", "date", "content"}
        assert source["role"] in {"user", "assistant"}
        assert parse_instant(source["date"]) is not None
        assert isinstance(source["content"], str) and source["content"].strip()
    expected = case["expected"]
    assert set(expected) == {
        "status",
        "answer_facts",
        "required_source_ids",
        "forbidden_facts",
        "reason",
    }
    assert expected["status"] in {"ANSWER", "ABSTAIN"}
    assert all(
        isinstance(value, str) and value.strip()
        for field in ("answer_facts", "required_source_ids", "forbidden_facts")
        for value in expected[field]
    )
    assert set(expected["required_source_ids"]) <= set(source_ids)
    assert expected["reason"].strip()
    if expected["status"] == "ANSWER":
        assert expected["answer_facts"] and expected["required_source_ids"]
        by_id = {source["id"]: source for source in sources}
        assert all(
            parse_instant(by_id[source_id]["date"]) <= when
            for source_id in expected["required_source_ids"]
        )
    else:
        assert expected["answer_facts"] == expected["required_source_ids"] == []


def test_missing_comparisons_and_future_injections_are_present_as_negative_controls():
    cases = DATASET["cases"]
    assert all(
        case["expected"]["status"] == "ABSTAIN"
        for case in cases
        if case["category"] == "missing_comparison"
    )
    for case in cases:
        if case["category"] != "source_injection":
            continue
        assert any(source["role"] == "assistant" for source in case["sources"])
        assert any(
            parse_instant(source["date"]) > parse_instant(case["date"])
            for source in case["sources"]
        )
        assert case["expected"]["forbidden_facts"]
