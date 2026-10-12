"""Independent zero-network reproduction and preflight for R28."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
from oracle_statement_experiment import (
    DEFAULT_OUTPUT, PARENT, PARENT_VERDICTS, digest, make_reader, generate_record, same_input,
    restore_visible_request, read_json, _load_run, _load_verdicts, load_oracle_dataset,
    budget_ledgers, peak_cost, code_snapshot, atomic_write_json, audit_citations,
    audit_submission, trace_filename, CacheTransport, MeteredClient, EXTRA, THINKING)
from oracle_reader_pair import audit_run_context
from oracle_statement_experiment import coverage, cov_metrics, offline_eligible, summarize, eligible
from oracle_v4_experiment import evidence_audit
from dive_memory.token_budget import ApproximateTokenCounter
from dive_memory.eval_adapters.longmemeval import parse_instant
from dive_memory.oracle.pipeline import role_source_type
from oracle_statement_experiment import R27_ROOT


def canonical(value):
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if k == 'latency_ms':
                continue
            if k == 'detail' and isinstance(v, str):
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
            result[k] = canonical(v)
        return result
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--results', action='store_true')
    ap.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()
    out = args.output.resolve()
    plan = read_json(out / 'frozen-plan.json')
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    _, parent = _load_run(PARENT)
    _, candidate = _load_run(out / 'offline-candidate')
    if code_snapshot(ROOT) != plan['source_snapshot'] or dataset.sha256 != plan['dataset_sha256']:
        raise ValueError('Frozen sources or dataset changed')
    for name, h in plan['helper_sha256'].items():
        assert digest(ROOT / 'audit_tools' / name) == h
    offline_summary = read_json(out / 'offline-summary.json')
    for q, h in offline_summary['offline_trace_sha256'].items():
        assert digest(out / 'offline-candidate/traces' / trace_filename(q)) == h
    assert set(candidate) == set(plan['screen_ids'])
    cases = [dataset.by_id()[q] for q in plan['screen_ids']]
    assert digest(R27_ROOT / 'screen-verdicts.jsonl') == plan['r27_verdicts_sha256']
    for q, h in plan['r27_trace_sha256'].items():
        assert digest(R27_ROOT / 'screen/traces' / trace_filename(q)) == h
    source_checks = 0
    roles = {}
    for case in cases:
        t = candidate[case.question_id]
        events = {event: turn for turn in case.turns
                  if (event := t['formation']['turn_events'].get(turn.ref))}
        for view in t['evidence']['views']:
            sources = view['statement_sources']
            assert [s['source_ref'] for s in sources] == view['source_refs']
            for source in sources:
                turn = events[source['source_ref']]
                assert source == {'source_ref': source['source_ref'], 'role': role_source_type(turn.role),
                                  'statement_at': parse_instant(turn.occurred_at).isoformat()}
                assert parse_instant(source['statement_at']) <= parse_instant(t['as_of'])
                source_checks += 1
                roles[source['role']] = roles.get(source['role'], 0) + 1
    assert source_checks > 0
    metrics = {}
    for label, run_path in [('baseline', out / 'offline-baseline'), ('candidate', out / 'offline-candidate')]:
        _, ts = _load_run(run_path)
        metrics[label] = {**cov_metrics(coverage(cases, ts)),
                          'visible_chars': sum(len(t['reader_context_text']) for t in ts.values())}
    assert metrics == offline_summary['metrics']
    assert offline_summary['eligible_for_screen'] == offline_eligible(metrics['baseline'], metrics['candidate'])
    for q, h in plan['parent_trace_sha256'].items():
        assert digest(PARENT / 'traces' / trace_filename(q)) == h
    assert digest(PARENT_VERDICTS) == plan['parent_verdicts_sha256']
    ledgers = budget_ledgers(out)
    before = {str(p): digest(p) for p in ledgers if p.exists()}
    changed = []
    predicted = Decimal(0)
    for q in plan['screen_ids']:
        c = dataset.by_id()[q]
        a, b = (restore_visible_request(c.question, t[q]) for t in (parent, candidate))
        if same_input(a, b):
            continue
        changed.append(q)
        usage = parent[q]['reader']['usage']
        if any(type(usage[k]) is not int for k in ('input_tokens', 'output_tokens')):
            raise ValueError('Unknown parent usage prevents cost planning')
        scale = max(Decimal(1), Decimal(len(b.context_text)) / max(1, len(a.context_text)))
        predicted += (Decimal(2 * usage['input_tokens']) * scale + 8 * usage['output_tokens'] + 3024) / 1000000
    current = peak_cost(ledgers)
    allowed = Decimal(plan['screen_stop_cny']) - Decimal(current['peak_upper_cny'])
    preflight = {'changed_inputs': len(changed), 'unchanged_inputs': len(candidate) - len(changed),
        'statement_source_checks': source_checks, 'source_roles': roles,
        'predicted_reader_and_judge_cny': str(predicted), 'planning_allowance_cny': str(predicted * Decimal('1.25')),
        'remaining_screen_allowance_cny': str(allowed),
        'fits_screen_budget': predicted * Decimal('1.25') < allowed,
        'method': 'parent per-case stages; increased context scale; 3024 micro-CNY judge allowance each; 25% margin',
        'caveat': 'Observed usage projection, not a guaranteed bound; per-request ledger enforces cap',
        'parent_traces_and_verdicts_unchanged': True, 'source_and_dependencies_match': True}
    if not args.results:
        atomic_write_json(out / 'screen-cost-preflight.json', preflight)
        print(json.dumps(preflight))
        return
    assert read_json(out / 'screen-completion.json')['complete']
    _, traces = _load_run(out / 'screen')
    normal, thinking = CacheTransport(EXTRA), CacheTransport(THINKING)
    # Cache reads only: use a separate empty audit meter and prohibit new calls.
    meter = MeteredClient(normal, out / 'audit-cache', max_calls=0, max_tokens=3000000,
        reuse_roots=[p.parent for p in ledgers])
    reader = make_reader(meter, normal, thinking)
    checks = 0
    for q in plan['screen_ids']:
        c = dataset.by_id()[q]
        meter.phase = 'reader-audit'
        replay = generate_record(c, candidate[q], parent[q], reader)
        assert canonical(replay['reader']) == canonical(traces[q]['reader']), q
        for key in ('hypothesis', 'hypothesis_origin', 'status', 'failure', 'calls', 'tokens', 'replay'):
            assert replay[key] == traces[q][key], (q, key)
        checks += audit_citations(traces[q])
    # This experiment has a distinct manifest schema with a flat pipeline
    # configuration. Validate it explicitly; never rewrite frozen artifacts
    # just to satisfy the generic runner-manifest auditor.
    manifest = read_json(out / 'screen/manifest.json')
    assert manifest['schema_version'] == 'dive-source-retention-screen-v1'
    assert manifest['configuration'] == plan['config']
    evidence_audit(candidate, traces)
    budget = manifest['configuration']['token_budget']
    counter = ApproximateTokenCounter()
    counts = [counter.count(t['reader_context_text']) for t in traces.values()]
    assert all(t['packing']['estimated_tokens'] == counter.count(t['reader_context_text']) for t in traces.values())
    ctx = {'auditable_cases': len(counts), 'configured_context_budget': budget,
           'manifest_schema': manifest['schema_version'], 'token_counter': counter.name,
           'reader_context_heuristic_tokens': {'max': max(counts), 'over_budget_cases': sum(n > budget for n in counts)}}
    assert ctx['auditable_cases'] == 80 and not ctx['reader_context_heuristic_tokens']['over_budget_cases']
    rows = [json.loads(l) for l in (out / 'screen/submission.jsonl').read_text(encoding='utf-8').splitlines()]
    audit_submission(rows, traces)
    header, verdicts = _load_verdicts(out / 'screen-verdicts.jsonl', traces, dataset.sha256)
    parent_header, parent_v = _load_verdicts(PARENT_VERDICTS, parent, dataset.sha256)
    assert header == parent_header and set(verdicts) == set(plan['screen_ids'])
    for q in verdicts:
        if traces[q]['hypothesis'] == parent[q]['hypothesis'] and traces[q]['reader']['status'] == parent[q]['reader']['status']:
            assert verdicts[q] == parent_v[q]
    base_score = summarize(cases, parent, parent_v)
    score = summarize(cases, traces, verdicts, exposed_ids=plan['screen_ids'])
    assert score['overall'] == read_json(out / 'screen-evaluation.json')['overall']
    assert eligible(base_score, score) == read_json(out / 'selection.json')['eligible_for_full']
    assert sum(verdicts.values()) == read_json(out / 'screen-completion.json')['correct']
    assert before == {str(p): digest(p) for p in ledgers if p.exists()}
    atomic_write_json(out / 'post-run-audit.json', {'audit_passed': True, 'cases': 80,
        'citation_checks': checks, 'new_api_calls': 0, 'cached_reader_requests': meter.summary(),
        'parent_500_traces_and_verdicts_unchanged': True, 'budget_ledgers_unchanged': True,
        'audit_driver_sha256': digest(Path(__file__)),
        'source_and_dependencies_match': True, 'context_audit': ctx,
        'comparison_excludes': 'local latency only; recursively includes all stage outputs and usage'})
    print(json.dumps({'audit_passed': True, 'cases': 80, 'citations': checks, 'new_api_calls': 0}))


if __name__ == '__main__':
    main()
