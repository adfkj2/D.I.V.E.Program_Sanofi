"""Fixed-evidence refusal review: audited saved baseline plus bounded new calls.

Default prepares all 500 requests without network access. --paid runs the fixed
80-case screen; --paid --full requires that screen to pass its frozen rule.
"""
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
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'ops'), str(ROOT / 'audit_tools')]
from audit_adjudicated_result import visible_passages, audit_citations, audit_submission
from replay_format_recovery import restore_outcome
from oracle_calculation_experiment import budget_ledgers, eligible
from oracle_complete_test import read_json, summarize, score_trace
from oracle_currency_budget import CurrencyBudget, peak_cost
from oracle_low_cost_pilot import MeteredClient, PilotStopped, MODEL, ENDPOINT, EXTRA, private_get
from oracle_narrative_experiment import TotalBudgetGuard
from oracle_reader_pair import _load_run, _load_verdicts, _accuracy, audit_run_context
from oracle_v4_experiment import evidence_audit
from dive_memory.eval_manifest import code_snapshot
from dive_memory.models import Memory, MemoryStatus, EvidenceState, RetrievalItem
from dive_memory.oracle.contracts import ReaderStatus
from dive_memory.oracle.evaluate import JudgeSpec, LLMJudge
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.premise_review_reader import NecessaryPremiseReader, RefusalReviewReader, REVIEW_VERSION
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.reader import OracleReaderRequest, ReaderSpec, reader_policy, render_reader_context
from dive_memory.oracle.runner import atomic_write_json, atomic_write_text, trace_filename
from dive_memory.oracle.validator import validate_submission
from dive_memory.token_budget import ApproximateTokenCounter

PARENT = ROOT / 'eval/reports/oracle-format-recovery-20261007'
DEFAULT_OUTPUT = ROOT / 'eval/reports/oracle-premise-review-20261007-v2'
POLICY = reader_policy('indexed_evidence')
REVIEW_SPEC = ReaderSpec('deepseek', MODEL, prompt_version=REVIEW_VERSION, max_tokens=1024)


def restore_visible_request(question, trace):
    """Restore only visible spans, refusing any rendering or metadata mismatch."""
    passages = visible_passages(trace)
    items = []
    statuses = {'CURRENT': MemoryStatus.ACTIVE, 'SUPERSEDED': MemoryStatus.SUPERSEDED,
                'ARCHIVED': MemoryStatus.ARCHIVED, 'REMOVED': MemoryStatus.DELETED}
    for index, view in enumerate(trace['evidence']['views']):
        if index != view['index']:
            raise ValueError('Noncontiguous evidence indices')
        content, refs = passages[view['memory_id']]
        if refs != set(view['source_refs']):
            raise ValueError('Source mismatch')
        memory = Memory(view['memory_id'], trace['namespace'], 'visible_replay',
            EvidenceState.OBSERVATION, content, {'role': view['role']},
            status=statuses[view['lifecycle']], valid_from=view['valid_from'],
            valid_to=view['valid_to'], contradicts_id=view['conflict_group'])
        items.append(RetrievalItem(memory, view['retrieval_score'],
                                   channels=view['channels'], source_refs=view['source_refs']))
    # The original pipeline uses its end-of-day visibility cutoff for both clocks.
    request = OracleReaderRequest(question, trace['as_of'], trace['as_of'], tuple(items),
        trace['reader_context_text'], token_budget=6000, policy=POLICY,
        evidence_omitted=trace['packing']['omitted'])
    if render_reader_context(request) != trace['reader_context_text']:
        raise ValueError('Restored visible request differs from audited parent context')
    if ApproximateTokenCounter().count(request.context_text) > request.token_budget:
        raise ValueError('Restored context exceeds the original budget')
    return request


class SavedPrimary:
    def __init__(self, saved):
        self.spec = ReaderSpec(**saved['spec'])
        self.policy = POLICY
        self.outcome = restore_outcome({k: v for k, v in saved.items() if k != 'spec'})
        self.used = False

    def generate(self, request):
        if self.used:
            raise ValueError('Saved primary consumed twice')
        self.used = True
        return self.outcome


def save_verdicts(path, header, rows):
    atomic_write_text(path, ''.join(json.dumps(row, ensure_ascii=False) + '\n'
                                    for row in [header, *rows]))


def prepare(output):
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    dataset.require_full_benchmark()
    manifest, parent = _load_run(PARENT)
    if (set(parent) != set(dataset.question_ids) or dataset.sha256 != manifest['dataset']['sha256']
            or not read_json(PARENT / 'post-run-audit.json')['audit_passed']):
        raise ValueError('Parent identity or coverage invalid')
    requests = {case.question_id: restore_visible_request(case.question, parent[case.question_id])
                for case in dataset.cases}
    citations = sum(audit_citations(t) for t in parent.values())
    ids = read_json(ROOT / 'eval/reports/oracle-reasoning-20260929/frozen-plan.json')['ids']
    ledgers = budget_ledgers(output)
    rules = read_json(ROOT / 'eval/reports/oracle-road-to-90-budget.json')
    spent = peak_cost(ledgers)
    frozen = output / 'frozen-plan.json'
    existing = read_json(frozen) if frozen.exists() else None
    initial = existing['initial_aggregate_cost'] if existing else spent
    bound = min(Decimal(rules['planning_stop_cny']), Decimal(initial['peak_upper_cny']) + Decimal('3'))
    if bound > Decimal(rules['user_limit_cny']) - Decimal(rules['settlement_reserve_cny']):
        raise ValueError('Planning boundary violates shared reserve')
    plan = {'dataset_sha256': dataset.sha256, 'source_snapshot': code_snapshot(ROOT),
        'helper_sha256': {name: sha256((ROOT / 'audit_tools' / name).read_bytes()).hexdigest()
                         for name in ('audit_adjudicated_result.py', 'replay_format_recovery.py')},
        'parent_trace_sha256': {qid: sha256((PARENT / 'traces' / trace_filename(qid)).read_bytes()).hexdigest()
                                for qid in dataset.question_ids},
        'parent_verdicts_sha256': sha256((PARENT / 'full-verdicts.jsonl').read_bytes()).hexdigest(),
        'screen_ids': ids, 'all_ids': list(dataset.question_ids),
        'review_spec': REVIEW_SPEC.to_dict(), 'extra_body': EXTRA,
        'selection': '+3/50 answerable relative to R21; no loss on all 30 unanswerable',
        'selection_uses_correctness': False,
        'routing': 'One necessary-premise review after genuine final ABSTAIN only',
        'execution_mode': 'saved_baseline_plus_new_review_not_independent_full_pipeline',
        'aggregate_budget': rules, 'initial_aggregate_cost': initial,
        'experiment_stop_cny': str(bound), 'max_new_cost_cny': '3',
        'currency_ledger_paths': [str(p) for p in ledgers]}
    if existing is not None and existing != plan:
        raise ValueError('Frozen experiment changed; use a new directory')
    atomic_write_json(frozen, plan)
    atomic_write_json(output / 'request-recovery-audit.json', {'cases': len(requests),
        'exact_context_matches': len(requests), 'parent_citation_checks': citations,
        'routing_inputs': ['runtime reader status'], 'extra_history_access': False,
        'new_api_calls': 0})
    print(json.dumps({'prepared': 500, 'screen_cases': len(ids),
        'screen_refusals': sum(parent[q]['reader']['status'] == 'ABSTAIN' for q in ids),
        'full_refusals': sum(t['reader']['status'] == 'ABSTAIN' for t in parent.values()),
        'spent': spent, 'experiment_stop_cny': str(bound)}), flush=True)
    return dataset, manifest, parent, requests, plan, ledgers


def run_stage(dataset, parent_manifest, parent, requests, plan, client, output, *, full):
    label = 'full' if full else 'screen'
    ids = plan['all_ids'] if full else plan['screen_ids']
    cases = [dataset.by_id()[qid] for qid in ids]
    out = output / label
    traces = {}
    reviewer = NecessaryPremiseReader(client, REVIEW_SPEC, policy=POLICY)
    for n, case in enumerate(cases, 1):
        qid = case.question_id
        path = out / 'traces' / trace_filename(qid)
        screen_path = output / 'screen/traces' / trace_filename(qid)
        if path.exists():
            record = read_json(path)
        elif full and screen_path.exists():
            record = read_json(screen_path)
        else:
            client.phase = 'reader-' + label
            primary = SavedPrimary(parent[qid]['reader'])
            reader = RefusalReviewReader(primary, reviewer)
            result = reader.generate(requests[qid])
            record = deepcopy(parent[qid])
            record['reader'] = {**result.to_dict(), 'spec': reader.spec.to_dict()}
            record.update(hypothesis=result.hypothesis if result.is_failure else result.resolved_hypothesis,
                          hypothesis_origin=result.origin.value if result.origin else None,
                          status='DEGRADED' if result.is_failure else 'COMPLETED',
                          failure={'question_id': qid, 'stage': 'reader', 'code': result.reason_code}
                                  if result.is_failure else None)
            record['calls']['reader'] = result.calls
            record['tokens'].update(reader_input=result.usage.input_tokens,
                                    reader_output=result.usage.output_tokens,
                                    reader_usage_source=result.usage.source)
            record['timing_ms']['total'] += result.latency_ms - record['timing_ms']['read']
            record['timing_ms']['read'] = result.latency_ms
            stages = json.loads(result.detail)['stages']
            record['replay'] = {'execution_mode': plan['execution_mode'],
                'parent_trace_sha256': plan['parent_trace_sha256'][qid],
                'parent_usage_is_inherited': True, 'pipeline_timing_combines_inherited_and_new': True,
                'new_review_stage': stages[1] if len(stages) == 2 else None}
        if record['replay']['parent_trace_sha256'] != plan['parent_trace_sha256'][qid]:
            raise ValueError('Checkpoint parent mismatch')
        audit_citations(record)
        atomic_write_json(path, record)
        traces[qid] = record
        if n % 20 == 0 or n == len(cases):
            print(json.dumps({'phase': label, 'generated': n, 'total': len(cases)}), flush=True)
    atomic_write_json(out / 'manifest.json', {'schema_version': 'dive-saved-baseline-review-v1',
        'dataset': parent_manifest['dataset'], 'configuration': parent_manifest['configuration'],
        'experiment_source_snapshot': plan['source_snapshot'], 'execution_mode': plan['execution_mode'],
        'review_spec': REVIEW_SPEC.to_dict(), 'ids': ids})
    paired = evidence_audit({q: parent[q] for q in ids}, traces)
    atomic_write_json(output / (label + '-evidence-audit.json'), paired)
    context_audit = audit_run_context(out)
    if context_audit['auditable_cases'] != len(cases) or context_audit['reader_context_heuristic_tokens']['over_budget_cases']:
        raise ValueError('Context audit failed')
    atomic_write_json(output / (label + '-context-audit.json'), context_audit)
    submission = [{'question_id': q, 'hypothesis': traces[q]['hypothesis'] or
                   SavedPrimary(traces[q]['reader']).outcome.resolved_hypothesis} for q in ids]
    audit_submission(submission, traces)
    atomic_write_text(out / 'submission.jsonl', ''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in submission))
    validation = validate_submission(out / 'submission.jsonl', expected_ids=ids, require_full=full)
    if validation.status != 'VALID':
        raise ValueError('Submission invalid')
    atomic_write_json(output / (label + '-submission-validation.json'), validation.to_dict())
    # Labels are loaded only after uniform generation/routing has finished.
    header, parent_v = _load_verdicts(PARENT / 'full-verdicts.jsonl', parent, dataset.sha256)
    judge = LLMJudge(client, JudgeSpec(**header['judge']))
    rows = []
    verdict_path = output / (label + '-verdicts.jsonl')
    cached = {}
    if verdict_path.exists():
        cached = {r['question_id']: r for r in (json.loads(line) for line in verdict_path.read_text(encoding='utf-8').splitlines()) if not r.get('_header')}
    if full:
        for r in (json.loads(line) for line in (output / 'screen-verdicts.jsonl').read_text(encoding='utf-8').splitlines()):
            if not r.get('_header'):
                cached.setdefault(r['question_id'], r)
    for case in cases:
        q = case.question_id
        t = traces[q]
        hypothesis = t['hypothesis']
        digest = sha256(('null' if hypothesis is None else hypothesis).encode()).hexdigest()[:16]
        if (hypothesis == parent[q]['hypothesis'] and t['reader']['status'] == parent[q]['reader']['status']):
            row = {'question_id': q, 'hypothesis_sha256': digest, 'correct': parent_v[q],
                   'raw': 'EXACT_PARENT_VERDICT', 'error': None}
        elif q in cached:
            row = cached[q]
            if row['hypothesis_sha256'] != digest or type(row['correct']) is not bool:
                raise ValueError('Checkpoint verdict mismatch')
        else:
            client.phase = 'judge-' + label
            verdict = score_trace(judge, case, t)
            row = {'question_id': q, 'hypothesis_sha256': digest, 'correct': verdict.correct,
                   'raw': verdict.raw, 'error': verdict.error}
        rows.append(row)
        save_verdicts(verdict_path, header, rows)
    _, verdicts = _load_verdicts(verdict_path, traces, dataset.sha256)
    before = summarize(cases, parent, parent_v)
    report = summarize(cases, traces, verdicts, exposed_ids=plan['screen_ids'])
    report['execution_mode'] = plan['execution_mode']
    atomic_write_json(output / (label + '-baseline-evaluation.json'), before)
    atomic_write_json(output / (label + '-evaluation.json'), report)
    atomic_write_json(output / (label + '-comparison.json'), _accuracy(
        {q: parent[q] for q in ids}, traces, parent_v, verdicts, {c.question_id: c.question_type for c in cases}))
    if code_snapshot(ROOT) != plan['source_snapshot']:
        raise ValueError('Source or environment drift during experiment')
    atomic_write_json(output / (label + '-completion.json'), {'complete': True, 'cases': len(cases),
        'correct': report['overall']['correct'], 'execution_mode': plan['execution_mode'],
        'official_score': 'NOT_EVALUATED', 'evidence_unchanged': True,
        'citation_checks': sum(audit_citations(t) for t in traces.values()),
        'source_and_dependencies_unchanged': True})
    if not full:
        atomic_write_json(output / 'selection.json', {'eligible_for_full': eligible(before, report),
            'rule': plan['selection'], 'full_run': 'NOT_RUN'})
    print(json.dumps({'phase': label, 'baseline': before['overall'], 'candidate': report['overall'],
        'unanswerable': report['unanswerable'], 'eligible_for_full': eligible(before, report) if not full else None}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--paid', action='store_true')
    parser.add_argument('--full', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    dataset, manifest, parent, requests, plan, ledgers = prepare(output)
    if not args.paid:
        return
    if args.full:
        if not read_json(output / 'screen-completion.json')['complete'] or not read_json(output / 'selection.json')['eligible_for_full']:
            raise PilotStopped('Screen did not pass the frozen promotion rule; no full run')
    key = os.environ.get('DIVE_READER_API_KEY') or getpass.getpass('DeepSeek key (hidden, never saved): ')
    if not key.strip() or MODEL not in [r['id'] for r in private_get('models', key).get('data', [])]:
        raise PilotStopped('Missing credential or unavailable model')
    guard = TotalBudgetGuard(key, output, limit=Decimal(plan['max_new_cost_cny']))
    transport = OpenAICompatibleChatClient(ENDPOINT, key, provider='deepseek', extra_body=EXTRA)
    client = MeteredClient(transport, output / 'api-cache', max_calls=260, max_tokens=3000000,
        reuse_roots=[p.parent for p in ledgers if p.parent != output / 'api-cache'],
        before_request=guard.check, currency_guard=CurrencyBudget(ledgers, limit=Decimal(plan['experiment_stop_cny'])))
    try:
        run_stage(dataset, manifest, parent, requests, plan, client, output, full=args.full)
        guard.check(client.summary()['attempts'], force=True)
    except (Exception, PilotStopped) as exc:
        atomic_write_json(output / 'stopped.json', {'status': 'STOPPED', 'error_type': type(exc).__name__,
                          'stage': 'full' if args.full else 'screen', 'no_complete_score_claimed': True})
        raise
    finally:
        atomic_write_json(output / 'usage.json', client.summary())
        atomic_write_json(output / 'aggregate-peak-cost.json', peak_cost(ledgers))


if __name__ == '__main__':
    try:
        main()
    except PilotStopped as exc:
        print(str(exc), flush=True)
        sys.exit(2)
