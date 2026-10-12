"""Create separate R31 drivers; preserve the completed R30 sources and artifacts."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def swap(text, old, new):
    if old not in text:
        raise ValueError('Expected template fragment missing: ' + old[:70])
    return text.replace(old, new)


driver = (ROOT / 'ops/oracle_premise_ledger_experiment.py').read_text(encoding='utf-8')
driver = swap(driver, 'R30: frozen R27 evidence, explicit premise ledger;', 'R31: contextual interpretation on the R27 adjudicated reader;')
driver = swap(driver, 'oracle-premise-ledger-20261009-v2', 'oracle-contextual-reader-20261010')
driver = swap(driver, 'audit_premise_ledger.py', 'audit_contextual_reader.py')
driver = swap(driver, 'OUTPUT_CAP = 1600', 'OUTPUT_CAP = 8192\nfrom oracle_refusal_gated_experiment import ModeClient, THINKING')
start, end = driver.index('def make_reader(client):'), driver.index('\n\ndef check_frozen(out):')
driver = driver[:start] + '''def make_reader(client):
    from dive_memory.oracle.indexed_reader import IndexedEvidenceReader
    from dive_memory.oracle.adjudicated_reader import AdjudicatedReader
    from dive_memory.oracle.contextual_reader import ContextualEvidenceReader, ContextualEvidenceReferee, VERSION, REFEREE_VERSION
    normal = getattr(client, 'normal_transport', getattr(client, 'client', None))
    thinking = getattr(client, 'thinking_transport', CacheTransport(THINKING)) if client else None
    if client is not None:
        client.normal_transport, client.thinking_transport = normal, thinking
    policy = previous.CANDIDATE.policy()
    primary = IndexedEvidenceReader(ModeClient(client, normal), ReaderSpec('deepseek', MODEL,
        prompt_version=policy.prompt_version, max_tokens=512), policy=policy)
    secondary = ContextualEvidenceReader(ModeClient(client, thinking), ReaderSpec('deepseek', MODEL,
        prompt_version=VERSION, max_tokens=OUTPUT_CAP, timeout_seconds=120), policy=policy)
    referee = ContextualEvidenceReferee(ModeClient(client, normal), ReaderSpec('deepseek', MODEL,
        prompt_version=REFEREE_VERSION, max_tokens=1024), policy=policy)
    reader = AdjudicatedReader(primary, secondary, referee, recover_invalid_secondary=True)
    reader.rules = secondary.rules
    return reader
''' + driver[end:]
driver = swap(driver, "'extra_body': EXTRA,", "'extra_body': EXTRA, 'primary_extra_body': EXTRA, 'secondary_extra_body': THINKING, 'judge_extra_body': EXTRA,")
driver = swap(driver, "'same premise-ledger reader for every case; no correctness/type/ID routing'",
    "'R27 primary; contextual thinking secondary; contextual independent referee on answerability disagreement; unchanged format recovery; no correctness/type/ID routing'")
start, end = driver.index('def estimate_cost('), driver.index('\n\ndef preflight(')
driver = driver[:start] + '''def estimate_cost(traces, *, judge_cases=None, evidence=None):
    """Observed parent thinking stages; no gold labels influence cost or execution."""
    from dive_memory.oracle.indexed_reader import RULES as OLD_RULES
    extra_bytes = max(0, len(make_reader(None).rules.encode()) - len(OLD_RULES.encode()))
    predicted, primary_new, inputs_total = Decimal(0), 0, 0
    for q, t in traces.items():
        raw = t.get('reader', {}).get('detail')
        try:
            stages = json.loads(raw).get('stages', []) if raw else []
        except (TypeError, ValueError, AttributeError):
            stages = []
        secondary = stages[1] if len(stages) > 1 else {}
        first = stages[0] if stages else {}
        old_context = t['reader_context_text']
        new_context = evidence[q]['reader_context_text'] if evidence is not None else old_context
        delta = max(0, len(new_context.encode()) - len(old_context.encode()))
        usage = secondary.get('usage', {})
        i, o = usage.get('input_tokens'), usage.get('output_tokens')
        if type(i) is not int or i < 0:
            i = len(new_context.encode()) + 2048
        else:
            i += delta
        if type(o) is not int or o < 0:
            o = OUTPUT_CAP
        i += extra_bytes
        inputs_total += i
        # A 10-percent referee allowance plus a 25-percent total margin covers
        # expected variation; per-request reservations enforce the hard boundary.
        normal_i = first.get('usage', {}).get('input_tokens')
        if type(normal_i) is not int or normal_i < 0:
            normal_i = len(new_context.encode()) + 2048
        else:
            normal_i += delta
        predicted += Decimal(2*i + 8*o) / 1000000
        predicted += Decimal('0.10') * Decimal(2*(normal_i+extra_bytes)+8*1024) / 1000000
        if evidence is not None and (old_context != new_context or t['as_of'] != evidence[q]['as_of']):
            primary_new += 1
            predicted += Decimal(2*normal_i + 8*512) / 1000000
    if judge_cases is None:
        judge_cases = len(traces)
    predicted += Decimal(3024*judge_cases) / 1000000
    return {'cases':len(traces), 'pending_judge_cases':judge_cases, 'primary_new_input_cases':primary_new,
        'projected_first_input_tokens':inputs_total,
        'predicted_reader_and_judge_cny':str(predicted),
        'planning_allowance_cny':str(predicted*Decimal('1.25')),
        'method':'observed parent thinking input/output; added prompt bytes; 10% independent referee allowance; changed-input primary; pending judges; 25% margin',
        'caveat':'Expected usage, not a worst-case completion guarantee. Exact primary cache reuse is audited; per-request reservations and cumulative hard ceiling remain enforced.'}


def calibrated_full_cost(out, prediction):
    # R30 single-pass ledger token statistics do not describe this thinking stack.
    return prediction
''' + driver[end:]
driver = swap(driver, "result = combine(outcome, [outcome], 'explicit_premise_ledger', version=reader.spec.prompt_version)", 'result = outcome')
driver = swap(driver, "'dive-premise-ledger-v1'", "'dive-contextual-reader-v1'")
driver = swap(driver, "    judge = LLMJudge(client, JudgeSpec(**header['judge']))",
    "    client.client = client.normal_transport\n    judge = LLMJudge(client, JudgeSpec(**header['judge']))")
driver = swap(driver, "    ledgers = budget_ledgers(out)\n    client = MeteredClient(normal,", "    thinking = OpenAICompatibleChatClient(ENDPOINT, key, provider='deepseek', extra_body=THINKING)\n    ledgers = budget_ledgers(out)\n    client = MeteredClient(normal,")
driver = swap(driver, "    try:\n        run_stage(out,", "    client.normal_transport, client.thinking_transport = normal, thinking\n    try:\n        run_stage(out,")

auditor = (ROOT / 'audit_tools/audit_premise_ledger.py').read_text(encoding='utf-8')
auditor = swap(auditor, 'R30 reader', 'R31 reader')
auditor = swap(auditor, 'import oracle_premise_ledger_experiment as e', 'import oracle_contextual_reader_experiment as e')
auditor = swap(auditor, "    judge = e.LLMJudge(meter, e.JudgeSpec(**header['judge']))", "    meter.client = meter.normal_transport\n    judge = e.LLMJudge(meter, e.JudgeSpec(**header['judge']))")

for rel, text in [('ops/oracle_contextual_reader_experiment.py', driver), ('audit_tools/audit_contextual_reader.py', auditor)]:
    target = ROOT / rel
    if target.exists():
        raise ValueError('Do not overwrite an existing experiment driver')
    target.write_text(text, encoding='utf-8')
print('Created R31 drivers; no API requests.')
