"""Resume R31 with its audited interruption retained as a scored failure."""
import contextual_recovery
from oracle_contextual_reader_experiment import *
from oracle_contextual_reader_experiment import _load_run, _load_verdicts, _accuracy
from contextual_recovery import *

def run_stage(out, dataset, manifest, r26, r27, plan, sources, client, *, full=False):
    recovery = verify(out)
    label = 'full' if full else 'screen'
    ids = plan['all_ids'] if full else plan['screen_ids']
    reader = make_reader(client)
    records = {}
    for n, q in enumerate(ids, 1):
        path = out / label / 'traces' / trace_filename(q)
        screening = out / 'screen/traces' / trace_filename(q)
        if path.exists():
            record = read_json(path)
        elif full and screening.exists():
            record = read_json(screening)
        else:
            client.phase = 'reader-' + label
            record = make_record(dataset.by_id()[q].question, sources[q], reader)
        if record['reader']['spec'] != plan['reader_spec']:
            raise ValueError('Checkpoint reader mismatch')
        if record['reader_context_text'] != sources[q]['reader_context_text']:
            raise ValueError('Checkpoint evidence mismatch')
        previous.evidence_audit({q:sources[q]}, {q:record})
        audit_citations(record)
        save_once(path, record)
        records[q] = record
        if record['reader']['status'] in {'ERROR', 'TIMEOUT'} and q != recovery['failure_id']:
            raise PilotStopped('Transport failure checkpoint retained; no automatic resampling')
        if n % 10 == 0:
            print(json.dumps({'phase': label, 'generated': n, 'total': len(ids)}), flush=True)
    save_once(out / label / 'manifest.json', {'schema_version': 'dive-contextual-reader-v1',
        'dataset': manifest['dataset'], 'configuration': plan['config'],
        'source_snapshot': plan['source_snapshot'], 'reader_spec': plan['reader_spec']})
    submission = [{'question_id': q, 'hypothesis': records[q]['hypothesis'] or CANONICAL_ABSTENTION_HYPOTHESIS} for q in ids]
    audit_submission(submission, records)
    atomic_write_text(out / label / 'submission.jsonl', ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in submission))
    if validate_submission(out / label / 'submission.jsonl', expected_ids=ids, require_full=full).status != 'VALID':
        raise ValueError('Invalid submission')
    # Correctness is loaded only after all candidate outputs are fixed.
    header, v26 = _load_verdicts(R26_VERDICTS, r26, dataset.sha256)
    h27, v27 = _load_verdicts(R27 / 'screen-verdicts.jsonl', r27, dataset.sha256)
    if h27 != header:
        raise ValueError('Judge changed')
    client.client = client.normal_transport
    judge = LLMJudge(client, JudgeSpec(**header['judge']))
    verdict_path = out / (label + '-verdicts.jsonl')
    checkpoints = {}
    paths = [verdict_path] + ([out / 'screen-verdicts.jsonl'] if full else [])
    for p in paths:
        allowed = plan['screen_ids'] if p == out / 'screen-verdicts.jsonl' else ids
        for q, row in load_verdict_checkpoints(p, header, allowed, records).items():
            if q in checkpoints and checkpoints[q] != row:
                raise ValueError('Conflicting screening and full verdict checkpoint')
            checkpoints[q] = row
    rows = []
    for q in ids:
        t = records[q]
        h = sha256(('null' if t['hypothesis'] is None else t['hypothesis']).encode()).hexdigest()[:16]
        parent = r27[q] if q in r27 else r26[q]
        pv = v27[q] if q in r27 else v26[q]
        if t['hypothesis'] == parent['hypothesis'] and t['reader']['status'] == parent['reader']['status']:
            row = {'question_id': q, 'hypothesis_sha256': h, 'correct': pv, 'raw': 'EXACT_PARENT_VERDICT', 'error': None}
        elif q in checkpoints:
            row = checkpoints[q]
            if row['hypothesis_sha256'] != h or type(row['correct']) is not bool:
                raise ValueError('Verdict checkpoint mismatch')
        else:
            client.phase = 'judge-' + label
            verdict = score_trace(judge, dataset.by_id()[q], t)
            row = {'question_id': q, 'hypothesis_sha256': h, 'correct': verdict.correct,
                   'raw': verdict.raw, 'error': verdict.error}
        rows.append(row)
        save_verdicts(verdict_path, header, rows)
    _, verdicts = _load_verdicts(verdict_path, records, dataset.sha256)
    cases = [dataset.by_id()[q] for q in ids]
    before = summarize(cases, r26, v26)
    after = summarize(cases, records, verdicts, exposed_ids=plan['screen_ids'])
    save_once(out / (label + '-evaluation.json'), after)
    save_once(out / (label + '-comparison.json'), _accuracy({q:r26[q] for q in ids}, records, v26,
        verdicts, {c.question_id:c.question_type for c in cases}))
    if not full:
        save_once(out / 'screen-vs-r27-comparison.json', _accuracy(r27, records, v27, verdicts,
            {c.question_id:c.question_type for c in cases}))
        save_once(out / 'selection.json', {'eligible_for_full': eligible(before, after),
            'rule': plan['selection'], 'full_run': 'NOT_RUN'})
    check_frozen(out)
    save_once(out / (label + '-completion.json'), {'complete': True, 'cases': len(ids),
        'correct': after['overall']['correct'], 'official_score': 'NOT_EVALUATED',
        'execution_mode': plan['execution_mode'], 'fully_judged': after['overall']['fully_judged']})
    print(json.dumps({'stage': label, 'overall': after['overall'], 'answerable': after['answerable'],
        'unanswerable': after['unanswerable'], 'eligible_for_full': eligible(before, after) if not full else None}), flush=True)


if __name__ == '__main__':
    recovery = verify(OUTPUT)
    NoResendClient.interrupted_key = recovery['interrupted_request']['key']
    e.OUTPUT = OUTPUT
    e.MeteredClient = NoResendClient
    e.run_stage = run_stage
    e.main()
