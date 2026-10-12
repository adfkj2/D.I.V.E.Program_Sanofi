"""R38: advice refusal review on frozen R37 evidence. Offline by default."""
import argparse
from collections import Counter
from decimal import Decimal
from dataclasses import replace
import getpass
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/p) for p in ('src','ops','audit_tools')]
import oracle_user_document_priority_experiment as e
import oracle_temporal_refusal_experiment as previous_reader
from oracle_refusal_gated_experiment import ModeClient, THINKING
from dive_memory.oracle.advice_refusal_reader import AdviceRefusalReader, advice_question
from dive_memory.oracle.premise_review_reader import NecessaryPremiseReader, REVIEW_VERSION
from dive_memory.oracle.reader import ReaderSpec
from audit_precise_detail_results import canonical

PARENT = previous_reader.OUTPUT
BASE_CONFIG = e.CANDIDATE_CONFIG
CANDIDATE_CONFIG = replace(BASE_CONFIG, contextual_fact_anchors=True)
EVIDENCE_PARENT = ROOT/'eval/reports/oracle-contextual-fact-retention-20261011'
OUTPUT = ROOT/'eval/reports/oracle-advice-refusal-review-20261011'
HELPERS = ('audit_advice_refusal_results.py','audit_adjudicated_result.py',
           'audit_precise_detail_results.py','replay_format_recovery.py')


def make_reader(meter):
    old = previous_reader.make_reader(meter)
    thinking = meter.thinking_transport if meter is not None else None
    reviewer = NecessaryPremiseReader(ModeClient(meter, thinking), ReaderSpec('deepseek', e.MODEL,
        prompt_version=REVIEW_VERSION, max_tokens=8192, timeout_seconds=120), policy=old.policy)
    return AdviceRefusalReader(old, reviewer)


def screen_ids(all_ids, legacy):
    return [*legacy,*sorted(set(all_ids)-set(legacy),
        key=lambda q:sha256(('r37-coverage-screen-v1:'+q).encode()).hexdigest())[:120]]


def eligible(before, after, legacy_before, legacy_after):
    return (after['overall']['fully_judged'] and after['overall']['cases']==200
        and after['overall']['correct']>=before['overall']['correct']+2
        and legacy_after['overall']['fully_judged'] and legacy_after['overall']['cases']==80
        and legacy_after['overall']['correct']>=legacy_before['overall']['correct']
        and after['unanswerable']['cases']==30
        and after['unanswerable']['correct']>=before['unanswerable']['correct'])


def cache_meter(out, label):
    paths = e.budget_ledgers(out)
    meter = e.MeteredClient(e.CacheTransport(e.EXTRA), out/label, max_calls=0,
        max_tokens=15000000, reuse_roots=[p.parent for p in paths])
    meter.normal_transport = e.CacheTransport(e.EXTRA)
    meter.thinking_transport = e.CacheTransport(THINKING)
    return meter


def check_frozen(out):
    plan=e.read_json(out/'frozen-plan.json')
    if e.code_snapshot(ROOT)!=plan['source_snapshot']:
        raise ValueError('Frozen source/dependencies changed')
    for name,h in plan['helper_sha256'].items():
        if e.digest(ROOT/'audit_tools'/name)!=h:
            raise ValueError('Frozen audit helper changed')
    for q,h in plan['parent_trace_sha256'].items():
        if e.digest(PARENT/'full/traces'/e.trace_filename(q))!=h:
            raise ValueError('Parent trace changed')
    for name,h in plan['parent_artifact_sha256'].items():
        if e.digest(PARENT/name)!=h:
            raise ValueError('Parent artifact changed')
    for name,h in plan['evidence_parent_sha256'].items():
        if e.digest(EVIDENCE_PARENT/name)!=h:
            raise ValueError('Evidence parent artifact changed')
    return plan


def prepare(out):
    dataset=e.load_oracle_dataset(ROOT/'eval/external/longmemeval/longmemeval_oracle.json')
    dataset.require_full_benchmark()
    manifest,parent=e._load_run(PARENT/'full')
    if set(parent)!=set(dataset.question_ids) or manifest['dataset']['sha256']!=dataset.sha256:
        raise ValueError('Parent dataset mismatch')
    if not e.read_json(PARENT/'full-post-run-audit.json')['audit_passed']:
        raise ValueError('Parent not audited')
    legacy=e.read_json(ROOT/'eval/reports/oracle-reasoning-20260929/frozen-plan.json')['ids']
    ids=screen_ids(dataset.question_ids,legacy)
    if len(ids)!=200 or len(set(ids))!=200 or sum(dataset.by_id()[q].should_abstain for q in ids)!=30:
        raise ValueError('Screen changed')
    existing=e.read_json(out/'frozen-plan.json') if (out/'frozen-plan.json').exists() else None
    rules=e.read_json(ROOT/'eval/reports/oracle-road-to-90-budget.json')
    plan={'dataset_sha256':dataset.sha256,'source_snapshot':e.code_snapshot(ROOT),
        'helper_sha256':{n:e.digest(ROOT/'audit_tools'/n) for n in HELPERS},
        'parent_trace_sha256':{q:e.digest(PARENT/'full/traces'/e.trace_filename(q)) for q in parent},
        'parent_artifact_sha256':{n:e.digest(PARENT/n) for n in
            ('full/manifest.json','full-verdicts.jsonl','full-post-run-audit.json')},
        'screen_ids':ids,'legacy_screen_ids':legacy,'selection_salt':'r37-coverage-screen-v1:', 'all_ids':list(dataset.question_ids),
        'config':CANDIDATE_CONFIG.to_dict(),'control_config':BASE_CONFIG.to_dict(),'reader_spec':make_reader(None).spec.to_dict(),
        'selection':'R36 +2/200 AND R37 +1/200 correct, original 80 no decrease, all 30 unanswerable no decrease',
        'evidence_parent_sha256':{n:e.digest(EVIDENCE_PARENT/n) for n in ('evidence-plan.json',
            'offline-candidate/manifest.json','screen-post-run-audit.json','screen-evaluation.json',
            'screen-verdicts.jsonl','frozen-plan.json')},
        'r37_screen_correct':e.read_json(EVIDENCE_PARENT/'screen-evaluation.json')['overall']['correct'],
        'routing':'R36 reader on R37 evidence; only valid final ABSTAIN and present advice request gets original necessary-premise review',
        'execution_mode':'frozen_R37_evidence_R36_reader_plus_advice_refusal_review_exact_cache',
        'screen_status':'reused public development set, not blind holdout',
        'extra_body':{'primary':e.EXTRA,'secondary':THINKING,'referee':e.EXTRA,'temporal_review':THINKING,'advice_review':THINKING,'judge':e.EXTRA},
        'aggregate_budget':existing['aggregate_budget'] if existing else rules,
        'initial_cost':existing['initial_cost'] if existing else e.peak_cost(e.budget_ledgers(out))}
    e.save_once(out/'frozen-plan.json',plan)
    check_frozen(out)
    # Evaluation-only baseline for the predeclared 200-case set; never passed to the reader.
    _,parent_verdicts=e._load_verdicts(PARENT/'full-verdicts.jsonl',parent,dataset.sha256)
    e.save_once(out/'screen-parent-evaluation.json',e.summarize(
        [dataset.by_id()[q] for q in ids],parent,parent_verdicts,exposed_ids=ids))
    for rel in [*plan['source_snapshot']['files'], *('audit_tools/'+n for n in HELPERS)]:
        content=(ROOT/rel).read_bytes()
        target=out/'source-snapshot'/rel
        if target.exists():
            if target.read_bytes()!=content:raise ValueError('Snapshot copy changed')
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(content)
    # Independently reproduce the complete parent chain before granting any cache discount.
    audit_path=out/'parent-cache-audit.json'
    if not audit_path.exists():
        meter=cache_meter(out,'parent-cache-audit-cache')
        reader=previous_reader.make_reader(meter)
        for q,t in parent.items():
            replay=e.make_record(dataset.by_id()[q].question,t,reader)
            if canonical(replay['reader'])!=canonical(t['reader']):
                raise ValueError('Parent reader exact replay mismatch')
        e.save_once(audit_path,{'cases':500,'reader_spec':reader.spec.to_dict(),
            'new_api_calls':meter.summary()['attempts'],'cache_hits':meter.hits,
            'parent_trace_sha256':plan['parent_trace_sha256']})
    audit=e.read_json(audit_path)
    if audit['new_api_calls']!=0 or audit['parent_trace_sha256']!=plan['parent_trace_sha256']:
        raise ValueError('Parent cache audit changed')
    if not e.read_json(EVIDENCE_PARENT/'screen-post-run-audit.json')['audit_passed']:
        raise ValueError('R37 screen not audited')
    if plan['r37_screen_correct']!=180:
        raise ValueError('R37 baseline changed')
    cp=out/'evidence-parent-cache-audit.json'
    if not cp.exists():
        old_records=e._load_run(EVIDENCE_PARENT/'screen')[1]
        meter=cache_meter(out,'evidence-parent-audit-cache')
        reader=previous_reader.make_reader(meter)
        for q,t in old_records.items():
            replay=e.make_record(dataset.by_id()[q].question,t,reader)
            if canonical(replay['reader'])!=canonical(t['reader']):
                raise ValueError('R37 reader exact replay mismatch')
        e.save_once(cp,{'cases':len(old_records),'new_api_calls':meter.summary()['attempts'],'cache_hits':meter.hits})
    return dataset,manifest,parent,plan


def load_records(out, ids, *, full):
    records={}
    for q in ids:
        path=out/('full' if full else 'screen')/'traces'/e.trace_filename(q)
        if full and not path.exists():path=out/'screen/traces'/e.trace_filename(q)
        if path.exists():records[q]=e.read_json(path)
    return records


def validate_checkpoint(q,t,source,plan):
    if t['question_id']!=q or t['reader']['spec']!=plan['reader_spec']:
        raise ValueError('Checkpoint identity/spec mismatch')
    e.previous.evidence_audit({q:source},{q:t})
    e.audit_citations(t)


def preflight(out,dataset,parent,sources,plan,*,full=False):
    label='full' if full else 'screen'
    ids=plan['all_ids'] if full else plan['screen_ids']
    records=load_records(out,ids,full=full)
    for q,t in records.items():validate_checkpoint(q,t,sources[q],plan)
    meter=cache_meter(out,label+'-preflight-cache')
    reader=make_reader(meter)
    missing=[]
    for q in ids:
        if q in records:continue
        try:reader.generate(e.restore_visible_request(dataset.by_id()[q].question,sources[q]))
        except e.PilotStopped:missing.append(q)
    predicted=envelope=Decimal(0)
    for q in missing:
        old=parent[q]['reader']['usage']
        delta=max(0,len(sources[q]['reader_context_text'].encode())-len(parent[q]['reader_context_text'].encode()))
        i=old.get('input_tokens')
        if type(i)!=int:i=3*(len(sources[q]['reader_context_text'].encode())+4096)
        else:i+=3*delta
        o=max(2048,old.get('output_tokens') or 8192)
        predicted+=Decimal('1.2')*Decimal(2*i+8*o)/1000000
        envelope+=Decimal(2*(2*(i+4096)+8*(512+8192+1024+8192)))/1000000
    header=json.loads((PARENT/'full-verdicts.jsonl').read_text(encoding='utf-8').splitlines()[0])
    judged={}
    for name,allowed in [(label+'-verdicts.jsonl',ids)]+([('screen-verdicts.jsonl',plan['screen_ids'])] if full else []):
        judged.update(e.load_verdict_checkpoints(out/name,header,set(allowed),records))
    pending_judge=len(set(ids)-set(judged))
    judge_cost=Decimal(pending_judge*3024)/1000000
    predicted+=judge_cost
    envelope+=judge_cost
    rules=e.read_json(ROOT/'eval/reports/oracle-road-to-90-budget.json')
    ceiling=min(Decimal(rules['planning_stop_cny']),Decimal(rules['user_limit_cny'])-Decimal(rules['settlement_reserve_cny']))
    current=e.peak_cost(e.budget_ledgers(out))
    remaining=ceiling-Decimal(current['peak_upper_cny'])
    result={'cases':len(ids),'saved_checkpoints':len(records),'exact_cached_remaining':len(ids)-len(records)-len(missing),
        'uncached_case_ids':missing,'cache_hits':meter.hits,'new_api_calls':0,
        'pending_judge_cases':pending_judge,'predicted_reader_and_judge_cny':str(predicted),
        'planning_allowance_cny':str(predicted*Decimal('1.25')),'two_max_output_attempts_envelope_cny':str(envelope),
        'method':'Observed complete parent reader tokens with 2048 floor, 20% format repair, pending judges, 25% margin; per-request hard guard',
        'aggregate_cost':current,'remaining_planning_cny':str(remaining),'cumulative_stop_cny':str(ceiling),
        'fits_budget':predicted*Decimal('1.25')<remaining}
    name=label+'-cost-preflight.json'
    if (out/name).exists() and e.read_json(out/name)!=result:
        name=label+'-cost-preflight-'+sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()[:12]+'.json'
    e.save_once(out/name,result)
    print(json.dumps(result),flush=True)
    return result


def run_stage(out,dataset,manifest,parent,sources,plan,meter,*,full=False):
    label='full' if full else 'screen'
    ids=plan['all_ids'] if full else plan['screen_ids']
    reader=make_reader(meter)
    records=load_records(out,ids,full=full)
    for n,q in enumerate(ids,1):
        if q not in records:
            meter.phase='reader-'+label
            records[q]=e.make_record(dataset.by_id()[q].question,sources[q],reader)
        t=records[q]
        validate_checkpoint(q,t,sources[q],plan)
        e.save_once(out/label/'traces'/e.trace_filename(q),t)
        if t['reader']['status'] in {'ERROR','TIMEOUT'}:
            raise e.PilotStopped('Transport failure retained, no automatic resampling')
        if n%10==0:print(json.dumps({'phase':label,'generated':n,'total':len(ids)}),flush=True)
    e.save_once(out/label/'manifest.json',{'schema_version':'dive-advice-refusal-review-v1','dataset':manifest['dataset'],
        'configuration':plan['config'],'source_snapshot':plan['source_snapshot'],'reader_spec':plan['reader_spec']})
    submission=[{'question_id':q,'hypothesis':records[q]['hypothesis'] or e.CANONICAL_ABSTENTION_HYPOTHESIS} for q in ids]
    e.audit_submission(submission,records)
    e.atomic_write_text(out/label/'submission.jsonl',''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in submission))
    if e.validate_submission(out/label/'submission.jsonl',expected_ids=ids,require_full=full).status!='VALID':
        raise ValueError('Invalid submission')
    # Correctness enters only after all model outputs are frozen.
    header,pv=e._load_verdicts(PARENT/'full-verdicts.jsonl',parent,dataset.sha256)
    meter.client=meter.normal_transport
    judge=e.LLMJudge(meter,e.JudgeSpec(**header['judge']))
    path=out/(label+'-verdicts.jsonl')
    checkpoints=e.load_verdict_checkpoints(path,header,set(ids),records)
    if full:checkpoints.update(e.load_verdict_checkpoints(out/'screen-verdicts.jsonl',header,set(plan['screen_ids']),records))
    rows=[]
    for q in ids:
        t=records[q]
        h=sha256(('null' if t['hypothesis'] is None else t['hypothesis']).encode()).hexdigest()[:16]
        if t['hypothesis']==parent[q]['hypothesis'] and t['reader']['status']==parent[q]['reader']['status']:
            row={'question_id':q,'hypothesis_sha256':h,'correct':pv[q],'raw':'EXACT_PARENT_VERDICT','error':None}
        elif q in checkpoints:row=checkpoints[q]
        else:
            meter.phase='judge-'+label
            v=e.score_trace(judge,dataset.by_id()[q],t)
            row={'question_id':q,'hypothesis_sha256':h,'correct':v.correct,'raw':v.raw,'error':v.error}
        rows.append(row)
        e.save_verdicts(path,header,rows)
    _,verdicts=e._load_verdicts(path,records,dataset.sha256)
    cases=[dataset.by_id()[q] for q in ids]
    before=e.summarize(cases,parent,pv)
    score=e.summarize(cases,records,verdicts,exposed_ids=plan['screen_ids'])
    e.save_once(out/(label+'-evaluation.json'),score)
    e.save_once(out/(label+'-comparison.json'),e._accuracy({q:parent[q] for q in ids},records,pv,verdicts,
        {c.question_id:c.question_type for c in cases}))
    if not full:
        legacy_cases=[dataset.by_id()[q] for q in plan['legacy_screen_ids']]
        lb=e.summarize(legacy_cases,parent,pv)
        la=e.summarize(legacy_cases,records,verdicts)
        e.save_once(out/'selection.json',{'eligible_for_full':eligible(before,score,lb,la) and score['overall']['correct']>=plan['r37_screen_correct']+1,
            'rule':plan['selection'],'legacy_before':lb,'legacy_after':la})
    check_frozen(out)
    e.save_once(out/(label+'-completion.json'),{'complete':True,'cases':len(ids),'correct':score['overall']['correct'],
        'fully_judged':score['overall']['fully_judged'],'official_score':'NOT_EVALUATED','execution_mode':plan['execution_mode']})
    print(json.dumps({'stage':label,'overall':score['overall'],'answerable':score['answerable'],
        'unanswerable':score['unanswerable'],'eligible_for_full':e.read_json(out/'selection.json')['eligible_for_full'] if not full else None}),flush=True)


def prepare_sources(out,dataset,parent,plan):
    evidence=e.read_json(EVIDENCE_PARENT/'evidence-plan.json')
    if set(evidence['candidate_trace_sha256'])!=set(plan['all_ids']):
        raise ValueError('Incomplete evidence freeze')
    for q,h in evidence['candidate_trace_sha256'].items():
        if e.digest(EVIDENCE_PARENT/'offline-candidate/traces'/e.trace_filename(q))!=h:
            raise ValueError('Candidate evidence changed')
    manifest,sources=e._load_run(EVIDENCE_PARENT/'offline-candidate')
    if manifest['configuration']['oracle_pipeline']!=CANDIDATE_CONFIG.to_dict():
        raise ValueError('R37 evidence configuration mismatch')
    e.save_once(out/'evidence-plan.json',{'cases':500,'unchanged_R37_evidence':True,
        'new_api_calls':0,'source_directory':str(EVIDENCE_PARENT/'offline-candidate'),
        'candidate_trace_sha256':evidence['candidate_trace_sha256']})
    return sources


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',type=Path,default=OUTPUT)
    ap.add_argument('--paid',action='store_true')
    ap.add_argument('--full',action='store_true')
    args=ap.parse_args()
    out=args.output.resolve()
    dataset,manifest,parent,plan=prepare(out)
    if args.full and not (e.read_json(out/'selection.json')['eligible_for_full']
        and e.read_json(out/'screen-post-run-audit.json')['audit_passed']):
        raise e.PilotStopped('Screen promotion and independent audit required')
    sources=prepare_sources(out,dataset,parent,plan)
    cost=preflight(out,dataset,parent,sources,plan,full=args.full)
    if not args.paid:return
    if not cost['fits_budget']:raise e.PilotStopped('Cost exceeds authorization; no request sent')
    public=e.read_json(ROOT/'eval/reports/oracle-statement-provenance-20261009/public-dataset-provenance.json')
    if not(public['exact_public_file_match'] and public['local_sha256']==public['public_lfs_sha256']==dataset.sha256
        and public['model_destination']==e.ENDPOINT):raise e.PilotStopped('Approved public dataset/destination mismatch')
    key=os.environ.get('DIVE_READER_API_KEY') or getpass.getpass('DeepSeek key (hidden, never saved): ')
    if not key.strip() or e.MODEL not in [r['id'] for r in e.private_get('models',key).get('data',[])]:
        raise e.PilotStopped('Missing credential or model unavailable')
    guard=e.TotalBudgetGuard(key,out,limit=Decimal(cost['remaining_planning_cny']))
    guard.check(0,force=True)
    normal=e.OpenAICompatibleChatClient(e.ENDPOINT,key,provider='deepseek',extra_body=e.EXTRA)
    thinking=e.OpenAICompatibleChatClient(e.ENDPOINT,key,provider='deepseek',extra_body=THINKING)
    ledgers=e.budget_ledgers(out)
    meter=e.MeteredClient(normal,out/'api-cache',max_calls=1600,max_tokens=15000000,
        reuse_roots=[p.parent for p in ledgers if p.parent!=out/'api-cache'],before_request=guard.check,
        currency_guard=e.CurrencyBudget(ledgers,limit=Decimal(cost['cumulative_stop_cny'])))
    meter.normal_transport,meter.thinking_transport=normal,thinking
    try:
        run_stage(out,dataset,manifest,parent,sources,plan,meter,full=args.full)
        guard.check(meter.summary()['attempts'],force=True)
    except (Exception,e.PilotStopped) as exc:
        e.atomic_write_json(out/'stopped.json',{'status':'STOPPED','error_type':type(exc).__name__,
            'stage':'full' if args.full else 'screen','no_complete_score_claimed':True})
        raise
    finally:
        e.atomic_write_json(out/'usage.json',meter.summary())
        e.atomic_write_json(out/'aggregate-peak-cost.json',e.peak_cost(ledgers))


if __name__=='__main__':
    try:main()
    except e.PilotStopped as exc:
        print(str(exc),flush=True)
        sys.exit(2)
