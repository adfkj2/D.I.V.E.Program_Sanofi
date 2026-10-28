"""Semantic fixtures stay disclosed, scoped, and outside model evaluation data.

These checks validate dataset and transport boundaries. They deliberately do not
implement a keyword scorer or claim that scripted outputs solve the cases.
"""

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest
from test_reader_challenge_runner import challenge

from dive_memory.application.readers import create_reader
from dive_memory.reading.evidence import build_evidence_views, render_evidence_block
from dive_memory.reading.providers import ScriptedChatClient
from dive_memory.temporal import parse_instant

PATH = (
    Path(__file__).parents[1] / "eval/datasets/reader_challenge_grounding_v1/cases.json"
)
DATA = json.loads(PATH.read_bytes())


def render(request):
    return render_evidence_block(
        build_evidence_views(
            request.items,
            question=request.question,
            question_date=request.question_date,
        )
    )


def test_eight_new_bilingual_semantic_cases_have_explicit_development_disclosure():
    assert DATA["schema_version"] == "reader-challenge-v1"
    assert DATA["dataset_kind"] == "handwritten_development_fixtures"
    assert (
        DATA["independent_holdout"]
        is DATA["contains_real_personal_data"]
        is DATA["derived_from_benchmark"]
        is False
    )
    assert DATA["scope"] == "reader_only_not_memory_formation_or_official_benchmark"
    cases, digest = challenge.load_cases(PATH)
    assert len(digest) == 64
    assert len(cases) == len({case["id"] for case in cases}) == 8
    assert Counter(case["language"] for case in cases) == {"en": 4, "zh": 4}
    assert Counter(case["expected"]["status"] for case in cases) == {
        "ANSWER": 6,
        "ABSTAIN": 2,
    }
    old_ids = {
        case["id"]
        for directory in ("reader_challenge_v1", "reader_challenge_tools_v1")
        for case in json.loads(
            (PATH.parents[1] / directory / "cases.json").read_bytes()
        )["cases"]
    }
    assert old_ids.isdisjoint({case["id"] for case in cases})
    assert (
        len(
            {
                json.dumps(
                    {key: case[key] for key in ("question", "date", "sources")},
                    sort_keys=True,
                )
                for case in cases
            }
        )
        == 8
    )


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda case: case["id"])
def test_every_case_has_complete_initial_sources_and_preserves_role_and_clock(case):
    request, source = challenge.build_request(case)
    by_ref = {row["id"]: row for row in case["sources"]}
    assert len(by_ref) == len(case["sources"]) >= 2
    assert len(request.items) == len(source.items) == len(by_ref)
    assert {ref for item in request.items for ref in item.source_refs} == set(by_ref)
    for item in request.items:
        row = by_ref[item.source_refs[0]]
        assert set(row) == {"id", "role", "date", "content"}
        assert parse_instant(row["date"]) <= parse_instant(case["date"])
        assert item.memory.content == row["content"]
        assert item.memory.observed_at == row["date"]
        assert item.memory.structured_content == {
            "_reader_statement_sources": [
                {
                    "source_ref": row["id"],
                    "role": row["role"],
                    "statement_at": row["date"],
                }
            ]
        }
        assert source.read(item.memory.id).memory.content == row["content"]


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda case: case["id"])
def test_evaluation_contract_requires_separate_semantics_instead_of_keyword_grading(
    case,
):
    expected = case["expected"]
    assert set(expected) == {
        "status",
        "answer_facts",
        "required_source_ids",
        "forbidden_facts",
        "reason",
    }
    assert expected["reason"].strip()
    assert expected["forbidden_facts"]
    assert len(set(expected["required_source_ids"])) == len(
        expected["required_source_ids"]
    )
    assert set(expected["required_source_ids"]) <= {
        row["id"] for row in case["sources"]
    }
    if expected["status"] == "ABSTAIN":
        assert expected["answer_facts"] == expected["required_source_ids"] == []
    else:
        assert expected["answer_facts"] and expected["required_source_ids"]
    contract = case["evaluation_contract"]
    assert contract["tool_required"] is False
    assert set(contract["allowed_tools"]) == {"search", "read"}
    assert contract["review_mode"] == "independent_semantic_review"
    assert len(contract["semantic_requirements"]) >= 3
    assert all(
        isinstance(value, str) and value.strip()
        for value in contract["semantic_requirements"]
    )


def test_required_source_sets_do_not_demand_optional_context_or_abstention_citations():
    # These are minimum necessary premises, not a requirement to cite every
    # mention, rejected alternative, or optional corroborating observation.
    assert {
        case["id"]: set(case["expected"]["required_source_ids"])
        for case in DATA["cases"]
    } == {
        "grounding-distinct-target-exclusion-zh": {"complete-visit-record"},
        "grounding-complete-qualifiers-en": {"purchase-confirmation"},
        "grounding-statement-clock-zh": {"completion-statement"},
        "grounding-completed-components-en": {
            "north-completion",
            "south-completion",
        },
        "grounding-assistant-recap-is-not-confirmation-en": set(),
        "grounding-retelling-versus-new-event-zh": {
            "first-rehearsal",
            "second-rehearsal",
        },
        "grounding-question-referent-constraint-en": {"reservation-capacity"},
        "grounding-question-does-not-supply-price-zh": set(),
    }


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda case: case["id"])
def test_expected_contract_and_case_metadata_never_reach_any_actual_reader_phase(case):
    poisoned = deepcopy(case)
    poisoned["id"] = "PRIVATE_CASE_ID_SENTINEL"
    poisoned["category"] = "PRIVATE_CATEGORY_SENTINEL"
    poisoned["language"] = "PRIVATE_LANGUAGE_SENTINEL"
    poisoned["expected"] = {"private": "PRIVATE_EXPECTED_SENTINEL"}
    poisoned["evaluation_contract"] = {"private": "PRIVATE_CONTRACT_SENTINEL"}
    poisoned["oracle_gold"] = "PRIVATE_GOLD_SENTINEL"
    snapshot = deepcopy(poisoned)
    request, source = challenge.build_request(poisoned)
    visible = render(request) + str(source.search(request.question))
    assert "PRIVATE_" not in visible

    # Scripted abstention is solely a transport probe; it is not graded as an
    # answer to these fixtures and does not import their expected values.
    missing = ["synthetic transport probe"]
    plan = {
        "source_indices": [],
        "queries": [],
        "reads": [],
        "question_contract": {
            "target": "transport probe",
            "answer_unit": "probe",
            "include": [],
            "exclude": [],
            "question_premises": [],
            "required_details": [],
        },
    }
    decision = {
        "status": "ABSTAIN",
        "answer": None,
        "source_indices": [],
        "missing": missing,
    }
    draft = {
        **decision,
        "analysis": {
            "scope": "transport probe",
            "events": [],
            "answer_details": [],
            "uncertainties": missing,
        },
    }
    review = {**decision, "review": {"action": "accept", "defects": []}}
    normal = ScriptedChatClient(
        [json.dumps(plan)], input_tokens_per_call=10, output_tokens_per_call=5
    )
    thinking = ScriptedChatClient(
        [json.dumps(draft), json.dumps(review)],
        input_tokens_per_call=10,
        output_tokens_per_call=5,
    )
    reader = create_reader(
        normal, thinking, strategy="deliberative-grounded", model="fixture"
    )
    result = challenge.run_case(poisoned, reader)
    assert result["reader"]["status"] == "ABSTAIN"
    prompts = normal.prompts + thinking.prompts
    assert len(prompts) == 3
    assert all("PRIVATE_" not in prompt for prompt in prompts)
    assert poisoned == snapshot
    assert result["semantic_correctness"] is None
    assert result["evaluation"] == "NOT_EVALUATED_requires_separate_semantic_review"


def test_grounded_strategy_dataset_plan_makes_no_transport_or_output(
    tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dataset planning must not construct a transport")

    monkeypatch.setattr(challenge, "OpenAICompatibleChatClient", forbidden)
    output = tmp_path / "uncreated"
    assert (
        challenge.main(
            [
                "--dataset",
                str(PATH),
                "--output",
                str(output),
                "--strategy",
                "deliberative-grounded",
            ]
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["cases"] == 8 and plan["execution"] == "PLAN_ONLY"
    assert plan["official_benchmark"] is False
    assert plan["semantic_evaluation"] == "NOT_EVALUATED"
    assert not output.exists()
