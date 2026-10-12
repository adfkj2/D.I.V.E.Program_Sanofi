import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
from oracle_adjudicated_full import projected_cost, require_budget, PilotStopped


def test_cost_excludes_cached_primary_but_includes_secondary_repairs_and_referee():
    stages = [{"usage": {"input_tokens": i, "output_tokens": o}}
              for i, o in ((999999, 999999), (100, 20), (50, 10))]
    traces = {"sample": {"reader": {"detail": json.dumps({"stages": stages})}}}
    cost = projected_cost(traces, ["sample"], 10)
    assert cost["reader_review_cny"] == "0.00540"
    assert cost["judge_allowance_cny"] == "0.030240"
    assert cost["planning_allowance_cny"] == "0.04455000"


def test_existing_budget_stops_full_run_before_any_credential_or_request():
    rules = {"planning_stop_cny": "9", "user_limit_cny": "10", "settlement_reserve_cny": "1"}
    with pytest.raises(PilotStopped, match="exceeds the recorded shared budget"):
        require_budget({"planning_allowance_cny": "10.65"}, "8.27", rules)
    rules.update(planning_stop_cny="19", user_limit_cny="20")
    require_budget({"planning_allowance_cny": "10.65"}, "8.27", rules)
    rules["planning_stop_cny"] = "20"
    with pytest.raises(PilotStopped, match="settlement reserve"):
        require_budget({"planning_allowance_cny": "10.65"}, "8.27", rules)


def test_unknown_usage_cannot_be_used_to_authorize_full_run():
    stages = [{}, {"usage": {"input_tokens": None, "output_tokens": 10}}]
    with pytest.raises(PilotStopped, match="Unknown screening usage"):
        projected_cost({"sample": {"reader": {"detail": json.dumps({"stages": stages})}}}, ["sample"], 10)
