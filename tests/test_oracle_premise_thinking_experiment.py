from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
from oracle_premise_thinking_experiment import PhaseMeter
from dive_memory.oracle.reader import ChatCompletion


class Transport:
    endpoint = 'test://no-network'

    def __init__(self, mode):
        self.extra_body = {'thinking': {'type': mode}}
        self.calls = 0
        self.mode = mode

    def complete(self, **kwargs):
        self.calls += 1
        return ChatCompletion(self.mode, 'test', input_tokens=3, output_tokens=1)


def test_review_thinking_does_not_change_judge_or_share_incompatible_cache(tmp_path):
    normal, thinking = Transport('disabled'), Transport('enabled')
    meter = PhaseMeter(normal, thinking, tmp_path)
    call = {'model': 'test', 'messages': [{'role': 'user', 'content': 'same request'}],
            'temperature': 0.0, 'max_tokens': 8, 'timeout_seconds': 1}
    meter.phase = 'reader-screen'
    assert meter.complete(**call).text == 'enabled'
    meter.phase = 'judge-screen'
    assert meter.complete(**call).text == 'disabled'
    assert normal.calls == thinking.calls == 1
    meter.phase = 'reader-full'
    assert meter.complete(**call).text == 'enabled'
    assert meter.hits == 1
    meter.phase = 'unknown'
    with pytest.raises(ValueError):
        meter.complete(**call)
