"""Plan or run handwritten reader challenges; never report an inferred QA score.

This tests the reading stage only. It does not measure memory formation or the
official benchmark. Expected answers are excluded from every model request.
"""

import argparse
import hashlib
import json
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from dive_memory.application.oracle_run import MODEL_PRICES, RequestLedger, RunStopped
from dive_memory.application.readers import create_reader
from dive_memory.models import EvidenceState, Memory, RetrievalItem
from dive_memory.reading.evidence import relevance_proxy
from dive_memory.reading.providers import OpenAICompatibleChatClient
from dive_memory.reading.reader import ReaderRequest
from dive_memory.reading.workspace import BoundedEvidenceWorkspace
from dive_memory.temporal import parse_instant, require_instant


def load_cases(path):
    raw = Path(path).read_bytes()
    dataset = json.loads(raw)
    if (
        dataset.get("schema_version") != "reader-challenge-v1"
        or dataset.get("dataset_kind") != "handwritten_development_fixtures"
        or dataset.get("independent_holdout") is not False
        or not isinstance(dataset.get("cases"), list)
        or not dataset["cases"]
    ):
        raise ValueError("Expected handwritten development challenge cases")
    seen = set()
    for case in dataset["cases"]:
        if not isinstance(case.get("id"), str) or not case["id"] or case["id"] in seen:
            raise ValueError("Case IDs must be unique nonempty strings")
        seen.add(case["id"])
        # Validate every input before starting a potentially paid run.
        build_request(case)
    return dataset["cases"], hashlib.sha256(raw).hexdigest()


class ChallengeSource:
    """Ephemeral scoped fixture evidence, with no access to expected answers."""

    def __init__(self, items, initial=()):
        self.items = {item.memory.id: item for item in items}
        self.discovered = {item.memory.id for item in initial}
        self.search_calls = self.read_calls = 0

    def search(self, query):
        self.search_calls += 1
        found = sorted(
            self.items.values(),
            key=lambda item: (
                -relevance_proxy(query, item.memory.content, item.channels)
            ),
        )[:8]
        self.discovered.update(item.memory.id for item in found)
        return found

    def read(self, memory_id):
        self.read_calls += 1
        if memory_id not in self.discovered:
            raise ValueError("Memory is not in the admitted challenge scope")
        return self.items[memory_id]


def build_request(case, *, token_budget=6000):
    question = case.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Question must be a nonempty string")
    date = require_instant(case.get("date"), "date")
    if not date:
        raise ValueError("Challenge date is required")
    sources = case.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Sources must be a nonempty list")
    items, seen = [], set()
    for source in sources:
        source_id = source.get("id")
        if not isinstance(source_id, str) or not source_id or source_id in seen:
            raise ValueError("Source IDs must be unique nonempty strings")
        seen.add(source_id)
        when = require_instant(source.get("date"), "source.date")
        if not when:
            raise ValueError("Source date is required")
        content = source.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Source content must be nonempty")
        role = source.get("role")
        if role not in {"user", "assistant"}:
            raise ValueError("Source role must be user or assistant")
        if parse_instant(when) > parse_instant(date):
            continue
        items.append(
            RetrievalItem(
                Memory(
                    f"source-{len(items)}",
                    "challenge",
                    "semantic_fact",
                    EvidenceState.OBSERVATION,
                    content,
                    {
                        "_reader_statement_sources": [
                            {
                                "source_ref": source_id,
                                "role": role,
                                "statement_at": when,
                            }
                        ]
                    },
                    observed_at=when,
                    source_event_ids=[source_id],
                ),
                1.0,
                ["challenge_fixture"],
                [source_id],
            )
        )
    request = ReaderRequest(
        question, date, date, tuple(items), "", token_budget=token_budget
    )
    workspace = BoundedEvidenceWorkspace(request)
    workspace.add(items)
    initial, _ = workspace.pack()
    return replace(request, items=initial), ChallengeSource(items, initial)


def run_case(case, reader):
    request, source = build_request(case)
    bind = getattr(reader, "bind_evidence", None)
    if callable(bind):
        reader = bind(source)
    else:
        # The shared renderer binds policy-visible text for non-tool readers.
        from dive_memory.reading.reader import render_reader_context

        request = replace(request, policy=reader.policy)
        request = replace(request, context_text=render_reader_context(request))
    outcome = reader.generate(request)
    return {
        "case_id": case["id"],
        "reader": outcome.to_dict(),
        "source_calls": {"search": source.search_calls, "read": source.read_calls},
        "semantic_correctness": None,
        "evaluation": "NOT_EVALUATED_requires_separate_semantic_review",
    }


class RequestRecorder:
    """Persist prompts and raw replies underneath the budget gate, without keys."""

    def __init__(self, directory):
        self.directory = directory
        self.count = 0

    def wrap(self, client):
        recorder = self

        class RecordedClient:
            endpoint = client.endpoint
            extra_body = client.extra_body

            def complete(self, **kwargs):
                recorder.count += 1
                path = recorder.directory / f"request-{recorder.count:04d}.json"
                row = {"state": "pending", "request": kwargs, "mode": client.extra_body}
                # This runs only after the ledger has granted a reservation.
                with path.open("x", encoding="utf-8") as stream:
                    json.dump(row, stream, ensure_ascii=False, indent=2)
                try:
                    response = client.complete(**kwargs)
                except Exception as exc:  # noqa: BLE001 - persist exception type, never provider secrets
                    row.update(state="failed", error_type=type(exc).__name__)
                    path.write_text(
                        json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    raise
                row.update(
                    state="completed",
                    response=response.text,
                    resolved_model=response.model,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                )
                path.write_text(
                    json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return response

        return RecordedClient()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--strategy",
        choices=("indexed", "r40", "verified", "deliberative", "deliberative-refs"),
        default="deliberative",
    )
    parser.add_argument(
        "--model", choices=tuple(MODEL_PRICES), default="deepseek-flash"
    )
    parser.add_argument(
        "--reasoning-effort", choices=("low", "high", "max"), default="high"
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--max-reserved-tokens", type=int, default=0)
    parser.add_argument("--max-cost-cny", type=Decimal)
    args = parser.parse_args(argv)
    cases, digest = load_cases(args.dataset)
    plan = {
        "dataset_sha256": digest,
        "cases": len(cases),
        "strategy": args.strategy,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "scope": "READER_ONLY_HANDWRITTEN_DEVELOPMENT_FIXTURES",
        "execution": "NEW_GENERATION" if args.execute else "PLAN_ONLY",
        "semantic_evaluation": "NOT_EVALUATED",
        "official_benchmark": False,
        "peak_rates_cny_per_million": MODEL_PRICES[args.model].to_dict(),
        "max_calls": args.max_calls,
        "max_reserved_tokens": args.max_reserved_tokens,
        "max_cost_cny": str(args.max_cost_cny)
        if args.max_cost_cny is not None
        else None,
    }
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if (
        args.max_calls <= 0
        or args.max_reserved_tokens <= 0
        or args.max_cost_cny is None
    ):
        parser.error("execution requires positive request/token/currency caps")
    if not args.max_cost_cny.is_finite() or args.max_cost_cny <= 0:
        parser.error("currency cap must be finite and positive")
    key = os.getenv("DIVE_READER_API_KEY")
    if not key:
        parser.error("DIVE_READER_API_KEY is required")
    args.output.mkdir(parents=True, exist_ok=False)
    ledger = RequestLedger(
        args.output / "request-ledger.json",
        args.max_calls,
        args.max_reserved_tokens,
        args.max_cost_cny,
        prices=MODEL_PRICES[args.model],
    )
    endpoint = "https://api.deepseek.com/chat/completions"
    recorder = RequestRecorder(args.output)
    normal = ledger.wrap(
        recorder.wrap(
            OpenAICompatibleChatClient(
                endpoint, key, extra_body={"thinking": {"type": "disabled"}}
            )
        )
    )
    thinking = ledger.wrap(
        recorder.wrap(
            OpenAICompatibleChatClient(
                endpoint,
                key,
                extra_body={
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": args.reasoning_effort,
                },
            )
        )
    )
    reader = create_reader(
        normal, thinking, provider="deepseek", model=args.model, strategy=args.strategy
    )
    (args.output / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    rows = []
    stopped = None
    try:
        for index, case in enumerate(cases):
            result = run_case(case, reader)
            (args.output / f"case-{index:03d}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            rows.append({"case_id": case["id"], "status": result["reader"]["status"]})
            if any(row["state"] != "completed" for row in ledger.rows):
                raise RunStopped("Request ledger has unresolved transport or usage")
            if result["reader"]["status"] in {"ERROR", "INVALID", "TIMEOUT"}:
                raise RunStopped("Reader failure; remaining cases were not attempted")
    except RunStopped as exc:
        stopped = {"case_id": case["id"], "reason": str(exc), "complete": False}
        (args.output / "stopped.json").write_text(
            json.dumps(stopped, indent=2), encoding="utf-8"
        )
    finally:
        summary = {
            **plan,
            "completed": len(rows),
            "complete": stopped is None and len(rows) == len(cases),
            "stopped": stopped,
            "results": rows,
            "usage": ledger.usage_summary(),
            "peak_cost_cny": str(ledger.peak_cost_cny()),
        }
        (args.output / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "completed": len(rows),
                "semantic_evaluation": "NOT_EVALUATED",
                "output": str(args.output),
            }
        )
    )
    return 0 if summary["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
