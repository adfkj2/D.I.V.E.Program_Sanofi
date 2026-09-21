# ADR-008 Memory conflict resolution and immutable transitions

Status: Accepted for the deterministic baseline (2026-09-20)

## Context

The MVP superseded every different value of a single-valued predicate. It did
not distinguish duplicate, reinforcement, correction, temporal update, or
unresolved contradiction, and status changes were not represented as their own
auditable records.

## Options

1. Last write wins: simple, but silently rewrites uncertain truth.
2. Keep every statement active: preserves evidence, but cannot answer current facts.
3. LLM-only resolver: broad language coverage, but unsafe mutation authority.
4. Related-memory classification followed by deterministic domain policy.

## Decision

Choose option 4. The resolver emits exactly one of `unrelated`, `duplicate`,
`reinforcement`, `refinement`, `correction`, `temporal_update`,
`contradiction`, or `supersession`, plus confidence, reason and version.
Deterministic policy then performs `CREATE`, `MERGE_PROVENANCE`, `REINFORCE`,
`SUPERSEDE`, or `COEXIST`.

Duplicate evidence attaches to the existing memory. Reinforcement also raises
bounded confidence/importance. Correction, temporal update, refinement and
safe supersession close the old half-open validity interval and link the new
memory. Low-confidence contradiction coexists and records `contradicts_id`.
Every action is appended to `memory_transitions`; hard purge removes transition
references to the purged memory/event.

## Rejected alternatives

- Last-write-wins cannot explain or reconstruct truth changes.
- Keep-all makes current profile projection ambiguous.
- A classifier may propose a relationship later, but will not execute storage mutations.

## Consequences

The Chengdu-to-Shanghai case has both a correct current view and historical
view. The rule classifier is intentionally conservative and remains a baseline;
its per-relationship precision/recall must be measured before adding a model.
