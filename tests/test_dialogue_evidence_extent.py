"""Stored retention bounds and the currently visible passage are distinct."""

from dataclasses import replace

import pytest
from test_evidence_workspace import item

from dive_memory.dialogue_retention import PREDICATE, VERSION
from dive_memory.reading.evidence import build_evidence_views, render_evidence_block


def retained(**changes):
    source = item("I paid 37 for this painting.")
    metadata = {
        "predicate": PREDICATE,
        "retention_policy": VERSION,
        "role": "user",
        "source_span": [2, 29],
        "source_chars": 80,
        "retained_chars": 27,
        "truncated": True,
        **changes,
    }
    return replace(source, memory=replace(source.memory, structured_content=metadata))


def test_retained_source_extent_does_not_claim_the_whole_turn_is_visible():
    original = retained()
    clipped = replace(original, memory=replace(original.memory, content="37"))
    views = build_evidence_views([clipped], question="What did I pay?")
    text = render_evidence_block(views)
    assert "retained_source_span=2:29" in text
    assert "original_source_chars=80" in text
    assert "retained_chars=27" in text
    assert "retention_truncated=true" in text
    assert "displayed_chars=2" in text
    assert text.endswith("\n    37")
    assert views[0].to_dict()["retention_extent"] == {
        "retained_source_span": [2, 29],
        "original_source_chars": 80,
        "retained_chars": 27,
        "truncated": True,
        "displayed_chars": 2,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"source_span": [-1, 26]},
        {"source_span": [2, 81]},
        {"source_span": [True, 28]},
        {"source_span": "2:29"},
        {"source_span": [2, 2]},
        {"source_chars": True},
        {"retained_chars": 26},
        {"truncated": "true"},
        {"retention_policy": "unrecognized"},
        {"predicate": "arbitrary"},
    ],
)
def test_invalid_or_unrelated_metadata_does_not_become_a_retention_assertion(changes):
    views = build_evidence_views([retained(**changes)], question="What did I pay?")
    assert "retention_extent" not in views[0].to_dict()
    assert "retained_source_span" not in render_evidence_block(views)


def test_legacy_evidence_rendering_does_not_gain_retention_headers():
    views = build_evidence_views([item("Ordinary fact.")], question="What fact?")
    assert "retention_extent" not in views[0].to_dict()
    assert render_evidence_block(views).endswith("\n    Ordinary fact.")
    assert "displayed_chars" not in render_evidence_block(views)
