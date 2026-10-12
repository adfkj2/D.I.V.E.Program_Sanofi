"""Reconcile durable responses and prepare remaining work without resampling failures."""
import argparse
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
from oracle_source_retention_experiment import (
    PARENT, read_json, _load_run, load_oracle_dataset, code_snapshot, digest,
    atomic_write_json, restore_visible_request, budget_ledgers, peak_cost,
    make_reader, CacheTransport, EXTRA, THINKING, ENDPOINT, audit_citations)
from oracle_low_cost_pilot import digest as request_digest
from oracle_v4_experiment import evidence_audit


class RequestCaptured(BaseException):
    pass


class CaptureFirstRequest:
    def complete(self, **kwargs):
        self.key = request_digest({'endpoint': getattr(self.client, 'endpoint', ENDPOINT),
            'extra_body': dict(getattr(self.client, 'extra_body', EXTRA)), 'request': kwargs})
        raise RequestCaptured()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--reconcile', action='store_true')
    args = ap.parse_args()
    out = args.output.resolve()
    frozen = read_json(out / 'frozen-plan.json')
    proposal = read_json(out / 'prepared-full-plan.json')
    assert code_snapshot(ROOT) == frozen['source_snapshot']
    assert digest(ROOT / 'audit_tools/run_prepared_source_full.py') == proposal['execution_driver_sha256']
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    assert dataset.sha256 == frozen['dataset_sha256']
    _, parent = _load_run(PARENT)
    _, candidate = _load_run(out / 'offline-full-candidate')
    paths = list((out / 'full/traces').glob('*.json'))
    saved = {read_json(p)['question_id']: read_json(p) for p in paths}
    assert len(saved) == len(paths) and set(saved) <= set(dataset.question_ids)
    evidence_audit({q: candidate[q] for q in saved}, saved)
    for trace in saved.values():
        audit_citations(trace)
    failures = {q: t for q, t in saved.items() if t['reader']['status'] in {'ERROR', 'TIMEOUT'}}
    ledger_path = out / 'api-cache/ledger.json'
    ledger = read_json(ledger_path)
    unresolved = [r for r in ledger['calls'] if r['status'] != 'completed']
    archive = out / 'interruption-001'
    if unresolved:
        assert len(unresolved) == len(failures) == 1
        q, failed = next(iter(failures.items()))
        assert failed['reader']['missing_requirements'] == ['PermissionError']
        assert json.loads(failed['reader']['detail'])['route'] == 'primary_transport_failure'
        row = unresolved[0]
        assert row['status'] == 'pending'
        probe = CaptureFirstRequest()
        reader = make_reader(probe, CacheTransport(EXTRA), CacheTransport(THINKING))
        try:
            reader.primary.generate(restore_visible_request(dataset.by_id()[q].question, candidate[q]))
        except RequestCaptured:
            pass
        else:
            raise AssertionError('No first request captured')
        assert probe.key == row['key']
        response_path = out / 'api-cache/responses' / (row['key'] + '.json')
        response = read_json(response_path)
        assert all(type(response[k]) is int and response[k] >= 0 for k in ('input_tokens', 'output_tokens'))
        reconciliation = {'question_id': q, 'request_identity_verified': True,
            'old_ledger_sha256': digest(ledger_path), 'response_sha256': digest(response_path),
            'original_pending_row': deepcopy(row), 'observed_input_tokens': response['input_tokens'],
            'observed_output_tokens': response['output_tokens'],
            'failure_trace_preserved_and_scored_false': True, 'new_api_calls': 0,
            'reason': 'Response durably saved before PermissionError in ledger persistence; exact runtime request key verified.'}
        print(json.dumps({'reconciliation_ready': reconciliation}))
        if not args.reconcile:
            return
        assert not archive.exists(), 'Interruption archive already exists; review before mutation'
        archive.mkdir()
        for name in ('full-stopped.json', 'full-usage.json', 'aggregate-peak-cost.json'):
            shutil.copy2(out / name, archive / name)
        shutil.copy2(ledger_path, archive / 'ledger-before-reconciliation.json')
        shutil.copy2(out / 'full/traces' / (q + '.json'), archive / 'failed-trace.json')
        row.update(status='completed', input_tokens=response['input_tokens'], output_tokens=response['output_tokens'],
            resolved_model=response['model'], charged_budget=response['input_tokens'] + response['output_tokens'],
            reconciled_from_durable_response=True)
        atomic_write_json(ledger_path, ledger)
        reconciliation['new_ledger_sha256'] = digest(ledger_path)
        atomic_write_json(archive / 'reconciliation.json', reconciliation)
    elif failures:
        assert (archive / 'reconciliation.json').exists()
    remaining = [q for q in proposal['new_input_ids'] if q not in saved]
    reader_estimate = Decimal(0)
    for q in remaining:
        c = dataset.by_id()[q]
        a, b = (restore_visible_request(c.question, ts[q]) for ts in (parent, candidate))
        usage = parent[q]['reader']['usage']
        scale = max(Decimal(1), Decimal(len(b.context_text)) / max(1, len(a.context_text)))
        reader_estimate += (2 * Decimal(usage['input_tokens']) * scale + 8 * usage['output_tokens']) / 1000000
    # Reserve judging for every planned new-input case, including completed ones.
    judge_estimate = Decimal(3024 * len(proposal['new_input_ids'])) / 1000000
    estimate = reader_estimate + judge_estimate
    allowance = estimate * Decimal('1.25')
    spent = peak_cost(budget_ledgers(out))
    rules = read_json(ROOT / 'eval/reports/oracle-road-to-90-budget.json')
    resume = {'prepared_plan_sha256': digest(out / 'prepared-full-plan.json'),
        'frozen_plan_sha256': digest(out / 'frozen-plan.json'),
        'source_snapshot_sha256': frozen['source_snapshot']['sha256'],
        'saved_trace_sha256': {q: digest(out / 'full/traces' / (q + '.json')) for q in saved},
        'preserved_failure_ids': sorted(failures), 'remaining_new_input_ids': remaining,
        'remaining_reader_cases': len(remaining), 'judge_allowance_cases': len(proposal['new_input_ids']),
        'reader_estimate_cny': str(reader_estimate), 'judge_estimate_cny': str(judge_estimate),
        'estimated_additional_cny': str(estimate), 'planning_allowance_cny': str(allowance),
        'spent': spent, 'aggregate_budget': rules,
        'fits_budget': Decimal(spent['peak_upper_cny']) + allowance < Decimal(rules['planning_stop_cny']),
        'failed_cases_policy': 'Preserve saved ERROR/TIMEOUT exactly and score false; never resend their requests.',
        'persistence_policy': 'Retry identical local JSON writes on PermissionError only; never retry a network request.',
        'execution_driver_sha256': digest(ROOT / 'audit_tools/resume_prepared_source_full.py'),
        'preparation_driver_sha256': digest(Path(__file__)), 'new_api_calls': 0}
    path = out / 'resume-plan-001.json'
    assert not path.exists() or read_json(path) == resume
    atomic_write_json(path, resume)
    print(json.dumps({k: v for k, v in resume.items() if k not in ('saved_trace_sha256', 'remaining_new_input_ids', 'aggregate_budget')}))


if __name__ == '__main__':
    main()
