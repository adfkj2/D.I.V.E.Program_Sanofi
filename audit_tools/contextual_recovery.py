"""Preserve an interrupted request as a failure without resampling or releasing its reserve."""
from copy import deepcopy
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
import oracle_contextual_reader_experiment as e
from oracle_low_cost_pilot import digest as request_digest

ORIGINAL = e.OUTPUT
OUTPUT = ORIGINAL.with_name(ORIGINAL.name + '-resume-001')
HELPERS = ('contextual_recovery.py', 'resume_contextual_reader.py', 'audit_contextual_reader_resumed.py')


def request_key(client, kwargs):
    return request_digest({'endpoint': getattr(client, 'endpoint', e.ENDPOINT),
        'extra_body': dict(getattr(client, 'extra_body', e.EXTRA)), 'request': kwargs})


class InterruptedReplayClient(e.MeteredClient):
    """Cache-only replay: the identified missing response is an infrastructure failure."""
    interrupted_key = None
    interrupted_hits = 0

    def complete(self, **kwargs):
        if request_key(self.client, kwargs) == self.interrupted_key:
            self.interrupted_hits += 1
            raise InterruptedError('Recorded process interruption; response unavailable')
        return super().complete(**kwargs)


class NoResendClient(e.MeteredClient):
    interrupted_key = None

    def complete(self, **kwargs):
        if request_key(self.client, kwargs) == self.interrupted_key:
            raise e.PilotStopped('Previously interrupted request must never be resent')
        return super().complete(**kwargs)


def verify(out=OUTPUT):
    recovery = e.read_json(out / 'resume-plan.json')
    assert e.digest(ORIGINAL/'api-cache/ledger.json') == recovery['original_ledger_sha256']
    assert e.digest(ORIGINAL/'frozen-plan.json') == recovery['original_plan_sha256']
    assert e.digest(out/'frozen-plan.json') == recovery['original_plan_sha256']
    for q, h in recovery['saved_trace_sha256'].items():
        assert e.digest(out/'screen/traces'/e.trace_filename(q)) == h
    for name, h in recovery['helper_sha256'].items():
        assert e.digest(ROOT/'audit_tools'/name) == h
    assert e.read_json(out/'screen/traces'/e.trace_filename(recovery['failure_id']))['reader']['status'] == 'ERROR'
    assert e.check_frozen(out)
    return recovery


def prepare():
    if (OUTPUT/'resume-plan.json').exists():
        return verify()
    plan = e.check_frozen(ORIGINAL)
    ledger_path = ORIGINAL/'api-cache/ledger.json'
    ledger = e.read_json(ledger_path)
    uncertain = [r for r in ledger['calls'] if r['status'] != 'completed']
    assert len(uncertain) == 1 and uncertain[0]['status'] == 'pending'
    pending = uncertain[0]
    assert not (ORIGINAL/'api-cache/responses'/(pending['key']+'.json')).exists()
    saved = {e.read_json(p)['question_id']:p for p in (ORIGINAL/'screen/traces').glob('*.json')}
    assert set(saved) == set(plan['screen_ids'][:len(saved)])
    failure_id = plan['screen_ids'][len(saved)]
    OUTPUT.mkdir(exist_ok=True)
    shutil.copy2(ORIGINAL/'frozen-plan.json', OUTPUT/'frozen-plan.json')
    shutil.copytree(ORIGINAL/'source-snapshot', OUTPUT/'source-snapshot', dirs_exist_ok=True)
    for q, p in saved.items():
        dest = OUTPUT/'screen/traces'/e.trace_filename(q)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            assert e.digest(dest) == e.digest(p)
        else:
            shutil.copy2(p, dest)
    dataset = e.load_oracle_dataset(ROOT/'eval/external/longmemeval/longmemeval_oracle.json')
    _, sources = e._load_run(e.R27/'screen')
    ledgers = e.budget_ledgers(OUTPUT)
    before = {str(p):e.digest(p) for p in ledgers if p.exists()}
    meter = InterruptedReplayClient(e.CacheTransport(e.EXTRA), OUTPUT/'recovery-audit-cache',
        max_calls=0, max_tokens=15000000, reuse_roots=[p.parent for p in ledgers])
    meter.interrupted_key = pending['key']
    failed = e.make_record(dataset.by_id()[failure_id].question, sources[failure_id], e.make_reader(meter))
    assert meter.interrupted_hits == 1 and meter.summary()['attempts'] == 0
    assert failed['reader']['status'] == 'ERROR'
    assert before == {str(p):e.digest(p) for p in ledgers if p.exists()}
    e.save_once(OUTPUT/'screen/traces'/e.trace_filename(failure_id), failed)
    all_saved = {q:e.digest(OUTPUT/'screen/traces'/e.trace_filename(q)) for q in [*saved, failure_id]}
    recovery = {'original_directory':str(ORIGINAL), 'original_ledger_sha256':e.digest(ledger_path),
        'original_plan_sha256':e.digest(ORIGINAL/'frozen-plan.json'),
        'saved_trace_sha256':all_saved, 'completed_prefix_cases':len(saved),
        'failure_id':failure_id, 'interrupted_request':deepcopy(pending),
        'interrupted_request_identity_verified':True, 'durable_response_exists':False,
        'failure_policy':'Preserve ERROR and score false, never resend missing response; retain full uncertain reserve.',
        'original_ledger_preserved':True, 'new_api_calls':0,
        'helper_sha256':{name:e.digest(ROOT/'audit_tools'/name) for name in HELPERS},
        'cumulative_cost':e.peak_cost(ledgers)}
    e.save_once(OUTPUT/'resume-plan.json', recovery)
    verify()
    return recovery


if __name__ == '__main__':
    import json
    r = prepare()
    print(json.dumps({k:r[k] for k in ('completed_prefix_cases','failure_id','new_api_calls','cumulative_cost')}))
