from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import oracle_contextual_fact_experiment as x
from test_oracle_user_document_experiment import trace, write_json, write_verdicts, verdict


def test_extra_screen_is_reproducible_and_keeps_all_legacy_cases():
    all_ids=[f'case-{i}' for i in range(500)];legacy=all_ids[:80]
    a=x.screen_ids(all_ids,legacy)
    assert a==x.screen_ids(list(reversed(all_ids)),legacy)
    assert len(a)==len(set(a))==200 and a[:80]==legacy
    assert set(a[80:])<=set(all_ids)-set(legacy)


def score(n,correct,unanswerable=28):
    return {'overall':{'cases':n,'correct':correct,'fully_judged':True},
        'unanswerable':{'cases':30,'correct':unanswerable}}


def test_new_screen_cannot_hide_legacy_or_unanswerable_regression():
    b=score(200,175);a=score(200,177);lb=score(80,76);la=score(80,76)
    assert x.eligible(b,a,lb,la)
    assert not x.eligible(b,score(200,176),lb,la)
    assert not x.eligible(b,a,lb,score(80,75))
    assert not x.eligible(b,score(200,180,27),lb,la)
    a['overall']['fully_judged']=False
    assert not x.eligible(b,a,lb,la)


def test_frozen_evidence_checked_before_loading(tmp_path,monkeypatch):
    q='q1';p=tmp_path/'offline-candidate/traces'/x.e.trace_filename(q)
    write_json(p,trace())
    write_json(tmp_path/'evidence-plan.json',{'candidate_trace_sha256':{q:x.e.digest(p)}})
    write_json(p,trace(context='mutated'))
    loader=Mock(side_effect=AssertionError('must hash first'))
    monkeypatch.setattr(x.e,'_load_run',loader)
    with pytest.raises(ValueError,match='evidence changed'):
        x.prepare_sources(tmp_path,None,{}, {'all_ids':[q]})
    loader.assert_not_called()


@pytest.mark.parametrize('full',[False,True])
def test_failed_checkpoint_does_not_generate_again(tmp_path,monkeypatch,full):
    t=trace(status='ERROR',hypothesis=None)
    p=tmp_path/'screen/traces'/x.e.trace_filename('q1');write_json(p,t)
    original=p.read_bytes()
    monkeypatch.setattr(x,'make_reader',lambda m:object())
    monkeypatch.setattr(x,'validate_checkpoint',lambda *args:None)
    make=Mock(side_effect=AssertionError('must not generate'))
    monkeypatch.setattr(x.e,'make_record',make)
    plan={'all_ids':['q1'],'screen_ids':['q1']}
    with pytest.raises(x.e.PilotStopped,match='no automatic resampling'):
        x.run_stage(tmp_path,None,{}, {'q1':t},{'q1':t},plan,object(),full=full)
    assert original==p.read_bytes();make.assert_not_called()


def test_judge_only_resume_still_reserves_cost(tmp_path,monkeypatch):
    monkeypatch.setattr(x,'ROOT',tmp_path)
    monkeypatch.setattr(x,'PARENT',tmp_path/'parent')
    write_verdicts(x.PARENT/'full-verdicts.jsonl',[])
    write_json(tmp_path/'eval/reports/oracle-road-to-90-budget.json',
        {'planning_stop_cny':'39','user_limit_cny':'40','settlement_reserve_cny':'1'})
    monkeypatch.setattr(x.e,'budget_ledgers',lambda out:[])
    monkeypatch.setattr(x.e,'peak_cost',lambda paths:{'peak_upper_cny':'30'})
    monkeypatch.setattr(x,'validate_checkpoint',lambda *args:None)
    monkeypatch.setattr(x,'cache_meter',lambda *args:SimpleNamespace(hits=0))
    monkeypatch.setattr(x,'make_reader',lambda *args:object())
    out=tmp_path/'run';t=trace();write_json(out/'screen/traces'/x.e.trace_filename('q1'),t)
    plan={'screen_ids':['q1'],'all_ids':['q1']}
    first=x.preflight(out,None,{'q1':t},{'q1':t},plan)
    assert first['uncached_case_ids']==[] and first['pending_judge_cases']==1
    assert Decimal(first['planning_allowance_cny'])>0 and first['fits_budget']
    write_verdicts(out/'screen-verdicts.jsonl',[verdict()])
    second=x.preflight(out,None,{'q1':t},{'q1':t},plan)
    assert second['pending_judge_cases']==0
    assert x.e.read_json(out/'screen-cost-preflight.json')==first


@pytest.mark.parametrize('paid',[False,True])
def test_offline_or_unaffordable_never_reads_key(tmp_path,monkeypatch,paid):
    monkeypatch.setattr(sys,'argv',['experiment','--output',str(tmp_path)]+(['--paid'] if paid else []))
    monkeypatch.setattr(x,'prepare',lambda out:(None,{}, {},{}))
    monkeypatch.setattr(x,'prepare_sources',lambda *a:{})
    monkeypatch.setattr(x,'preflight',lambda *a,**k:{'fits_budget':False})
    key=Mock(side_effect=AssertionError('must not ask credential'))
    monkeypatch.setattr(x.getpass,'getpass',key)
    if paid:
        with pytest.raises(x.e.PilotStopped):x.main()
    else:x.main()
    key.assert_not_called()
