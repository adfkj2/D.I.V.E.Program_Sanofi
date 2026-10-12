"""Post-run launch-budget and R27 incremental comparison audit, offline only."""
from collections import Counter
from decimal import Decimal
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
import oracle_spare_document_experiment as e
from audit_adjudicated_result import visible_passages


def main():
    out = e.DEFAULT_OUTPUT
    for name in ('post-run-audit.json', 'verdict-provenance-audit.json'):
        assert e.read_json(out / name)['audit_passed']
    launch = e.read_json(out / 'screen-launch-addendum.json')
    assert e.digest(out / 'frozen-plan.json') == launch['frozen_plan_sha256']
    assert e.digest(out / 'screen-cost-preflight.json') == launch['preflight_sha256']
    assert e.digest(ROOT / 'audit_tools/run_spare_screen.py') == launch['launch_driver_sha256']
    assert e.digest(ROOT / 'eval/reports/oracle-statement-provenance-20261009/public-dataset-provenance.json') == launch['public_dataset_provenance_sha256']
    cost = e.read_json(out / 'verdict-provenance-audit.json')
    assert Decimal(cost['new_peak_cost_cny']) < Decimal(launch['screen_max_new_cny'])
    assert Decimal(cost['aggregate_cost']['peak_upper_cny']) < Decimal(launch['screen_stop_cny'])
    assert cost['aggregate_cost'] == e.peak_cost(e.budget_ledgers(out))
    dataset = e.load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    _, new = e._load_run(out / 'screen')
    _, old = e._load_run(e.R27_ROOT / 'screen')
    _, nv = e._load_verdicts(out / 'screen-verdicts.jsonl', new, dataset.sha256)
    _, ov = e._load_verdicts(e.R27_ROOT / 'screen-verdicts.jsonl', old, dataset.sha256)
    transitions, errors, details, identical = Counter(), [], [], 0
    for q in new:
        t, before, case = new[q], old[q], dataset.by_id()[q]
        identical += visible_passages(t) == visible_passages(before)
        kind = ('both_correct' if nv[q] else 'regression') if ov[q] else ('improvement' if nv[q] else 'both_wrong')
        transitions[kind] += 1
        detail = {'question_id': q, 'question_type': case.question_type, 'question': case.question,
            'reference_answer': case.answer, 'r27_hypothesis': before['hypothesis'],
            'r28_hypothesis': t['hypothesis'], 'transition_from_r27': kind,
            'r28_status': t['reader']['status'], 'missing_requirements': t['reader']['missing_requirements']}
        if kind in {'improvement', 'regression'}:
            details.append(detail)
        if not nv[q]:
            errors.append({**detail, 'reader_visible_context': t['reader_context_text'],
                           'reader_detail': t['reader']['detail']})
    report = {'audit_passed': True, 'cases': len(new), 'new_api_calls': 0,
        'exact_r27_verbatim_evidence_cases': identical, 'r27_transitions': dict(transitions),
        'r26_comparison': e.read_json(out / 'screen-comparison.json')['paired_transitions'],
        'score': e.read_json(out / 'screen-evaluation.json')['overall'],
        'answerable': e.read_json(out / 'screen-evaluation.json')['answerable'],
        'unanswerable': e.read_json(out / 'screen-evaluation.json')['unanswerable'],
        'selection': e.read_json(out / 'selection.json'),
        'new_peak_cost_cny': cost['new_peak_cost_cny'], 'aggregate_cost': cost['aggregate_cost'],
        'planning_remaining_cny': str(Decimal('29') - Decimal(cost['aggregate_cost']['peak_upper_cny'])),
        'launch_limit_verified': True, 'official_score': 'NOT_EVALUATED',
        'note': 'Fixed public development screening. Full 500 score remains 422/500; not an independent holdout.'}
    e.atomic_write_json(out / 'r27-increment-and-launch-audit.json', report)
    e.atomic_write_json(out / 'r27-transition-review.json', {'evaluation_only': True,
        'runtime_must_not_read_this_file': True, 'details': details})
    e.atomic_write_json(out / 'screen-remaining-errors.json', {'evaluation_only': True,
        'runtime_must_not_read_this_file': True, 'errors': errors})
    print(json.dumps(report))


if __name__ == '__main__':
    main()
