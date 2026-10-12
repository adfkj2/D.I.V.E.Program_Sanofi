"""Prepared full runs reconstruct the exact screened configuration."""
from dataclasses import replace
from pathlib import Path
import sys
import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / p) for p in ('ops', 'audit_tools')]
from prepare_source_candidate_full import config_from_plan
from oracle_source_retention_experiment import CANDIDATE


def test_all_screened_options_roundtrip_without_falling_back_to_defaults():
    config = replace(CANDIDATE, limit=60, reported_detail_assertions_only=True)
    restored = config_from_plan({'config': config.to_dict()})
    assert restored == config


def test_unknown_configuration_field_cannot_be_silently_discarded():
    values = {**CANDIDATE.to_dict(), 'unrecognized_behavior': True}
    with pytest.raises(ValueError, match='identical'):
        config_from_plan({'config': values})
