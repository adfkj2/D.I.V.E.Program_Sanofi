"""Independent R38 exact-cache, citations, scoring and budget audit; no network allowed."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/p) for p in ('src','ops','audit_tools')]
import oracle_advice_refusal_experiment as x
e=x.e


def audit(out,*,full=False):
    label='full' if full else 'screen'
    plan=x.check_frozen(out)
    dataset=e.load_oracle_dataset(ROOT/'eval/external/longmemeval/longmemeval_oracle.json')
    assert dataset.sha256==plan['dataset_sha256']
    ids=plan['all_ids'] if full else plan['screen_ids']
    _,parent=e._load_run(x.PARENT/'full')
    sources=x.prepare_sources(out,dataset,parent,plan)
    manifest,records=e._load_run(out/label)
    assert set(records)==set(ids)
    ledgers=e.budget_ledgers(out)
    before={str(p):e.digest(p) for p in ledgers if p.exists()}
    meter=x.cache_meter(out,label+'-audit-cache')
    reader=x.make_reader(meter)
    citations=changed_referees=unchanged_outputs=0
    for q in ids:
        replay=e.make_record(dataset.by_id()[q].question,sources[q],reader)
        t=records[q]
        assert x.canonical(replay['reader'])==x.canonical(t['reader']),q
        for key in ('hypothesis','hypothesis_origin','status','failure','calls','tokens','replay'):
            assert replay[key]==t[key],(q,key)
        x.validate_checkpoint(q,t,sources[q],plan)
        citations+=e.audit_citations(t)
        if t['reader_context_text']!=parent[q]['reader_context_text']:
            changed_referees+=1
        if t['hypothesis']==parent[q]['hypothesis']:unchanged_outputs+=1
    assert manifest['source_snapshot']==plan['source_snapshot'] and manifest['reader_spec']==plan['reader_spec']
    submission=[json.loads(l) for l in (out/label/'submission.jsonl').read_text(encoding='utf-8').splitlines()]
    e.audit_submission(submission,records)
    assert e.validate_submission(out/label/'submission.jsonl',expected_ids=ids,require_full=full).status=='VALID'
    header,verdicts=e._load_verdicts(out/(label+'-verdicts.jsonl'),records,dataset.sha256)
    old_header,pv=e._load_verdicts(x.PARENT/'full-verdicts.jsonl',parent,dataset.sha256)
    assert header==old_header
    rows={r['question_id']:r for r in map(json.loads,(out/(label+'-verdicts.jsonl')).read_text(encoding='utf-8').splitlines()) if not r.get('_header')}
    meter.client=meter.normal_transport
    judge=e.LLMJudge(meter,e.JudgeSpec(**header['judge']))
    origins,transitions,details=Counter(),Counter(),[]
    for q in ids:
        t=records[q]
        row=rows[q]
        if t['hypothesis']==parent[q]['hypothesis'] and t['reader']['status']==parent[q]['reader']['status']:
            assert row['raw']=='EXACT_PARENT_VERDICT' and row['correct']==pv[q]
            origins['exact_parent_verdict']+=1
        else:
            v=e.score_trace(judge,dataset.by_id()[q],t)
            assert (v.correct,v.raw,v.error)==(row['correct'],row['raw'],row['error']),q
            origins['cached_or_deterministic_verdict']+=1
        transition=('both_correct' if verdicts[q] else 'regression') if pv[q] else ('improvement' if verdicts[q] else 'both_wrong')
        transitions[transition]+=1
        if not verdicts[q] or transition=='improvement':
            c=dataset.by_id()[q]
            details.append({'question_id':q,'question':c.question,'reference_answer':c.answer,
                'parent_hypothesis':parent[q]['hypothesis'],'hypothesis':t['hypothesis'],
                'status':t['reader']['status'],'transition':transition})
    if not full:
        r37=e._load_run(x.EVIDENCE_PARENT/'screen')[1]
        _,v37=e._load_verdicts(x.EVIDENCE_PARENT/'screen-verdicts.jsonl',r37,dataset.sha256)
        e.save_once(out/'screen-r37-comparison.json',e._accuracy(r37,records,v37,verdicts,
            {q:dataset.by_id()[q].question_type for q in ids}))
    cases=[dataset.by_id()[q] for q in ids]
    score=e.summarize(cases,records,verdicts,exposed_ids=plan['screen_ids'])
    assert score==e.read_json(out/(label+'-evaluation.json'))
    completion=e.read_json(out/(label+'-completion.json'))
    assert completion['complete'] and completion['fully_judged'] and completion['correct']==sum(verdicts.values())
    if not full:
        legacy=[dataset.by_id()[q] for q in plan['legacy_screen_ids']]
        lb=e.summarize(legacy,parent,pv);la=e.summarize(legacy,records,verdicts)
        selection=e.read_json(out/'selection.json')
        assert (lb,la)==(selection['legacy_before'],selection['legacy_after'])
        assert (x.eligible(e.summarize(cases,parent,pv),score,lb,la) and score['overall']['correct']>=plan['r37_screen_correct']+1)==selection['eligible_for_full']
    assert before=={str(p):e.digest(p) for p in ledgers if p.exists()}
    assert meter.summary()['attempts']==0 and not (out/(label+'-audit-cache/ledger.json')).exists()
    path=out/'api-cache/ledger.json'
    own=e.read_json(path)['calls'] if path.exists() else []
    assert all(r['status']=='completed' for r in own)
    cost=e.peak_cost(ledgers)
    assert Decimal(cost['peak_upper_cny'])<Decimal(plan['aggregate_budget']['planning_stop_cny'])
    report={'audit_passed':True,'cases':len(ids),'citations':citations,'new_api_calls':0,
        'cache_hits':meter.hits,'reader_replay_cases':len(ids),'changed_input_cases':changed_referees,
        'unchanged_hypotheses':unchanged_outputs,'verdict_provenance':dict(origins),'transitions':dict(transitions),
        'new_requests':len(own),'new_requests_by_phase':dict(Counter(r['phase'] for r in own)),
        'new_peak_cost_cny':str(Decimal(cost['peak_upper_cny'])-Decimal(plan['initial_cost']['peak_upper_cny'])),
        'aggregate_cost':cost,'budget_ledgers_unchanged':True,'audit_driver_sha256':e.digest(Path(__file__)),
        'score':score['overall']}
    e.save_once(out/(label+'-post-run-audit.json'),report)
    e.save_once(out/(label+'-error-review.json'),{'evaluation_only':True,'runtime_must_not_read_this_file':True,'details':details})
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',type=Path,default=x.OUTPUT)
    ap.add_argument('--full',action='store_true')
    args=ap.parse_args()
    audit(args.output.resolve(),full=args.full)
