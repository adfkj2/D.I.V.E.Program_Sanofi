"""R23: fixed refusal-review prompt with low thinking; judge stays nonthinking."""
import argparse
from dataclasses import replace
from decimal import Decimal
import getpass
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'ops')]
import oracle_premise_review_experiment as experiment
from oracle_reasoning_experiment import THINKING
from oracle_low_cost_pilot import MeteredClient, PilotStopped, MODEL, ENDPOINT, EXTRA, private_get
from oracle_narrative_experiment import TotalBudgetGuard
from oracle_currency_budget import CurrencyBudget, peak_cost
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.runner import atomic_write_json


class PhaseMeter(MeteredClient):
    def __init__(self, normal, thinking, root, **kwargs):
        super().__init__(normal, root, **kwargs)
        self.normal, self.thinking = normal, thinking

    def complete(self, **kwargs):
        if self.phase.startswith('reader-'):
            self.client = self.thinking
        elif self.phase.startswith('judge-'):
            self.client = self.normal
        else:
            raise ValueError('Unknown request phase; no request sent')
        return super().complete(**kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'eval/reports/oracle-premise-thinking-20261007')
    parser.add_argument('--paid', action='store_true')
    parser.add_argument('--full', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    experiment.REVIEW_SPEC = replace(experiment.REVIEW_SPEC, max_tokens=8192, timeout_seconds=120)
    experiment.EXTRA = THINKING
    dataset, manifest, parent, requests, plan, ledgers = experiment.prepare(output)
    if not args.paid:
        return
    if args.full and (not experiment.read_json(output / 'screen-completion.json')['complete']
                     or not experiment.read_json(output / 'selection.json')['eligible_for_full']):
        raise PilotStopped('Screen did not pass the frozen promotion rule; no full run')
    key = os.environ.get('DIVE_READER_API_KEY') or getpass.getpass('DeepSeek key (hidden, never saved): ')
    if not key.strip() or MODEL not in [r['id'] for r in private_get('models', key).get('data', [])]:
        raise PilotStopped('Missing credential or unavailable model')
    guard = TotalBudgetGuard(key, output, limit=Decimal(plan['max_new_cost_cny']))
    normal = OpenAICompatibleChatClient(ENDPOINT, key, provider='deepseek', extra_body=EXTRA)
    thinking = OpenAICompatibleChatClient(ENDPOINT, key, provider='deepseek', extra_body=THINKING)
    client = PhaseMeter(normal, thinking, output / 'api-cache', max_calls=260, max_tokens=3000000,
        reuse_roots=[p.parent for p in ledgers if p.parent != output / 'api-cache'],
        before_request=guard.check, currency_guard=CurrencyBudget(ledgers, limit=Decimal(plan['experiment_stop_cny'])))
    try:
        experiment.run_stage(dataset, manifest, parent, requests, plan, client, output, full=args.full)
        guard.check(client.summary()['attempts'], force=True)
    except (Exception, PilotStopped) as exc:
        atomic_write_json(output / 'stopped.json', {'status': 'STOPPED', 'error_type': type(exc).__name__,
            'stage': 'full' if args.full else 'screen', 'no_complete_score_claimed': True})
        raise
    finally:
        atomic_write_json(output / 'usage.json', client.summary())
        atomic_write_json(output / 'aggregate-peak-cost.json', peak_cost(ledgers))


if __name__ == '__main__':
    try:
        main()
    except PilotStopped as exc:
        print(str(exc), flush=True)
        sys.exit(2)
