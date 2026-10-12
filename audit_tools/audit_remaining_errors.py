"""Audit all remaining R21 failures; evaluation labels never enter runtime."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'ops')]
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.runner import atomic_write_json
from oracle_reader_pair import _load_run, _load_verdicts


def main():
    parent = ROOT / 'eval/reports/oracle-format-recovery-20261007'
    old = ROOT / 'eval/reports/oracle-narrative-20260928-v2'
    output = ROOT / 'eval/reports/oracle-error-audit-20261007'
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    _, traces = _load_run(parent)
    _, baseline = _load_run(old / 'full-wide-indexed')
    h, verdicts = _load_verdicts(parent / 'full-verdicts.jsonl', traces, dataset.sha256)
    bh, bv = _load_verdicts(old / 'full-verdicts.jsonl', baseline, dataset.sha256)
    assert h == bh and set(traces) == set(verdicts) == set(dataset.question_ids)
    rows, counts, statuses = [], Counter(), Counter()
    for case in dataset.cases:
        qid = case.question_id
        if verdicts[qid]:
            continue
        t = traces[qid]
        f = t['formation']
        gold = [turn for turn in case.turns if turn.has_answer]
        retrieved = {s for group in t['retrieval']['returned_source_refs'] for s in group}
        packed = {s for view in t['evidence']['views'] for s in view['source_refs']}
        bucket = 'all_gold_sources_visible_content_or_reasoning_issue'
        if case.should_abstain:
            bucket = 'unanswerable_wrong_answer'
        elif not gold:
            bucket = 'no_gold_turn_annotation'
        elif any(not f['turn_memory_counts'].get(turn.ref, 0) for turn in gold):
            bucket = 'some_gold_source_not_formed'
        elif any(f['turn_events'].get(turn.ref) not in retrieved for turn in gold):
            bucket = 'some_gold_source_not_retrieved'
        elif any(f['turn_events'].get(turn.ref) not in packed for turn in gold):
            bucket = 'some_gold_source_not_packed'
        counts[bucket] += 1
        statuses[t['reader']['status']] += 1
        rows.append({'question_id': qid, 'question_type': case.question_type,
            'regression_from_366': bv[qid], 'question': case.question,
            'expected_answer': case.answer, 'hypothesis': t['hypothesis'],
            'baseline_hypothesis': baseline[qid]['hypothesis'],
            'reader_status': t['reader']['status'],
            'missing_requirements': t['reader']['missing_requirements'],
            'route': json.loads(t['reader']['detail'])['route'],
            'bucket': bucket,
            'gold_turns': [{'ref': turn.ref, 'role': turn.role, 'content': turn.content,
                            'formed': f['turn_memory_counts'].get(turn.ref, 0),
                            'source': f['turn_events'].get(turn.ref),
                            'retrieved': f['turn_events'].get(turn.ref) in retrieved,
                            'packed': f['turn_events'].get(turn.ref) in packed} for turn in gold],
            'reader_visible_context': t['reader_context_text']})
    assert len(rows) == 103 and sum(row['regression_from_366'] for row in rows) == 9
    summary = {'cases': 500, 'incorrect': len(rows), 'regressions_from_366': 9,
        'buckets': dict(counts), 'statuses': dict(statuses),
        'dataset_sha256': dataset.sha256, 'new_api_calls': 0,
        'driver_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
        'caveat': 'Source coverage is not factual-span coverage; all labels remain evaluation-only.'}
    atomic_write_json(output / 'summary.json', summary)
    atomic_write_json(output / 'errors.json', rows)
    print(json.dumps(summary, ensure_ascii=False))
    for row in sorted(rows, key=lambda r: (not r['regression_from_366'], r['question_id'])):
        print(json.dumps({k: row[k] for k in ('question_id', 'regression_from_366', 'question',
            'expected_answer', 'hypothesis', 'missing_requirements', 'bucket')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
