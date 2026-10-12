"""R31 routing, transport isolation, budgets and restart checks; no provider calls."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import oracle_contextual_reader_experiment as e
from dive_memory.models import EvidenceState, Memory, RetrievalItem
from dive_memory.oracle.reader import ChatCompletion, OracleReaderRequest, render_reader_context


HEADER = {"_header": True, "dataset_sha256": "fixture", "judge": {"model": "fixture"}}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def write_verdicts(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in [HEADER, *rows]), encoding="utf-8")


def verdict(q="q1", hypothesis="candidate", **changes):
    return {"question_id": q, "hypothesis_sha256": sha256(
        ("null" if hypothesis is None else hypothesis).encode()).hexdigest()[:16],
        "correct": True, "raw": "yes", "error": None, **changes}


def trace(q="q1", *, status="ANSWER", hypothesis="candidate", context="visible evidence"):
    return {"question_id": q, "reader_context_text": context, "hypothesis": hypothesis,
        "as_of": "2026-01-01", "reader": {"status": status,
        "spec": {"prompt_version": "fixture"}, "detail": json.dumps({"stages": [
            {"usage": {"input_tokens": 100, "output_tokens": 20}},
            {"usage": {"input_tokens": 200, "output_tokens": 300}}]})}}


def request():
    item = RetrievalItem(Memory("m1", "u", "episode", EvidenceState.OBSERVATION,
        "I bought four tickets for 80 dollars.", {"role": "user"}), 1, source_refs=["event1"])
    req = OracleReaderRequest("What did the tickets cost?", None, None, (item,), "",
        policy=e.previous.CANDIDATE.policy())
    return replace(req, context_text=render_reader_context(req))


def payload(status="ANSWER"):
    if status == "ABSTAIN":
        return json.dumps({"status": status, "answer": None, "citations": [], "missing": ["price"]})
    return json.dumps({"status": status, "answer": "80 dollars", "citations": [
        {"index": 0, "quote": "four tickets for 80 dollars"}], "missing": []})


class Transport:
    endpoint = e.ENDPOINT

    def __init__(self, extra, responses):
        self.extra_body = extra
        self.responses = list(responses)
        self.requests = []

    def complete(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return ChatCompletion(text=self.responses.pop(0), model=e.MODEL,
            input_tokens=100, output_tokens=20)


def metered(tmp_path, normal_responses, thinking_responses):
    normal = Transport(e.EXTRA, normal_responses)
    thinking = Transport(e.THINKING, thinking_responses)
    meter = e.MeteredClient(normal, tmp_path / "cache", max_calls=20, max_tokens=1000000)
    meter.normal_transport, meter.thinking_transport = normal, thinking
    return meter, normal, thinking


@pytest.mark.parametrize("first,second,normal_calls,route", [
    ("ANSWER", "ANSWER", 1, "same_answerability"),
    ("ABSTAIN", "ANSWER", 2, "independent_evidence_review"),
])
def test_factory_keeps_primary_and_referee_normal_and_secondary_thinking(
        tmp_path, first, second, normal_calls, route):
    meter, normal, thinking = metered(tmp_path, [payload(first), payload()], [payload(second)])
    reader = e.make_reader(meter)
    outcome = reader.generate(request())
    assert json.loads(outcome.detail)["route"] == route
    assert len(normal.requests) == normal_calls and len(thinking.requests) == 1
    assert normal.requests[0]["max_tokens"] == 512
    assert thinking.requests[0]["max_tokens"] == 8192
    assert thinking.requests[0]["timeout_seconds"] == 120
    if normal_calls == 2:
        assert normal.requests[1]["max_tokens"] == 1024
        # Referee receives the evidence only; neither previous answer is inserted.
        assert len(normal.requests[1]["messages"]) == 2
    assert reader.recover_invalid_secondary is True


def test_r27_primary_is_byte_identical_and_reuses_exact_cache(tmp_path):
    meter, normal, thinking = metered(tmp_path, [payload()], [payload()])
    prior = e.previous.make_reader(meter, normal, thinking)
    old = prior.primary.generate(request())
    old_request = deepcopy(normal.requests[0])
    candidate = e.make_reader(meter)
    current = candidate.primary.generate(request())
    assert current.prompt_sha256 == old.prompt_sha256
    assert candidate.primary.spec == prior.primary.spec
    assert len(normal.requests) == 1
    assert meter.hits == 1 and old_request["max_tokens"] == 512


def test_identical_request_in_different_modes_cannot_share_cache(tmp_path):
    meter, normal, thinking = metered(tmp_path, ["normal result"], ["thinking result"])
    kwargs = {"model": e.MODEL, "messages": [{"role": "user", "content": "same request"}],
        "temperature": 0, "max_tokens": 512, "timeout_seconds": 30}
    assert e.ModeClient(meter, normal).complete(**kwargs).text == "normal result"
    assert e.ModeClient(meter, thinking).complete(**kwargs).text == "thinking result"
    assert e.ModeClient(meter, normal).complete(**kwargs).text == "normal result"
    assert len(meter.ledger["calls"]) == 2 and meter.hits == 1
    assert len({r["key"] for r in meter.ledger["calls"]}) == 2


def test_audit_factory_has_only_cache_transports_and_stable_normal_mode(tmp_path):
    meter = e.MeteredClient(e.CacheTransport(e.EXTRA), tmp_path / "cache", max_calls=0)
    first = e.make_reader(meter)
    normal = meter.normal_transport
    assert isinstance(meter.thinking_transport, e.CacheTransport)
    assert meter.thinking_transport.extra_body == e.THINKING
    meter.client = meter.thinking_transport
    second = e.make_reader(meter)
    assert second.primary.client.transport is normal
    assert first.spec == second.spec
    with pytest.raises(e.PilotStopped):
        second.primary.generate(request())
    assert not (tmp_path / "cache/ledger.json").exists()


@pytest.mark.parametrize("full", [False, True])
def test_judge_resets_normal_after_reader_or_full_screen_checkpoint(monkeypatch, tmp_path, full):
    record = trace()
    parent = trace(hypothesis="parent answer")
    normal, thinking = object(), object()
    meter = SimpleNamespace(client=thinking, normal_transport=normal)
    reader = object()
    monkeypatch.setattr(e, "make_reader", lambda client: reader)
    def generate(question, source, actual_reader):
        assert actual_reader is reader
        meter.client = thinking
        return record
    monkeypatch.setattr(e, "make_record", generate)
    if full:
        write_json(tmp_path / "screen/traces" / e.trace_filename("q1"), record)
        write_verdicts(tmp_path / "screen-verdicts.jsonl", [])
        monkeypatch.setattr(e, "make_record", Mock(side_effect=AssertionError("must reuse screen")))
    monkeypatch.setattr(e.previous, "evidence_audit", lambda *args: None)
    monkeypatch.setattr(e, "audit_citations", lambda *args: None)
    monkeypatch.setattr(e, "audit_submission", lambda *args: None)
    monkeypatch.setattr(e, "validate_submission", lambda *args, **kwargs: SimpleNamespace(status="VALID"))
    monkeypatch.setattr(e, "_load_verdicts", lambda *args: (HEADER, {"q1": True}))
    monkeypatch.setattr(e, "JudgeSpec", lambda **kwargs: object())
    def judge(client, spec):
        assert client.client is normal
        return client
    monkeypatch.setattr(e, "LLMJudge", judge)
    scored = []
    def score(client, case, result):
        assert client.client is normal
        assert client.phase == "judge-" + ("full" if full else "screen")
        scored.append(case.question_id)
        return SimpleNamespace(correct=True, raw="yes", error=None)
    monkeypatch.setattr(e, "score_trace", score)
    summary = {"overall": {"correct": 1, "fully_judged": True}, "answerable": {}, "unanswerable": {}}
    monkeypatch.setattr(e, "summarize", lambda *args, **kwargs: summary)
    monkeypatch.setattr(e, "_accuracy", lambda *args, **kwargs: {})
    monkeypatch.setattr(e, "eligible", lambda *args: False)
    monkeypatch.setattr(e, "check_frozen", lambda *args: None)
    case = SimpleNamespace(question_id="q1", question="fixture question", question_type="fixture")
    dataset = SimpleNamespace(sha256="fixture", by_id=lambda: {"q1": case})
    plan = {"screen_ids": ["q1"], "all_ids": ["q1"], "reader_spec": record["reader"]["spec"],
        "config": {}, "source_snapshot": {}, "selection": "fixture", "execution_mode": "fixture"}
    e.run_stage(tmp_path, dataset, {"dataset": {}}, {"q1": parent}, {"q1": parent},
        plan, {"q1": record}, meter, full=full)
    assert scored == ["q1"]


def setup_budget(monkeypatch, tmp_path, *, spent="27", planning="39", limit="40", reserve="1"):
    monkeypatch.setattr(e, "ROOT", tmp_path)
    reference = tmp_path / "r26-verdicts.jsonl"
    write_verdicts(reference, [])
    monkeypatch.setattr(e, "R26_VERDICTS", reference)
    write_json(tmp_path / "eval/reports/oracle-road-to-90-budget.json", {
        "planning_stop_cny": planning, "user_limit_cny": limit, "settlement_reserve_cny": reserve})
    monkeypatch.setattr(e, "budget_ledgers", lambda out: [])
    monkeypatch.setattr(e, "peak_cost", lambda paths: {"peak_upper_cny": spent})
    monkeypatch.setattr(e, "audit_primary_cache", lambda out, sources: {
        "exact_cached_case_ids": list(sources), "uncached_case_ids": [], "cache_hits": len(sources), "new_api_calls": 0})
    return tmp_path / "candidate"


def test_judge_only_resume_reserves_pending_judges_and_preserves_preflight(monkeypatch, tmp_path):
    out = setup_budget(monkeypatch, tmp_path)
    sources = {q: trace(q) for q in ("q1", "q2")}
    for q, record in sources.items():
        write_json(out / "screen/traces" / e.trace_filename(q), record)
    first = e.preflight(out, sources)
    assert first["cases"] == 0 and first["pending_judge_cases"] == 2
    write_verdicts(out / "screen-verdicts.jsonl", [verdict()])
    resumed = e.preflight(out, sources)
    assert resumed["cases"] == 0 and resumed["pending_judge_cases"] == 1
    assert Decimal(resumed["planning_allowance_cny"]) * 2 == Decimal(first["planning_allowance_cny"])
    assert e.read_json(out / "screen-cost-preflight.json") == first
    assert len(list(out.glob("screen-cost-preflight*.json"))) == 2


@pytest.mark.parametrize("spent,planning,expected_stop,expected_fit", [
    ("27", "40", "39", True), ("39", "40", "39", False), ("38.99622", "39", "39", False),
])
def test_shared_budget_reserve_and_exact_boundary(monkeypatch, tmp_path,
        spent, planning, expected_stop, expected_fit):
    out = setup_budget(monkeypatch, tmp_path, spent=spent, planning=planning)
    record = trace()
    write_json(out / "screen/traces" / e.trace_filename("q1"), record)
    result = e.preflight(out, {"q1": record})
    assert result["cumulative_stop_cny"] == expected_stop
    assert result["fits_budget"] is expected_fit


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("status", ["ERROR", "TIMEOUT"])
def test_failed_checkpoint_is_preserved_without_resampling(monkeypatch, tmp_path, full, status):
    record = trace(status=status, hypothesis=None)
    checkpoint = tmp_path / "screen/traces" / e.trace_filename("q1")
    write_json(checkpoint, record)
    before = checkpoint.read_bytes()
    generate = Mock(side_effect=AssertionError("must not regenerate"))
    monkeypatch.setattr(e, "make_reader", lambda client: object())
    monkeypatch.setattr(e, "make_record", generate)
    monkeypatch.setattr(e.previous, "evidence_audit", lambda *args: None)
    monkeypatch.setattr(e, "audit_citations", lambda *args: None)
    plan = {"all_ids": ["q1"], "screen_ids": ["q1"], "reader_spec": record["reader"]["spec"]}
    with pytest.raises(e.PilotStopped, match="no automatic resampling"):
        e.run_stage(tmp_path, None, {}, {}, {}, plan, {"q1": record}, object(), full=full)
    assert checkpoint.read_bytes() == before
    generate.assert_not_called()


@pytest.mark.parametrize("change", [
    {"hypothesis_sha256": "stale"}, {"correct": 1}, {"correct": None},
    {"error": "timeout"}, {"question_id": "foreign"},
])
def test_invalid_judge_checkpoint_cannot_be_reused(tmp_path, change):
    path = tmp_path / "verdicts.jsonl"
    write_verdicts(path, [verdict(**change)])
    with pytest.raises(ValueError):
        e.load_verdict_checkpoints(path, HEADER, {"q1"}, {"q1": trace()})


@pytest.mark.parametrize("selection,audited", [(False, True), (True, False)])
def test_full_requires_selection_and_independent_audit(monkeypatch, tmp_path, selection, audited):
    write_json(tmp_path / "selection.json", {"eligible_for_full": selection})
    write_json(tmp_path / "screen-post-run-audit.json", {"audit_passed": audited})
    provider = Mock(side_effect=AssertionError("must not construct model"))
    monkeypatch.setattr(e.previous, "model_provider", provider)
    with pytest.raises(e.PilotStopped):
        e.prepare_full(tmp_path, None, {}, {})
    provider.assert_not_called()


def test_full_evidence_hash_checked_before_resume_loading(monkeypatch, tmp_path):
    write_json(tmp_path / "selection.json", {"eligible_for_full": True})
    write_json(tmp_path / "screen-post-run-audit.json", {"audit_passed": True})
    checkpoint = tmp_path / "offline-full/traces" / e.trace_filename("q1")
    write_json(checkpoint, trace())
    write_json(tmp_path / "full-evidence-plan.json", {
        "dataset_sha256": "fixture", "trace_sha256": {"q1": e.digest(checkpoint)}})
    write_json(checkpoint, trace(context="changed after freeze"))
    load = Mock(side_effect=AssertionError("must validate before loading"))
    monkeypatch.setattr(e, "_load_run", load)
    with pytest.raises(ValueError, match="Frozen full evidence changed"):
        e.prepare_full(tmp_path, SimpleNamespace(sha256="fixture"), {"all_ids": ["q1"]}, {})
    load.assert_not_called()


@pytest.mark.parametrize("paid", [False, True])
def test_offline_or_unaffordable_never_reads_credentials_or_provider(monkeypatch, tmp_path, paid):
    monkeypatch.setattr(sys, "argv", ["experiment", "--output", str(tmp_path)] + (["--paid"] if paid else []))
    monkeypatch.setattr(e, "prepare", lambda out: (object(), {}, {}, {}, {}))
    monkeypatch.setattr(e, "preflight", lambda *args, **kwargs: {"fits_budget": False})
    secrets = Mock(side_effect=AssertionError("must not read credential"))
    network = Mock(side_effect=AssertionError("must not contact provider"))
    monkeypatch.setattr(e.getpass, "getpass", secrets)
    monkeypatch.setattr(e, "private_get", network)
    if paid:
        with pytest.raises(e.PilotStopped, match="no request sent"):
            e.main()
    else:
        e.main()
    secrets.assert_not_called()
    network.assert_not_called()



def test_primary_audit_counts_exact_hits_and_missing_requests_without_network(monkeypatch, tmp_path):
    meter, normal, _ = metered(tmp_path, [payload()], [])
    req = request()
    e.make_reader(meter).primary.generate(req)
    before = meter.path.read_bytes()
    cases = {"q1": SimpleNamespace(question=req.question),
             "q2": SimpleNamespace(question="A different question with the same evidence")}
    monkeypatch.setattr(e, "load_oracle_dataset", lambda path: SimpleNamespace(by_id=lambda: cases))
    monkeypatch.setattr(e, "budget_ledgers", lambda out: [meter.path])
    monkeypatch.setattr(e, "restore_visible_request", lambda question, source: replace(req, question=question))
    audit = e.audit_primary_cache(tmp_path / "candidate", {"q1": trace(), "q2": trace("q2")})
    assert audit["exact_cached_case_ids"] == ["q1"]
    assert audit["uncached_case_ids"] == ["q2"]
    assert audit["cache_hits"] == 1 and audit["new_api_calls"] == 0
    assert meter.path.read_bytes() == before and len(normal.requests) == 1
    assert not (tmp_path / "candidate/primary-audit-cache/ledger.json").exists()


def test_primary_audit_does_not_credit_a_missing_format_repair(monkeypatch, tmp_path):
    meter, normal, _ = metered(tmp_path, ["invalid", payload()], [])
    req = request()
    result = e.make_reader(meter).primary.generate(req)
    assert result.calls == 2
    # Preserve the initial cached failure but omit its needed repair response.
    repair_key = meter.ledger["calls"][1]["key"]
    repair_path = meter.root / "responses" / (repair_key + ".json")
    repair_path.unlink()
    before = meter.path.read_bytes()
    monkeypatch.setattr(e, "load_oracle_dataset", lambda path: SimpleNamespace(
        by_id=lambda: {"q1": SimpleNamespace(question=req.question)}))
    monkeypatch.setattr(e, "budget_ledgers", lambda out: [meter.path])
    monkeypatch.setattr(e, "restore_visible_request", lambda *args: req)
    audit = e.audit_primary_cache(tmp_path / "candidate", {"q1": trace()})
    assert audit["exact_cached_case_ids"] == []
    assert audit["uncached_case_ids"] == ["q1"]
    assert audit["new_api_calls"] == 0 and meter.path.read_bytes() == before
    assert len(normal.requests) == 2


def test_preflight_reserves_uncached_primary_even_when_context_is_unchanged(monkeypatch, tmp_path):
    out = setup_budget(monkeypatch, tmp_path)
    sources = {"q1": trace()}
    cached = e.preflight(out, sources)
    monkeypatch.setattr(e, "audit_primary_cache", lambda out, sources: {
        "exact_cached_case_ids": [], "uncached_case_ids": list(sources), "cache_hits": 0, "new_api_calls": 0})
    missing = e.preflight(out, sources)
    delta = Decimal(missing["predicted_reader_and_judge_cny"]) - Decimal(cached["predicted_reader_and_judge_cny"])
    assert delta > Decimal(2 * 8 * 512) / 1000000
    assert delta == Decimal(missing["primary_uncached_extra_cny"])
    assert missing["primary_cache_audit"]["uncached_case_ids"] == ["q1"]


def test_full_resume_does_not_recharge_completed_screen_reader_and_judge(monkeypatch, tmp_path):
    out = setup_budget(monkeypatch, tmp_path)
    sources = {q: trace(q) for q in ("q1", "q2")}
    monkeypatch.setattr(e, "_load_run", lambda path: ({}, sources))
    write_json(out / "frozen-plan.json", {"screen_ids": ["q1"]})
    write_json(out / "screen/traces" / e.trace_filename("q1"), sources["q1"])
    write_verdicts(out / "screen-verdicts.jsonl", [verdict()])
    result = e.preflight(out, sources, full=True)
    assert result["cases"] == 1 and result["pending_judge_cases"] == 1
    assert result["primary_cache_audit"]["exact_cached_case_ids"] == ["q2"]


def test_forecast_uses_observed_thinking_output_and_retains_judge_allowance():
    first = trace()
    second = deepcopy(first)
    stages = json.loads(second["reader"]["detail"])
    stages["stages"][1]["usage"]["output_tokens"] += 1000
    second["reader"]["detail"] = json.dumps(stages)
    cheap = e.estimate_cost({"q1": first}, judge_cases=0)
    costly = e.estimate_cost({"q1": second}, judge_cases=0)
    assert Decimal(costly["predicted_reader_and_judge_cny"]) - Decimal(cheap["predicted_reader_and_judge_cny"]) == Decimal("0.008")
    judge_only = e.estimate_cost({}, judge_cases=2)
    assert judge_only["cases"] == 0
    assert Decimal(judge_only["planning_allowance_cny"]) > 0
