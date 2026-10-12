"""Execute an audited source-candidate full plan within the shared authorization."""
import argparse
from copy import deepcopy
from decimal import Decimal
import getpass
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
from oracle_spare_document_experiment import (
    PARENT_VERDICTS,
    PARENT, read_json, _load_run, _load_verdicts, _accuracy, load_oracle_dataset,
    code_snapshot, atomic_write_json, atomic_write_text, trace_filename,
    restore_visible_request, same_input, digest, budget_ledgers, peak_cost,
    make_reader, generate_record, audit_citations, audit_submission,
    MeteredClient, CurrencyBudget, TotalBudgetGuard, MODEL, ENDPOINT, EXTRA,
    THINKING, OpenAICompatibleChatClient, private_get, PilotStopped,
    LLMJudge, JudgeSpec, score_trace, save_verdicts, summarize, validate_submission,
    CANONICAL_ABSTENTION_HYPOTHESIS)
from oracle_v4_experiment import evidence_audit


def verdict_rows(path):
    if not path.exists():
        return {}
    return {r['question_id']: r for r in (json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()) if not r.get('_header')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--paid', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    plan = read_json(output / 'frozen-plan.json')
    proposal = read_json(output / 'prepared-full-plan.json')
    if digest(Path(__file__)) != proposal['execution_driver_sha256']:
        raise ValueError('Prepared execution driver changed')
    if code_snapshot(ROOT) != plan['source_snapshot'] or not read_json(output / 'post-run-audit.json')['audit_passed']:
        raise ValueError('Screened source/dependencies or audit changed')
    if not read_json(output / 'selection.json')['eligible_for_full']:
        raise ValueError('Screen did not pass')
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    dataset.require_full_benchmark()
    assert dataset.sha256 == plan['dataset_sha256']
    manifest, parent = _load_run(PARENT)
    _, candidate = _load_run(output / 'offline-full-candidate')
    _, screened = _load_run(output / 'screen')
    assert set(candidate) == set(dataset.question_ids)
    for folder, hashes in [('offline-full-candidate', proposal['candidate_trace_sha256']),
                           ('screen', proposal['screen_trace_sha256'])]:
        for q, h in hashes.items():
            assert digest(output / folder / 'traces' / trace_filename(q)) == h
    for q, h in plan['parent_trace_sha256'].items():
        assert digest(PARENT / 'traces' / trace_filename(q)) == h
    assert digest(PARENT_VERDICTS) == plan['parent_verdicts_sha256']
    assert digest(output / 'screen-verdicts.jsonl') == proposal['screen_verdicts_sha256']
    ledgers = budget_ledgers(output)
    current = peak_cost(ledgers)
    rules = read_json(ROOT / 'eval/reports/oracle-road-to-90-budget.json')
    limit = Decimal(rules['planning_stop_cny'])
    assert limit <= Decimal(rules['user_limit_cny']) - Decimal(rules['settlement_reserve_cny'])
    # The original full estimate is deliberately conservative on resume. A
    # interrupted paid run needs a separate reviewed remaining-work estimate.
    fits = Decimal(current['peak_upper_cny']) + Decimal(proposal['planning_allowance_cny']) < limit
    print(json.dumps({'full_run': 'READY' if fits else 'BUDGET_BLOCKED', 'spent': current,
        'allowance_cny': proposal['planning_allowance_cny'], 'authorized_limit_cny': rules['user_limit_cny']}), flush=True)
    if not args.paid:
        return
    if not fits:
        raise PilotStopped('Prepared full estimate exceeds shared planning authorization; no paid request sent')
    public = read_json(ROOT / 'eval/reports/oracle-statement-provenance-20261009/public-dataset-provenance.json')
    assert public['local_sha256'] == public['public_lfs_sha256'] == dataset.sha256
    assert public['model_destination'] == ENDPOINT
    key = os.environ.get('DIVE_READER_API_KEY') or getpass.getpass('DeepSeek key (hidden, never saved): ')
    if not key.strip() or MODEL not in [r['id'] for r in private_get('models', key).get('data', [])]:
        raise PilotStopped('Missing credential or unavailable model')
    guard = TotalBudgetGuard(key, output, limit=limit - Decimal(current['peak_upper_cny']))
    normal = OpenAICompatibleChatClient(ENDPOINT, key, provider='deepseek', extra_body=EXTRA)
    thinking = OpenAICompatibleChatClient(ENDPOINT, key, provider='deepseek', extra_body=THINKING)
    client = MeteredClient(normal, output / 'api-cache', max_calls=1800, max_tokens=12000000,
        reuse_roots=[p.parent for p in ledgers if p.parent != output / 'api-cache'],
        before_request=guard.check, currency_guard=CurrencyBudget(ledgers, limit=limit))
    out = output / 'full'
    reader = make_reader(client, normal, thinking)
    traces = {}
    try:
        for n, case in enumerate(dataset.cases, 1):
            q = case.question_id
            path = out / 'traces' / trace_filename(q)
            if path.exists():
                record = read_json(path)
            elif q in screened:
                assert same_input(restore_visible_request(case.question, candidate[q]),
                                  restore_visible_request(case.question, screened[q]))
                record = deepcopy(screened[q])
            else:
                client.phase = 'reader-full'
                record = generate_record(case, candidate[q], parent[q], reader)
            evidence_audit({q: candidate[q]}, {q: record})
            audit_citations(record)
            atomic_write_json(path, record)
            if record['reader']['status'] in {'ERROR', 'TIMEOUT'} and not record.get('replay', {}).get('exact_parent_input_reused'):
                raise PilotStopped('Saved transport/budget failure; stop for audit without resampling')
            traces[q] = record
            if n % 20 == 0:
                print(json.dumps({'generated': n, 'total': 500}), flush=True)
        atomic_write_json(out / 'manifest.json', {'schema_version': 'dive-source-candidate-full-v1',
            'dataset': manifest['dataset'], 'configuration': {'oracle_pipeline': plan['config']},
            'source_snapshot': plan['source_snapshot'], 'execution_driver_sha256': digest(Path(__file__)),
            'execution_mode': 'exact_parent_and_screen_reuse_plus_new_candidate_inputs'})
        submission = [{'question_id': q, 'hypothesis': t['hypothesis'] if t['hypothesis'] is not None
                        else CANONICAL_ABSTENTION_HYPOTHESIS} for q, t in traces.items()]
        audit_submission(submission, traces)
        atomic_write_text(out / 'submission.jsonl', ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in submission))
        validation = validate_submission(out / 'submission.jsonl', expected_ids=dataset.question_ids, require_full=True)
        assert validation.status == 'VALID'
        atomic_write_json(output / 'full-submission-validation.json', validation.to_dict())
        header, parent_v = _load_verdicts(PARENT_VERDICTS, parent, dataset.sha256)
        judge = LLMJudge(client, JudgeSpec(**header['judge']))
        verdict_path = output / 'full-verdicts.jsonl'
        cached = {**verdict_rows(output / 'screen-verdicts.jsonl'), **verdict_rows(verdict_path)}
        rows = []
        for c in dataset.cases:
            q = c.question_id
            t = traces[q]
            h = sha256(('null' if t['hypothesis'] is None else t['hypothesis']).encode()).hexdigest()[:16]
            if t['hypothesis'] == parent[q]['hypothesis'] and t['reader']['status'] == parent[q]['reader']['status']:
                row = {'question_id': q, 'hypothesis_sha256': h, 'correct': parent_v[q], 'raw': 'EXACT_PARENT_VERDICT', 'error': None}
            elif q in cached:
                row = cached[q]
                assert row['hypothesis_sha256'] == h and type(row['correct']) is bool
            else:
                client.phase, client.client = 'judge-full', normal
                v = score_trace(judge, c, t)
                row = {'question_id': q, 'hypothesis_sha256': h, 'correct': v.correct, 'raw': v.raw, 'error': v.error}
            rows.append(row)
            save_verdicts(verdict_path, header, rows)
        _, verdicts = _load_verdicts(verdict_path, traces, dataset.sha256)
        report = summarize(dataset.cases, traces, verdicts, exposed_ids=plan['screen_ids'])
        assert report['overall']['fully_judged'] and code_snapshot(ROOT) == plan['source_snapshot']
        atomic_write_json(output / 'full-evaluation.json', report)
        atomic_write_json(output / 'full-comparison.json', _accuracy(parent, traces, parent_v, verdicts,
            {c.question_id: c.question_type for c in dataset.cases}))
        guard.check(client.summary()['attempts'], force=True)
        atomic_write_json(output / 'full-completion.json', {'complete': True, 'cases': 500,
            'correct': report['overall']['correct'], 'target_reached': report['overall']['correct'] >= 451,
            'official_score': 'NOT_EVALUATED', 'execution_mode': 'exact_parent_and_screen_reuse_plus_new_candidate_inputs'})
        print(json.dumps(report['overall']), flush=True)
    except (Exception, PilotStopped) as exc:
        atomic_write_json(output / 'full-stopped.json', {'status': 'STOPPED', 'error_type': type(exc).__name__,
            'no_complete_score_claimed': True})
        raise
    finally:
        atomic_write_json(output / 'full-usage.json', client.summary())
        atomic_write_json(output / 'aggregate-peak-cost.json', peak_cost(ledgers))


if __name__ == '__main__':
    main()
