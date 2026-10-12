"""Bounded DeepSeek reader pilot. Credentials stay in process memory only.

Sequential/single-process only. Successful exact requests are cached; a durable
reservation precedes every paid request. Uncertain requests block resumption.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import getpass
import hashlib
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "ops"))

from dive_memory.oracle.reader import ChatCompletion, GroundedChatReader, ReaderSpec, reader_policy
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.runner import OracleRunner, atomic_write_json, atomic_write_text, trace_filename
from dive_memory.oracle.pipeline import PipelineConfig
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.evaluate import JudgeSpec, JudgeVerdict, LLMJudge
from dive_memory.eval_manifest import code_snapshot
from oracle_reader_pair import compare_runs
from oracle_retrieval_ablation import DEFAULT_MODEL, DEFAULT_REVISION, model_provider, make_gate

ENDPOINT = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
EXTRA = {"thinking": {"type": "disabled"}}


class PilotStopped(BaseException):
    """Escape reader/judge exception-to-fallback handling without further calls."""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class MeteredClient:
    def __init__(self, client, root, *, max_calls=100, max_tokens=350000,
                 reuse_roots=(), before_request=None, currency_guard=None):
        self.client = client
        self.root = Path(root)
        self.path = self.root / "ledger.json"
        self.max_calls, self.max_tokens = max_calls, max_tokens
        self.phase = "reader"
        self.hits = 0
        self.reuse_roots = tuple(Path(path) for path in reuse_roots)
        self.external_hits = 0
        self.before_request = before_request
        self.currency_guard = currency_guard
        self.ledger = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"calls": []}
        if any(row["status"] != "completed" for row in self.ledger["calls"]):
            raise PilotStopped("Uncertain/failed prior request: inspect ledger before resuming; no automatic retry.")

    def summary(self):
        rows = self.ledger["calls"]
        return {"attempts": len(rows), "cache_hits_this_process": self.hits,
                "external_cache_hits_this_process": self.external_hits,
                "input_tokens": sum(r.get("input_tokens") or 0 for r in rows),
                "output_tokens": sum(r.get("output_tokens") or 0 for r in rows),
                "reserved_or_actual_tokens": sum(r["charged_budget"] for r in rows),
                "max_calls": self.max_calls, "max_tokens": self.max_tokens,
                "unknown_usage_calls": sum(r.get("input_tokens") is None or r.get("output_tokens") is None for r in rows)}

    def complete(self, **kwargs):
        # Transport options are part of the request identity. In particular,
        # thinking-on must never reuse a thinking-off response. Defaults retain
        # the historical namespace for simple scripted clients and old tests.
        key = digest({"endpoint": getattr(self.client, "endpoint", ENDPOINT),
                      "extra_body": dict(getattr(self.client, "extra_body", EXTRA)),
                      "request": kwargs})
        cache = self.root / "responses" / f"{key}.json"
        if cache.exists():
            self.hits += 1
            return ChatCompletion(**json.loads(cache.read_text(encoding="utf-8")))
        for root in self.reuse_roots:
            previous = root / "responses" / f"{key}.json"
            if previous.exists():
                self.hits += 1
                self.external_hits += 1
                return ChatCompletion(**json.loads(previous.read_text(encoding="utf-8")))
        # Deliberately conservative byte-based reservation, not an exact tokenizer
        # or a currency guarantee. Output uses the provider's max_tokens cap.
        reserve = len(json.dumps(kwargs["messages"], ensure_ascii=False).encode()) + 1024 + kwargs["max_tokens"]
        if len(self.ledger["calls"]) >= self.max_calls or self.summary()["reserved_or_actual_tokens"] + reserve > self.max_tokens:
            raise PilotStopped("Call/token budget reached; no further request sent.")
        if self.before_request is not None:
            self.before_request(len(self.ledger["calls"]))
        if self.currency_guard is not None:
            self.currency_guard(reserve)
        row = {"key": key, "phase": self.phase, "status": "pending", "charged_budget": reserve}
        self.ledger["calls"].append(row)
        atomic_write_json(self.path, self.ledger)
        try:
            result = self.client.complete(**kwargs)
        except Exception as exc:
            row.update(status="failed", error_type=type(exc).__name__)
            atomic_write_json(self.path, self.ledger)
            # Deliberately omit upstream exception text: it can contain secrets.
            raise PilotStopped(f"Transport stopped ({type(exc).__name__}); no automatic retry.") from None
        row.update(status="completed", input_tokens=result.input_tokens,
                   output_tokens=result.output_tokens, resolved_model=result.model)
        if result.input_tokens is not None and result.output_tokens is not None:
            row["charged_budget"] = result.input_tokens + result.output_tokens
        atomic_write_json(cache, asdict(result))
        atomic_write_json(self.path, self.ledger)
        print(json.dumps({"paid_call": len(self.ledger["calls"]), "phase": self.phase,
                          "input": result.input_tokens, "output": result.output_tokens}), flush=True)
        return result


def private_get(path, key):
    request = urllib.request.Request("https://api.deepseek.com/" + path,
                                     headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise PilotStopped(f"Official API preflight HTTP {exc.code}; no paid request sent.") from None
    except Exception as exc:
        raise PilotStopped(f"Official API preflight failed ({type(exc).__name__}); no paid request sent.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "eval/reports/oracle-flash-pilot-20-20260927")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--judge-existing", action="store_true", help="Only evaluate immutable completed reader traces; never rerun readers.")
    args = parser.parse_args()
    key = getpass.getpass("DeepSeek key (hidden, never saved): ")
    if not key.strip():
        raise PilotStopped("Empty credential; no request sent.")
    models = private_get("models", key)
    ids = [item["id"] for item in models.get("data", [])]
    atomic_write_json(args.output / "preflight.json", {"available_models": ids, "requested_model": MODEL})
    if MODEL not in ids:
        raise PilotStopped(f"Requested model {MODEL} absent from official model list; no paid request sent.")
    balance = private_get("user/balance", key)
    if not (args.output / "balance-before.json").exists():
        atomic_write_json(args.output / "balance-before.json", balance)
    print(json.dumps({"preflight": "ok", "model": MODEL, "balance": balance}), flush=True)
    if args.preflight_only:
        return
    dataset_path = ROOT / "eval/external/longmemeval/longmemeval_oracle.json"
    dataset = load_oracle_dataset(dataset_path)
    ids_path = ROOT / "eval/reports/oracle-reader-pilot-20.ids"
    plan = json.loads(ids_path.with_suffix(".ids.plan.json").read_text(encoding="utf-8"))
    if dataset.sha256 != plan["dataset_sha256"] or hashlib.sha256(ids_path.read_text(encoding="utf-8").encode()).hexdigest() != plan["ids_sha256"]:
        raise PilotStopped("Pilot dataset/selection hash mismatch.")
    cases = [dataset.by_id()[qid] for qid in ids_path.read_text().splitlines() if qid]
    client = MeteredClient(OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=EXTRA), args.output / "api-cache")
    if not args.judge_existing:
        provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, "cuda")
        gate, gate_name = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
                                  document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
        for arm, policy_name in (("v2", "evidence_first_v2"), ("v3", "evidence_first")):
            client.phase = "reader-" + arm
            policy = reader_policy(policy_name)
            reader = GroundedChatReader(client, ReaderSpec(provider="deepseek", model=MODEL,
                                        prompt_version=policy.prompt_version, max_tokens=512), policy=policy)
            runner = OracleRunner(dataset, args.output / arm, reader, gate=gate, gate_name=gate_name,
                                  pipeline_config=PipelineConfig(reader_policy_name=policy_name,
                                      scoring_policy_name="balanced", document_selection_name="query_excerpt",
                                      document_chunk_search_name="controlled_supplement"),
                                  selection=cases, selection_meta={"ids_sha256": plan["ids_sha256"], "seed": plan["seed"]},
                                  progress=lambda event: print(json.dumps(event), flush=True))
            runner.run(resume=(args.output / arm / "manifest.json").exists())
    audit = compare_runs(args.output / "v2", args.output / "v3", dataset_path=dataset_path)
    atomic_write_json(args.output / "evidence-audit.json", audit)
    if not audit["comparable_reader_ablation"]:
        raise PilotStopped("Evidence audit failed; skipped paid judging.")
    if audit["cases_compared"] != len(cases):
        raise PilotStopped("Incomplete reader runs; skipped paid judging.")
    client.phase = "judge"
    spec = JudgeSpec(provider="deepseek", model=MODEL, max_tokens=128,
                     caveat="Same-family self-judging, non-official pilot only.")
    judge = LLMJudge(client, spec)
    header = {"_header": True, "schema_version": "dive-judge-verdicts-v1",
              "judge": spec.to_dict(), "dataset_sha256": dataset.sha256,
              "invalid_output_policy": "deterministic-failure-false; null digest is SHA256 of literal null"}
    for arm in ("v2", "v3"):
        rows = [header]
        for case in cases:
            trace = json.loads((args.output / arm / "traces" / trace_filename(case.question_id)).read_text(encoding="utf-8"))
            hypothesis = trace["hypothesis"]
            invalid = (trace.get("reader", {}).get("status") in {"INVALID", "ERROR", "TIMEOUT"}
                       or trace.get("hypothesis_origin") == "ERROR_FALLBACK")
            if invalid:
                verdict = JudgeVerdict(False, "INVALID_OUTPUT_RULE")
            elif not isinstance(hypothesis, str):
                raise PilotStopped("Non-failure trace lacks a text hypothesis.")
            else:
                verdict = judge.judge(question=case.question, reference=case.answer, hypothesis=hypothesis,
                                      question_type="abstention" if case.should_abstain else case.question_type)
            rows.append({"question_id": case.question_id,
                         "hypothesis_sha256": hashlib.sha256(("null" if hypothesis is None else hypothesis).encode()).hexdigest()[:16],
                         "correct": verdict.correct, "raw": verdict.raw, "error": verdict.error})
            atomic_write_text(args.output / f"{arm}-verdicts.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    report = compare_runs(args.output / "v2", args.output / "v3", dataset_path=dataset_path,
                         baseline_verdicts=args.output / "v2-verdicts.jsonl", candidate_verdicts=args.output / "v3-verdicts.jsonl")
    atomic_write_json(args.output / "comparison.json", report)
    atomic_write_json(args.output / "evaluation-code-snapshot.json", code_snapshot(ROOT))
    atomic_write_json(args.output / "usage.json", client.summary())
    atomic_write_json(args.output / "balance-after.json", private_get("user/balance", key))
    print(json.dumps({"usage": client.summary(), "accuracy": report["accuracy"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except PilotStopped as exc:
        print(str(exc), flush=True)
        sys.exit(2)
