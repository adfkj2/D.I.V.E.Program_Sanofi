import json
from dataclasses import replace
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import oracle_grounded_advice_experiment as x
from dive_memory.oracle.contracts import ReaderStatus
from dive_memory.oracle.grounded_advice_reader import GroundedAdviceReader
from test_oracle_user_document_experiment import metered, payload, request


def test_existing_answer_exactly_preserved_without_extra_review(tmp_path):
    meter,normal,thinking=metered(tmp_path,[payload()],[payload()])
    req=replace(request(),question='Can you suggest an activity?')
    result=x.make_reader(meter).generate(req)
    assert result.hypothesis=='80 dollars'
    assert len(normal.requests)==len(thinking.requests)==1


def test_residual_advice_uses_new_prompt_and_same_visible_fields(tmp_path):
    meter,normal,thinking=metered(tmp_path,[payload('ABSTAIN')],
        [payload('ABSTAIN'),payload('ABSTAIN'),payload()])
    result=x.make_reader(meter).generate(replace(request(),question='Can you suggest an activity?'))
    assert result.status==ReaderStatus.ANSWER
    assert len(thinking.requests)==3
    messages=thinking.requests[-1]['messages']
    assert messages[0]['content']==GroundedAdviceReader.rules
    assert set(json.loads(messages[1]['content']))=={'question','question_date','as_of','context'}
    assert thinking.requests[-1]['max_tokens']==8192


def test_invented_quote_is_invalid_after_existing_format_recovery(tmp_path):
    bad=json.loads(payload());bad['citations'][0]['quote']='The user already attended my proposed event.'
    bad=json.dumps(bad)
    meter,normal,thinking=metered(tmp_path,[payload('ABSTAIN')],
        [payload('ABSTAIN'),payload('ABSTAIN'),bad,bad])
    result=x.make_reader(meter).generate(replace(request(),question='Can you suggest an activity?'))
    assert result.status==ReaderStatus.INVALID
    assert len(thinking.requests)==4
    assert 'format_repair' in json.loads(thinking.requests[-1]['messages'][1]['content'])
