# Security and privacy threat model

## Protected assets and boundary

The service stores conversation-derived personal facts, source events,
provenance, transitions, vectors, entity/relation projections, profiles and
access logs. The local SQLite process is one trust boundary; the optional HTTP
adapter is another. Backups, exports, model providers and log aggregation are
outside the current executable boundary and must not be described as erased by
a database-only purge.

## Primary threats and controls

| Threat | Current control | Remaining production gap |
|---|---|---|
| Cross-namespace read/write | every store query is namespace/status scoped; API authorizer tests | PostgreSQL RLS and explicit tenant ownership are not implemented |
| Confused deputy/action escalation | authorizer receives action; static grants may be action-scoped | production IAM/KMS integration |
| Idempotency leakage | key uniqueness is `(namespace,key)` | rate limiting and audit export |
| Deleted/stale retrieval | hard status filter; FTS/vector/entity/relation cleanup tests | PostgreSQL and backup residue test |
| Secret persistence/egress | hard secret patterns reject known credentials | classifier policy for broader PII/PHI; DLP review |
| Model-provider leakage | providers are injected; no default paid API | egress allow-list, contracts and regional policy |
| Trace leakage | trace stores query SHA-256, counts and ids, not raw query | log retention/cardinality policy |
| Wrong truth mutation | strict schema, provenance, conservative resolver, immutable transition | calibrated classifier and human review workflow |

## Deployment gates

`RuntimeConfig(mode="production")` requires an authorizer and a semantic
embedding provider, rejects raw sensitive trace payloads, and keeps fallback
degradation visible. This is a fail-fast configuration check, not proof of
network, host, database or IAM hardening.

No current evidence supports claims of HIPAA/GDPR certification, encrypted
field-level vaulting, backup erasure, PostgreSQL RLS, or cross-region policy
compliance.
