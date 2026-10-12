"""Post-hoc error review only; never used by runtime routing or scoring."""
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
import oracle_statement_experiment as e
from audit_adjudicated_result import visible_passages

out = e.DEFAULT_OUTPUT
_, old = e._load_run(e.R27_ROOT / 'screen')
_, new = e._load_run(out / 'screen')
differences, stage_counts, extra_calls = [], Counter(), []
for q, t in new.items():
    a, b = visible_passages(old[q]), visible_passages(t)
    stages = json.loads(t['reader']['detail'])['stages']
    stage_counts[len(stages)] += 1
    if sum(s['calls'] for s in stages) > len(stages):
        extra_calls.append({'question_id': q, 'stages': stages})
    if a != b:
        differences.append({'question_id': q,
            'old_verbatim_chars': sum(len(v[0]) for v in a.values()),
            'new_verbatim_chars': sum(len(v[0]) for v in b.values()),
            'removed_memory_ids': sorted(a.keys() - b.keys()),
            'added_memory_ids': sorted(b.keys() - a.keys()),
            'changed': [{'memory_id': mid, 'before': a[mid][0], 'after': b[mid][0]}
                        for mid in a.keys() & b.keys() if a[mid] != b[mid]],
            'packing_before': old[q]['packing'], 'packing_after': t['packing']})
regressions = e.read_json(out / 'r27-transition-review.json')['details']
result = {'evaluation_only': True, 'runtime_must_not_read_this_file': True,
    'new_api_calls': 0, 'cases': len(new), 'unchanged_verbatim_evidence_cases': len(new) - len(differences),
    'changed_verbatim_evidence_cases': len(differences),
    'body_character_change': sum(r['new_verbatim_chars'] - r['old_verbatim_chars'] for r in differences),
    'stage_count_histogram': dict(stage_counts), 'extra_format_call_cases': len(extra_calls),
    'r27_transitions_in_changed_evidence': [r['question_id'] for r in regressions if r['question_id'] in {d['question_id'] for d in differences}],
    'interpretation': 'Formation/retrieval equality does not imply identical visible passages. Added annotations consume the unchanged independent 600-token document quota as well as global 6000 budget.',
    'differences': differences, 'extra_format_calls': extra_calls}
e.atomic_write_json(out / 'visible-span-and-stage-review.json', result)
print(json.dumps({k:v for k,v in result.items() if k not in {'differences','extra_format_calls'}}))
for r in regressions:
    q = r['question_id']
    print(json.dumps({'transition': r, 'visible_context': new[q]['reader_context_text'],
                      'stages': json.loads(new[q]['reader']['detail'])['stages']}, ensure_ascii=False))
