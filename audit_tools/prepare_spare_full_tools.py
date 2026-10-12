"""Prepare gated full-run tools without changing the frozen R29 experiment."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def swap(text, old, new):
    assert old in text, old
    return text.replace(old, new)


execution = (ROOT / 'audit_tools/run_prepared_source_full.py').read_text(encoding='utf-8')
execution = swap(execution, 'from oracle_source_retention_experiment import (',
                 'from oracle_spare_document_experiment import (\n    PARENT_VERDICTS,')
execution = swap(execution, "PARENT / 'full-verdicts.jsonl'", 'PARENT_VERDICTS')
execution = swap(execution, "report['overall']['correct'] >= 450", "report['overall']['correct'] >= 451")
execution = swap(execution, "            if record['reader']['status'] in {'ERROR', 'TIMEOUT'}:",
                 "            if record['reader']['status'] in {'ERROR', 'TIMEOUT'} and not record.get('replay', {}).get('exact_parent_input_reused'):")
execution = swap(execution, "    key = os.environ.get('DIVE_READER_API_KEY')", "    public = read_json(ROOT / 'eval/reports/oracle-statement-provenance-20261009/public-dataset-provenance.json')\n    assert public['local_sha256'] == public['public_lfs_sha256'] == dataset.sha256\n    assert public['model_destination'] == ENDPOINT\n    key = os.environ.get('DIVE_READER_API_KEY')")
target = ROOT / 'audit_tools/run_prepared_spare_full.py'
assert not target.exists()
target.write_text(execution, encoding='utf-8')

prep = (ROOT / 'audit_tools/prepare_source_candidate_full.py').read_text(encoding='utf-8')
prep = swap(prep, 'from dataclasses import fields', 'from dataclasses import fields, replace')
prep = swap(prep, 'from oracle_source_retention_experiment import (',
            'from oracle_spare_document_experiment import (\n    BASE,')
prep = swap(prep, 'from dive_memory.oracle.pipeline import PipelineConfig',
            'from dive_memory.oracle.pipeline import PipelineConfig\nfrom audit_adjudicated_result import visible_passages\nfrom oracle_v4_experiment import evidence_audit')
prep = swap(prep, "    before, after = coverage(dataset.cases, parent), coverage(dataset.cases, candidate)", '''    r27 = run(dataset, dataset.cases, replace(BASE, precise_detail_terms=True),
              output / 'offline-full-r27-control', gate, CachedEmbedder(provider))
    evidence_audit(r27, candidate, allow_coordinates=True)
    preserved = 0
    for q in dataset.question_ids:
        old_spans, new_spans = visible_passages(r27[q]), visible_passages(candidate[q])
        assert list(new_spans)[:len(old_spans)] == list(old_spans), q
        for mid, (content, refs) in old_spans.items():
            assert content in new_spans[mid][0] and refs == new_spans[mid][1], (q, mid)
            preserved += 1
    before, after = coverage(dataset.cases, parent), coverage(dataset.cases, candidate)''')
prep = swap(prep, '    predicted = Decimal(0)', '''    predicted = Decimal(0)
    fallback_cost_cases = []
    known = [t['reader']['usage'] for t in parent.values()
             if t['reader']['status'] not in {'ERROR', 'TIMEOUT'}
             and all(type(t['reader']['usage'][k]) is int for k in ('input_tokens', 'output_tokens'))]
    fallback_usage = {k: max(u[k] for u in known) for k in ('input_tokens', 'output_tokens')}''')
prep = swap(prep, "            if any(type(usage[k]) is not int for k in ('input_tokens', 'output_tokens')):\n                raise ValueError('Unknown usage blocks cost planning')", "            if parent[q]['reader']['status'] in {'ERROR', 'TIMEOUT'} or any(type(usage[k]) is not int for k in ('input_tokens', 'output_tokens')):\n                usage = fallback_usage\n                fallback_cost_cases.append(q)")
prep = swap(prep, "    report = {'cases': 500,", "    report = {'preserved_r27_passages': preserved, 'cases': 500,")
prep = swap(prep, "    proposal = {'full_run': 'NOT_RUN',", "    proposal = {'fallback_cost_cases': fallback_cost_cases, 'fallback_usage': fallback_usage, 'full_run': 'NOT_RUN',")
prep = swap(prep, 'run_prepared_source_full.py', 'run_prepared_spare_full.py')
target = ROOT / 'audit_tools/prepare_spare_full.py'
assert not target.exists()
target.write_text(prep, encoding='utf-8')
print('Prepared gated R29 full tools; no run launched and no API requests.')
