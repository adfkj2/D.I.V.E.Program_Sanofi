"""Create separate R28 drivers once, preserving frozen earlier sources."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def swap(text, old, new):
    assert old in text, old
    return text.replace(old, new)


driver = (ROOT / 'ops/oracle_precise_detail_experiment.py').read_text(encoding='utf-8')
driver = swap(driver, 'R27: precise modal inflections, compared with the audited R26 evidence writer.',
              'R28: explicit statement clocks/roles plus R27, compared with audited R26.')
driver = swap(driver, 'CANDIDATE = replace(BASE, precise_detail_terms=True)',
              'CANDIDATE = replace(BASE, precise_detail_terms=True, statement_provenance=True)')
driver = swap(driver, "oracle-precise-modality-20261008'", "oracle-statement-provenance-20261009'")
driver = swap(driver, "'1.80'", "'2.50'")
driver = swap(driver, 'from dive_memory.oracle.providers import OpenAICompatibleChatClient',
              'from restore_statement_request import restore_visible_request\nfrom dive_memory.oracle.providers import OpenAICompatibleChatClient')
driver = swap(driver, "'audit_adjudicated_result.py', 'replay_format_recovery.py')",
              "'audit_adjudicated_result.py', 'replay_format_recovery.py',\n            'restore_statement_request.py', 'audit_screen_verdicts.py', 'audit_statement_results.py')")
driver = swap(driver, 'type loss <=1; added sources or visible text',
              'type loss <=1; source clocks/roles count toward unchanged context budget')
target = ROOT / 'ops/oracle_statement_experiment.py'
assert not target.exists()
target.write_text(driver, encoding='utf-8')
auditor = (ROOT / 'audit_tools/audit_precise_detail_results.py').read_text(encoding='utf-8')
auditor = swap(auditor, 'for R27.', 'for R28.')
auditor = swap(auditor, 'oracle_precise_detail_experiment', 'oracle_statement_experiment')
target = ROOT / 'audit_tools/audit_statement_results.py'
assert not target.exists()
target.write_text(auditor, encoding='utf-8')
print('Created separate R28 experiment and audit drivers; no API calls.')
