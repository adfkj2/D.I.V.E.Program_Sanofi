# Phase 4 Readiness Checklist

Checklist date: 2026-09-22  
Checklist baseline: commit `3b1907afda4c3fdca84870312f32cef16536d66a`, working tree dirty with
the Phase 4 closure changes  
Companion documents: `docs/22-phase-4-production-gap-analysis.md` (narrative gap
analysis), `docs/21-phase-4-architecture-review.md` (architecture verdicts)

> ## Purpose and standing
>
> This checklist is the itemised half of the production-readiness review that plan
> §P4-P2-3 requires. `docs/22` explains *why* each domain is where it is; this
> document records **what was checked, what the verdict is, and what evidence
> exists** — at a granularity where every line can be re-verified by re-running a
> command or re-reading one artifact.
>
> **Overall readiness verdict: `NOT READY`.** Plan §P4-P2-3: *"Phase 4 completion
> does not imply production readiness."* Nothing here overrides that.
>
> **Unchanged rule:** only **direct evidence** earns `PASS`. Code existence is not
> evidence of behaviour; a deterministic unit test is not evidence of scale; a
> benchmark number is not evidence of an SLO.

---

## 0. How to use this checklist

| Verdict | Meaning |
|---|---|
| `PASS` | Direct evidence exists for the stated scope and is reproducible |
| `PARTIAL` | Evidence covers part of the scope, or a narrower workload/environment |
| `NOT TESTED` | An executable experiment is known and has not been run |
| `NOT IMPLEMENTED` | The capability is absent from the repository — established by search, not assumed |
| `NOT DEFINED` | No target exists to test against |
| `UNKNOWN` | Not enough information in the repository to define the test without external policy or access |
| `NOT APPLICABLE` | Out of scope for the current system |

A verdict is a statement about the **item**, not about the domain containing it.
`PASS` on "migrations are checksum-verified" does not upgrade "production rollout
has been rehearsed".

---

## 1. Summary

| Domain | # items | `PASS` | `PARTIAL` | `NOT TESTED` | `NOT IMPL.` | `NOT DEF.` | `UNKNOWN` | Domain verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1 Data integrity | 4 | 2 | 1 | 1 | 0 | 0 | 0 | `PARTIAL` |
| 2 Concurrency | 5 | 2 | 2 | 1 | 0 | 0 | 0 | `PARTIAL` |
| 3 Security | 6 | 0 | 4 | 1 | 1 | 0 | 0 | `PARTIAL` |
| 4 RLS | 4 | 0 | 0 | 2 | 2 | 0 | 0 | `NOT IMPLEMENTED` |
| 5 Backup / restore | 4 | 0 | 0 | 0 | 1 | 1 | 2 | `UNKNOWN` |
| 6 Migration | 4 | 4 | 0 | 0 | 0 | 0 | 0 | **`PASS`** |
| 7 Observability | 3 | 0 | 1 | 0 | 2 | 0 | 0 | `NOT IMPLEMENTED` |
| 8 Capacity | 5 | 1 | 1 | 2 | 1 | 0 | 0 | `PARTIAL` |
| 9 Deployment / rollback | 4 | 0 | 0 | 0 | 4 | 0 | 0 | `NOT IMPLEMENTED` |
| 10 Disaster recovery | 3 | 0 | 0 | 0 | 1 | 1 | 1 | `NOT IMPLEMENTED` |
| 11 Erasure | 5 | 2 | 1 | 2 | 0 | 0 | 0 | `PARTIAL` |
| 12 Secrets | 4 | 0 | 1 | 0 | 1 | 1 | 1 | `PARTIAL` |
| 13 Logs | 4 | 0 | 1 | 1 | 1 | 1 | 0 | `PARTIAL` |
| 14 Metrics | 2 | 0 | 0 | 0 | 2 | 0 | 0 | `NOT IMPLEMENTED` |
| 15 Alerts | 2 | 0 | 0 | 0 | 2 | 0 | 0 | `NOT IMPLEMENTED` |
| 16 Cost | 3 | 0 | 0 | 1 | 1 | 1 | 0 | `NOT IMPLEMENTED` |
| 17 SLO | 4 | 0 | 0 | 0 | 0 | 4 | 0 | `NOT DEFINED` |
| 18 Runbooks | 4 | 1 | 1 | 0 | 2 | 0 | 0 | `PARTIAL` |
| **Total** | **70** | **12** | **13** | **11** | **21** | **9** | **4** | **`NOT READY`** |

Counts are arithmetic over the item tables in §2: `12 + 13 + 11 + 21 + 9 + 4 = 70`.

**12 of 70 items earn `PASS`.** The passing items are concentrated in migration
mechanics, deterministic integrity invariants, deletion inside the live database
and the migration runbook — i.e. the parts of the system that are *locally
verifiable without external infrastructure*.

---

## 2. Item-level checklist

### 1. Data integrity

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1.1 | Migrations apply idempotently on a real database | `PASS` | 8/8 PG integration tests; isolated-schema clean-history path |
| 1.2 | Projection rollback works | `PASS` | PG integration suite, projection-rollback test |
| 1.3 | Full-corpus integrity audit at 100K | `NOT TESTED` | recovery manifest `integrity_audit` = `PENDING`; **never run** |
| 1.4 | Data-set checksums recorded for published runs | `PARTIAL` | LongMemEval artifact carries `dataset_sha256 = d6f21ea9…`; embedding run carries its own; the false-memory artifacts carry **no** dataset hash |

### 2. Concurrency

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 2.1 | Disjoint claim correctness under `SKIP LOCKED` | `PASS` | 100K main run `concurrency.json` |
| 2.2 | Concurrent idempotency | `PASS` | PG integration suite (separate from `2.1`) |
| 2.3 | Single-writer insert throughput | `PARTIAL` | 57.49/s, p50 16.00 ms, p95 25.64 ms (synthetic 96-d) |
| 2.4 | 4/8-writer throughput and latency | `PARTIAL` | 177.31/s at 4 writers, 0 failures; branch A 1/2/4/8 PASS, 0/64 failed; recovery run **unfinished** |
| 2.5 | **Mixed** read/ingest/delete workload (70/20/10) | `NOT TESTED` | never executed — main run stopped at Checkpoint G |

### 3. Security

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 3.1 | Namespace-scoped access at store level | `PARTIAL` | every store query namespace/status scoped; method audit shows the boundary is application-level |
| 3.2 | Action-scoped authorizer | `PARTIAL` | authorizer receives action; static grants only |
| 3.3 | Strict request/response schemas | `PARTIAL` | strict Pydantic schemas with an established 400 contract; a correctness/contract control, not a security boundary by itself |
| 3.4 | Secret rejection at the write gate | `PARTIAL` | `SENSITIVE_PATTERNS` reject known credential shapes; no broader PII/PHI classifier |
| 3.5 | Penetration / adversarial end-to-end test | `NOT TESTED` | no poisoning matrix beyond the 34-event suite |
| 3.6 | IAM / KMS / network / egress hardening | `NOT IMPLEMENTED` | providers injected; no allow-list, no contracts |

### 4. RLS

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 4.1 | RLS migration exists | `NOT IMPLEMENTED` | repository-wide search: **0** matches for `ROW LEVEL SECURITY` / `ENABLE ROW LEVEL`; `migrations/001`–`007`, none RLS |
| 4.2 | RLS integration matrix (user/worker/admin × read/write/search/delete) | `NOT TESTED` | experiment defined in `docs/18` §3; not started |
| 4.3 | Missing-identity and wrong-identity cases | `NOT TESTED` | same experiment |
| 4.4 | Compensating control documented if RLS is declined | `NOT IMPLEMENTED` | no decision record declining RLS |

### 5. Backup / restore

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 5.1 | Backup tooling defined in this repository | `NOT IMPLEMENTED` | no `pg_dump` / `pg_basebackup` reference outside prose |
| 5.2 | Retention and expiry policy | `NOT DEFINED` | not fixed anywhere |
| 5.3 | Legal-hold precedence | `UNKNOWN` | requires external policy input |
| 5.4 | Restore drill performed once, with erasure behaviour measured across it | `UNKNOWN` | never performed; `docs/deletion-semantics.md` scopes erasure to the live database for this reason |

### 6. Migration

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 6.1 | Forward-only | `PASS` | `dive_memory.postgres_migrations` |
| 6.2 | Checksum-verified; editing an applied migration errors | `PASS` | same module; `ops/postgres/README.md` migration policy |
| 6.3 | Idempotent re-run | `PASS` | verified against a real database |
| 6.4 | Version inventory is coherent | `PASS` | `001_initial` … `007_memory_resolution_keys`, no gaps |

### 7. Observability

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 7.1 | Dependency-aware health check | `PARTIAL` | `GET /healthz` exists but returns a **static** `{"status":"ok","storage":"sqlite"}` — it reports neither database connectivity nor dependency state |
| 7.2 | Structured runtime logging | `NOT IMPLEMENTED` | none |
| 7.3 | Tracing / dashboards | `NOT IMPLEMENTED` | none |

### 8. Capacity

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 8.1 | 10K exact latency measured | `PASS` | p50/p95/p99 = 167.18 / 176.78 / 179.36 ms, 20-query controlled run |
| 8.2 | 100K exact latency measured | `PARTIAL` | p50 156.12 / p95 165.93 ms; **not comparable** to 8.1 (bulk-loaded without full projections, different sample count) |
| 8.3 | 100K with a semantic-dimension corpus | `NOT TESTED` | vectors are synthetic 96-d; 768/1024/1536-d figures are labelled **calculated estimates** |
| 8.4 | 1M scale | `NOT TESTED` | correctly gated behind a stable 100K run (plan P4-P2-1) |
| 8.5 | Headroom / saturation model | `NOT IMPLEMENTED` | no queue-depth, connection-pool, lock-wait, WAL-growth or disk-exhaustion analysis exists; the first saturation (`/dev/shm`, 64 MiB) was found by failure, not by modelling |

### 9. Deployment / rollback

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 9.1 | Build artifact definition | `NOT IMPLEMENTED` | no `Dockerfile` |
| 9.2 | Orchestration / configuration deployment | `NOT IMPLEMENTED` | no compose, no manifests, no IaC |
| 9.3 | Rollback procedure | `NOT IMPLEMENTED` | none |
| 9.4 | Production configuration gate | `NOT IMPLEMENTED` | `RuntimeConfig(mode="production")` asserts authorizer + semantic embeddings and refuses raw sensitive traces — a fail-fast **configuration check**, not a deployment control |

### 10. Disaster recovery

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 10.1 | DR plan | `NOT IMPLEMENTED` | none |
| 10.2 | RPO / RTO targets | `NOT DEFINED` | none |
| 10.3 | Failover / replication topology | `UNKNOWN` | nothing defined; downstream of a verified restore (5.4) |

### 11. Erasure

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 11.1 | Soft delete removes FTS/vector/entity/relation state | `PASS` | deterministic tests; 16.28 ms on isolated fixture |
| 11.2 | Hard purge removes row, vectors, sources, versions, transitions, access, projections, retaining a content-free tombstone | `PASS` | PG integration residue assertions; 19.37 ms on isolated fixture |
| 11.3 | Event deletion (soft and hard) with relation provenance reassignment | `PARTIAL` | SQLite path verified; PG parity for event deletion remains follow-up work |
| 11.4 | Erasure across backups, exports, snapshots, telemetry, provider logs | `NOT TESTED` | explicitly outside the verified boundary (`docs/deletion-semantics.md`) |
| 11.5 | Deletion SLA definition | `NOT TESTED` | must enumerate the systems in 11.4 plus retention and legal-hold precedence |

### 12. Secrets

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 12.1 | Known credential shapes rejected at write | `PARTIAL` | hard patterns; no classifier for broader PII/PHI |
| 12.2 | KMS / vault integration | `NOT IMPLEMENTED` | none |
| 12.3 | Rotation policy | `NOT DEFINED` | none |
| 12.4 | Plaintext credential containment mechanised | `UNKNOWN` | `ops/postgres/README.md` holds a local test password with an instruction not to reuse it; nothing enforces that |

### 13. Logs

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 13.1 | Query text minimised in traces | `PARTIAL` | traces store query SHA-256, counts and ids; production mode refuses raw sensitive payloads. Deliberate data minimisation, but the policy is not written down as a retention rule |
| 13.2 | Access-audit export | `NOT TESTED` | listed as an open gap alongside idempotency in the threat model |
| 13.3 | Retention policy | `NOT DEFINED` | none |
| 13.4 | Cardinality bound / log shipping | `NOT IMPLEMENTED` | no aggregation, shipping or cardinality bound |

### 14. Metrics

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 14.1 | Runtime metric emission | `NOT IMPLEMENTED` | none; benchmark artifacts are published measurements, not instrumentation |
| 14.2 | Metrics endpoint | `NOT IMPLEMENTED` | none |

### 15. Alerts

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 15.1 | Alert thresholds and routing | `NOT IMPLEMENTED` | none |
| 15.2 | On-call / escalation definition | `NOT IMPLEMENTED` | none |

### 16. Cost

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 16.1 | Parameterised cost model (1K/10K/100K daily users) | `NOT IMPLEMENTED` | plan P4-P2-2 `NOT STARTED` |
| 16.2 | Workload assumptions and sensitivity ranges | `NOT DEFINED` | none |
| 16.3 | Unit economics measured | `NOT TESTED` | `dependencies = []`; provider-injected models mean there is no composition to price yet |

### 17. SLO

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 17.1 | SLIs defined | `NOT DEFINED` | none |
| 17.2 | SLO targets and error budget | `NOT DEFINED` | none |
| 17.3 | Availability / latency objectives | `NOT DEFINED` | none |
| 17.4 | Distinction between benchmark thresholds and SLOs recorded | `NOT DEFINED` | the 500 ms p95 target is a **provisional benchmark acceptance criterion**, not an SLO; this distinction is now stated in `docs/22` §2.17 |

### 18. Runbooks

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 18.1 | Local database bring-up, migration policy, known limits | `PASS` | `ops/postgres/README.md`; existence regression-protected by `tests/test_phase3_artifacts.py` |
| 18.2 | Host-runtime failure procedure | `PARTIAL` | documented for the Docker Desktop `engine.sock` failure, including what **not** to reset |
| 18.3 | Incident response / failover | `NOT IMPLEMENTED` | none |
| 18.4 | Restore-from-backup and rollback runbooks | `NOT IMPLEMENTED` | named as required by `ops/postgres/README.md` itself; neither exists |

---

## 3. Conditional checklist — what flips a verdict

For each domain verdict, the shortest path to an upgrade. This is the part that
makes the checklist actionable rather than descriptive.

| Domain | After the next step, the verdict becomes | The step |
|---|---|---|
| 1 Data integrity | `PASS`-for-scope | run the 100K `integrity_audit`; record dataset hashes in the false-memory artifacts |
| 2 Concurrency | `PASS`-for-scope | finish the recovery run: `smoke`, `branch_b` 2/4/8, `mixed_workload`, `integrity_audit`, `summary` (needs Docker) |
| 3 Security | `PARTIAL` → higher | adversarial end-to-end poisoning matrix; PII/PHI classifier policy |
| 4 RLS | `NOT IMPLEMENTED` → `PARTIAL` | RLS migration + missing/wrong-identity matrix |
| 5 Backup / restore | `UNKNOWN` → `PARTIAL` | fix retention + legal-hold policy, then one restore drill |
| 6 Migration | already `PASS` | no action; keep it that way |
| 7 Observability | `NOT IMPLEMENTED` → `PARTIAL` | real health check reporting dependency state + structured logging |
| 8 Capacity | `PARTIAL` → higher | semantic-dimension corpus at 100K, then 1M behind the P4-P2-1 gate |
| 9 Deployment / rollback | `NOT IMPLEMENTED` → `PARTIAL` | minimal image + documented rollback path |
| 10 Disaster recovery | blocking | RPO/RTO decision, then a failover or restore rehearsal |
| 11 Erasure | `PARTIAL` → higher | enumerate and manage the out-of-database systems; define the deletion SLA |
| 12 Secrets | `PARTIAL` → higher | secret-manager integration + rotation policy |
| 13 Logs | `PARTIAL` → higher | retention and cardinality policy; audit export |
| 14 Metrics | `NOT IMPLEMENTED` → `PARTIAL` | one metrics endpoint |
| 15 Alerts | needs 14 first | thresholds wired to the metrics from 14 |
| 16 Cost | `NOT IMPLEMENTED` → `PARTIAL` | the parameterised model with explicit assumptions |
| 17 SLO | needs 14 first | measure SLIs, then set targets from the measurements |
| 18 Runbooks | `PARTIAL` → higher | incident, restore and rollback runbooks |

**Dependency order that is not negotiable:** 14 → 15 and 14 → 17. Alerts and SLOs
cannot precede instrumentation, because there would be no signal to threshold or
target.

---

## 4. Sign-off position

| Question | Answer |
|---|---|
| Is the system production-ready? | **No.** 12 of 70 checklist items earn `PASS`. |
| Did Phase 4 break anything that was working? | **No.** Unit suite **348 passed / 9 skipped / 0 errors**; acceptance smoke recall 1.0, abstention 1.0, false-memory rate 0.0, provenance coverage 1.0. |
| Is Phase 4 complete as an evaluation milestone? | **Yes** — once this checklist, `docs/20`, `docs/21` and `docs/22` are published, which is the closure condition in plan §6. |
| Does Phase 5 start automatically? | **No.** Plan §6: Phase 4 stops; Phase 5 does not start automatically. |
| What is the single highest-exposure gap? | **Erasure beyond the live database** — it is a legal-exposure domain, and it is blocked by a policy decision rather than by engineering effort. |
| What is the cheapest way to unblock the most domains? | **One metrics endpoint plus a dependency-aware health check** — it unblocks metrics, alerts and SLO simultaneously. |

---

## 5. Evidence index for this checklist

| Item group | Where to re-verify |
|---|---|
| 1, 6 | `ops/postgres/README.md`; PG integration suite; `migrations/001`–`007` |
| 2, 8 | `eval/reports/postgres-100k/postgres-100k-20260921-084800/`; `.../postgres-100k-concurrency-20260921-110255/`; `docs/benchmark/postgres-exact-100k-report.md` |
| 3, 4, 5, 11, 12, 13 | `docs/security-threat-model.md`; `docs/deletion-semantics.md`; `src/dive_memory/api.py`, `auth.py`, `sensitivity.py`, `gate.py`, `config.py`; repository searches recorded in `docs/22` §1 |
| 7, 9, 10, 14, 15, 16, 17, 18 | negative results and `ops/postgres/README.md`; the absence is established by the searches in `docs/22` §1 |
| Test and smoke baselines | `pytest -q --basetemp .pt-tmp -p no:cacheprovider`; `python -m dive_memory.smoke` |

Supplementary: `docs/20-phase-4-results.md` §2.2 (artifact-contract compliance),
`docs/18-phase-4-evidence-gap-analysis.md` §3 and §5 (claim-status matrix),
`docs/19-phase-4-experiment-plan.md` §P4-P2-3 (the scope this checklist covers).
