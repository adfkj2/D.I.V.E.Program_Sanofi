"""Cold Oracle entry point: public inputs, explicit preset, no result/cache imports.

By default only prints a plan. Execution requires explicit caps and provider
credentials. Currency authorization is external: token caps are not a price.
"""

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import cast

from ..oracle.loader import load_oracle_dataset
from ..oracle.pipeline import PipelineConfig
from ..oracle.providers import OpenAICompatibleChatClient
from ..oracle.runner import OracleRunner, atomic_write_json
from .readers import create_reader


def r40_pipeline() -> PipelineConfig:
    return PipelineConfig(
        limit=60,
        token_budget=6000,
        reader_policy_name="indexed_evidence",
        scoring_policy_name="balanced",
        document_selection_name="query_excerpt",
        document_chunk_search_name="controlled_supplement",
        episodic_retention=True,
        semantic_retrieval_only=True,
        narrative_retention=True,
        source_diversity=True,
        reported_detail_anchors=True,
        reported_detail_assertions_only=True,
        precise_detail_terms=True,
        contextual_fact_anchors=True,
        explicit_user_documents=True,
        prefer_explicit_user_documents=True,
    )


class RunStopped(BaseException):
    """Stop the entire run; readers must not turn a budget stop into an answer."""


@dataclass(frozen=True)
class TokenPrices:
    """Peak CNY per million tokens; estimates are never a settled invoice."""

    input: Decimal = Decimal("2")
    output: Decimal = Decimal("8")

    def __post_init__(self):
        if any(
            not value.is_finite() or value <= 0 for value in (self.input, self.output)
        ):
            raise ValueError("token prices must be finite and positive")

    def to_dict(self) -> dict[str, str]:
        return {"input": str(self.input), "output": str(self.output)}


# Explicit, dated presets from the provider's published peak, uncached rates.
# A new model must supply its own pricing instead of inheriting Flash prices.
MODEL_PRICES = {
    "deepseek-flash": TokenPrices(),
    "deepseek-v4-pro": TokenPrices(Decimal("9"), Decimal("27")),
}
PRICING_AS_OF = "2026-10-19"


class RequestLedger:
    """Single-process durable reservation; retains uncertainty, no implicit retries."""

    def __init__(
        self,
        path: Path,
        max_calls: int,
        max_reserved_tokens: int,
        max_cost_cny: Decimal | None = None,
        *,
        prices: TokenPrices = TokenPrices(),
    ):
        self.path = path
        self.max_calls = max_calls
        self.max_reserved_tokens = max_reserved_tokens
        if max_cost_cny is not None and (
            not max_cost_cny.is_finite() or max_cost_cny <= 0
        ):
            raise ValueError("currency cap must be finite and positive")
        self.max_cost_cny = max_cost_cny
        self.prices = prices
        self.rows: list[dict] = []
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            # Earlier ledgers predate model selection and used Flash's 2/8 rates.
            stored_prices = saved.get(
                "peak_rates_cny_per_million", TokenPrices().to_dict()
            )
            if stored_prices != prices.to_dict():
                raise RunStopped("Ledger rates changed; start a separate priced run")
            self.rows = saved["requests"]
        if any(r["state"] != "completed" for r in self.rows):
            raise RunStopped(
                "Prior request is uncertain/failed; reconcile before starting a new run"
            )

    def wrap(self, client):
        ledger = self

        class CappedClient:
            def complete(self, **kwargs):
                # UTF-8 bytes plus envelope allowance is a conservative estimate,
                # not the model's exact tokenizer. All reservations are retained.
                reserved_input = (
                    len(json.dumps(kwargs["messages"], ensure_ascii=False).encode())
                    + 1024
                )
                reserved_output = kwargs["max_tokens"]
                reserve = reserved_input + reserved_output
                if (
                    len(ledger.rows) >= ledger.max_calls
                    or sum(r["reserved_tokens"] for r in ledger.rows) + reserve
                    > ledger.max_reserved_tokens
                ):
                    raise RunStopped(
                        "Request/reserved-token cap reached; no further request sent"
                    )
                if ledger.max_cost_cny is not None:
                    # Keep conservative token upper bounds, charging each at its
                    # own peak rate. Output tokens must not be priced as input.
                    projected = (
                        ledger.peak_cost_cny()
                        + (
                            ledger.prices.input * reserved_input
                            + ledger.prices.output * reserved_output
                        )
                        / 1_000_000
                    )
                    if projected > ledger.max_cost_cny:
                        raise RunStopped(
                            "Currency cap reached; no further request sent"
                        )
                row = {
                    "state": "pending",
                    "reserved_tokens": reserve,
                    "reserved_input_tokens": reserved_input,
                    "reserved_output_tokens": reserved_output,
                    "request_sha256": hashlib.sha256(
                        json.dumps(
                            {
                                "request": kwargs,
                                "endpoint": client.endpoint,
                                "mode": client.extra_body,
                            },
                            sort_keys=True,
                        ).encode()
                    ).hexdigest(),
                }
                ledger.rows.append(row)
                ledger.save()
                started = perf_counter()
                try:
                    response = client.complete(**kwargs)
                except Exception as exc:  # noqa: BLE001 - persist uncertainty, never serialize provider text
                    row.update(
                        state="failed",
                        error_type=type(exc).__name__,
                        elapsed_seconds=perf_counter() - started,
                    )
                    ledger.save()
                    raise RunStopped(
                        "Transport failed; inspect the ledger before resuming"
                    ) from None
                row.update(
                    state="completed",
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    finish_reason=getattr(response, "finish_reason", None),
                    reasoning_tokens=getattr(response, "reasoning_tokens", None),
                    resolved_model=response.model,
                    elapsed_seconds=perf_counter() - started,
                )
                ledger.save()
                if not all(
                    type(value) is int and value >= 0
                    for value in (response.input_tokens, response.output_tokens)
                ):
                    row["state"] = "uncertain_usage"
                    ledger.save()
                    raise RunStopped(
                        "Provider usage is missing or invalid; reconcile before continuing"
                    )
                return response

        return CappedClient()

    def peak_cost_cny(self) -> Decimal:
        total = Decimal(0)
        for row in self.rows:
            inputs, outputs = row.get("input_tokens"), row.get("output_tokens")
            if row.get("state") == "completed" and all(
                type(value) is int and value >= 0 for value in (inputs, outputs)
            ):
                total += (
                    self.prices.input * cast(int, inputs)
                    + self.prices.output * cast(int, outputs)
                ) / 1_000_000
            else:
                reserved_input = row.get("reserved_input_tokens")
                reserved_output = row.get("reserved_output_tokens")
                if all(
                    type(v) is int and v >= 0 for v in (reserved_input, reserved_output)
                ):
                    total += (
                        self.prices.input * cast(int, reserved_input)
                        + self.prices.output * cast(int, reserved_output)
                    ) / 1_000_000
                else:
                    # Preserve the conservative accounting for old uncertain rows.
                    total += (
                        max(self.prices.input, self.prices.output)
                        * row["reserved_tokens"]
                        / 1_000_000
                    )
        return total

    def usage_summary(self):
        # Never present a partial token sum as usage for the whole run.
        def total(field):
            values = [row.get(field) for row in self.rows]
            return (
                sum(values) if all(type(v) is int and v >= 0 for v in values) else None
            )

        return {
            "attempted_calls": len(self.rows),
            "completed_calls": sum(row["state"] == "completed" for row in self.rows),
            "input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"),
            "missing_usage_calls": sum(
                any(
                    type(row.get(field)) is not int or row[field] < 0
                    for field in ("input_tokens", "output_tokens")
                )
                for row in self.rows
            ),
            "cost_cny": None,
            "cost_note": "Unknown until current provider tariff/billing is supplied; never zero by omission.",
        }

    def save(self):
        atomic_write_json(
            self.path,
            {
                "max_calls": self.max_calls,
                "max_reserved_tokens": self.max_reserved_tokens,
                "max_cost_cny": str(self.max_cost_cny)
                if self.max_cost_cny is not None
                else None,
                "peak_rates_cny_per_million": self.prices.to_dict(),
                "estimated_peak_cost_cny": str(self.peak_cost_cny()),
                "requests": self.rows,
                "cache_replay": False,
                "currency_cost": "NOT_COMPUTED_current_provider_rates_required",
                "usage": self.usage_summary(),
            },
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--max-reserved-tokens", type=int, default=0)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument(
        "--model", choices=tuple(MODEL_PRICES), default="deepseek-flash"
    )
    parser.add_argument(
        "--reader-strategy",
        choices=(
            "r40",
            "adaptive",
            "verified",
            "deliberative",
            "deliberative-refs",
            "deliberative-staged",
            "deliberative-grounded",
        ),
        default="r40",
    )
    parser.add_argument(
        "--fact-policy", choices=("historical", "scoped"), default="historical"
    )
    parser.add_argument("--dialogue-retention", action="store_true")
    parser.add_argument("--dialogue-max-chars", type=int, default=2000)
    parser.add_argument("--dialogue-max-turn-chars", type=int, default=16000)
    parser.add_argument(
        "--reasoning-effort", choices=("low", "high", "max"), default="low"
    )
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--max-cost-cny", type=Decimal)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    args = parser.parse_args(argv)
    if args.max_cases is not None and args.max_cases <= 0:
        parser.error("max-cases must be positive")
    try:
        config = replace(
            r40_pipeline(),
            scoped_fact_anchors=args.fact_policy == "scoped",
            dialogue_retention=args.dialogue_retention,
            dialogue_max_chars=args.dialogue_max_chars,
            dialogue_max_turn_chars=args.dialogue_max_turn_chars,
        )
    except ValueError as exc:
        parser.error(str(exc))
    plan = {
        "pipeline": config.to_dict(),
        "model": args.model,
        "peak_rates_cny_per_million": MODEL_PRICES[args.model].to_dict(),
        "pricing_as_of": PRICING_AS_OF,
        "reader_strategy": args.reader_strategy,
        "reasoning_effort": args.reasoning_effort,
        "selection": str(args.selection) if args.selection else None,
        "max_cost_cny": str(args.max_cost_cny)
        if args.max_cost_cny is not None
        else None,
        "execution": "NEW_GENERATION" if args.execute else "PLAN_ONLY",
        "official_evaluation": "NOT_EVALUATED",
        "max_calls": args.max_calls,
        "max_reserved_tokens": args.max_reserved_tokens,
        "cache_replay": False,
        "gate_embedding_model": "BAAI/bge-m3",
        "model_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "dataset_exposure": "All 500 Oracle cases used during development; this is not a blind test",
    }
    if not args.execute:
        print(json.dumps(plan, indent=2))
        return 0
    if args.max_calls <= 0 or args.max_reserved_tokens <= 0:
        parser.error("execution requires positive request and reserved-token caps")
    if args.output.exists():
        parser.error(
            "cold runs require a new output directory; old outputs are never overwritten"
        )
    key = os.getenv("DIVE_READER_API_KEY")
    if not key:
        parser.error("DIVE_READER_API_KEY is required")
    from ..embeddings import LocalSentenceTransformerEmbeddingProvider
    from ..semantic_gate import POLICY_VERSION, DocumentAdmissionPolicy, SemanticGate

    dataset = load_oracle_dataset(args.dataset)
    selected = dataset.cases[: args.max_cases] if args.max_cases else dataset.cases
    if args.selection:
        ids = json.loads(args.selection.read_text(encoding="utf-8"))
        known = dataset.by_id()
        if (
            not isinstance(ids, list)
            or not ids
            or not all(isinstance(q, str) for q in ids)
            or len(set(ids)) != len(ids)
            or set(ids) - set(known)
            or args.max_cases
        ):
            parser.error(
                "selection requires unique known IDs and cannot combine with max-cases"
            )
        selected = tuple(known[q] for q in ids)
    provider = LocalSentenceTransformerEmbeddingProvider.load(
        model=plan["gate_embedding_model"],
        revision=plan["model_revision"],
        device=args.device,
    )
    gate = SemanticGate(
        provider.encoder,
        cache_size=1024,
        encode_batch_size=8,
        max_sequence_length=512,
        document_admission=DocumentAdmissionPolicy(mode="all"),
    )
    # Pin the same encoder truncation as the gate and R40's evidence preparation.
    provider.encoder.max_seq_length = 512
    ledger = RequestLedger(
        args.output / "request-ledger.json",
        args.max_calls,
        args.max_reserved_tokens,
        args.max_cost_cny,
        prices=MODEL_PRICES[args.model],
    )
    endpoint = "https://api.deepseek.com/chat/completions"
    normal = ledger.wrap(
        OpenAICompatibleChatClient(
            endpoint,
            key,
            provider="deepseek",
            extra_body={"thinking": {"type": "disabled"}},
        )
    )
    thinking = ledger.wrap(
        OpenAICompatibleChatClient(
            endpoint,
            key,
            provider="deepseek",
            extra_body={
                "thinking": {"type": "enabled"},
                "reasoning_effort": args.reasoning_effort,
            },
        )
    )
    reader = create_reader(
        normal,
        thinking,
        model=args.model,
        provider="deepseek",
        strategy=args.reader_strategy,
        reasoning_effort=args.reasoning_effort,
    )
    atomic_write_json(
        args.output / "execution-plan.json",
        {
            **plan,
            "reader": reader.spec.to_dict(),
            "reader_execution": getattr(
                reader, "execution_configuration", lambda: None
            )(),
            "pipeline_fields": asdict(config),
            "dataset_sha256": dataset.sha256,
        },
    )
    runner = OracleRunner(
        dataset,
        args.output / "run",
        reader,
        gate=gate,
        gate_name=POLICY_VERSION,
        embedder=provider,
        embedder_name="BAAI/bge-m3@" + plan["model_revision"],
        pipeline_config=config,
        selection=selected,
    )
    started = perf_counter()
    try:
        summary = runner.run()
    except RunStopped as exc:
        atomic_write_json(
            args.output / "stopped.json", {"reason": str(exc), "complete": False}
        )
        print(str(exc))
        return 2
    finally:
        atomic_write_json(
            args.output / "execution-metrics.json",
            {
                **ledger.usage_summary(),
                "wall_seconds": perf_counter() - started,
                "timing_scope": "runner only; excludes model loading and judge",
                "cache_replay": False,
                "official_score": "NOT_EVALUATED",
            },
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
