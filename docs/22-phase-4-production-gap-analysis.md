# Phase 4 Production Gap Analysis

Analysis date: 2026-09-22  
Analysis baseline: commit `3b1907afda4c3fdca84870312f32cef16536d66a`, working tree dirty with
the Phase 4 closure changes  
Companion documents: `docs/21-phase-4-architecture-review.md` (architecture),
`docs/23-phase-4-readiness-checklist.md` (the itemised verdict table)

> ## Read this first
>
> **This system is not production-ready, and Phase 4 completion does not make it
> so.** Plan §P4-P2-3 states that explicitly, and this analysis confirms it.
>
> Nothing in this document may be read as readiness. The repository must not be
> described as production-ready, and no external claim of certification,
> compliance, SLO attainment or operational fitness is supported by the evidence
> in it.
>
> **Verdict rule applied throughout:** only **direct evidence** receives `PASS`.
> Everything else is `PARTIAL`, `NOT TESTED`, `NOT IMPLEMENTED` or
> `NOT APPLICABLE`. "The code exists" is never a `PASS` — this project's own
> vocabulary (`docs/18` §1) is that `Implemented` is not a synonym for `VERIFIED`.

---

## 0. Verdict summary

Eighteen domains, in the order plan §P4-P2-3 lists them.

| # | Domain | Verdict | One-line basis |
|---:|---|---|---|
| 1 | Data integrity | `PARTIAL` | Real PG integration assertions and rollback tests pass (E3) for the measured path; a full 100K integrity audit was **never run** |
| 2 | Concurrency | `PARTIAL` | Branch A passes at 1/2/4/8 workers with 0 failed ops; the recovery run is unfinished and the mixed workload never ran |
| 3 | Security | `PARTIAL` | Namespace-scoped stores, authorizer, strict schemas, production fail-fast config; no network/host/IAM hardening |
| 4 | RLS | **`NOT IMPLEMENTED`** | Zero occurrences of `ROW LEVEL SECURITY` anywhere in the repository |
| 5 | Backup / restore | **`UNKNOWN`** | No `pg_dump` / `pg_basebackup` / `wal_level` / PITR reference outside prose; retention policy unfixed |
| 6 | Migration | `PASS` | Forward-only, checksum-verified, idempotent; 7 migrations; proven by real PG integration |
| 7 | Observability | **`NOT IMPLEMENTED`** | No metrics, no tracing backend, no dashboards |
| 8 | Capacity | `PARTIAL` | 10K is E4-measured; 100K is partial; 1M untested; no headroom model |
| 9 | Deployment / rollback | **`NOT IMPLEMENTED`** | No `Dockerfile`, no compose, no IaC, no rollback procedure |
| 10 | Disaster recovery | **`NOT IMPLEMENTED`** | No DR plan, no RPO/RTO, no restore drill |
| 11 | Erasure | `PARTIAL` | Verified inside the live database (E3); **not** verified across backups/exports/telemetry/provider logs |
| 12 | Secrets | `PARTIAL` | Hard secret patterns reject known credentials; no KMS/vault/rotation |
| 13 | Logs | `PARTIAL` | Traces store query SHA-256, counts and ids rather than raw text; no retention or cardinality policy |
| 14 | Metrics | **`NOT IMPLEMENTED`** | Same finding as #7 |
| 15 | Alerts | **`NOT IMPLEMENTED`** | No alerting of any kind |
| 16 | Cost | **`NOT IMPLEMENTED`** | No cost model (plan P4-P2-2 `NOT STARTED`) |
| 17 | SLO | **`NOT DEFINED`** | No SLI/SLO/error budget defined anywhere |
| 18 | Runbooks | `PARTIAL` | `ops/postgres/README.md` documents local bring-up, migration policy and known limits; no incident, failover or data-restore runbook |

**Tally: 1 `PASS`, 8 `PARTIAL`, 9 `NOT IMPLEMENTED` / `NOT DEFINED` / `UNKNOWN`, 0 `NOT APPLICABLE`.**

Of the nine: seven are `NOT IMPLEMENTED` (RLS, observability, deployment/rollback,
DR, metrics, alerts, cost), one `NOT DEFINED` (SLO) and one `UNKNOWN`
(backup/restore).

The single `PASS` is migration discipline — and it passes because it is a narrow,
machine-verified property, not because it is the most important domain.

---

## 1. Method and verdict vocabulary

| Verdict | Meaning in this document |
|---|---|
| `PASS` | Direct evidence exists for the stated scope, recorded and reproducible |
| `PARTIAL` | Evidence covers part of the scope, or a narrower workload/environment than the domain requires |
| `NOT TESTED` | An executable experiment is known and has not been run |
| `NOT IMPLEMENTED` | The capability does not exist in the repository, established by search rather than by assumption |
| `NOT DEFINED` | No target exists to test against |
| `UNKNOWN` | The repository does not contain enough information to define the test without external policy or access |
| `NOT APPLICABLE` | The domain does not apply to the current scope at all |

Several verdicts below rest on **negative searches** — checks that the capability
is *absent*. Those checks are listed with their method so they can be re-run:

| Capability searched | Method | Result |
|---|---|---|
| Row-level security | repository-wide search for `ROW LEVEL SECURITY` / `ENABLE ROW LEVEL` across `.sql`, `.py`, `.md` | 0 matches |
| Containerisation / orchestration / IaC | search for `Dockerfile*`, `docker-compose*`, `*.yaml`, `*.yml`, `*.tf` outside virtualenvs | 0 matches |
| Metrics / tracing / alerting endpoints | search across source and docs | planning text only; the sole runtime endpoint is `GET /healthz` |
| Backup / restore tooling | search for `pg_dump`, `pg_basebackup`, `pg_restore`, `wal_level`, PITR | docs-only mentions; no implementation |

**Measurement environment.** Single machine, single process: Windows 11
`10.0.26100`, AMD Ryzen 7 8745H (8c/16t), 15.31 GB RAM, RTX 4060 Laptop 8 GiB,
PostgreSQL + pgvector in a local Docker container. **No measurement in this
document describes a production deployment, and none should be transferred to one
without re-measurement.**

---

## 2. Domain-by-domain analysis

### 1. Data integrity — `PARTIAL`

**What exists.** The PostgreSQL integration suite (8/8 real tests) verifies
migration idempotency, worker claims and recovery, projection rollback,
concurrent idempotency, namespace isolation, temporal transitions, exact pgvector
search and hard purge. Soft delete (16.28 ms) and hard purge (19.37 ms) were
verified on an isolated fixture with row/vector/projection cleanup, tombstones and
rollback all passing.

**What is missing.** The 100K recovery run planned an `integrity_audit` step; it
is still `PENDING`, so **no full-corpus integrity audit has ever run**. The audit
step in the recovery manifest exists as a name, not as a result.

**Why not `PASS`.** Deterministic invariant tests on the measured path are not a
statement about a 100K corpus or a production data set.

### 2. Concurrency — `PARTIAL`

| Workers | Ops | Failed | Throughput | Retrieval p50 | p95 |
|---:|---:|---:|---:|---:|---:|
| 1 | 8 | 0 | 18.1 ops/s | 49.6 ms | — |
| 2 | 16 | 0 | 34.6 ops/s | 51.4 ms | — |
| 4 | 32 | 0 | 48.6 ops/s | 71.7 ms | — |
| 8 | 64 | **0** | 62.1 ops/s | 87.1 ms | 239 ms |

`read_consistent = true` and zero unexpected row-count delta at every level.

**Two findings that must not be lost.**

1. **The original failure was configuration-induced.** In the main run, 8 workers
   produced 44/64 failures through `/dev/shm` exhaustion. In the recovery run the
   same worker level passes with zero failures. The difference is
   `max_parallel_workers_per_gather`. **This is not a database boundary and must
   not be cited as one.**
2. **Scaling is sublinear and the mixed workload never ran.** 8× workers produce
   3.43× throughput (18.1 → 62.1 ops/s). The planned 70/20/10
   read/ingest/delete workload — the only test of *mixed* load — was never
   executed, because the main run stopped at Checkpoint G.

**Blocking condition:** Docker must be running; the recovery run needs
`smoke`, `branch_b` (2/4/8), `mixed_workload`, `integrity_audit` and `summary`
before any concurrency claim is complete. No final summary artifact exists today.

### 3. Security — `PARTIAL`

**Controls that exist and are tested.** Every store query is namespace/status
scoped; the API authorizer receives the action, so grants can be action-scoped;
idempotency keys are `(namespace, key)`; deleted/stale data is excluded by a hard
status filter with FTS/vector/entity/relation cleanup tests; hard secret patterns
reject known credentials; trace records store query SHA-256 rather than raw query
text; providers are injected, with no default paid API.

**The production fail-fast check.** `RuntimeConfig(mode="production")` requires an
authorizer and a semantic embedding provider and refuses raw sensitive trace
payloads. This is a **configuration assertion, not hardening** — it proves the
process was configured, not that the deployment is safe.

**What is missing.** Network policy, host hardening, IAM/KMS integration, egress
allow-lists, provider contracts, regional policy, DLP review for broader PII/PHI,
and rate limiting.

### 4. RLS — `NOT IMPLEMENTED`

**Established by search, not assumed:** zero occurrences of `ROW LEVEL SECURITY`
or `ENABLE ROW LEVEL` in any `.sql`, `.py` or `.md` file in the repository. The
migration set is `001`–`007`; none is an RLS migration. `migrations/003` is
`tombstone_namespace`.

Isolation currently rests **entirely on application-level predicates**. The
threat-model entry is exact about the consequence: *"A missing WHERE clause leaks
or deletes another tenant."* There is no second line of defence at the database.

`docs/18` §3 records the required experiment: RLS migration plus a direct-SQL
integration matrix covering user/worker/admin read, write, search and delete,
including **missing and wrong identity**. Not started.

### 5. Backup / restore — `UNKNOWN`

No `pg_dump`, `pg_basebackup`, `pg_restore`, `wal_level` or PITR reference exists
outside narrative documentation. No retention, expiry, restore-time replay or
legal-hold policy is fixed. No restore drill has been performed.

**This is `UNKNOWN` rather than `NOT IMPLEMENTED` deliberately:** the absence of
tooling in the repository does not establish that the operating environment has no
backups — it establishes that this repository defines and verifies **no** backup
behaviour. Either way, no erasure claim can extend past the live database (#11).

### 6. Migration — `PASS` (the only `PASS`)

Direct, machine-verified evidence, and the domain is narrow enough that the
evidence covers it:

- Migrations are **forward-only** and **checksum-verified** by
  `dive_memory.postgres_migrations`;
- re-running them is **idempotent**;
- editing an already-applied migration raises a checksum error, so silent
  divergence is prevented;
- the integration suite creates isolated schemas where a clean migration history
  is required, and verifies migration idempotency against a real database;
- seven migrations exist (`001_initial` … `007_memory_resolution_keys`).

**Scope of this `PASS`.** It covers the migration *mechanism* on the measured
local database. It does **not** cover a production rollout, which still needs a
backup/restore rehearsal and a version-specific rollback plan — both stated in
`ops/postgres/README.md` and both absent.

### 7. Observability — `NOT IMPLEMENTED`

The only runtime endpoint is `GET /healthz`, returning a static
`{"status": "ok", "storage": "sqlite"}`. It reports **neither database
connectivity nor dependency health**, and it is hard-coded to `sqlite` even when
the PostgreSQL adapter is in use.

There is no metrics endpoint, no tracing backend, no structured application log
pipeline, no dashboard. The word "observability" appears in planning documents and
nowhere in an executable path.

### 8. Capacity — `PARTIAL`

| Scale | Status | Evidence |
|---|---|---|
| 10K exact | **E4 measured** | p50/p95/p99 = 167.18 / 176.78 / 179.36 ms; 38.62 ingest/s; 20-query controlled run, synthetic 96-d |
| 100K exact | `PARTIAL` | p50 156.12 / p95 165.93 ms over 100 warm queries; checkpoints A–F PASS, **G FAILED**, H not run |
| 100K storage | `PARTIAL` | 167,550,976 B for 96-d; 768/1024/1536-d values in `database-size.json` are labelled **calculated estimates** |
| 1M | `NOT TESTED` | correctly gated behind a stable 100K run (plan P4-P2-1) |

**No headroom model exists.** There is no queue-depth, connection-pool, lock-wait,
WAL-growth or disk-exhaustion analysis. The one resource that actually saturated —
the container's 64 MiB `/dev/shm` — was discovered by failure, not by modelling.

The 100K and 10K figures are **not comparable** (the 100K corpus was bulk-loaded
without the full event/provenance projections, and the sample count changed), so
no linear-scaling claim is supportable in either direction.

### 9. Deployment / rollback — `NOT IMPLEMENTED`

No `Dockerfile`, no `docker-compose.yml`, no Kubernetes manifest, no Terraform.
Nothing in the repository defines how this service is built, released, versioned
as an artifact or rolled back.

What exists is a **local bring-up recipe** for the test database
(`ops/postgres/README.md`) and a fail-fast `RuntimeConfig` production check. The
former is a developer convenience; the latter is a configuration assertion. Neither
is a deployment story. There is no rollback procedure of any kind.

**Note the configuration model itself:** `pyproject.toml` declares
`dependencies = []` with opt-in extras (`api`, `dev`, `postgres`), so a deployed
artifact composition is not yet defined.

### 10. Disaster recovery — `NOT IMPLEMENTED`

No DR plan, no RPO, no RTO, no failover target, no replication topology, no
restore drill. This is downstream of #5 (no verified backup): DR cannot be
assessed before a restore has been demonstrated once.

### 11. Erasure — `PARTIAL`

**Verified inside the live database (E3).** Hard memory purge checks the memory
row, vector state, source rows, versions, keys, access rows, entity/relation
links, transition references and source-event relation residue, retaining only a
content-free tombstone. Soft delete removes FTS, active vector state/staging,
entity links and relation links immediately while keeping the source event and row
for audit. Delete costs were 16.28 ms (soft) and 19.37 ms (hard) on an isolated
fixture.

**Not verified outside it.** Exports, filesystem snapshots, database backups,
telemetry sinks, model-provider logs and user-held copies are **explicitly not
managed** by these methods. `docs/deletion-semantics.md` states the correct scope:
*"an active-database guarantee rather than an end-to-end regulatory erasure
claim."*

**Why this matters more than its verdict suggests.** Erasure is a legal-exposure
domain, not a feature domain. A production deletion SLA must enumerate every
system above, plus backup retention and legal-hold precedence — none of which is
defined.

### 12. Secrets — `PARTIAL`

**Exists.** Hard secret patterns reject known credentials at the write gate
(`SENSITIVE_PATTERNS` in `gate.py`, surfaced via `sensitivity.py`), so
`"remember my API key is sk-…"` is refused as `SENSITIVE_DATA`. `.env` is
git-ignored.

**Missing.** No KMS or vault integration, no rotation policy, no field-level
encryption, no secret-scanning in CI. `ops/postgres/README.md` carries a
plaintext password for the **local test container**, and the document itself
correctly instructs that it must not be reused for a shared or production
database — but nothing mechanises that instruction.

### 13. Logs — `PARTIAL`

**Exists.** Retrieval traces record query **SHA-256**, counts and ids rather than
raw query text, and production mode refuses raw sensitive trace payloads — a
deliberate data-minimisation choice.

**Missing.** No retention policy, no cardinality bound, no log shipping or
aggregation, no access-audit export. The threat model lists "log
retention/cardinality policy" as an open gap and "audit export" as an open gap
alongside idempotency.

### 14. Metrics — `NOT IMPLEMENTED`

Same finding as #7. No metrics are emitted. The benchmark artifacts contain rich
*published* measurements (latency percentiles, throughput, storage), but
measurement-in-a-report is not instrumentation-in-a-service: nothing emits a
metric at runtime.

### 15. Alerts — `NOT IMPLEMENTED`

No alerting of any kind — no thresholds, no routing, no on-call definition, no
synthetic checks. This is downstream of #7 and #14: there is nothing to alert on.

### 16. Cost — `NOT IMPLEMENTED`

Plan P4-P2-2 (`NOT STARTED`). No model of 1K/10K/100K daily users across
embedding calls, LLM tokens, reranking, consolidation, GB-month storage and
compute; no workload assumptions or sensitivity ranges. With
`dependencies = []` and provider-injected models, there is currently no
infrastructure composition to price.

### 17. SLO — `NOT DEFINED`

No SLI, SLO, error budget, availability target or latency objective is defined
anywhere in the repository. The only numeric targets that exist are **provisional
benchmark thresholds** — e.g. the 500 ms p95 target used to judge 100K retrieval —
and those are experiment acceptance criteria, **not** service objectives. Nothing
is instrumented to measure attainment of even those.

A production SLO cannot be written before #7/#14 exist, because there would be no
signal to measure.

### 18. Runbooks — `PARTIAL`

**Exists.** `ops/postgres/README.md` is a real operational document: it covers
local container bring-up and teardown, install and test invocation, the migration
policy (forward-only, checksum-verified, idempotent, add-don't-edit), a known-limits
section, and a specific host-runtime failure mode with a corrective procedure that
explicitly forbids resetting project files or the schema as a workaround.
`tests/test_phase3_artifacts.py` asserts the decision record and runbook are
present, so its existence is regression-protected.

**Missing.** Incident response, failover, restore-from-backup, key rotation,
capacity response, schema-divergence recovery and escalation paths. The document's
own "Migration policy" section states that a production rollout "still needs
backup/restore rehearsal and a version-specific rollback plan" — naming the two
runbooks that do not exist.

---

## 3. The gaps that matter most

Ranked by exposure per unit of effort, not by count.

| Rank | Gap | Why it ranks here | Cheapest first step |
|---:|---|---|---|
| 1 | **Erasure stops at the live database (#11, #5)** | Regulatory exposure. Deleted personal data may survive in backups and exports, and no policy says whether it must not | Fix backup retention and legal-hold precedence as **policy first**; then one restore drill |
| 2 | **No database-level isolation (#4)** | A single missing predicate leaks or deletes another tenant's data, with nothing behind it | RLS migration + missing/wrong-identity matrix |
| 3 | **No runtime instrumentation (#7, #14, #15, #17)** | Four domains are blocked by one absence. SLOs cannot even be written | One metrics endpoint + one real health check that reports dependency state |
| 4 | **No deployment or rollback definition (#9, #10)** | The service cannot be released or reverted reproducibly, so no operational claim is meaningful | A minimal image + a documented rollback path |
| 5 | **No cost model (#16)** | Capacity decisions cannot be priced, including the 1M decision | Parameterised model with explicit workload assumptions |
| 6 | **Concurrency evidence incomplete (#2)** | Mixed load is the realistic load, and it has never run | Finish the recovery run once Docker is up |

**Note the ordering logic.** #3 outranks #4 because it is the cheapest way to
unblock the most domains, and it is a prerequisite for #17 — not because
observability matters more than rollback in production.

---

## 4. What must be true before any production claim

Stated as conditions, so that the list can be checked rather than argued about.

**Blocking (no production claim is defensible while any of these is unmet):**

1. RLS implemented **and** verified against missing/wrong identity, or the
   database-threshold argument for not having it recorded explicitly.
2. Backup retention and legal-hold precedence **decided**, and one restore drill
   performed, with erasure behaviour measured across the restore.
3. A metrics endpoint and a dependency-aware health check, so that SLIs can be
   measured at all.
4. SLOs defined **after** #3, with the measurement behind them.
5. A reproducible build artifact **and** a documented rollback path.
6. Alerting wired to the SLOs from #3/#4.
7. The 100K mixed workload completed, plus the integrity audit.
8. A cost model with explicit assumptions, or an explicit decision not to have
   one at the target scale.

**Strongly indicated but not strictly blocking:**

9. ANN comparison before any latency claim at scale (plan P4-P1-1).
10. Growth-pollution curve before any "does not degrade" claim.
11. Anonymous-identity / multi-tenant threat review beyond the app-predicate layer.

**Explicitly out of scope for this document:** 1M scale (P4-P2-1), the
vector-database reconsideration gate (P4-P2-4, currently "do not introduce another
datastore"), and any certification or compliance statement.

---

## 5. Statements that must not be made

Recorded because each has been tempting at some point in this project's history.

| Statement | Why it is unsupported |
|---|---|
| "The system is production-ready" | Plan §P4-P2-3 forbids it; 8 of 18 domains are `NOT IMPLEMENTED`/`NOT DEFINED` |
| "PostgreSQL does not scale past 8 concurrent workers" | The failure was configuration-induced and does not reproduce |
| "100K is 0.93× of 10K, so latency is flat" | The corpora and load protocols are not comparable; no scaling claim is supportable in either direction |
| "The false-memory rate is 0" | 0/34 observed; the 95% Wilson upper bound is **10.15%** |
| "RLS protects tenant isolation" | Zero RLS code exists; isolation is application predicates only |
| "Deletion is complete" | Verified inside the live database only; backups, exports, telemetry and provider logs are unmanaged |
| "The write gate is validated" | In-sample accuracy is 1.0, but the held-out set is 0.8276 [0.6545, 0.9240] — calibrated-but-provisional |
| "Hybrid retrieval beats naive vector memory" | No naive-vector baseline has ever been run under the same queries |
| "The system meets its 500 ms p95 target" | That is a provisional **benchmark** threshold, not an SLO; nothing measures it at runtime |

---

## 6. Conclusion

Phase 4 produced real infrastructure evidence — 100K exact retrieval inside a
provisional target, real concurrent load, real migration discipline, real
measurement of the write gate and of the full official corpus. **None of it is
production evidence.**

The honest position is the one plan §P4-P2-3 requires: **Phase 4 is complete as a
research and evaluation milestone, and the system remains non-production.** The
gaps are not concentrated in the code — the code has no `PASS` for deployment, DR,
metrics, alerts, cost or RLS because those capabilities are absent, and the two
highest-exposure gaps (erasure beyond the database, and database-level isolation)
are **policy and design decisions before they are implementation tasks**.

Item-level verdicts with evidence pointers:
**`docs/23-phase-4-readiness-checklist.md`**.
