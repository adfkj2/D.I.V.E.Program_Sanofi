"""Evaluation-only source and visible-span audit; no model requests or runtime routing."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'ops')]
from dive_memory.oracle.loader import load_oracle_dataset
from oracle_reader_pair import _load_run, _load_verdicts
from dive_memory.oracle.runner import atomic_write_json


def main():
    parent = ROOT/'eval/reports/oracle-explicit-document-priority-20261010'
    out = ROOT/'eval/reports/oracle-r33-error-diagnosis-20261011'
    dataset = load_oracle_dataset(ROOT/'eval/external/longmemeval/longmemeval_oracle.json')
    _, traces = _load_run(parent/'full')
    _, verdicts = _load_verdicts(parent/'full-verdicts.jsonl', traces, dataset.sha256)
    assert set(traces) == set(verdicts) == set(dataset.question_ids)
    rows, buckets, statuses = [], Counter(), Counter()
    for case in dataset.cases:
        q = case.question_id
        if verdicts[q]:
            continue
        t = traces[q]
        f = t['formation']
        gold = [turn for turn in case.turns if turn.has_answer]
        retrieved = {s for group in t['retrieval']['returned_source_refs'] for s in group}
        views = t['evidence']['views']
        packed = {s for v in views for s in v['source_refs']}
        bucket = 'source_visible_necessary_span_or_reading_requires_inspection'
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
        buckets[bucket] += 1
        statuses[t['reader']['status']] += 1
        sources = []
        for turn in gold:
            event = f['turn_events'].get(turn.ref)
            sources.append({'ref':turn.ref, 'role':turn.role, 'content':turn.content,
                'formed':f['turn_memory_counts'].get(turn.ref,0), 'retrieved':event in retrieved,
                'packed':event in packed,
                'visible_views':[v for v in views if event in v['source_refs']]})
        rows.append({'question_id':q,'question_type':case.question_type,'question':case.question,
            'reference_answer':case.answer,'hypothesis':t['hypothesis'],
            'status':t['reader']['status'],'missing':t['reader']['missing_requirements'],
            'bucket':bucket,'gold_sources':sources,'context':t['reader_context_text'],
            'reader_detail':json.loads(t['reader']['detail'])})
    assert len(rows)==74 and statuses=={'ABSTAIN':43,'ANSWER':31}
    summary={'evaluation_only':True,'runtime_must_not_read_this_file':True,'cases':500,
        'incorrect':len(rows),'buckets':dict(buckets),'statuses':dict(statuses),
        'new_api_calls':0,'dataset_sha256':dataset.sha256,
        'driver_sha256':sha256(Path(__file__).read_bytes()).hexdigest(),
        'caveat':'Source visibility does not prove the necessary sentence is visible. No automatic semantic root-cause claim.'}
    for name,data in [('summary.json',summary),('errors.json',rows)]:
        path=out/name
        if path.exists():
            assert json.loads(path.read_text(encoding='utf-8'))==data
        else:
            atomic_write_json(path,data)
    print(json.dumps(summary,ensure_ascii=False))
    for r in rows:
        print(json.dumps({k:r[k] for k in ('question_id','bucket','status','missing')},ensure_ascii=False))


if __name__=='__main__':
    main()
