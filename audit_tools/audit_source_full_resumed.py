"""Audit resumed full run: exact caches plus immutable scored infrastructure failures."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
from oracle_source_retention_experiment import (
    PARENT, read_json, _load_run, _load_verdicts, load_oracle_dataset,
    code_snapshot, atomic_write_json, restore_visible_request, same_input,
    digest, trace_filename, budget_ledgers, make_reader, generate_record, audit_citations,
    audit_submission, MeteredClient, CacheTransport, EXTRA, THINKING, summarize)
from audit_source_retention_results import canonical
from oracle_v4_experiment import evidence_audit
from oracle_reader_pair import audit_run_context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    plan = read_json(output / 'frozen-plan.json')
    assert read_json(output / 'full-completion.json')['complete']
    recovery = read_json(output / 'resume-plan-001.json')
    assert digest(ROOT / 'audit_tools/resume_prepared_source_full.py') == recovery['execution_driver_sha256']
    for q, h in recovery['saved_trace_sha256'].items():
        assert digest(output / 'full/traces' / trace_filename(q)) == h
    assert code_snapshot(ROOT) == plan['source_snapshot']
    for q, h in plan['parent_trace_sha256'].items():
        assert digest(PARENT / 'traces' / trace_filename(q)) == h
    assert digest(PARENT / 'full-verdicts.jsonl') == plan['parent_verdicts_sha256']
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    _, parent = _load_run(PARENT)
    _, candidate = _load_run(output / 'offline-full-candidate')
    _, screen = _load_run(output / 'screen')
    _, actual = _load_run(output / 'full')
    assert set(actual) == set(dataset.question_ids)
    ledgers = budget_ledgers(output)
    hashes = {str(p): digest(p) for p in ledgers if p.exists()}
    normal, thinking = CacheTransport(EXTRA), CacheTransport(THINKING)
    meter = MeteredClient(normal, output / 'full-audit-cache', max_calls=0, max_tokens=12000000,
                         reuse_roots=[p.parent for p in ledgers])
    reader = make_reader(meter, normal, thinking)
    checks = 0
    for case in dataset.cases:
        q = case.question_id
        if q in recovery['preserved_failure_ids']:
            expected = read_json(output / 'interruption-001/failed-trace.json')
            assert expected['question_id'] == q and expected['reader']['status'] in {'ERROR', 'TIMEOUT'}
            assert digest(output / 'interruption-001/failed-trace.json') == recovery['saved_trace_sha256'][q]
        elif q in screen:
            assert same_input(restore_visible_request(case.question, candidate[q]),
                              restore_visible_request(case.question, screen[q]))
            expected = screen[q]
        else:
            meter.phase = 'reader-audit-full'
            expected = generate_record(case, candidate[q], parent[q], reader)
        assert canonical(expected['reader']) == canonical(actual[q]['reader']), q
        for key in ('hypothesis', 'hypothesis_origin', 'status', 'failure', 'calls', 'tokens', 'replay'):
            assert expected[key] == actual[q][key], (q, key)
        checks += audit_citations(actual[q])
    evidence_audit(candidate, actual)
    context = audit_run_context(output / 'full')
    assert context['auditable_cases'] == 500 and not context['reader_context_heuristic_tokens']['over_budget_cases']
    audit_submission([json.loads(s) for s in (output / 'full/submission.jsonl').read_text(encoding='utf-8').splitlines()], actual)
    header, values = _load_verdicts(output / 'full-verdicts.jsonl', actual, dataset.sha256)
    old_header, old_values = _load_verdicts(PARENT / 'full-verdicts.jsonl', parent, dataset.sha256)
    assert header == old_header and set(values) == set(dataset.question_ids)
    for q in values:
        if actual[q]['hypothesis'] == parent[q]['hypothesis'] and actual[q]['reader']['status'] == parent[q]['reader']['status']:
            assert values[q] == old_values[q]
    assert all(values[q] is False for q in recovery['preserved_failure_ids'])
    report = summarize(dataset.cases, actual, values, exposed_ids=plan['screen_ids'])
    assert report['overall'] == read_json(output / 'full-evaluation.json')['overall']
    assert hashes == {str(p): digest(p) for p in ledgers if p.exists()}
    atomic_write_json(output / 'full-post-run-audit.json', {'audit_passed': True, 'cases': 500,
        'citation_checks': checks, 'new_api_calls': 0, 'cache_hits': meter.hits,
        'cost_ledgers_unchanged': True, 'source_and_dependencies_match': True,
        'preserved_failure_ids_scored_false': recovery['preserved_failure_ids'],
        'completed_prefix_unchanged': len(recovery['saved_trace_sha256']),
        'context_audit': context, 'audit_driver_sha256': digest(Path(__file__))})
    print(json.dumps({'audit_passed': True, 'cases': 500, 'correct': sum(values.values()), 'new_api_calls': 0}))


if __name__ == '__main__':
    main()
