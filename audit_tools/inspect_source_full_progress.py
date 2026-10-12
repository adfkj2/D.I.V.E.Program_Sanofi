"""Record local progress without network calls or modifying frozen evidence."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
from oracle_source_retention_experiment import (
    read_json, budget_ledgers, peak_cost, atomic_write_json, digest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    proposal = read_json(output / 'prepared-full-plan.json')
    rules = read_json(ROOT / 'eval/reports/oracle-road-to-90-budget.json')
    initial = Decimal(proposal['spent']['peak_upper_cny'])
    traces = [read_json(p) for p in (output / 'full/traces').glob('*.json')]
    ledger = output / 'api-cache/ledger.json'
    calls = read_json(ledger)['calls'] if ledger.exists() else []
    spent = peak_cost(budget_ledgers(output))
    verdict_path = output / 'full-verdicts.jsonl'
    verdicts = [json.loads(s) for s in verdict_path.read_text(encoding='utf-8').splitlines()
                if s.strip()] if verdict_path.exists() else []
    judged = [r for r in verdicts if not r.get('_header')]
    launch = output / 'full-launch-audit.json'
    if not launch.exists():
        atomic_write_json(launch, {
            'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
            'authorization': rules,
            'shared_budget_sha256': digest(ROOT / 'eval/reports/oracle-road-to-90-budget.json'),
            'prepared_plan_sha256': digest(output / 'prepared-full-plan.json'),
            'frozen_plan_sha256': digest(output / 'frozen-plan.json'),
            'execution_driver_sha256': digest(ROOT / 'audit_tools/run_prepared_source_full.py'),
            'baseline_cumulative_cost_cny': str(initial),
            'note': 'Recorded during execution after READY; time is not the process launch time.'})
    summary = {
        'observed_at_utc': datetime.now(timezone.utc).isoformat(),
        'saved_cases': len(traces), 'total_cases': 500,
        'reader_statuses': dict(Counter(t['reader']['status'] for t in traces)),
        'new_input_cases_saved': sum(t['question_id'] in proposal['new_input_ids'] for t in traces),
        'new_input_cases_planned': len(proposal['new_input_ids']),
        'judged_cases': len(judged),
        'request_states': dict(Counter(r['status'] for r in calls)),
        'new_requests_by_phase': dict(Counter(r['phase'] for r in calls)),
        'aggregate_cost': spent,
        'new_cost_upper_including_pending_cny': str(Decimal(spent['peak_upper_cny']) - initial),
        'planning_remaining_including_pending_cny': str(Decimal(rules['planning_stop_cny']) - Decimal(spent['peak_upper_cny'])),
        'complete_marker': (output / 'full-completion.json').exists(),
        'stopped_marker': (output / 'full-stopped.json').exists(),
        'is_accuracy_result': False,
    }
    atomic_write_json(output / 'full-progress.json', summary)
    with (output / 'full-progress-history.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(summary, ensure_ascii=False) + '\n')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
