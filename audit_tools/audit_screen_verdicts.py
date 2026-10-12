"""Replay every new screen verdict from exact request caches, with no network."""
import argparse
from collections import Counter
from decimal import Decimal
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]


def audit(module_name):
    e = importlib.import_module(module_name)
    out = e.DEFAULT_OUTPUT
    plan = e.read_json(out / 'frozen-plan.json')
    assert e.read_json(out / 'post-run-audit.json')['audit_passed']
    assert e.code_snapshot(ROOT) == plan['source_snapshot']
    dataset = e.load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    assert dataset.sha256 == plan['dataset_sha256']
    _, parent = e._load_run(e.PARENT)
    _, traces = e._load_run(out / 'screen')
    header, verdicts = e._load_verdicts(out / 'screen-verdicts.jsonl', traces, dataset.sha256)
    parent_header, old = e._load_verdicts(e.PARENT_VERDICTS, parent, dataset.sha256)
    assert header == parent_header
    rows = [json.loads(line) for line in (out / 'screen-verdicts.jsonl').read_text(encoding='utf-8').splitlines()]
    rows = {r['question_id']: r for r in rows if not r.get('_header')}
    assert set(rows) == set(plan['screen_ids']) == set(traces)
    ledgers = e.budget_ledgers(out)
    before = {str(p): e.digest(p) for p in ledgers if p.exists()}
    meter = e.MeteredClient(e.CacheTransport(e.EXTRA), out / 'judge-audit-cache', max_calls=0,
                           max_tokens=3000000, reuse_roots=[p.parent for p in ledgers])
    judge = e.LLMJudge(meter, e.JudgeSpec(**header['judge']))
    origins, transitions, details = Counter(), Counter(), []
    for q in plan['screen_ids']:
        case, t, row = dataset.by_id()[q], traces[q], rows[q]
        if t['hypothesis'] == parent[q]['hypothesis'] and t['reader']['status'] == parent[q]['reader']['status']:
            assert row['raw'] == 'EXACT_PARENT_VERDICT' and row['correct'] == old[q]
            origins['exact_parent_verdict'] += 1
        else:
            v = e.score_trace(judge, case, t)
            assert (v.correct, v.raw, v.error) == (row['correct'], row['raw'], row['error']), q
            origins['exact_cached_or_deterministic_verdict'] += 1
        kind = ('both_correct' if verdicts[q] else 'regression') if old[q] else ('improvement' if verdicts[q] else 'both_wrong')
        transitions[kind] += 1
        if kind in {'improvement', 'regression'}:
            details.append({'question_id': q, 'transition': kind, 'question_type': case.question_type,
                'question': case.question, 'reference_answer': case.answer,
                'parent_hypothesis': parent[q]['hypothesis'], 'candidate_hypothesis': t['hypothesis']})
    assert before == {str(p): e.digest(p) for p in ledgers if p.exists()}
    own = e.read_json(out / 'api-cache/ledger.json')['calls']
    assert all(r['status'] == 'completed' for r in own)
    cost = e.peak_cost(ledgers)
    report = {'audit_passed': True, 'cases': len(traces), 'new_audit_api_calls': 0,
        'verdict_provenance': dict(origins), 'judge_cache_hits': meter.hits,
        'transitions': dict(transitions), 'new_requests': len(own),
        'new_requests_by_phase': dict(Counter(r['phase'] for r in own)),
        'new_peak_cost_cny': str(Decimal(cost['peak_upper_cny']) - Decimal(plan['initial_cost']['peak_upper_cny'])),
        'aggregate_cost': cost, 'ledgers_unchanged': True, 'audit_driver_sha256': e.digest(Path(__file__))}
    e.atomic_write_json(out / 'verdict-provenance-audit.json', report)
    e.atomic_write_json(out / 'transition-review.json', {'evaluation_only': True,
        'runtime_must_not_read_this_file': True, 'changed_correctness': details})
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    audit(parser.parse_args().experiment)
