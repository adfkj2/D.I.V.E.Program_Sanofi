"""Create separate R29 drivers once; leave all earlier frozen sources intact."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def swap(text, old, new):
    assert old in text, old
    return text.replace(old, new)


driver = (ROOT / 'ops/oracle_statement_experiment.py').read_text(encoding='utf-8')
driver = swap(driver, 'R28: explicit statement clocks/roles plus R27, compared with audited R26.',
              'R29: preserve existing evidence and use spare budget for retrieved documents.')
driver = swap(driver, 'CANDIDATE = replace(BASE, precise_detail_terms=True, statement_provenance=True)',
              'CANDIDATE = replace(BASE, precise_detail_terms=True, spare_document_context=True)')
driver = swap(driver, "oracle-statement-provenance-20261009'", "oracle-spare-documents-20261009'")
driver = swap(driver, "'2.50'", "'1.80'")
driver = swap(driver, "'audit_statement_results.py'", "'audit_spare_results.py'")
driver = swap(driver, 'type loss <=1; source clocks/roles count toward unchanged context budget',
              'type loss <=1; preserve every R27 visible span and source; global budget unchanged')
driver = swap(driver, "        key = os.environ.get('DIVE_READER_API_KEY')", "        preflight = read_json(output / 'screen-cost-preflight.json')\n        if not preflight['fits_screen_budget']:\n            raise PilotStopped('Screen cost projection does not fit its allocation')\n        public = read_json(ROOT / 'eval/reports/oracle-statement-provenance-20261009/public-dataset-provenance.json')\n        assert public['local_sha256'] == public['public_lfs_sha256'] == dataset.sha256\n        assert public['model_destination'] == ENDPOINT\n        key = os.environ.get('DIVE_READER_API_KEY')")
target = ROOT / 'ops/oracle_spare_document_experiment.py'
assert not target.exists()
target.write_text(driver, encoding='utf-8')
auditor = (ROOT / 'audit_tools/audit_precise_detail_results.py').read_text(encoding='utf-8')
auditor = swap(auditor, 'for R27.', 'for R29.')
auditor = swap(auditor, 'oracle_precise_detail_experiment', 'oracle_spare_document_experiment')
auditor = swap(auditor, 'from dive_memory.token_budget import ApproximateTokenCounter',
              'from dive_memory.token_budget import ApproximateTokenCounter\nfrom oracle_spare_document_experiment import R27_ROOT\nfrom audit_adjudicated_result import visible_passages')
marker = '    metrics = {}\n'
checks = '''    assert digest(R27_ROOT / 'screen-verdicts.jsonl') == plan['r27_verdicts_sha256']
    for q, h in plan['r27_trace_sha256'].items():
        assert digest(R27_ROOT / 'screen/traces' / trace_filename(q)) == h
    _, r27 = _load_run(R27_ROOT / 'screen')
    preserved = 0
    expanded_cases = 0
    new_chars = 0
    for q in plan['screen_ids']:
        before, after = visible_passages(r27[q]), visible_passages(candidate[q])
        assert list(after)[:len(before)] == list(before), q
        for mid, (content, refs) in before.items():
            assert content in after[mid][0] and refs == after[mid][1], (q, mid)
            preserved += 1
        delta = sum(len(v[0]) for v in after.values()) - sum(len(v[0]) for v in before.values())
        assert delta >= 0
        new_chars += delta
        expanded_cases += delta > 0
        assert candidate[q]['packing']['spare_document_expansion']['baseline_visible_preserved']
    metrics = {}
'''
auditor = swap(auditor, marker, checks)
auditor = swap(auditor, "    preflight = {'changed_inputs': len(changed),", "    preflight = {'preserved_r27_passages': preserved, 'expanded_cases': expanded_cases,\n        'new_verbatim_characters': new_chars, 'changed_inputs': len(changed),")
target = ROOT / 'audit_tools/audit_spare_results.py'
assert not target.exists()
target.write_text(auditor, encoding='utf-8')
print('Created R29-only experiment/audit drivers; no API requests.')
