"""Cold Oracle entry point: public inputs, explicit preset, no result/cache imports.

By default only prints a plan. Execution requires explicit caps and provider
credentials. Currency authorization is external: token caps are not a price.
"""

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

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


class RequestLedger:
    """Single-process durable reservation; retains uncertainty, no implicit retries."""

    def __init__(self, path: Path, max_calls: int, max_reserved_tokens: int):
        self.path = path
        self.max_calls = max_calls
        self.max_reserved_tokens = max_reserved_tokens
        self.rows: list[dict] = []
        if path.exists():
            self.rows = json.loads(path.read_text(encoding="utf-8"))["requests"]
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
                reserve = (
                    len(json.dumps(kwargs["messages"], ensure_ascii=False).encode())
                    + 1024
                    + kwargs["max_tokens"]
                )
                if (
                    len(ledger.rows) >= ledger.max_calls
                    or sum(r["reserved_tokens"] for r in ledger.rows) + reserve
                    > ledger.max_reserved_tokens
                ):
                    raise RunStopped(
                        "Request/reserved-token cap reached; no further request sent"
                    )
                row = {
                    "state": "pending",
                    "reserved_tokens": reserve,
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
                try:
                    response = client.complete(**kwargs)
                except Exception as exc:  # noqa: BLE001 - persist uncertainty, never serialize provider text
                    row.update(state="failed", error_type=type(exc).__name__)
                    ledger.save()
                    raise RunStopped(
                        "Transport failed; inspect the ledger before resuming"
                    ) from None
                row.update(
                    state="completed",
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    resolved_model=response.model,
                )
                ledger.save()
                return response

        return CappedClient()

    def save(self):
        atomic_write_json(
            self.path,
            {
                "max_calls": self.max_calls,
                "max_reserved_tokens": self.max_reserved_tokens,
                "requests": self.rows,
                "cache_replay": False,
                "currency_cost": "NOT_COMPUTED_current_provider_rates_required",
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
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    args = parser.parse_args(argv)
    config = r40_pipeline()
    plan = {
        "pipeline": config.to_dict(),
        "reader_strategy": "r40",
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
        args.output / "request-ledger.json", args.max_calls, args.max_reserved_tokens
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
            extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        )
    )
    reader = create_reader(
        normal, thinking, model="deepseek-flash", provider="deepseek", strategy="r40"
    )
    atomic_write_json(
        args.output / "execution-plan.json",
        {
            **plan,
            "reader": reader.spec.to_dict(),
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
        selection=dataset.cases[: args.max_cases] if args.max_cases else dataset.cases,
    )
    try:
        summary = runner.run()
    except RunStopped as exc:
        atomic_write_json(
            args.output / "stopped.json", {"reason": str(exc), "complete": False}
        )
        print(str(exc))
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
