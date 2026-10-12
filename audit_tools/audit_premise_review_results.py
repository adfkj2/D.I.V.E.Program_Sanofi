"""Independently reproduce both review screens from exact API caches, offline."""
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'ops'), str(ROOT / 'audit_tools')]
from oracle_premise_review_experiment import PARENT, SavedPrimary, restore_visible_request
from oracle_refusal_gated_experiment import CacheTransport
from oracle_low_cost_pilot import MeteredClient
from oracle_reader_pair import _load_run, _load_verdicts, _accuracy, audit_run_context
from oracle_complete_test import read_json, summarize
from oracle_calculation_experiment import budget_ledgers, eligible
from oracle_currency_budget import peak_cost
from oracle_v4_experiment import evidence_audit
from audit_adjudicated_result import audit_citations, audit_submission
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.premise_review_reader import RefusalReviewReader, NecessaryPremiseReader
from dive_memory.oracle.reader import ReaderSpec, reader_policy
from dive_memory.oracle.runner import atomic_write_json, trace_filename
from dive_memory.eval_manifest import code_snapshot


def comparable_outcome(outcome):
    """Local cache lookup latency differs; compare every other field recursively."""
    result = {k: v for k, v in outcome.items() if k not in ('latency_ms', 'spec')}
    detail = result.get('detail')
    if detail:
        parsed = json.loads(detail)
        if isinstance(parsed, dict) and 'stages' in parsed:
            parsed['stages'] = [comparable_outcome(stage) for stage in parsed['stages']]
            result['detail'] = parsed
    return result


def main():
    dataset = load_oracle_dataset(ROOT / 'eval/external/longmemeval/longmemeval_oracle.json')
    _, parent = _load_run(PARENT)
    parent_header, parent_v = _load_verdicts(PARENT / 'full-verdicts.jsonl', parent, dataset.sha256)
    for name in ('oracle-premise-review-20261007-v2', 'oracle-premise-thinking-20261007'):
        output = ROOT / 'eval/reports' / name
        plan = read_json(output / 'frozen-plan.json')
        snapshot = code_snapshot(ROOT)
        if (any(snapshot['files'].get(p) != digest for p, digest in plan['source_snapshot']['files'].items())
                or any(snapshot[k] != plan['source_snapshot'][k] for k in ('python', 'dependencies'))):
            raise ValueError('A frozen source or dependency changed after run')
        if any(sha256((ROOT / 'audit_tools' / p).read_bytes()).hexdigest() != digest
               for p, digest in plan['helper_sha256'].items()):
            raise ValueError('Frozen audit helper changed')
        if dataset.sha256 != plan['dataset_sha256']:
            raise ValueError('Dataset mismatch')
        for qid, digest in plan['parent_trace_sha256'].items():
            if sha256((PARENT / 'traces' / trace_filename(qid)).read_bytes()).hexdigest() != digest:
                raise ValueError('Parent trace changed')
        if sha256((PARENT / 'full-verdicts.jsonl').read_bytes()).hexdigest() != plan['parent_verdicts_sha256']:
            raise ValueError('Parent verdict changed')
        _, traces = _load_run(output / 'screen')
        ids = plan['screen_ids']
        if set(traces) != set(ids) or len(ids) != 80:
            raise ValueError('Incomplete screen')
        cases = [dataset.by_id()[q] for q in ids]
        meter = MeteredClient(CacheTransport(plan['extra_body']), output / 'api-cache', max_calls=0,
            reuse_roots=[Path(p).parent for p in plan['currency_ledger_paths']])
        ledger_before = (output / 'api-cache/ledger.json').read_bytes()
        reviewer = NecessaryPremiseReader(meter, ReaderSpec(**plan['review_spec']),
                                         policy=reader_policy('indexed_evidence'))
        reviewed = 0
        for case in cases:
            q = case.question_id
            req = restore_visible_request(case.question, parent[q])
            reader = RefusalReviewReader(SavedPrimary(parent[q]['reader']), reviewer)
            replay = reader.generate(req)
            saved = traces[q]['reader']
            if (reader.spec.to_dict() != saved['spec'] or
                    comparable_outcome(replay.to_dict()) != comparable_outcome(saved)):
                raise ValueError('Exact cached responses do not reproduce saved result')
            reviewed += parent[q]['reader']['status'] == 'ABSTAIN'
            if traces[q]['calls']['reader'] != saved['calls']:
                raise ValueError('Reader call totals inconsistent')
            if traces[q]['tokens']['reader_input'] != saved['usage']['input_tokens']:
                raise ValueError('Reader input totals inconsistent')
        if ledger_before != (output / 'api-cache/ledger.json').read_bytes():
            raise ValueError('Offline audit modified paid ledger')
        evidence_audit({q:parent[q] for q in ids}, traces)
        context = audit_run_context(output / 'screen')
        if context['reader_context_heuristic_tokens']['over_budget_cases']:
            raise ValueError('Evidence budget exceeded')
        rows = [json.loads(line) for line in (output / 'screen/submission.jsonl').read_text(encoding='utf-8').splitlines()]
        audit_submission(rows, traces)
        header, verdicts = _load_verdicts(output / 'screen-verdicts.jsonl', traces, dataset.sha256)
        if header != parent_header or set(verdicts) != set(ids):
            raise ValueError('Judge specification or coverage mismatch')
        for q in ids:
            if (traces[q]['hypothesis'] == parent[q]['hypothesis'] and
                    traces[q]['reader']['status'] == parent[q]['reader']['status'] and
                    verdicts[q] != parent_v[q]):
                raise ValueError('Identical answer/status has inconsistent paired verdict')
        before = summarize(cases, parent, parent_v)
        after = summarize(cases, traces, verdicts, exposed_ids=ids)
        if after['overall'] != read_json(output / 'screen-evaluation.json')['overall']:
            raise ValueError('Reported score mismatch')
        if eligible(before, after) != read_json(output / 'selection.json')['eligible_for_full']:
            raise ValueError('Promotion rule mismatch')
        result = {'audit_passed': True, 'cases': len(ids), 'reviewed_cases': reviewed,
            'exact_cached_stage_reproduction': True, 'new_api_calls': 0,
            'cache_hits': meter.hits, 'paid_ledger_unchanged': True,
            'parent_traces_and_verdicts_unchanged': True,
            'citation_checks': sum(audit_citations(t) for t in traces.values()),
            'frozen_files_and_environment_match': True,
            'live_snapshot_exact_match': snapshot == plan['source_snapshot'],
            'files_added_after_freeze': sorted(set(snapshot['files'])-set(plan['source_snapshot']['files'])),
            'baseline_correct': before['overall']['correct'], 'candidate_correct': after['overall']['correct'],
            'eligible_for_full': eligible(before, after),
            'aggregate_cost_at_audit': peak_cost(budget_ledgers(output)),
            'driver_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
            'official_score': 'NOT_EVALUATED'}
        atomic_write_json(output / 'post-run-audit.json', result)
        print(json.dumps({'run':name, **result}), flush=True)


if __name__ == '__main__':
    main()
