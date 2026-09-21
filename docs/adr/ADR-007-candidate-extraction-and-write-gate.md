# ADR-007 Strict candidate extraction and auditable write gate

Status: Accepted for the Phase 3 local path (2026-09-20)

## Context

The MVP let an OpenAI-compatible extractor return loosely checked dictionaries
and silently replaced transport, JSON, and schema failures with heuristic
output. The keyword gate persisted a reason but not its features or versions.
That made false memories and provider drift difficult to diagnose.

## Options

1. Keep regex-only extraction: deterministic, but low multilingual and compound-fact recall.
2. Let an LLM write `Memory` rows: flexible, but couples an untrusted response to mutation.
3. Validate an ephemeral candidate, normalize it, apply policy, then commit.
4. Train a dedicated classifier now: premature without labeled formation data.

## Decision

Choose option 3. `CandidateMemoryV1` strictly validates enums, finite 0–1
bounds, evidence substrings, canonical predicate/value, and half-open validity
intervals. Provenance must be bound before commit. Provider outcomes distinguish
`EMPTY`, `POLICY_SKIP`, `MALFORMED`, `TIMEOUT`, `FALLBACK_USED`, and
`COMMITTED`. The gate records its monotonic feature vector and
schema/prompt/model/policy versions. Sensitive secrets remain a hard skip.

The heuristic provider remains the offline fallback and deterministic test
provider. A fallback is visible and is never reported as model-backed success.

## Rejected alternatives

- Regex-only remains a fallback, not the production recommendation.
- Direct LLM mutation is rejected because schema validity is not factual validity.
- A learned gate is deferred until a user/session-disjoint labeled set exists.

## Consequences

Formation failures are replayable and measurable, but strict validation may
lower recall. Rejection distributions therefore belong in every model-backed
formation report. The current utility weights are an explainable baseline, not
a calibrated production classifier.
