"""Independent R36 exact-cache, citations, scoring and budget audit; no network allowed."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/p) for p in ('src','ops','audit_tools')]
import oracle_temporal_refusal_experiment as x
e=x.e


def audit(out,*,full=False):
    label='full' if full else 'screen'
    plan=x.check_frozen(out)
    dataset=e.load_oracle_dataset(ROOT/'eval/external/longmemeval/longmemeval_oracle.json')
    assert dataset.sha256==plan['dataset_sha256']
    ids=plan['all_ids'] if full else plan['screen_ids']
    _,parent=e._load_run(x.PARENT/'full')
    manifest,records=e._load_run(out/label)
    assert set(records)==set(ids)
    ledgers=e.budget_ledgers(out)
    before={str(p):e.digest(p) for p in ledgers if p.exists()}
    meter=x.cache_meter(out,label+'-audit-cache')
    reader=x.make_reader(meter)
    citations=changed_referees=unchanged_outputs=0
    for q in ids:
        replay=e.make_record(dataset.by_id()[q].question,parent[q],reader)
        t=records[q]
        assert x.canonical(replay['reader'])==x.canonical(t['reader']),q
        for key in ('hypothesis','hypothesis_origin','status','failure','calls','tokens','replay'):
            assert replay[key]==t[key],(q,key)
        x.validate_checkpoint(q,t,parent[q],plan)
        citations+=e.audit_citations(t)
        b=json.loads(t['reader']['detail'])
        old={k:v for k,v in parent[q]['reader'].items() if k!='spec'}
        assert x.canonical(old)==x.canonical(b['stages'][0]),q
        routed=(parent[q]['reader']['status']=='ABSTAIN' and x.temporal_question(dataset.by_id()[q].question))
        if routed:
            changed_referees+=1
            assert b['route']=='temporal_premise_review' and len(b['stages'])==2
        else:
            assert b['route']=='unchanged_non_temporal_refusal' and len(b['stages'])==1
            assert t['hypothesis']==parent[q]['hypothesis'] and t['reader']['status']==parent[q]['reader']['status']
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
    cases=[dataset.by_id()[q] for q in ids]
    score=e.summarize(cases,records,verdicts,exposed_ids=plan['screen_ids'])
    assert score==e.read_json(out/(label+'-evaluation.json'))
    completion=e.read_json(out/(label+'-completion.json'))
    assert completion['complete'] and completion['fully_judged'] and completion['correct']==sum(verdicts.values())
    if not full:assert x.eligible(e.summarize(cases,parent,pv),score)==e.read_json(out/'selection.json')['eligible_for_full']
    assert before=={str(p):e.digest(p) for p in ledgers if p.exists()}
    assert meter.summary()['attempts']==0 and not (out/(label+'-audit-cache/ledger.json')).exists()
    path=out/'api-cache/ledger.json'
    own=e.read_json(path)['calls'] if path.exists() else []
    assert all(r['status']=='completed' for r in own)
    cost=e.peak_cost(ledgers)
    assert Decimal(cost['peak_upper_cny'])<Decimal(plan['aggregate_budget']['planning_stop_cny'])
    report={'audit_passed':True,'cases':len(ids),'citations':citations,'new_api_calls':0,
        'cache_hits':meter.hits,'whole_parent_chain_exact':len(ids),'temporal_review_cases':changed_referees,
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
