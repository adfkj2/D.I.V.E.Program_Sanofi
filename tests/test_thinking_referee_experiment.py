"""R35 transport, exact-cache, routing and selection boundaries without network."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import oracle_thinking_referee_experiment as x
from test_oracle_user_document_experiment import metered, payload, request
from dive_memory.oracle.adjudicated_reader import EvidenceReferee


@pytest.mark.parametrize('first,second,referee',[
    ('ANSWER','ANSWER',False),('ABSTAIN','ABSTAIN',False),
    ('ABSTAIN','ANSWER',True),('ANSWER','ABSTAIN',True)])
def test_only_original_disagreement_uses_thinking_referee(tmp_path,first,second,referee):
    meter,normal,thinking=metered(tmp_path,[payload(first)],[payload(second)]+([payload()] if referee else []))
    reader=x.make_reader(meter)
    result=reader.generate(request())
    assert len(normal.requests)==1 and len(thinking.requests)==1+referee
    assert normal.requests[0]['max_tokens']==512
    assert reader.referee.rules==EvidenceReferee.rules
    if referee:
        call=thinking.requests[1]
        assert call['max_tokens']==8192 and call['timeout_seconds']==120
        sent=json.loads(call['messages'][1]['content'])
        assert set(sent)=={'question','question_date','as_of','context'}
    assert len(json.loads(result.detail)['stages'])==2+referee
    assert not thinking.responses and not normal.responses


def test_exact_primary_secondary_cache_reused_but_normal_referee_not_reused(tmp_path):
    meter,normal,thinking=metered(tmp_path,[payload('ABSTAIN'),payload('ABSTAIN')],[payload()])
    old=x.e.make_reader(meter).generate(request())
    assert old.status.value=='ABSTAIN'
    first_requests=len(meter.ledger['calls'])
    thinking.responses=[payload()]
    result=x.make_reader(meter).generate(request())
    assert result.status.value=='ANSWER'
    assert len(meter.ledger['calls'])==first_requests+1 and meter.hits==2
    assert len(normal.requests)==2 and len(thinking.requests)==2


def test_referee_failure_is_preserved_not_replaced_by_old_answer(tmp_path):
    meter,normal,thinking=metered(tmp_path,[payload('ABSTAIN')],[payload()])
    with pytest.raises(x.e.PilotStopped,match='Transport stopped'):
        x.make_reader(meter).generate(request())
    assert len(normal.requests)==1 and len(thinking.requests)==2
    assert meter.ledger['calls'][-1]['status']!='completed'


def test_promotion_requires_complete_net_gain_and_preserves_unanswerable():
    base={'overall':{'fully_judged':True},'answerable':{'cases':50,'correct':46},'unanswerable':{'cases':30,'correct':28}}
    assert not x.eligible(base,base)
    after=deepcopy(base);after['answerable']['correct']=47
    assert x.eligible(base,after)
    after['unanswerable']['correct']=27
    assert not x.eligible(base,after)
    after['unanswerable']['correct']=28;after['overall']['fully_judged']=False
    assert not x.eligible(base,after)


def test_checkpoint_cannot_change_question_or_reader_identity():
    with pytest.raises(ValueError,match='identity/spec'):
        x.validate_checkpoint('expected',{'question_id':'different','reader':{'spec':{}}},{},{'reader_spec':{}})


def test_budget_scope_includes_pending_historical_ledger(tmp_path, monkeypatch):
    # Exercise scope discovery without relying on an untracked developer ledger.
    reports = tmp_path / 'eval/reports'
    historical = reports / 'oracle-contextual-reader-20261010'
    historical.joinpath('api-cache').mkdir(parents=True)
    reports.joinpath('oracle-road-to-90-budget.json').write_text(
        json.dumps({'scope': 'fixture-shared-budget'}), encoding='utf-8')
    historical.joinpath('frozen-plan.json').write_text(
        json.dumps({'aggregate_budget': {'scope': 'fixture-shared-budget'}}), encoding='utf-8')
    pending = historical / 'api-cache/ledger.json'
    pending.write_text(json.dumps({'calls': [{'status': 'pending', 'charged_budget': 2048}]}), encoding='utf-8')
    monkeypatch.setitem(x.e.budget_ledgers.__globals__, 'ROOT', tmp_path)
    current = reports / 'new-run'
    paths = x.e.budget_ledgers(current)
    assert pending in paths
    assert current / 'api-cache/ledger.json' in paths
