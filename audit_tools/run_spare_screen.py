"""R29 launch addendum: reallocate existing budget before any new model result."""
from decimal import Decimal
import getpass
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'ops', 'audit_tools')]
import oracle_spare_document_experiment as e


def main():
    out = e.DEFAULT_OUTPUT
    dataset, cases, manifest, parent, ledgers, frozen = e.prepare(out)
    public = e.read_json(ROOT / 'eval/reports/oracle-statement-provenance-20261009/public-dataset-provenance.json')
    assert public['exact_public_file_match']
    assert public['local_sha256'] == public['public_lfs_sha256'] == dataset.sha256
    assert e.ENDPOINT == public['model_destination']
    preflight = e.read_json(out / 'screen-cost-preflight.json')
    assert e.read_json(out / 'offline-summary.json')['eligible_for_screen']
    assert preflight['preserved_r27_passages'] > 0
    rules = e.read_json(ROOT / 'eval/reports/oracle-road-to-90-budget.json')
    max_new = Decimal('2.60')
    stop = min(Decimal(rules['planning_stop_cny']), Decimal(frozen['initial_cost']['peak_upper_cny']) + max_new)
    assert stop <= Decimal(rules['user_limit_cny']) - Decimal(rules['settlement_reserve_cny'])
    addendum = {'date': '2026-10-09', 'frozen_plan_sha256': e.digest(out / 'frozen-plan.json'),
        'preflight_sha256': e.digest(out / 'screen-cost-preflight.json'),
        'public_dataset_provenance_sha256': e.digest(ROOT / 'eval/reports/oracle-statement-provenance-20261009/public-dataset-provenance.json'),
        'launch_driver_sha256': e.digest(Path(__file__)), 'screen_max_new_cny': str(max_new),
        'screen_stop_cny': str(stop), 'user_limit_cny': rules['user_limit_cny'],
        'reason': 'Before any paid R29 result, 25-percent-margin projection 2.458667 exceeds internal 1.80 allocation; allocate 2.60 within existing cumulative authorization. No source, evidence, selection, model, judge or total authorization change.'}
    launch_path = out / 'screen-launch-addendum.json'
    if launch_path.exists():
        assert e.read_json(launch_path) == addendum
    else:
        assert not (out / 'api-cache/ledger.json').exists()
        assert Decimal(preflight['planning_allowance_cny']) < stop - Decimal(e.peak_cost(ledgers)['peak_upper_cny'])
        e.atomic_write_json(launch_path, addendum)
    plan = {**frozen, 'screen_max_new_cny': str(max_new), 'screen_stop_cny': str(stop)}
    key = os.environ.get('DIVE_READER_API_KEY') or getpass.getpass('DeepSeek key (hidden, never saved): ')
    if not key.strip() or e.MODEL not in [r['id'] for r in e.private_get('models', key).get('data', [])]:
        raise e.PilotStopped('Missing credential or unavailable model')
    guard = e.TotalBudgetGuard(key, out, limit=max_new)
    guard.check(0, force=True)
    normal = e.OpenAICompatibleChatClient(e.ENDPOINT, key, provider='deepseek', extra_body=e.EXTRA)
    thinking = e.OpenAICompatibleChatClient(e.ENDPOINT, key, provider='deepseek', extra_body=e.THINKING)
    client = e.MeteredClient(normal, out / 'api-cache', max_calls=300, max_tokens=3000000,
        reuse_roots=[p.parent for p in ledgers if p.parent != out / 'api-cache'],
        before_request=guard.check, currency_guard=e.CurrencyBudget(ledgers, limit=stop))
    try:
        e.screen(out, dataset, cases, manifest, parent, client, normal, thinking, plan)
        guard.check(client.summary()['attempts'], force=True)
    except (Exception, e.PilotStopped) as exc:
        e.atomic_write_json(out / 'stopped.json', {'status': 'STOPPED', 'error_type': type(exc).__name__,
            'no_complete_score_claimed': True})
        raise
    finally:
        e.atomic_write_json(out / 'usage.json', client.summary())
        e.atomic_write_json(out / 'aggregate-peak-cost.json', e.peak_cost(ledgers))


if __name__ == '__main__':
    main()
