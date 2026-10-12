import inspect
from pathlib import Path
import sys

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / p) for p in ('ops', 'audit_tools')]
import contextual_recovery as r
import resume_contextual_reader as resume


@pytest.mark.parametrize('cls,error', [(r.InterruptedReplayClient, InterruptedError),
                                     (r.NoResendClient, r.e.PilotStopped)])
def test_interrupted_key_never_reaches_network_or_creates_ledger(tmp_path, cls, error):
    class Transport:
        extra_body = r.e.THINKING
        def complete(self, **kwargs):
            raise AssertionError('Network must not be called')
    client = cls(Transport(), tmp_path, max_calls=1)
    request = {'messages':[{'role':'user','content':'example'}], 'max_tokens':100}
    client.interrupted_key = r.request_key(client.client, request)
    with pytest.raises(error):
        client.complete(**request)
    assert client.summary()['attempts'] == 0
    assert not client.path.exists()


def test_interrupted_identity_includes_transport_mode():
    request = {'messages':[], 'max_tokens':100}
    assert r.request_key(r.e.CacheTransport(r.e.EXTRA), request) != r.request_key(r.e.CacheTransport(r.e.THINKING), request)


def test_resume_stage_only_allows_verified_failure_checkpoint():
    original = inspect.getsource(r.e.run_stage).strip()
    resumed = inspect.getsource(resume.run_stage).strip()
    expected = original.replace("    label = 'full' if full else 'screen'", "    recovery = verify(out)\n    label = 'full' if full else 'screen'", 1)
    expected = expected.replace("if record['reader']['status'] in {'ERROR', 'TIMEOUT'}:",
        "if record['reader']['status'] in {'ERROR', 'TIMEOUT'} and q != recovery['failure_id']:", 1)
    assert resumed == expected
    assert resume.OUTPUT != r.ORIGINAL
