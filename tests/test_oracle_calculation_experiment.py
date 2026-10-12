import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import oracle_calculation_experiment as experiment


def report(positive, negative, *, judged=True):
    return {"overall": {"fully_judged": judged},
            "answerable": {"cases": 50, "correct": positive},
            "unanswerable": {"cases": 30, "correct": negative}}


def test_promotion_requires_complete_strata_and_real_gain():
    baseline = report(37, 28)
    assert experiment.eligible(baseline, report(40, 28))
    assert not experiment.eligible(baseline, report(39, 30))
    assert not experiment.eligible(baseline, report(45, 27))
    assert not experiment.eligible(baseline, report(45, 30, judged=False))
    incomplete = report(45, 30)
    incomplete["unanswerable"]["cases"] = 29
    assert not experiment.eligible(baseline, incomplete)


def test_new_output_does_not_drop_previous_calculation_spend(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, "ROOT", tmp_path)
    previous = tmp_path / "eval/reports/oracle-calculation-prior/api-cache/ledger.json"
    previous.parent.mkdir(parents=True)
    previous.write_text('{"calls":[]}', encoding="utf-8")
    root = tmp_path / "eval/reports/oracle-calculation-new"
    paths = experiment.budget_ledgers(root)
    assert previous in paths
    assert root / "api-cache/ledger.json" in paths
    assert len(paths) == len(set(paths))


def test_shared_budget_finds_later_experiments_by_scope_not_directory_name(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, "ROOT", tmp_path)
    reports = tmp_path / "eval/reports"
    reports.mkdir(parents=True)
    (reports / "oracle-road-to-90-budget.json").write_text(json.dumps({"scope": "shared goal"}), encoding="utf-8")
    for name, scope in [("oracle-extended-reasoning-next", "shared goal"),
                        ("custom-next-run", "shared goal"), ("unrelated-run", "other task")]:
        folder = reports / name
        folder.mkdir()
        (folder / "frozen-plan.json").write_text(json.dumps({"aggregate_budget": {"scope": scope}}), encoding="utf-8")
    paths = experiment.budget_ledgers(reports / "current")
    assert reports / "oracle-extended-reasoning-next/api-cache/ledger.json" in paths
    assert reports / "custom-next-run/api-cache/ledger.json" in paths
    assert reports / "unrelated-run/api-cache/ledger.json" not in paths
