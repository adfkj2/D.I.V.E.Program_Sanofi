"""R24 free 500-case coverage audit; never answers questions or calls an API."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
from oracle_source_retention_experiment import (
    DEFAULT_OUTPUT, PARENT, CANDIDATE, read_json, _load_run, load_oracle_dataset,
    code_snapshot, model_provider, make_gate, DEFAULT_MODEL, DEFAULT_REVISION,
    CachedEmbedder, run, coverage, cov_metrics, atomic_write_json,
    restore_visible_request, same_input, digest, trace_filename)


def main():
    plan = read_json(DEFAULT_OUTPUT / 'frozen-plan.json')
    assert code_snapshot(ROOT) == plan['source_snapshot']
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    _, parent = _load_run(PARENT)
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, 'cuda')
    gate, _ = make_gate(provider, argparse.Namespace(gate='semantic', document_admission='all',
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    candidate = run(dataset, dataset.cases, CANDIDATE, DEFAULT_OUTPUT / 'offline-full-candidate', gate, CachedEmbedder(provider))
    before, after = coverage(dataset.cases, parent), coverage(dataset.cases, candidate)
    atomic_write_json(DEFAULT_OUTPUT / 'full-candidate-coverage.json', after)
    atomic_write_json(DEFAULT_OUTPUT / 'full-parent-coverage.json', before)
    old = {r['question_id']: r for r in before['per_case']}
    differences = []
    for row in after['per_case']:
        q = row['question_id']
        deltas = {k: row[k] - old[q][k] for k in ('formed', 'retrieved', 'packed')}
        if any(deltas.values()):
            differences.append({'question_id': q, 'before': old[q], 'after': row, 'delta': deltas})
    unchanged = sum(same_input(restore_visible_request(c.question, parent[c.question_id]),
                              restore_visible_request(c.question, candidate[c.question_id])) for c in dataset.cases)
    for q, h in plan['parent_trace_sha256'].items():
        assert digest(PARENT / 'traces' / trace_filename(q)) == h
    assert code_snapshot(ROOT) == plan['source_snapshot']
    summary = {'cases': 500, 'baseline': cov_metrics(before), 'candidate': cov_metrics(after),
        'source_coverage_deltas': differences, 'unchanged_reader_inputs': unchanged,
        'is_accuracy': False, 'new_api_calls': 0, 'parent_traces_unchanged': True,
        'audit_driver_sha256': digest(Path(__file__))}
    atomic_write_json(DEFAULT_OUTPUT / 'full-coverage-audit.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'source_coverage_deltas'}), flush=True)


if __name__ == '__main__':
    main()
