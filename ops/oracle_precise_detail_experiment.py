"""R27: precise modal inflections, compared with the audited R26 evidence writer."""
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
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
from oracle_premise_review_experiment import (
    PARENT, restore_visible_request, save_verdicts, audit_citations, audit_submission,
    _load_run, _load_verdicts, _accuracy, read_json, summarize, score_trace,
    budget_ledgers, eligible, code_snapshot, atomic_write_json, atomic_write_text,
    trace_filename, load_oracle_dataset, validate_submission, LLMJudge, JudgeSpec)
from oracle_narrative_experiment import ARMS, run, TotalBudgetGuard
from oracle_structural_experiment import CachedEmbedder, cov_metrics
from oracle_v4_experiment import coverage, evidence_audit
from oracle_retrieval_ablation import model_provider, make_gate, DEFAULT_MODEL, DEFAULT_REVISION
from oracle_refusal_gated_experiment import (
    ModeClient, CacheTransport, IndexedEvidenceReader, AdjudicatedReader,
    EvidenceReferee, REFEREE_VERSION, THINKING, OUTPUT_CAP, ReaderSpec)
from oracle_low_cost_pilot import MeteredClient, PilotStopped, MODEL, ENDPOINT, EXTRA, private_get
from oracle_currency_budget import CurrencyBudget, peak_cost
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.contracts import CANONICAL_ABSTENTION_HYPOTHESIS

PARENT_ROOT = ROOT / 'eval/reports/oracle-asserted-details-20261008'
PARENT = PARENT_ROOT / 'full'
PARENT_VERDICTS = PARENT_ROOT / 'full-verdicts.jsonl'
BASE = replace(ARMS['wide'], reader_policy_name='indexed_evidence', limit=60,
    reported_detail_anchors=True, reported_detail_assertions_only=True)
CANDIDATE = replace(BASE, precise_detail_terms=True)
DEFAULT_OUTPUT = ROOT / 'eval/reports/oracle-precise-modality-20261008'

# Retry identical local persistence only; the network call is never retried.
import oracle_low_cost_pilot as metering
_original_atomic_write_json = atomic_write_json
def atomic_write_json(path, payload):
    for attempt in range(6):
        try:
            return _original_atomic_write_json(path, payload)
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (2 ** attempt))
metering.atomic_write_json = atomic_write_json


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def make_reader(meter, normal, thinking):
    policy = BASE.policy()
    primary = IndexedEvidenceReader(ModeClient(meter, normal), ReaderSpec('deepseek', MODEL,
        prompt_version=policy.prompt_version, max_tokens=512), policy=policy)
    secondary = IndexedEvidenceReader(ModeClient(meter, thinking), ReaderSpec('deepseek', MODEL,
        prompt_version=policy.prompt_version, max_tokens=OUTPUT_CAP, timeout_seconds=120), policy=policy)
    referee = EvidenceReferee(ModeClient(meter, normal), ReaderSpec('deepseek', MODEL,
        prompt_version=REFEREE_VERSION, max_tokens=1024), policy=policy)
    return AdjudicatedReader(primary, secondary, referee, recover_invalid_secondary=True)


def same_input(a, b):
    # All model-visible payload fields plus citation resolution / omission state.
    return (a.question, a.question_date, a.as_of, a.context_text, a.evidence_omitted,
            a.token_budget, a.policy) == (b.question, b.question_date, b.as_of,
            b.context_text, b.evidence_omitted, b.token_budget, b.policy)


def offline_eligible(before, after):
    return (after['packed'] >= before['packed']
        and after['complete_positive_cases'] >= before['complete_positive_cases']
        and all(after['by_type_packed'].get(k, 0) >= v - 1 for k, v in before['by_type_packed'].items())
        and (after['packed'] > before['packed'] or after['visible_chars'] > before['visible_chars']))


def prepare(output):
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    dataset.require_full_benchmark()
    ids = read_json(ROOT / 'eval/reports/oracle-reasoning-20260929/frozen-plan.json')['ids']
    cases = [dataset.by_id()[q] for q in ids]
    if len(cases) != 80 or sum(c.should_abstain for c in cases) != 30:
        raise ValueError('Invalid fixed development selection')
    manifest, parent = _load_run(PARENT)
    if manifest['dataset']['sha256'] != dataset.sha256 or set(parent) != set(dataset.question_ids):
        raise ValueError('Invalid parent dataset or coverage')
    ledgers = budget_ledgers(output)
    frozen = output / 'frozen-plan.json'
    existing = read_json(frozen) if frozen.exists() else None
    initial = existing['initial_cost'] if existing else peak_cost(ledgers)
    rules = read_json(ROOT / 'eval/reports/oracle-road-to-90-budget.json')
    ceiling = min(Decimal(rules['planning_stop_cny']), Decimal(initial['peak_upper_cny']) + Decimal('1.80'))
    if ceiling > Decimal(rules['user_limit_cny']) - Decimal(rules['settlement_reserve_cny']):
        raise ValueError('Shared budget reserve violated')
    plan = {'dataset_sha256': dataset.sha256, 'source_snapshot': code_snapshot(ROOT),
        'helper_sha256': {name: digest(ROOT / 'audit_tools' / name) for name in (
            'audit_adjudicated_result.py', 'replay_format_recovery.py')},
        'screen_ids': ids, 'config': CANDIDATE.to_dict(), 'control_config': BASE.to_dict(),
        'parent_trace_sha256': {q: digest(PARENT / 'traces' / trace_filename(q)) for q in dataset.question_ids},
        'parent_verdicts_sha256': digest(PARENT_VERDICTS),
        'reader_spec': make_reader(None, None, None).spec.to_dict(),
        'primary_extra_body': EXTRA, 'secondary_extra_body': THINKING, 'judge_extra_body': EXTRA,
        'selection': '+3/50 answerable against R26; no loss on all 30 unanswerable',
        'screen_status': 'reused public development data, not a blind holdout',
        'execution_mode': 'candidate_evidence_with_exact_parent_input_reuse',
        'offline_gate': 'packed/complete nondecreasing; type loss <=1; added sources or visible text',
        'aggregate_budget': rules, 'initial_cost': initial, 'screen_stop_cny': str(ceiling),
        'screen_max_new_cny': '1.80', 'currency_ledger_paths': [str(p) for p in ledgers]}
    if existing is not None and plan != existing:
        raise ValueError('Frozen experiment changed; use a new output directory')
    atomic_write_json(frozen, plan)
    return dataset, cases, manifest, parent, ledgers, plan


def offline(output, dataset, cases, parent):
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, 'cuda')
    gate, _ = make_gate(provider, argparse.Namespace(gate='semantic', document_admission='all',
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    provider = CachedEmbedder(provider)
    metrics = {}
    for label, config in [('baseline', BASE), ('candidate', CANDIDATE)]:
        traces = run(dataset, cases, config, output / ('offline-' + label), gate, provider)
        if label == 'baseline':
            atomic_write_json(output / 'baseline-replay-audit.json', evidence_audit(
                {c.question_id: parent[c.question_id] for c in cases}, traces))
        cov = coverage(cases, traces)
        atomic_write_json(output / (label + '-coverage.json'), cov)
        metrics[label] = {**cov_metrics(cov),
                         'visible_chars': sum(len(t['reader_context_text']) for t in traces.values())}
    changed = []
    for c in cases:
        new = restore_visible_request(c.question, traces[c.question_id])
        old = restore_visible_request(c.question, parent[c.question_id])
        if not same_input(old, new):
            changed.append(c.question_id)
    summary = {'metrics': metrics, 'eligible_for_screen': offline_eligible(metrics['baseline'], metrics['candidate']),
        'changed_input_ids': changed, 'unchanged_input_count': len(cases) - len(changed),
        'is_accuracy': False, 'new_api_calls': 0,
        'offline_trace_sha256': {q: digest(output / 'offline-candidate/traces' / trace_filename(q)) for q in traces}}
    atomic_write_json(output / 'offline-summary.json', summary)
    print(json.dumps(summary), flush=True)


def generate_record(case, candidate, parent, reader):
    new = restore_visible_request(case.question, candidate)
    old = restore_visible_request(case.question, parent)
    record = deepcopy(candidate)
    unchanged = same_input(old, new)
    if unchanged:
        record['reader'] = deepcopy(parent['reader'])
        for k in ('hypothesis', 'hypothesis_origin', 'status', 'failure'):
            record[k] = deepcopy(parent[k])
        read_ms = parent['timing_ms']['read']
    else:
        outcome = reader.generate(new)
        record['reader'] = {**outcome.to_dict(), 'spec': reader.spec.to_dict()}
        record.update(hypothesis=outcome.resolved_hypothesis if outcome.origin else None,
            hypothesis_origin=outcome.origin.value if outcome.origin else None,
            status='DEGRADED' if outcome.is_failure else 'COMPLETED',
            failure={'question_id': case.question_id, 'stage': 'reading', 'code': outcome.reason_code}
                    if outcome.is_failure else None)
        read_ms = outcome.latency_ms
    result = record['reader']
    record['calls']['reader'] = result['calls']
    record['tokens'].update(reader_input=result['usage']['input_tokens'], reader_output=result['usage']['output_tokens'],
                            reader_usage_source=result['usage']['source'])
    record['timing_ms']['total'] += read_ms - record['timing_ms']['read']
    record['timing_ms']['read'] = read_ms
    record['replay'] = {'exact_parent_input_reused': unchanged, 'pipeline_timing_combines_separate_stages': True,
                        'parent_usage_inherited': unchanged}
    audit_citations(record)
    return record


def screen(output, dataset, cases, manifest, parent, client, normal, thinking, plan):
    summary = read_json(output / 'offline-summary.json')
    if not summary['eligible_for_screen']:
        raise PilotStopped('Offline gate failed; no paid screen')
    _, candidates = _load_run(output / 'offline-candidate')
    for q in plan['screen_ids']:
        if digest(output / 'offline-candidate/traces' / trace_filename(q)) != summary['offline_trace_sha256'][q]:
            raise ValueError('Prepared evidence changed')
    reader = make_reader(client, normal, thinking)
    if any(reader.spec.to_dict() != parent[q]['reader']['spec'] for q in plan['screen_ids']):
        raise ValueError('Reader differs from R26 parent')
    out = output / 'screen'
    traces = {}
    for n, c in enumerate(cases, 1):
        q = c.question_id
        path = out / 'traces' / trace_filename(q)
        if path.exists():
            record = read_json(path)
        else:
            client.phase = 'reader-screen'
            record = generate_record(c, candidates[q], parent[q], reader)
            atomic_write_json(path, record)
        # Completed checkpoints remain failures when applicable; never resample.
        if record['reader']['status'] in {'ERROR', 'TIMEOUT'}:
            raise PilotStopped('Transport/budget failure recorded; stop further requests')
        evidence_audit({q: candidates[q]}, {q: record})
        audit_citations(record)
        traces[q] = record
        if n % 10 == 0:
            print(json.dumps({'generated': n, 'total': len(cases)}), flush=True)
    atomic_write_json(out / 'manifest.json', {'schema_version': 'dive-source-retention-screen-v1',
        'dataset': manifest['dataset'], 'configuration': plan['config'], 'source_snapshot': plan['source_snapshot']})
    submission = [{'question_id': q, 'hypothesis': traces[q]['hypothesis'] if traces[q]['hypothesis'] is not None
                  else CANONICAL_ABSTENTION_HYPOTHESIS}
                  for q in plan['screen_ids']]
    audit_submission(submission, traces)
    atomic_write_text(out / 'submission.jsonl', ''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in submission))
    validation = validate_submission(out / 'submission.jsonl', expected_ids=plan['screen_ids'], require_full=False)
    if validation.status != 'VALID':
        raise ValueError('Invalid submission')
    atomic_write_json(output / 'screen-submission-validation.json', validation.to_dict())
    header, parent_v = _load_verdicts(PARENT_VERDICTS, parent, dataset.sha256)
    judge = LLMJudge(client, JudgeSpec(**header['judge']))
    verdict_path = output / 'screen-verdicts.jsonl'
    cached = {}
    if verdict_path.exists():
        cached = {r['question_id']: r for r in (json.loads(l) for l in verdict_path.read_text(encoding='utf-8').splitlines()) if not r.get('_header')}
    rows = []
    for c in cases:
        q = c.question_id
        t = traces[q]
        h = sha256(('null' if t['hypothesis'] is None else t['hypothesis']).encode()).hexdigest()[:16]
        if t['hypothesis'] == parent[q]['hypothesis'] and t['reader']['status'] == parent[q]['reader']['status']:
            row = {'question_id': q, 'hypothesis_sha256': h, 'correct': parent_v[q], 'raw': 'EXACT_PARENT_VERDICT', 'error': None}
        elif q in cached:
            row = cached[q]
            if row['hypothesis_sha256'] != h or type(row['correct']) is not bool:
                raise ValueError('Verdict checkpoint mismatch')
        else:
            client.phase, client.client = 'judge-screen', normal
            v = score_trace(judge, c, t)
            row = {'question_id': q, 'hypothesis_sha256': h, 'correct': v.correct, 'raw': v.raw, 'error': v.error}
        rows.append(row)
        save_verdicts(verdict_path, header, rows)
    _, verdicts = _load_verdicts(verdict_path, traces, dataset.sha256)
    before = summarize(cases, parent, parent_v)
    after = summarize(cases, traces, verdicts, exposed_ids=plan['screen_ids'])
    atomic_write_json(output / 'screen-baseline-evaluation.json', before)
    atomic_write_json(output / 'screen-evaluation.json', after)
    atomic_write_json(output / 'screen-comparison.json', _accuracy({q: parent[q] for q in traces}, traces,
        parent_v, verdicts, {c.question_id: c.question_type for c in cases}))
    atomic_write_json(output / 'selection.json', {'eligible_for_full': eligible(before, after), 'full_run': 'NOT_RUN', 'rule': plan['selection']})
    if code_snapshot(ROOT) != plan['source_snapshot']:
        raise ValueError('Source or dependency drift')
    atomic_write_json(output / 'screen-completion.json', {'complete': True, 'cases': len(cases),
        'correct': after['overall']['correct'], 'official_score': 'NOT_EVALUATED',
        'execution_mode': plan['execution_mode'], 'citation_checks': sum(audit_citations(t) for t in traces.values())})
    print(json.dumps({'before': before['overall'], 'after': after['overall'],
        'answerable': after['answerable'], 'unanswerable': after['unanswerable'], 'promote': eligible(before, after)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--paid', action='store_true')
    parser.add_argument('--cache-only', action='store_true')
    args = parser.parse_args()
    if args.paid and args.cache_only:
        parser.error('Choose either paid or cache-only')
    output = args.output.resolve()
    dataset, cases, manifest, parent, ledgers, plan = prepare(output)
    if not args.paid and not args.cache_only:
        offline(output, dataset, cases, parent)
        return
    if not read_json(output / 'offline-summary.json')['eligible_for_screen']:
        raise PilotStopped('Offline gate failed; no paid request')
    guard = None
    if args.paid:
        key = os.environ.get('DIVE_READER_API_KEY') or getpass.getpass('DeepSeek key (hidden, never saved): ')
        if not key.strip() or MODEL not in [r['id'] for r in private_get('models', key).get('data', [])]:
            raise PilotStopped('Missing credential or unavailable model')
        guard = TotalBudgetGuard(key, output, limit=Decimal(plan['screen_max_new_cny']))
        guard.check(0, force=True)
        normal = OpenAICompatibleChatClient(ENDPOINT, key, provider='deepseek', extra_body=EXTRA)
        thinking = OpenAICompatibleChatClient(ENDPOINT, key, provider='deepseek', extra_body=THINKING)
    else:
        normal, thinking = CacheTransport(EXTRA), CacheTransport(THINKING)
    client = MeteredClient(normal, output / 'api-cache', max_calls=300 if args.paid else 0, max_tokens=3000000,
        reuse_roots=[p.parent for p in ledgers if p.parent != output / 'api-cache'],
        before_request=guard.check if guard else None,
        currency_guard=CurrencyBudget(ledgers, limit=Decimal(plan['screen_stop_cny'])) if args.paid else None)
    try:
        screen(output, dataset, cases, manifest, parent, client, normal, thinking, plan)
        if guard:
            guard.check(client.summary()['attempts'], force=True)
    except (Exception, PilotStopped) as exc:
        atomic_write_json(output / 'stopped.json', {'status': 'STOPPED', 'error_type': type(exc).__name__, 'no_complete_score_claimed': True})
        raise
    finally:
        atomic_write_json(output / 'usage.json', client.summary())
        atomic_write_json(output / 'aggregate-peak-cost.json', peak_cost(ledgers))


if __name__ == '__main__':
    main()
