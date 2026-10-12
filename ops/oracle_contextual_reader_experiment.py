"""R31: contextual interpretation on the R27 adjudicated reader; offline preparation by default."""
import argparse
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import getpass
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
import oracle_precise_detail_experiment as previous
from oracle_premise_review_experiment import restore_visible_request, save_verdicts
from oracle_precise_detail_experiment import (
    read_json, _load_run, _load_verdicts, summarize, score_trace, _accuracy,
    budget_ledgers, peak_cost, code_snapshot, atomic_write_json, atomic_write_text,
    audit_citations, audit_submission, trace_filename, load_oracle_dataset,
    validate_submission, LLMJudge, JudgeSpec, eligible,
    MeteredClient, PilotStopped, MODEL, ENDPOINT, EXTRA, private_get,
    CurrencyBudget, TotalBudgetGuard, OpenAICompatibleChatClient, CacheTransport)
from dive_memory.oracle.adjudicated_reader import combine
from dive_memory.oracle.reader import ReaderSpec
from dive_memory.oracle.contracts import CANONICAL_ABSTENTION_HYPOTHESIS
from dive_memory.token_budget import ApproximateTokenCounter

OUTPUT = ROOT / 'eval/reports/oracle-contextual-reader-20261010'
R27 = previous.DEFAULT_OUTPUT
R26 = previous.PARENT
R26_VERDICTS = previous.PARENT_VERDICTS
OUTPUT_CAP = 8192
from oracle_refusal_gated_experiment import ModeClient, THINKING


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def save_once(path, payload):
    if path.exists():
        if read_json(path) != payload:
            raise ValueError('Immutable artifact differs: ' + path.name)
    else:
        atomic_write_json(path, payload)


def make_reader(client):
    from dive_memory.oracle.indexed_reader import IndexedEvidenceReader
    from dive_memory.oracle.adjudicated_reader import AdjudicatedReader
    from dive_memory.oracle.contextual_reader import ContextualEvidenceReader, ContextualEvidenceReferee, VERSION, REFEREE_VERSION
    normal = getattr(client, 'normal_transport', getattr(client, 'client', None))
    thinking = getattr(client, 'thinking_transport', CacheTransport(THINKING)) if client else None
    if client is not None:
        client.normal_transport, client.thinking_transport = normal, thinking
    policy = previous.CANDIDATE.policy()
    primary = IndexedEvidenceReader(ModeClient(client, normal), ReaderSpec('deepseek', MODEL,
        prompt_version=policy.prompt_version, max_tokens=512), policy=policy)
    secondary = ContextualEvidenceReader(ModeClient(client, thinking), ReaderSpec('deepseek', MODEL,
        prompt_version=VERSION, max_tokens=OUTPUT_CAP, timeout_seconds=120), policy=policy)
    referee = ContextualEvidenceReferee(ModeClient(client, normal), ReaderSpec('deepseek', MODEL,
        prompt_version=REFEREE_VERSION, max_tokens=1024), policy=policy)
    reader = AdjudicatedReader(primary, secondary, referee, recover_invalid_secondary=True)
    reader.rules = secondary.rules
    return reader


def check_frozen(out):
    plan = read_json(out / 'frozen-plan.json')
    if code_snapshot(ROOT) != plan['source_snapshot']:
        raise ValueError('Frozen source/dependency mismatch')
    for name, h in plan['helper_sha256'].items():
        if digest(ROOT / 'audit_tools' / name) != h:
            raise ValueError('Frozen audit helper changed')
    for label, directory in [('r26', R26), ('r27', R27 / 'screen')]:
        for q, h in plan[label + '_trace_sha256'].items():
            if digest(directory / 'traces' / trace_filename(q)) != h:
                raise ValueError('Frozen parent trace changed')
    if digest(R26_VERDICTS) != plan['r26_verdicts_sha256']:
        raise ValueError('R26 verdict changed')
    if digest(R27 / 'screen-verdicts.jsonl') != plan['r27_verdicts_sha256']:
        raise ValueError('R27 verdict changed')
    return plan


def prepare(out):
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    dataset.require_full_benchmark()
    m26, r26 = _load_run(R26)
    m27, r27 = _load_run(R27 / 'screen')
    ids = read_json(ROOT / 'eval/reports/oracle-reasoning-20260929/frozen-plan.json')['ids']
    if set(r26) != set(dataset.question_ids) or set(r27) != set(ids):
        raise ValueError('Incomplete fixed parent data')
    if m26['dataset']['sha256'] != dataset.sha256 or m27['dataset']['sha256'] != dataset.sha256:
        raise ValueError('Parent dataset mismatch')
    if len(ids) != 80 or sum(dataset.by_id()[q].should_abstain for q in ids) != 30:
        raise ValueError('Fixed screening selection changed')
    rules = read_json(ROOT / 'eval/reports/oracle-road-to-90-budget.json')
    existing = read_json(out / 'frozen-plan.json') if (out / 'frozen-plan.json').exists() else None
    spent = existing['initial_cost'] if existing else peak_cost(budget_ledgers(out))
    plan = {'dataset_sha256': dataset.sha256, 'source_snapshot': code_snapshot(ROOT),
        'helper_sha256': {name:digest(ROOT/'audit_tools'/name) for name in
            ('audit_contextual_reader.py', 'audit_adjudicated_result.py',
             'audit_precise_detail_results.py', 'replay_format_recovery.py')},
        'screen_ids': ids, 'all_ids': list(dataset.question_ids),
        'r26_trace_sha256': {q: digest(R26 / 'traces' / trace_filename(q)) for q in r26},
        'r27_trace_sha256': {q: digest(R27 / 'screen/traces' / trace_filename(q)) for q in ids},
        'r26_verdicts_sha256': digest(R26_VERDICTS),
        'r27_verdicts_sha256': digest(R27 / 'screen-verdicts.jsonl'),
        'reader_spec': make_reader(None).spec.to_dict(), 'extra_body': EXTRA, 'primary_extra_body': EXTRA, 'secondary_extra_body': THINKING, 'judge_extra_body': EXTRA,
        'config': previous.CANDIDATE.to_dict(),
        'selection': '+3/50 answerable against R26; no loss on all 30 unanswerable',
        'routing': 'R27 primary; contextual thinking secondary; contextual independent referee on answerability disagreement; unchanged format recovery; no correctness/type/ID routing',
        'execution_mode': 'fixed_R27_evidence_new_reader_with_exact_request_cache',
        'screen_status': 'reused public development selection, not blind holdout',
        'aggregate_budget': existing['aggregate_budget'] if existing else rules,
        'initial_cost': spent}
    for q in ids:
        restore_visible_request(dataset.by_id()[q].question, r27[q])
        audit_citations(r27[q])
    save_once(out / 'frozen-plan.json', plan)
    check_frozen(out)
    for rel in plan['source_snapshot']['files']:
        target = out / 'source-snapshot' / rel
        content = (ROOT / rel).read_bytes()
        if target.exists() and target.read_bytes() != content:
            raise ValueError('Snapshot copy mismatch')
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)
    for name, h in plan['helper_sha256'].items():
        source, target = ROOT/'audit_tools'/name, out/'source-snapshot/audit_tools'/name
        if digest(source) != h:
            raise ValueError('Helper changed while freezing')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and digest(target) != h:
            raise ValueError('Helper snapshot copy mismatch')
        if not target.exists():
            target.write_bytes(source.read_bytes())
    save_once(out / 'request-recovery-audit.json', {'cases': 80, 'exact_context_matches': 80,
        'context_budget': 6000, 'new_api_calls': 0, 'no_new_history': True,
        'max_context_tokens': max(ApproximateTokenCounter().count(t['reader_context_text']) for t in r27.values())})
    return dataset, m26, r26, r27, plan


def estimate_cost(traces, *, judge_cases=None, evidence=None):
    """Observed parent thinking stages; no gold labels influence cost or execution."""
    from dive_memory.oracle.indexed_reader import RULES as OLD_RULES
    extra_bytes = max(0, len(make_reader(None).rules.encode()) - len(OLD_RULES.encode()))
    predicted, primary_new, inputs_total = Decimal(0), 0, 0
    for q, t in traces.items():
        raw = t.get('reader', {}).get('detail')
        try:
            stages = json.loads(raw).get('stages', []) if raw else []
        except (TypeError, ValueError, AttributeError):
            stages = []
        secondary = stages[1] if len(stages) > 1 else {}
        first = stages[0] if stages else {}
        old_context = t['reader_context_text']
        new_context = evidence[q]['reader_context_text'] if evidence is not None else old_context
        delta = max(0, len(new_context.encode()) - len(old_context.encode()))
        usage = secondary.get('usage', {})
        i, o = usage.get('input_tokens'), usage.get('output_tokens')
        if type(i) is not int or i < 0:
            i = len(new_context.encode()) + 2048
        else:
            i += delta
        if type(o) is not int or o < 0:
            o = OUTPUT_CAP
        i += extra_bytes
        inputs_total += i
        # A 10-percent referee allowance plus a 25-percent total margin covers
        # expected variation; per-request reservations enforce the hard boundary.
        normal_i = first.get('usage', {}).get('input_tokens')
        if type(normal_i) is not int or normal_i < 0:
            normal_i = len(new_context.encode()) + 2048
        else:
            normal_i += delta
        predicted += Decimal(2*i + 8*o) / 1000000
        predicted += Decimal('0.10') * Decimal(2*(normal_i+extra_bytes)+8*1024) / 1000000
        if evidence is not None and (old_context != new_context or t['as_of'] != evidence[q]['as_of']):
            primary_new += 1
            predicted += Decimal(2*normal_i + 8*512) / 1000000
    if judge_cases is None:
        judge_cases = len(traces)
    predicted += Decimal(3024*judge_cases) / 1000000
    return {'cases':len(traces), 'pending_judge_cases':judge_cases, 'primary_new_input_cases':primary_new,
        'projected_first_input_tokens':inputs_total,
        'predicted_reader_and_judge_cny':str(predicted),
        'planning_allowance_cny':str(predicted*Decimal('1.25')),
        'method':'observed parent thinking input/output; added prompt bytes; 10% independent referee allowance; changed-input primary; pending judges; 25% margin',
        'caveat':'Expected usage, not a worst-case completion guarantee. Exact primary cache reuse is audited; per-request reservations and cumulative hard ceiling remain enforced.'}


def calibrated_full_cost(out, prediction):
    # R30 single-pass ledger token statistics do not describe this thinking stack.
    return prediction


def audit_primary_cache(out, sources):
    """Run exact primary requests through a zero-call meter before crediting reuse."""
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    ledgers = budget_ledgers(out)
    before = {str(p):digest(p) for p in ledgers if p.exists()}
    meter = MeteredClient(CacheTransport(EXTRA), out/'primary-audit-cache', max_calls=0,
        max_tokens=15000000, reuse_roots=[p.parent for p in ledgers])
    reader = make_reader(meter).primary
    hits, misses = [], []
    for q, source in sources.items():
        try:
            reader.generate(restore_visible_request(dataset.by_id()[q].question, source))
        except PilotStopped:
            misses.append(q)
        else:
            hits.append(q)
    if before != {str(p):digest(p) for p in ledgers if p.exists()} or meter.summary()['attempts']:
        raise ValueError('Cache audit changed a request ledger')
    return {'exact_cached_case_ids':hits, 'uncached_case_ids':misses,
            'cache_hits':meter.hits, 'new_api_calls':0}


def preflight(out, sources, *, full=False):
    label = 'full' if full else 'screen'
    traces = {q: t for q, t in sources.items()
              if not (out / label / 'traces' / trace_filename(q)).exists()
              and not (full and (out / 'screen/traces' / trace_filename(q)).exists())}
    # Offline full traces contain no observed reader usage; use the matched R26 context as cost proxy.
    if full:
        _, r26 = _load_run(R26)
        traces = {q: r26[q] for q in traces}
    header = json.loads(R26_VERDICTS.read_text(encoding='utf-8').splitlines()[0])
    records = {}
    for q in sources:
        p = out / label / 'traces' / trace_filename(q)
        if full and not p.exists():
            p = out / 'screen/traces' / trace_filename(q)
        if p.exists():
            records[q] = read_json(p)
    verdict_path = out / (label + '-verdicts.jsonl')
    completed_verdicts = set(load_verdict_checkpoints(verdict_path, header, set(sources), records))
    if full:
        completed_verdicts.update(load_verdict_checkpoints(out/'screen-verdicts.jsonl', header,
            set(read_json(out/'frozen-plan.json')['screen_ids']), records))
    # Even after every reader checkpoint exists, an interrupted judge stage still costs money.
    estimate = estimate_cost(traces, judge_cases=len(set(sources) - completed_verdicts), evidence=sources if full else None)
    primary = audit_primary_cache(out, {q:sources[q] for q in traces})
    additional = Decimal(0)
    for q in primary['uncached_case_ids']:
        if full and (traces[q]['reader_context_text'] != sources[q]['reader_context_text']
                     or traces[q]['as_of'] != sources[q]['as_of']):
            continue  # Already reserved by estimate_cost for a changed primary input.
        # No exact primary response exists, so do not grant the predicted cache discount.
        # UTF-8 byte input and both possible attempts are a conservative fallback.
        n = len(sources[q]['reader_context_text'].encode()) + 2048
        additional += Decimal(2*(2*n+8*512)) / 1000000
    predicted = Decimal(estimate['predicted_reader_and_judge_cny']) + additional
    estimate.update(predicted_reader_and_judge_cny=str(predicted),
        planning_allowance_cny=str(predicted*Decimal('1.25')),
        primary_cache_audit=primary, primary_uncached_extra_cny=str(additional))
    if full:
        estimate = calibrated_full_cost(out, estimate)
    rules = read_json(ROOT / 'eval/reports/oracle-road-to-90-budget.json')
    ceiling = min(Decimal(rules['planning_stop_cny']),
                  Decimal(rules['user_limit_cny']) - Decimal(rules['settlement_reserve_cny']))
    current = peak_cost(budget_ledgers(out))
    remaining = ceiling - Decimal(current['peak_upper_cny'])
    result = {**estimate, 'aggregate_cost': current, 'remaining_planning_cny': str(remaining),
        'cumulative_stop_cny': str(ceiling),
        'fits_budget': Decimal(estimate['planning_allowance_cny']) < remaining}
    name = label + '-cost-preflight.json'
    if (out / name).exists() and read_json(out / name) != result:
        name = label + '-cost-preflight-' + sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:12] + '.json'
    save_once(out / name, result)
    return result


def load_verdict_checkpoints(path, header, ids, records):
    if not path.exists():
        return {}
    rows = list(map(json.loads, path.read_text(encoding='utf-8').splitlines()))
    if not rows or rows[0] != header or any(r.get('_header') for r in rows[1:]):
        raise ValueError('Verdict checkpoint judge/header mismatch')
    result = {}
    for row in rows[1:]:
        q = row.get('question_id')
        if q not in ids or q in result or q not in records:
            raise ValueError('Duplicate or unexpected verdict checkpoint')
        h = records[q]['hypothesis']
        expected = sha256(('null' if h is None else h).encode()).hexdigest()[:16]
        if row.get('hypothesis_sha256') != expected or type(row.get('correct')) is not bool or row.get('error') is not None:
            raise ValueError('Verdict checkpoint content mismatch')
        result[q] = row
    return result


def make_record(question, source, reader):
    request = restore_visible_request(question, source)
    outcome = reader.generate(request)
    # Keep the original ledger detail inside the sole stage for existing citation auditors.
    result = outcome
    record = deepcopy(source)
    record['reader'] = {**result.to_dict(), 'spec': reader.spec.to_dict()}
    record.update(hypothesis=result.resolved_hypothesis if result.origin else None,
        hypothesis_origin=result.origin.value if result.origin else None,
        status='DEGRADED' if result.is_failure else 'COMPLETED',
        failure={'question_id': source['question_id'], 'stage': 'reading', 'code': result.reason_code}
                if result.is_failure else None)
    record['calls']['reader'] = result.calls
    record['tokens'].update(reader_input=result.usage.input_tokens,
        reader_output=result.usage.output_tokens, reader_usage_source=result.usage.source)
    record['timing_ms']['total'] += result.latency_ms - record['timing_ms']['read']
    record['timing_ms']['read'] = result.latency_ms
    record['replay'] = {'execution_mode': 'restored_evidence_new_reader',
        'source_context_sha256': sha256(source['reader_context_text'].encode()).hexdigest(),
        'parent_reader_usage_inherited': False, 'pipeline_timing_combines_separate_stages': True}
    audit_citations(record)
    return record


def run_stage(out, dataset, manifest, r26, r27, plan, sources, client, *, full=False):
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
        if record['reader']['status'] in {'ERROR', 'TIMEOUT'}:
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


def prepare_full(out, dataset, plan, r27):
    if not read_json(out / 'selection.json')['eligible_for_full'] or not read_json(out / 'screen-post-run-audit.json')['audit_passed']:
        raise PilotStopped('Screen must pass selection and independent audit first')
    evidence_plan = out / 'full-evidence-plan.json'
    if evidence_plan.exists():
        frozen = read_json(evidence_plan)
        if frozen['dataset_sha256'] != dataset.sha256 or set(frozen['trace_sha256']) != set(plan['all_ids']):
            raise ValueError('Frozen full evidence identity mismatch')
        for q, h in frozen['trace_sha256'].items():
            if digest(out/'offline-full/traces'/trace_filename(q)) != h:
                raise ValueError('Frozen full evidence changed')
        _, sources = _load_run(out / 'offline-full')
        if set(sources) != set(plan['all_ids']):
            raise ValueError('Incomplete full evidence')
        return sources
    provider = previous.model_provider(previous.DEFAULT_MODEL, previous.DEFAULT_REVISION, 'cuda')
    gate, _ = previous.make_gate(provider, argparse.Namespace(gate='semantic', document_admission='all',
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    provider = previous.CachedEmbedder(provider)
    sources = previous.run(dataset, list(dataset.cases), previous.CANDIDATE, out / 'offline-full', gate, provider)
    for q in plan['screen_ids']:
        if not previous.same_input(restore_visible_request(dataset.by_id()[q].question, r27[q]),
                                   restore_visible_request(dataset.by_id()[q].question, sources[q])):
            raise ValueError('Full evidence no longer reproduces screening')
    save_once(out / 'full-evidence-plan.json', {'dataset_sha256': dataset.sha256,
        'trace_sha256': {q:digest(out/'offline-full/traces'/trace_filename(q)) for q in sources},
        'cases': 500, 'screen_contexts_exact': 80, 'new_api_calls': 0})
    check_frozen(out)
    return sources


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, default=OUTPUT)
    ap.add_argument('--paid', action='store_true')
    ap.add_argument('--full', action='store_true')
    args = ap.parse_args()
    out = args.output.resolve()
    dataset, manifest, r26, r27, plan = prepare(out)
    sources = prepare_full(out, dataset, plan, r27) if args.full else r27
    cost = preflight(out, sources, full=args.full)
    print(json.dumps(cost), flush=True)
    if not args.paid:
        return
    if not cost['fits_budget']:
        raise PilotStopped('Prepared cost does not fit existing authorization; no request sent')
    public = read_json(ROOT / 'eval/reports/oracle-statement-provenance-20261009/public-dataset-provenance.json')
    if not (public['exact_public_file_match'] and public['local_sha256'] == public['public_lfs_sha256'] == dataset.sha256
            and public['model_destination'] == ENDPOINT):
        raise PilotStopped('Public dataset or approved destination mismatch')
    key = os.environ.get('DIVE_READER_API_KEY') or getpass.getpass('DeepSeek key (hidden, never saved): ')
    if not key.strip() or MODEL not in [r['id'] for r in private_get('models', key).get('data', [])]:
        raise PilotStopped('Missing credential or model unavailable')
    guard = TotalBudgetGuard(key, out, limit=Decimal(cost['remaining_planning_cny']))
    guard.check(0, force=True)
    normal = OpenAICompatibleChatClient(ENDPOINT, key, provider='deepseek', extra_body=EXTRA)
    thinking = OpenAICompatibleChatClient(ENDPOINT, key, provider='deepseek', extra_body=THINKING)
    ledgers = budget_ledgers(out)
    client = MeteredClient(normal, out / 'api-cache', max_calls=1600, max_tokens=15000000,
        reuse_roots=[p.parent for p in ledgers if p.parent != out / 'api-cache'],
        before_request=guard.check, currency_guard=CurrencyBudget(ledgers, limit=Decimal(cost['cumulative_stop_cny'])))
    client.normal_transport, client.thinking_transport = normal, thinking
    try:
        run_stage(out, dataset, manifest, r26, r27, plan, sources, client, full=args.full)
        guard.check(client.summary()['attempts'], force=True)
    except (Exception, PilotStopped) as exc:
        atomic_write_json(out / 'stopped.json', {'status': 'STOPPED', 'error_type': type(exc).__name__,
            'stage': 'full' if args.full else 'screen', 'no_complete_score_claimed': True})
        raise
    finally:
        atomic_write_json(out / 'usage.json', client.summary())
        atomic_write_json(out / 'aggregate-peak-cost.json', peak_cost(ledgers))


if __name__ == '__main__':
    try:
        main()
    except PilotStopped as exc:
        print(str(exc), flush=True)
        sys.exit(2)
