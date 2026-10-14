# Deployment and Operations

Date: 2026-10-13. This document defines how the service is **built, released,
observed and rolled back**, and records — with the same verdict vocabulary as
`docs/22` — which production gaps this closes and which remain open. It does
not declare the system production-ready.

## 1. Build and release

| Artifact | Definition | Verification |
|---|---|---|
| Container image | `ops/docker/Dockerfile`, built from the repository root | CI `docker` job: build, run, probe `/api/readiness` + `/metrics` |
| Compose deployment | `ops/docker/compose.yml` | same image; host loopback binding only |
| Wheel | `python -m build --wheel` | `tools/verify_wheel.py` in the CI runtime job |

The image runs as a non-root system user (`dive`, uid 900), installs only
`requirements/runtime.lock` plus the package itself, and stores state under
`/data` (declared volume, owned by the service user). No secrets are baked
into the image; live-mode reader credentials are passed through the host
environment at start time.

Release = a pinned image tag. Version tags follow the package version
(currently `0.4.0`).

## 2. Runtime interface

| Endpoint | Meaning | Failure behaviour |
|---|---|---|
| `GET /healthz` | Liveness: the process serves HTTP | static `{"status":"alive"}` |
| `GET /api/readiness` | **Real dependency probe**: storage backend, reader composition, embedding provider | HTTP 503 when storage is unavailable |
| `GET /metrics` | Prometheus text exposition | scrapes even when storage is down |

`/metrics` exports:

- `dive_http_requests_total{path,status}` — labels use the **route template**
  (bounded cardinality; unmatched requests collapse to `path="unmatched"`);
- `dive_http_request_duration_seconds_sum/_count{path}` — cumulative handling time;
- `dive_readiness{component=overall|database}` — from the same probe as `/api/readiness`;
- `dive_process_uptime_seconds`, `dive_build_info{version,mode}`.

The counters are process-local: they reset on restart. For a single-process
local deployment this is adequate; a multi-replica deployment would need a
shared aggregation layer, which this repository does not define.

## 3. Deployment procedure

```bash
docker build -f ops/docker/Dockerfile -t dive-memory:0.4.0 .
docker compose -f ops/docker/compose.yml up -d
curl --fail http://127.0.0.1:8765/api/readiness
```

Compose binds the published port to **127.0.0.1 on the host**. Exposing the
service beyond the loopback is a deliberate, separate act (reverse proxy with
TLS, or an explicit port-map edit) that this repository does not automate.

## 4. Rollback

Migrations are **forward-only** (`ops/postgres/README.md`); there is no
down-migration. Rollback therefore has exactly one path:

1. Redeploy the previous image tag (`docker compose` `image:` pinned).
2. If a migration ran in the meantime and the old image is incompatible with
   the new schema, **restore the database** from the last pre-deploy backup
   (`ops/postgres/restore.sh`) into a fresh database and repoint the DSN.
3. Record both the failed release and the restore in the incident log.

A rollback that has not been rehearsed is a hypothesis. The restore drill in
`ops/postgres/BACKUP_POLICY.md` is the rehearsal; until it is recorded, step 2
is unverified.

## 5. Provisional SLI sketch

Not SLOs — no attainment is measured anywhere yet. These are the signals the
metrics endpoint makes measurable, so a target can be defined later:

- Availability: `dive_readiness{component="overall"}` over time (probe-based).
- Traffic/errors: rate of `dive_http_requests_total` by `status` class.
- Latency: increase of `dive_http_request_duration_seconds_sum` / `_count` per path.

## 6. Database-level isolation (RLS) — design, NOT IMPLEMENTED

`docs/22` domain #4 is unchanged by this round: **no RLS exists**. The design
recorded here so implementation is a decision, not an invention:

- Fail-closed policy on every namespace-scoped table:
  `USING (namespace = current_setting('dive.namespace', true)) WITH CHECK (same)`
  — a session that never sets the variable sees **no rows at all**.
- The application binds the namespace per operation via
  `SELECT set_config('dive.namespace', $1, false)` on the store connection.
- The outbox worker path is cross-namespace by design; it must run under a
  **separate session kind** (`dive.session_kind = 'worker'`) whose policies
  cover only the claim/decision tables. The existence of a worker kind is a
  bypass-by-configuration risk and must be covered by the identity matrix.
- The service connects as a **non-owner role** (table owners bypass RLS
  unless `FORCE ROW LEVEL SECURITY` is set; forcing it would also bind every
  future migration).

Required verification before the verdict can change (docs/18 §3): an
integration matrix over {app role, worker role, unset identity, wrong
identity} × {read, write, search, delete}. **None of this exists yet**; the
verdict stays `NOT IMPLEMENTED`.

## 7. Domain scoreboard against docs/22

| docs/22 domain | Before | After this round | Basis |
|---|---|---|---|
| 7 Observability | NOT IMPLEMENTED | `PARTIAL` | real readiness probe existed; now plus `/metrics` with bounded labels; no tracing/dashboards |
| 9 Deployment/rollback | NOT IMPLEMENTED | `PARTIAL` | image + compose + CI container job + documented rollback; no IaC, no staged rollout |
| 14 Metrics | NOT IMPLEMENTED | `PARTIAL` | process-local Prometheus exposition; no scraping infra, no persistence |
| 5 Backup/restore | UNKNOWN | `PARTIAL` | scripts + policy with retention and legal-hold precedence; **restore drill not yet run** |
| 10 Disaster recovery | NOT IMPLEMENTED | unchanged (downstream of the drill) |
| 4 RLS | NOT IMPLEMENTED | unchanged; design recorded in §6 |
| 15 Alerts, 16 Cost, 17 SLO | NOT IMPLEMENTED / NOT DEFINED | unchanged |

"Honest labels": `PARTIAL` here means *the mechanism exists and is wired to
verification*, per the `docs/22` rule that code-existing is never `PASS`.
The observability and metrics rows would need a measured deployment — not a
local one — before any stronger label.

## 8. What this document must not be used for

It is not a certification, not an SLO attainment record, and not evidence of
production operation. Per `docs/22` §5, none of the forbidden statements become
sayable because of this round.
