"""Restricted fixture visibility, tool scope, and evaluator-only metadata."""

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest
from test_reader_challenge_runner import challenge

from dive_memory.reading.evidence import build_evidence_views, render_evidence_block
from dive_memory.temporal import parse_instant

PATH = Path(__file__).parents[1] / "eval/datasets/reader_challenge_tools_v1/cases.json"
DATA = json.loads(PATH.read_bytes())


def render(request):
    return render_evidence_block(
        build_evidence_views(
            request.items,
            question=request.question,
            question_date=request.question_date,
        )
    )


def test_eight_independent_bilingual_tool_cases_are_disclosed_and_unique():
    assert (
        DATA["independent_holdout"]
        is DATA["derived_from_benchmark"]
        is DATA["contains_real_personal_data"]
        is False
    )
    cases, _ = challenge.load_cases(PATH)
    assert len(cases) == len({case["id"] for case in cases}) == 8
    assert Counter(case["language"] for case in cases) == {"en": 4, "zh": 4}
    assert (
        len(
            {
                json.dumps(
                    {
                        k: c.get(k)
                        for k in (
                            "question",
                            "sources",
                            "initial_source_ids",
                            "initial_content_chars",
                        )
                    },
                    sort_keys=True,
                )
                for c in cases
            }
        )
        == 8
    )
    for case in cases:
        assert case["evaluation_contract"]["tool_required"] is True
        assert set(case["evaluation_contract"]["allowed_tools"]) <= {"search", "read"}
        assert set(case["expected"]["required_source_ids"]) <= {
            s["id"]
            for s in case["sources"]
            if parse_instant(s["date"]) <= parse_instant(case["date"])
        }


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda case: case["id"])
def test_initial_sources_are_restricted_and_hidden_sources_require_search(case):
    request, source = challenge.build_request(case)
    initial_refs = {ref for item in request.items for ref in item.source_refs}
    assert initial_refs == set(case["initial_source_ids"])
    for item in request.items:
        if "initial_content_chars" in case:
            assert len(item.memory.content) <= case["initial_content_chars"]
    admitted = {
        s["id"]
        for s in case["sources"]
        if parse_instant(s["date"]) <= parse_instant(case["date"])
    }
    all_refs = {ref for item in source.items.values() for ref in item.source_refs}
    assert all_refs == admitted
    for memory_id, item in source.items.items():
        if not set(item.source_refs) & initial_refs:
            with pytest.raises(ValueError, match="admitted"):
                source.read(memory_id)
    found = source.search(case["question"])
    assert {ref for item in found for ref in item.source_refs} == admitted
    assert all(source.read(item.memory.id) == item for item in found)
    assert source.search_calls == 1


def test_initial_truncation_hides_the_required_appendix_but_retains_readable_full_source():
    for case in DATA["cases"]:
        if "initial_content_chars" not in case:
            continue
        request, source = challenge.build_request(case)
        visible = request.items[0]
        full = source.read(visible.memory.id)
        assert len(full.memory.content) > len(visible.memory.content)
        assert full.memory.content.startswith(visible.memory.content)
        assert "initial_content_chars" not in render(request)
        assert any(
            fact not in visible.memory.content
            for fact in case["expected"]["answer_facts"]
        )


def test_expected_and_contract_never_enter_initial_or_tool_evidence():
    case = deepcopy(DATA["cases"][0])
    case["expected"] = {"private": "EXPECTED_SENTINEL_NEVER_INPUT"}
    case["evaluation_contract"] = {"private": "CONTRACT_SENTINEL_NEVER_INPUT"}
    request, source = challenge.build_request(case)
    contents = render(request) + str(source.search(case["question"]))
    assert "EXPECTED_SENTINEL" not in contents and "CONTRACT_SENTINEL" not in contents


@pytest.mark.parametrize(
    "value", [None, "index", ["missing"], ["index", "index"], [1], [True]]
)
def test_invalid_initial_source_selection_is_rejected(value):
    case = deepcopy(DATA["cases"][0])
    case["initial_source_ids"] = value
    with pytest.raises(ValueError, match="initial_source_ids"):
        challenge.build_request(case)


@pytest.mark.parametrize("value", [None, 0, -1, True, 1.5, "180"])
def test_initial_content_cap_must_be_a_positive_integer(value):
    case = deepcopy(DATA["cases"][0])
    case["initial_content_chars"] = value
    with pytest.raises(ValueError, match="initial_content_chars"):
        challenge.build_request(case)


def test_future_source_cannot_be_promoted_to_initial_visibility():
    case = deepcopy(DATA["cases"][-1])
    case["initial_source_ids"] = ["future"]
    with pytest.raises(ValueError, match="initial_source_ids"):
        challenge.build_request(case)


def test_omitting_new_fields_keeps_old_initial_packet_and_full_source():
    case = deepcopy(DATA["cases"][0])
    case.pop("initial_source_ids")
    before, source = challenge.build_request(case)
    explicit = deepcopy(case)
    explicit["initial_source_ids"] = [s["id"] for s in case["sources"]]
    after, other = challenge.build_request(explicit)
    assert render(before) == render(after)
    assert [
        (item.memory.id, item.memory.content, item.source_refs)
        for item in source.items.values()
    ] == [
        (item.memory.id, item.memory.content, item.source_refs)
        for item in other.items.values()
    ]
