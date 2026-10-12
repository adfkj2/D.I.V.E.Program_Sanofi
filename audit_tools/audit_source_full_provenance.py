"""Audit full-run hashes, verdict provenance, costs and paired transitions offline."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
from oracle_source_retention_experiment import (
    PARENT, read_json, _load_run, _load_verdicts, load_oracle_dataset,
    digest, trace_filename, atomic_write_json, budget_ledgers, peak_cost,
    CacheTransport, MeteredClient, EXTRA, LLMJudge, JudgeSpec, score_trace)


def rows_by_id(path):
    rows = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip()]
    values = [r for r in rows if not r.get('_header')]
    assert len(values) == len({r['question_id'] for r in values})
    return {r['question_id']: r for r in values}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    plan = read_json(output / 'frozen-plan.json')
    proposal = read_json(output / 'prepared-full-plan.json')
    launch = read_json(output / 'full-launch-audit.json')
    assert read_json(output / 'full-post-run-audit.json')['audit_passed']
    assert digest(output / 'frozen-plan.json') == launch['frozen_plan_sha256']
    assert digest(output / 'prepared-full-plan.json') == launch['prepared_plan_sha256']
    assert digest(ROOT / 'audit_tools/run_prepared_source_full.py') == proposal['execution_driver_sha256']
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    assert dataset.sha256 == plan['dataset_sha256']
    for folder, hashes in [('offline-full-candidate', proposal['candidate_trace_sha256']),
                           ('screen', proposal['screen_trace_sha256'])]:
        for q, h in hashes.items():
            assert digest(output / folder / 'traces' / trace_filename(q)) == h
    assert digest(output / 'screen-verdicts.jsonl') == proposal['screen_verdicts_sha256']
    _, parent = _load_run(PARENT)
    _, actual = _load_run(output / 'full')
    header, verdicts = _load_verdicts(output / 'full-verdicts.jsonl', actual, dataset.sha256)
    _, parent_values = _load_verdicts(PARENT / 'full-verdicts.jsonl', parent, dataset.sha256)
    rows = rows_by_id(output / 'full-verdicts.jsonl')
    screen_rows = rows_by_id(output / 'screen-verdicts.jsonl')
    ledgers = budget_ledgers(output)
    before = {str(p): digest(p) for p in ledgers if p.exists()}
    meter = MeteredClient(CacheTransport(EXTRA), output / 'full-judge-audit-cache',
                         max_calls=0, max_tokens=12000000, reuse_roots=[p.parent for p in ledgers])
    judge = LLMJudge(meter, JudgeSpec(**header['judge']))
    origins, transitions, details = Counter(), Counter(), []
    errors, error_buckets, error_statuses = [], Counter(), Counter()
    groups = {
        'screen_reuse': set(proposal['reusable_screen_ids']),
        'exact_parent_input_reuse': set(proposal['reusable_parent_ids']),
        'new_input_execution': set(proposal['new_input_ids']),
    }
    assert sum(len(ids) for ids in groups.values()) == 500
    assert set.union(*groups.values()) == set(dataset.question_ids)
    cohort = {name: {'cases': len(ids), 'parent_correct': sum(parent_values[q] for q in ids),
                    'candidate_correct': sum(verdicts[q] for q in ids)} for name, ids in groups.items()}
    for case in dataset.cases:
        q = case.question_id
        t, old, row = actual[q], parent[q], rows[q]
        same_output = t['hypothesis'] == old['hypothesis'] and t['reader']['status'] == old['reader']['status']
        assert type(row['correct']) is bool and row['error'] is None
        if t['reader']['status'] in {'ERROR', 'TIMEOUT', 'INVALID'}:
            assert row['correct'] is False and row['raw'] == 'INVALID_OUTPUT_RULE'
            origins['deterministic_failure_scored_false'] += 1
        elif same_output:
            assert row['raw'] == 'EXACT_PARENT_VERDICT' and row['correct'] == parent_values[q]
            origins['exact_parent_verdict'] += 1
        else:
            replay = score_trace(judge, case, t)
            assert (replay.correct, replay.raw, replay.error) == (row['correct'], row['raw'], row['error']), q
            origins['exact_judge_cache_replay'] += 1
            if q in screen_rows:
                assert row == screen_rows[q]
                origins['screen_verdict_also_verified'] += 1
        if q in groups['screen_reuse']:
            assert digest(output / 'full/traces' / trace_filename(q)) == proposal['screen_trace_sha256'][q]
        if q in groups['exact_parent_input_reuse']:
            assert same_output and t['reader'] == old['reader'] and row['correct'] == parent_values[q]
        kind = ('both_correct' if row['correct'] else 'regression') if parent_values[q] else ('improvement' if row['correct'] else 'both_wrong')
        transitions[kind] += 1
        if kind in {'improvement', 'regression'}:
            details.append({'question_id': q, 'question_type': case.question_type, 'transition': kind,
                'question': case.question, 'reference_answer': case.answer,
                'parent_hypothesis': old['hypothesis'], 'candidate_hypothesis': t['hypothesis'],
                'parent_reader_status': old['reader']['status'], 'candidate_reader_status': t['reader']['status'],
                'input_cohort': next(name for name, ids in groups.items() if q in ids)})
        if not row['correct']:
            formed = t['formation']
            gold = [turn for turn in case.turns if turn.has_answer]
            retrieved = {s for group in t['retrieval']['returned_source_refs'] for s in group}
            packed = {s for view in t['evidence']['views'] for s in view['source_refs']}
            bucket = 'all_gold_sources_visible_content_or_reasoning_issue'
            if t['reader']['status'] in {'ERROR', 'TIMEOUT', 'INVALID'}:
                bucket = 'infrastructure_or_invalid_output'
            elif case.should_abstain:
                bucket = 'unanswerable_wrong_answer'
            elif not gold:
                bucket = 'no_gold_turn_annotation'
            elif any(not formed['turn_memory_counts'].get(turn.ref, 0) for turn in gold):
                bucket = 'some_gold_source_not_formed'
            elif any(formed['turn_events'].get(turn.ref) not in retrieved for turn in gold):
                bucket = 'some_gold_source_not_retrieved'
            elif any(formed['turn_events'].get(turn.ref) not in packed for turn in gold):
                bucket = 'some_gold_source_not_packed'
            error_buckets[bucket] += 1
            error_statuses[t['reader']['status']] += 1
            errors.append({'question_id': q, 'question_type': case.question_type,
                'question': case.question, 'reference_answer': case.answer,
                'hypothesis': t['hypothesis'], 'regression_from_parent': parent_values[q],
                'reader_status': t['reader']['status'], 'bucket': bucket,
                'missing_requirements': t['reader']['missing_requirements'],
                'gold_turns': [{'ref': turn.ref, 'role': turn.role, 'content': turn.content,
                                'formed': formed['turn_memory_counts'].get(turn.ref, 0),
                                'packed': formed['turn_events'].get(turn.ref) in packed} for turn in gold],
                'reader_visible_context': t['reader_context_text']})
    assert before == {str(p): digest(p) for p in ledgers if p.exists()}
    own = read_json(output / 'api-cache/ledger.json')['calls']
    assert all(r['status'] == 'completed' for r in own)
    if (output / 'resume-plan-001.json').exists():
        recovery = read_json(output / 'resume-plan-001.json')
        assert digest(ROOT / 'audit_tools/resume_prepared_source_full.py') == recovery['execution_driver_sha256']
        archive = output / 'interruption-001'
        reconciliation = read_json(archive / 'reconciliation.json')
        previous = read_json(archive / 'ledger-before-reconciliation.json')['calls']
        assert digest(archive / 'ledger-before-reconciliation.json') == reconciliation['old_ledger_sha256']
        for old, current in zip(previous, own):
            if old['status'] == 'completed':
                assert old == current
            else:
                assert old == reconciliation['original_pending_row']
                assert current['key'] == old['key'] and current['reconciled_from_durable_response'] is True
                response = output / 'api-cache/responses' / (current['key'] + '.json')
                assert digest(response) == reconciliation['response_sha256']
                payload = read_json(response)
                assert all(current[k] == payload[k] for k in ('input_tokens', 'output_tokens'))
                assert current['charged_budget'] == payload['input_tokens'] + payload['output_tokens']
    cost = peak_cost(ledgers)
    launch_limit = Decimal(launch['authorization']['planning_stop_cny'])
    assert Decimal(cost['peak_upper_cny']) < launch_limit
    report = {
        'audit_passed': True, 'cases': 500, 'new_audit_api_calls': 0,
        'verdict_provenance': dict(origins), 'judge_cache_hits': meter.hits,
        'cohorts': cohort, 'transitions': dict(transitions),
        'new_requests': len(own), 'new_requests_by_phase': dict(Counter(r['phase'] for r in own)),
        'new_peak_cost_cny': str(Decimal(cost['peak_upper_cny']) - Decimal(proposal['spent']['peak_upper_cny'])),
        'aggregate_cost': cost,
        'planning_remaining_cny': str(launch_limit - Decimal(cost['peak_upper_cny'])),
        'ledgers_unchanged': True, 'frozen_inputs_unchanged': True,
        'audit_driver_sha256': digest(Path(__file__)),
        'caveat': 'Reused public development data and saved model stages; not an independent all-fresh sample or official judge.'}
    atomic_write_json(output / 'full-verdict-provenance-audit.json', report)
    atomic_write_json(output / 'full-transition-review.json', {
        'evaluation_only': True, 'runtime_must_not_read_this_file': True,
        'transitions': dict(transitions), 'changed_correctness': details})
    atomic_write_json(output / 'full-remaining-errors.json', {
        'evaluation_only': True, 'runtime_must_not_read_this_file': True,
        'incorrect': len(errors), 'buckets': dict(error_buckets), 'statuses': dict(error_statuses),
        'caveat': 'Visible gold sources do not prove that required factual spans are visible.',
        'errors': errors})
    print(json.dumps(report))


if __name__ == '__main__':
    main()
