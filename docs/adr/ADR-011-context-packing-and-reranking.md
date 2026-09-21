# ADR-011 Context packing before model reranking

Status: Packing baseline accepted; reranker deferred (2026-09-20)

## Context

The MVP inserted retrieval order directly into context with a `len/4` token
estimate. Canonical duplicates wasted budget, current answers could receive
outdated items when callers bypassed storage filtering, and contradictions were
not labeled.

## Options

1. Preserve rank order only: cheap, but wasteful and temporally unsafe.
2. Deterministic correctness packing before optional learned reranking.
3. LLM compression/reranking: flexible, but expensive and capable of deleting evidence.

## Decision

Choose option 2. Packing filters terminal/outdated status according to query
mode, canonicalizes subject/predicate/value, merges duplicate provenance,
labels contradiction groups, and records every inclusion/exclusion reason.
`TokenCounter` is injected from the actual reader; packing recomputes the token
count for the fully joined context and never exceeds the budget. Without an
injected reader tokenizer the dependency-free heuristic remains available but
is explicitly returned as degraded/approximate.

No cross-encoder or LLM reranker is selected. It may become a variant only
after held-out candidate recall is stable and its QA gain exceeds latency/run
variance.

## Consequences

Current and historical context semantics are explicit and debuggable. The
default local path still lacks the reader's exact tokenizer, so API consumers
must treat `token_count_degraded=true` as a deployment configuration gap.
