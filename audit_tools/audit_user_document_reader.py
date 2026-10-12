"""Independent zero-network replay of R31 reader, citations, judge and cost ledgers."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
import oracle_user_document_experiment as e
from audit_precise_detail_results import canonical


def audit(out, *, full=False):
    label = 'full' if full else 'screen'
    plan = e.check_frozen(out)
    dataset = e.load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    assert dataset.sha256 == plan['dataset_sha256']
    ids = plan['all_ids'] if full else plan['screen_ids']
    _, r26 = e._load_run(e.R26)
    _, r27 = e._load_run(e.R27 / 'screen')
    _, sources = e._load_run(out / 'offline-full') if full else e._load_run(out/'offline-candidate')
    if not full:
        for q,h in e.read_json(out/'offline-summary.json')['offline_trace_sha256'].items():
            assert e.digest(out/'offline-candidate/traces'/e.trace_filename(q)) == h
    _, records = e._load_run(out / label)
    assert set(records) == set(ids)
    if full:
        ep = e.read_json(out / 'full-evidence-plan.json')
        assert set(ep['trace_sha256']) == set(ids)
        for q, h in ep['trace_sha256'].items():
            assert e.digest(out / 'offline-full/traces' / e.trace_filename(q)) == h
    ledgers = e.budget_ledgers(out)
    before = {str(p):e.digest(p) for p in ledgers if p.exists()}
    meter = e.MeteredClient(e.CacheTransport(e.EXTRA), out / (label + '-audit-cache'),
        max_calls=0, max_tokens=15000000, reuse_roots=[p.parent for p in ledgers])
    reader = e.make_reader(meter)
    citations = 0
    for q in ids:
        replay = e.make_record(dataset.by_id()[q].question, sources[q], reader)
        assert canonical(replay['reader']) == canonical(records[q]['reader']), q
        for k in ('hypothesis', 'hypothesis_origin', 'status', 'failure', 'calls', 'tokens', 'replay'):
            assert replay[k] == records[q][k], (q, k)
        assert replay['reader_context_text'] == sources[q]['reader_context_text']
        citations += e.audit_citations(records[q])
    manifest = e.read_json(out / label / 'manifest.json')
    assert manifest['source_snapshot'] == plan['source_snapshot']
    assert manifest['reader_spec'] == plan['reader_spec']
    assert manifest['configuration'] == plan['config']
    submission = [json.loads(l) for l in (out / label / 'submission.jsonl').read_text(encoding='utf-8').splitlines()]
    e.audit_submission(submission, records)
    assert e.validate_submission(out / label / 'submission.jsonl', expected_ids=ids, require_full=full).status == 'VALID'
    header, verdicts = e._load_verdicts(out / (label + '-verdicts.jsonl'), records, dataset.sha256)
    h26, v26 = e._load_verdicts(e.R26_VERDICTS, r26, dataset.sha256)
    h27, v27 = e._load_verdicts(e.R27 / 'screen-verdicts.jsonl', r27, dataset.sha256)
    assert header == h26 == h27
    rows = {r['question_id']:r for r in map(json.loads,
        (out / (label + '-verdicts.jsonl')).read_text(encoding='utf-8').splitlines()) if not r.get('_header')}
    assert set(rows) == set(ids)
    meter.client = meter.normal_transport
    judge = e.LLMJudge(meter, e.JudgeSpec(**header['judge']))
    origins, details, transitions = Counter(), [], Counter()
    for q in ids:
        parent, pv = (r27[q], v27[q]) if q in r27 else (r26[q], v26[q])
        t, row, c = records[q], rows[q], dataset.by_id()[q]
        if t['hypothesis'] == parent['hypothesis'] and t['reader']['status'] == parent['reader']['status']:
            assert row['raw'] == 'EXACT_PARENT_VERDICT' and row['correct'] == pv
            origins['exact_parent_verdict'] += 1
        else:
            v = e.score_trace(judge, c, t)
            assert (v.correct, v.raw, v.error) == (row['correct'], row['raw'], row['error']), q
            origins['cached_or_deterministic_verdict'] += 1
        transition = ('both_correct' if verdicts[q] else 'regression') if pv else ('improvement' if verdicts[q] else 'both_wrong')
        transitions[transition] += 1
        if not verdicts[q] or transition == 'improvement':
            details.append({'question_id':q, 'question_type':c.question_type, 'question':c.question,
                'reference_answer':c.answer, 'parent_hypothesis':parent['hypothesis'],
                'candidate_hypothesis':t['hypothesis'], 'status':t['reader']['status'],
                'transition_from_r27_screen_otherwise_r26':transition})
    cases = [dataset.by_id()[q] for q in ids]
    score = e.summarize(cases, records, verdicts, exposed_ids=plan['screen_ids'])
    assert score == e.read_json(out / (label + '-evaluation.json'))
    completion = e.read_json(out / (label + '-completion.json'))
    assert completion['complete'] and completion['fully_judged'] and completion['correct'] == sum(verdicts.values())
    if not full:
        assert e.eligible(e.summarize(cases, r26, v26), score) == e.read_json(out / 'selection.json')['eligible_for_full']
    assert before == {str(p):e.digest(p) for p in ledgers if p.exists()}
    assert not (out / (label + '-audit-cache/ledger.json')).exists()
    own = e.read_json(out / 'api-cache/ledger.json')['calls']
    assert all(r['status'] == 'completed' for r in own)
    cost = e.peak_cost(ledgers)
    report = {'audit_passed':True, 'cases':len(ids), 'citations':citations, 'new_api_calls':0,
        'reader_and_judge_cache_hits':meter.hits, 'verdict_provenance':dict(origins),
        'transitions_from_r27_screen_otherwise_r26':dict(transitions),
        'new_requests':len(own), 'new_requests_by_phase':dict(Counter(r['phase'] for r in own)),
        'new_peak_cost_cny':str(Decimal(cost['peak_upper_cny'])-Decimal(plan['initial_cost']['peak_upper_cny'])),
        'aggregate_cost':cost, 'budget_ledgers_unchanged':True,
        'audit_driver_sha256':e.digest(Path(__file__)), 'score':score['overall']}
    e.save_once(out / (label + '-post-run-audit.json'), report)
    e.save_once(out / (label + '-error-review.json'), {'evaluation_only':True,
        'runtime_must_not_read_this_file':True, 'details':details})
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, default=e.OUTPUT)
    ap.add_argument('--full', action='store_true')
    args = ap.parse_args()
    audit(args.output.resolve(), full=args.full)
