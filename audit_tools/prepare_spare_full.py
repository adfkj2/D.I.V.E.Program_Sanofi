"""Free full evidence reconstruction and remaining-cost proposal after screening."""
import argparse
from dataclasses import fields, replace
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
from oracle_spare_document_experiment import (
    BASE,
    PARENT, read_json, _load_run, load_oracle_dataset, code_snapshot,
    model_provider, make_gate, DEFAULT_MODEL, DEFAULT_REVISION, CachedEmbedder,
    run, coverage, cov_metrics, atomic_write_json, restore_visible_request,
    same_input, digest, trace_filename, budget_ledgers, peak_cost)
from dive_memory.oracle.pipeline import PipelineConfig
from audit_adjudicated_result import visible_passages
from oracle_v4_experiment import evidence_audit


def config_from_plan(plan):
    aliases = {'reader_policy': 'reader_policy_name', 'scoring_policy': 'scoring_policy_name',
               'document_selection': 'document_selection_name', 'document_chunk_search': 'document_chunk_search_name'}
    names = {f.name for f in fields(PipelineConfig)}
    config = PipelineConfig(**{aliases.get(k, k): v for k, v in plan['config'].items()
                               if aliases.get(k, k) in names})
    if config.to_dict() != plan['config']:
        raise ValueError('Cannot reconstruct identical frozen pipeline configuration')
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    plan = read_json(output / 'frozen-plan.json')
    if not read_json(output / 'selection.json')['eligible_for_full']:
        raise ValueError('Candidate did not pass its frozen screen rule')
    if code_snapshot(ROOT) != plan['source_snapshot']:
        raise ValueError('Frozen source/dependency mismatch')
    config = config_from_plan(plan)
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    assert dataset.sha256 == plan['dataset_sha256']
    _, parent = _load_run(PARENT)
    _, screen = _load_run(output / 'screen')
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, 'cuda')
    gate, _ = make_gate(provider, argparse.Namespace(gate='semantic', document_admission='all',
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    candidate = run(dataset, dataset.cases, config, output / 'offline-full-candidate', gate, CachedEmbedder(provider))
    r27 = run(dataset, dataset.cases, replace(BASE, precise_detail_terms=True),
              output / 'offline-full-r27-control', gate, CachedEmbedder(provider))
    evidence_audit(r27, candidate, allow_coordinates=True)
    preserved = 0
    for q in dataset.question_ids:
        old_spans, new_spans = visible_passages(r27[q]), visible_passages(candidate[q])
        assert list(new_spans)[:len(old_spans)] == list(old_spans), q
        for mid, (content, refs) in old_spans.items():
            assert content in new_spans[mid][0] and refs == new_spans[mid][1], (q, mid)
            preserved += 1
    before, after = coverage(dataset.cases, parent), coverage(dataset.cases, candidate)
    atomic_write_json(output / 'full-parent-coverage.json', before)
    atomic_write_json(output / 'full-candidate-coverage.json', after)
    old = {r['question_id']: r for r in before['per_case']}
    changed_coverage = [{'question_id': r['question_id'], 'before': old[r['question_id']], 'after': r}
                        for r in after['per_case'] if any(r[k] != old[r['question_id']][k] for k in ('formed', 'retrieved', 'packed'))]
    reusable_parent, reusable_screen, new_ids = [], [], []
    predicted = Decimal(0)
    fallback_cost_cases = []
    known = [t['reader']['usage'] for t in parent.values()
             if t['reader']['status'] not in {'ERROR', 'TIMEOUT'}
             and all(type(t['reader']['usage'][k]) is int for k in ('input_tokens', 'output_tokens'))]
    fallback_usage = {k: max(u[k] for u in known) for k in ('input_tokens', 'output_tokens')}
    for case in dataset.cases:
        q = case.question_id
        a = restore_visible_request(case.question, parent[q])
        b = restore_visible_request(case.question, candidate[q])
        if q in screen:
            assert same_input(b, restore_visible_request(case.question, screen[q]))
            reusable_screen.append(q)
        elif same_input(a, b):
            reusable_parent.append(q)
        else:
            new_ids.append(q)
            usage = parent[q]['reader']['usage']
            if parent[q]['reader']['status'] in {'ERROR', 'TIMEOUT'} or any(type(usage[k]) is not int for k in ('input_tokens', 'output_tokens')):
                usage = fallback_usage
                fallback_cost_cases.append(q)
            scale = max(Decimal(1), Decimal(len(b.context_text)) / max(1, len(a.context_text)))
            predicted += (Decimal(2 * usage['input_tokens']) * scale + 8 * usage['output_tokens'] + 3024) / 1000000
    spent = peak_cost(budget_ledgers(output))
    allowance = predicted * Decimal('1.25')
    rules = plan['aggregate_budget']
    available = Decimal(rules['planning_stop_cny']) - Decimal(spent['peak_upper_cny'])
    for q, h in plan['parent_trace_sha256'].items():
        assert digest(PARENT / 'traces' / trace_filename(q)) == h
    assert code_snapshot(ROOT) == plan['source_snapshot']
    report = {'preserved_r27_passages': preserved, 'cases': 500, 'baseline': cov_metrics(before), 'candidate': cov_metrics(after),
        'source_coverage_deltas': changed_coverage, 'is_accuracy': False, 'new_api_calls': 0,
        'parent_traces_unchanged': True, 'source_and_dependencies_match': True,
        'audit_driver_sha256': digest(Path(__file__))}
    atomic_write_json(output / 'full-coverage-audit.json', report)
    proposal = {'fallback_cost_cases': fallback_cost_cases, 'fallback_usage': fallback_usage, 'full_run': 'NOT_RUN', 'source_snapshot': plan['source_snapshot'], 'config': plan['config'],
        'reusable_parent_ids': reusable_parent, 'reusable_screen_ids': reusable_screen,
        'new_input_ids': new_ids, 'expected_new_input_cases': len(new_ids), 'spent': spent,
        'candidate_trace_sha256': {q: digest(output / 'offline-full-candidate/traces' / trace_filename(q)) for q in candidate},
        'screen_trace_sha256': {q: digest(output / 'screen/traces' / trace_filename(q)) for q in screen},
        'screen_verdicts_sha256': digest(output / 'screen-verdicts.jsonl'),
        'execution_driver_sha256': digest(ROOT / 'audit_tools/run_prepared_spare_full.py'),
        'estimated_additional_cny': str(predicted), 'planning_allowance_cny': str(allowance),
        'available_planning_cny': str(available), 'fits_authorized_budget': allowance < available,
        'minimum_cumulative_authorization_with_reserve_cny': str(Decimal(spent['peak_upper_cny']) + allowance + Decimal(rules['settlement_reserve_cny'])),
        'method': 'per-case parent reader stage usage scaled for larger evidence; judge allowance and 25% margin',
        'caveat': 'Usage projection, not guaranteed cost or accuracy; same judge and strict ledger bound remain required',
        'helper_sha256': digest(Path(__file__))}
    atomic_write_json(output / 'prepared-full-plan.json', proposal)
    print(json.dumps({k: v for k, v in proposal.items() if k not in ('source_snapshot', 'config', 'reusable_parent_ids',
        'reusable_screen_ids', 'new_input_ids', 'candidate_trace_sha256', 'screen_trace_sha256')}), flush=True)


if __name__ == '__main__':
    main()
