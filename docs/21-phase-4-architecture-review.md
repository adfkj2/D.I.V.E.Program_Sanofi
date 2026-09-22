# Phase 4 Architecture Review

Review date: 2026-09-22  
Review baseline: commit `3b1907afda4c3fdca84870312f32cef16536d66a`, working tree dirty with
the Phase 4 closure changes  
Supersedes: `docs/15-phase-3-architecture-review.md` §0 (the three "unsolved core
problems" verdict), not the document — Phase 3's capability and limitation sections
remain the accurate record of the state they were written against.

> **Method rule.** Plan §6 closes Phase 4 when every requested architecture
> question is answered **from executed evidence** or explicitly recorded as a
> remaining unknown **with its blocking condition**. This review therefore cites
> an artifact on disk for every verdict, and it does **not** carry a verdict
> forward from an earlier document without re-checking that artifact. Two claims
> inherited from Phase 3 documents were re-checked and found **understated**;
> they are corrected in §4.
>
> **What this review is not.** It is not a production-readiness statement.
> Plan §P4-P2-3 is explicit that Phase 4 completion does not imply readiness;
> the readiness position is `docs/22` and `docs/23`.

---

## 0. Executive verdict

**The architecture direction is confirmed. The architecture is not finished, and
the largest single gap is no longer in retrieval — it is that the system has no
grounded reader, so no end-to-end answer quality exists to converge against.**

Phase 3 recorded three unsolved core problems: *formation*, *evolution* and
*retrieval*. Phase 4 moved them by very different amounts:

| Phase 3 core problem | Phase 4 movement | Honest verdict |
|---|---|---|
| **Formation** — "decided by a few regexes and a keyword gate" | **Materially fixed.** A semantic gate replaces keyword matching for the write decision; gold evidence-turn formation goes **6.14% → 82.48%** on the full official corpus, measured, 500/500 cases | `PARTIALLY SOLVED` — the decision is now measurable and much better, but its gain is measured **at the gate**, with no reader to show it helps answers |
| **Evolution** — "only single-valued supersede + manual duplicate consolidation" | **Narrowly improved.** The single-valued predicate semantics are now enforced *by provenance* as well (`residence`/`primary_tool`/`goal` cannot be created by an untrusted source); eight relationship classes remain E1–E2 verified | `STILL OPEN` — contradiction/refinement/temporal-update semantics beyond rule fixtures are unimplemented and untested |
| **Retrieval** — "not semantic retrieval by default" | **Measured, not yet proven better.** The full 500-case retrieval stage now runs and reports memory-level ranking, but **no naive-vector baseline was ever run under the same queries**, and the A–H artifact's dense channel is still a non-semantic hash fixture | `MEASURED, NOT COMPARED` — the architecture's advantage remains an untested claim |

**The most consequential new finding is a measurement contract defect, not a
missing feature.** The write gate emits one boolean, `decision.accepted`, but the
false-memory harness scores that same boolean against **two opposite
expectations** (`memory_expected` and `user_fact_allowed`). One labelled event
(`ec-third-party-residence`) is therefore **unsatisfiable by any binary policy**.
This is an architecture finding: the gate's output domain is too small for the
question being asked of it, and it needs a three-valued outcome before that case
can be scored honestly. Phase 4 forwarded the closest candidate fix through a
measured rejection rather than adopting it (§5, Q14).

**Three architecture decisions that must not be re-litigated without new
evidence**, because Phase 4 produced the evidence:

1. **Do not introduce a vector database.** No PostgreSQL threshold was crossed.
2. **Do not relax the negation guard.** Its clause-scoped "fix" was measured and
   would re-admit 7.36% of the corpus.
3. **Do not cap `max_seq_length` or raise `encode_batch_size` to go faster.**
   Both were measured as inert or slower; the device is the only real lever.
4. **Do not treat the Postgres `/dev/shm` failure as a database boundary.** It is
   configuration-induced and does not reproduce after a settings change.

---

## 1. Audit method and reproducible evidence

Checks performed for this review, with the command that produced each result.

| # | Check | Result | Artifact / command |
|---|---|---|---|
| 1 | Every claim row in `docs/18` §3 and §5 re-read against the artifact it cites | 2 rows understated → corrected in §4 | `eval/reports/**` listed before any edit |
| 2 | Full corpus formation + retrieval executed end to end | 500/500 rows, 0 errors, 6,850.1 s | `longmemeval-s-retrieval-gpu.json` |
| 3 | Gate calibration, including a **held-out** probe set | 22 calibration / 29 held-out; held-out accuracy 0.8276, Wilson [0.6545, 0.9240] | `gate-calibration.json`, `ops/calibrate_gate_threshold.py` (probe tuples counted by executing the module: `len(CALIBRATION_PROBES)=22`, `len(HELDOUT_PROBES)=29`) |
| 4 | False-memory suite under all three policies | v2 + overwrite floor: agreement 0.9706, 0 false accepts | `false-memory-v1.json`, `-v2.json`, `-v2-overwrite-floor.json` |
| 5 | RLS presence in the repository | **Zero** occurrences of `ROW LEVEL SECURITY` / `ENABLE ROW LEVEL` in any `.sql`, `.py`, `.md` | repository-wide search |
| 6 | Container / orchestration / IaC definitions | **None** — no `Dockerfile`, no `docker-compose*`, no manifests, no Terraform | repository-wide search |
| 7 | Metrics / SLO / runbook implementation | Planning text only; the sole runtime endpoint is `GET /healthz` returning a static `{"status":"ok","storage":"sqlite"}` | `src/dive_memory/api.py:154` |
| 8 | Backup / restore / PITR implementation | No `pg_dump`/`pg_basebackup`/`wal_level` reference outside narrative documentation | repository-wide search |
| 9 | Unit + integration suite | **348 passed / 9 skipped / 0 errors** | `pytest -q --basetemp .pt-tmp -p no:cacheprovider` |
| 10 | Deterministic acceptance smoke | recall 1.0, abstention_accuracy 1.0, false_memory_rate 0.0, provenance_coverage 1.0 | `python -m dive_memory.smoke` |

Checks 5–8 are deliberately **negative searches**. Their value is that "no
evidence" is established by looking rather than assumed.

**Environment for the measurements cited here**: Windows 11 `10.0.26100`, AMD
Ryzen 7 8745H (8c/16t), 15.31 GB RAM, RTX 4060 Laptop 8 GiB (driver 591.59),
torch `2.14.0+cu126`, gate encoder `BAAI/bge-m3` @ `5617a9f6…`, fp32. Single
machine, single process. Full table: `docs/20-phase-4-results.md` §2.1.

---

## 2. The architecture questions this review answers

Plan §6 refers to "each requested architecture question" but does not enumerate
them, so they are listed here **with their provenance** — none of them is
invented for this review, and a reader can audit the list rather than trust it.

| Q | Architecture question | Provenance of the question |
|---:|---|---|
| Q1 | Is formation now driven by something measurable, rather than keyword matching? | `docs/15` §0 problem 1 |
| Q2 | Have evolution semantics (contradiction, refinement, temporal update) been completed? | `docs/15` §0 problem 2 |
| Q3 | Is retrieval semantic, and does the hybrid architecture beat naive vector memory? | `docs/15` §0 problem 3; `docs/18` §3 row "better than naive vector memory" |
| Q4 | Are the write-gate thresholds calibrated from measurement? | `docs/00` §9; `docs/18` §3 row "write gate decision quality" |
| Q5 | Which embedding model is right for this workload? | `docs/00` §9; ADR-009; plan P4-P0-2 |
| Q6 | Does PostgreSQL + pgvector hold at 100K? | plan P4-P0-3; `docs/18` §3 row "acceptable at 100K" |
| Q7 | Is a dedicated vector database required? | plan P4-P2-4; `docs/00` §9; ADR-003 |
| Q8 | Does ANN (HNSW / IVFFlat) buy latency inside a recall budget? | plan P4-P1-1 |
| Q9 | How does the system behave under concurrent load? | plan P4-P0-3, P4-P1-4; ADR-012 |
| Q10 | Is tenant isolation enforced at the database boundary, and is erasure end-to-end? | plan P4-P1-2, P4-P1-3; `docs/security-threat-model.md` |
| Q11 | Does long-term growth pollute retrieval, and does consolidation help? | plan P4-P1-4, P4-P1-5; `docs/00` §9 |
| Q12 | Is there a parameterized cost model? | plan P4-P2-2 |
| Q13 | What happens at 1M scale? | plan P4-P2-1 |
| Q14 | Is the write gate's output expressive enough for the decisions it is asked to make? | **New in this review** — found by re-reading the scoring contract, §5/Q14 |

---

## 3. Question-by-question verdicts

### Q1 — Is formation measurable rather than keyword-matched? — `PARTIALLY SOLVED`

**Fix adopted.** The write decision is now made by a semantic gate
(`semantic_gate.py`, `policy_version` `semantic-utility-v2`) that scores a
candidate against a **prototype index built from real `bge-m3` vectors** and
decides on a nearest-anchor margin, instead of a keyword list. v1 remains the
default for existing callers; v2 is opt-in.

**Evidence.**

| Measure | v1 keyword gate | v2 semantic gate | Source |
|---|---:|---:|---|
| Gold evidence-turn formation (micro-suite, 896 gold turns) | 55/896 = **6.14%** | 760/896 = **84.82%** | `gate-formation-semantic.json` |
| Gold evidence-turn formation (full corpus) | — | 739/896 = **82.48%** | `formation` block, `longmemeval-s-retrieval-gpu.json` |
| Memories per non-empty turn | 0.2343 | 0.3801 | `formation.memory_per_nonempty_turn`; `gate-formation-baseline.json` |

The full-corpus figure is the one to quote: **739/896 = 82.48%**, over
246,738 non-empty turns producing 93,774 memories.

**Why not `VERIFIED`.** The gain is measured **at the gate**. There is no
grounded reader, so nothing demonstrates that better formation improves an
answer. Phase 4 also cannot show that the gate does not over-form: 135 of the 896
gold evidence turns land in `SEMANTIC_REVIEW_BAND` and are withheld, which is
honest conservatism but is not free (see Q14) and has no answer-level cost
measurement.

**Notable secondary result.** 5 gold evidence turns that v1 accepted are
**rejected** by v2 (`evidence_flips.v1_accept_v2_reject = 5`). The gate is not a
pure superset of v1; those 5 are unreviewed.

### Q2 — Have evolution semantics been completed? — `STILL OPEN`

**What advanced.** The single-valued predicate set
(`relations.SINGLE_VALUED_PREDICATES` = `{residence, primary_tool, goal}`) became
enforceable at the **provenance** layer, not only at the value layer: a
non-trusted source may no longer create a single-valued *user* fact, because
those predicates carry overwrite semantics. Multi-valued predicates
(`preference`, `statement`) still accumulate, and third-party subjects are
excluded because their residence is an entity fact, not a profile overwrite.
This closed the one remaining false accept in the false-memory suite.

**What did not.** There is still no contradiction/refinement/temporal-update
classifier beyond authored rule fixtures, no model-generated ambiguous corpus
with per-class metrics, and no long-horizon evolution simulation. The eight
relationship classes remain `VERIFIED` at E1–E2 (authored cases only) — exactly
the Phase 3 position.

### Q3 — Is retrieval semantic, and is it provably better than naive vector memory? — `MEASURED, NOT COMPARED`

**What is now measured** (500 official cases, memory level, 470 of them carrying
gold evidence):

| Level | Recall-any@1 | Recall-any@5 | Recall-any@10 | MRR | Source |
|---|---:|---:|---:|---:|---|
| Turn | 0.3660 | 0.5681 | **0.6319** | 0.4520 | `retrieval.turn_level` |
| Session | 0.5787 | 0.7128 | **0.7766** | 0.6409 | `retrieval.session_level` |

Retrieval latency p50 20.09 / p95 35.50 / p99 39.40 ms over 500 samples.

**What that does not settle.** These are **absolute** figures. The architecture's
comparative claim — that hybrid retrieval beats naive vector memory — requires a
naive-vector baseline executed under the *same* queries, and none exists. The A–H
artifact's dense channel is still a non-semantic hash fixture, as it was in
Phase 3. The row therefore stays `NOT TESTED`; absolute numbers do not answer a
comparative question.

**Corollary worth stating plainly:** this is the single cheapest high-value
experiment left. It needs no reader, no judge and no external credentials — only
a baseline run over the queries that already exist.

### Q4 — Are the gate thresholds calibrated from measurement? — `YES, WITH AN IMPORTANT QUALIFICATION`

`gate-calibration.json` records two disjoint probe sets and Wilson intervals:

| Set | Probes | Scored | Floor rejects | Accuracy at current threshold | Wilson 95% |
|---|---:|---:|---:|---:|---|
| Calibration (weights tuned here) | 22 | 16 | 6 | **1.0000** | [0.8513, 1.0000] |
| **Held-out** (written afterwards) | 29 | 21 | 8 | **0.8276** | **[0.6545, 0.9240]** |

The held-out set deliberately covers phrasing families the calibration set lacks
(questions the user asks *about themselves*, third-person facts, impersonal
instructions, hedged self-descriptions, second-person advice).

**The qualification is a documentation defect this review found and corrected.**
Both `semantic_gate.SemanticWeights`' docstring and the opening paragraph of
`ops/calibrate_gate_threshold.py` described the calibration as **19** probes, and
`docs/18` §5 asserted there is "**no** held-out labelled formation set beyond the
19 probes". Executing the module gives `len(CALIBRATION_PROBES) = 22` and
`len(HELDOUT_PROBES) = 29`, and the artifact's own `limitations` field already
says 22. So:

- the probe count was **stale at 19** (the true count is 22), and
- the "no held-out set" claim was **wrong in the understating direction** — a
  held-out set exists and is reported with an interval.

Corrected in `src/dive_memory/semantic_gate.py`,
`ops/calibrate_gate_threshold.py` and `docs/18` §5.

**What the held-out result does *not* license.** 0.8276 is accuracy on **29
probes for the accept/reject decision**. It is not a formation-coverage figure
and it is not an end-to-end quality figure. A held-out **formation** set — gold
evidence turns the gate was never calibrated against — still does not exist.
`gate-formation-semantic.json`'s own `limitations` list still says so, and that
statement is correct.

The threshold is documented as **calibrated-but-provisional**, and the held-out
interval [0.6545, 0.9240] is the honest reason why.

### Q5 — Which embedding model is right? — `PARTIALLY VERIFIED`

Five candidates ran in one controlled comparison over 32 queries / 48 documents
across 8 categories (semantic paraphrase, preference retrieval, old episodic
recall, temporal change, conflicting facts, near-duplicate, entity ambiguity,
long-tail), 3 languages.

| Candidate | Semantic | Dim | Throughput (doc/s) | Query throughput (q/s) | Model storage | Peak RSS |
|---|---|---:|---:|---:|---:|---:|
| `deterministic-sha256-v1` | no | 96 | 21,474.6 | — | 0 B | — |
| `bm25-word-cjk-bigram-v1` | no | — | — | — | 0 B | — |
| **`bge-m3`** | yes | 1024 | 24.58 | 32.22 | 2.32 GB | 2.94 GB |
| **`multilingual-e5-large-instruct`** | yes | 1024 | 24.23 | 15.79 | 1.14 GB | 5.44 GB |
| **`gte-multilingual-base`** | yes | 768 | **53.47** | **76.72** | 0.63 GB | 4.99 GB |

Direction of the result: E5 highest retrieval quality, BGE-M3 close behind and
faster on the query path, GTE fastest and smallest. `status = COMPLETED`,
`failures = []`.

**Why not `VERIFIED`.** The preregistered protocol for P4-P0-2 required the
**frozen project corpus** split by user/session with contradiction and
near-duplicate slices. That corpus was not used; this is a project-authored
32-query set. The artifact says so itself: *"Project-owned controlled set: 32
queries and 48 documents; quality estimates have high uncertainty."* Model choice
is therefore **supported but not settled**, and BGE-M3 is retained by
*adequacy*, not by having won a preregistered comparison.

### Q6 — Does PostgreSQL + pgvector hold at 100K? — `PARTIALLY VERIFIED`

| Measure | Value |
|---|---:|
| Checkpoints | **A–F PASS**, **G FAILED**, H `NOT_COMPLETED` |
| Corpus | exactly 100,000 memories, 100,000 × 96-d synthetic vectors |
| Exact retrieval (100 warm service queries) | **p50 156.12 / p95 165.93 / p99 169.06 ms** |
| Provisional target | 500 ms p95 → **met** |
| Filtered (1% / 10% / 50%) | p50 20.01 / 30.87 / 81.51 ms |
| Temporal current (80k eligible) | p50 301.25 ms |
| COPY bulk load | 20,279.41 rows/s |
| Service ingest | 57.49/s (1 writer), 177.31/s (4 writers), idempotency passed |
| Schema size, 96-d | 167,550,976 B (1675.5 B/memory incl. overhead) |
| Hard purge / soft delete (isolated fixture) | 19.37 ms / 16.28 ms |

**Why not `VERIFIED` — and these reasons are load-bearing:**

1. The run **stopped at Checkpoint G**; the planned 70/20/10 mixed workload never
   ran. This is a partial benchmark, not a certified workload.
2. The 100K corpus was **bulk-loaded without the full event/provenance
   projections**, so it is **not comparable** to the 10K service baseline. The
   observed 100K p50 (156.12 ms) is *lower* than the 10K p50 (167.18 ms) — which
   is precisely why no linear-scaling claim may be made, and none is.
3. Vectors are **synthetic 96-d**, not `bge-m3` 1024-d. Memory footprint at the
   real dimension is a labelled estimate, not a measurement.

### Q7 — Is a dedicated vector database required? — `NO, AND THE DECISION IS RECORDED`

Plan P4-P2-4 gates vector-database adoption on a **crossed PostgreSQL
threshold** (latency/recall, filter, index-build, memory, scaling or
operational). No threshold was crossed: 100K exact retrieval met the provisional
target, and the first hard saturation was the **container's 64 MiB `/dev/shm`**,
a host configuration limit rather than a database limit.

The correct statement of the decision is the one the plan mandates: **"do not
introduce another datastore"**, *not* "vector databases were proven inferior".
Qdrant/Milvus/Weaviate/OpenSearch remain untested and that claim is not made.

### Q8 — Does ANN buy latency inside a recall budget? — `NOT TESTED`

No HNSW or IVFFlat index was created in any run. There is no recall@K against
exact ground truth, no filtered-recall figure, no index-build/update/storage or
cold/warm data. The 100K `query-plans.json` evidence covers plans **without** any
ANN index (Bitmap Heap Scan, Bitmap Index Scan, Gather Merge, Hash Join, Index
Scan, Nested Loop, Seq Scan, Sort). ANN selection remains gated behind exact-path
stability, which is itself only partial (Q6).

### Q9 — How does the system behave under concurrent load? — `PARTIALLY VERIFIED`

| Workers | Ops | Failed | Throughput | Retrieval p50 | Retrieval p95 |
|---:|---:|---:|---:|---:|---:|
| 1 | 8 | 0 | 18.1 ops/s | 49.6 ms | — |
| 2 | 16 | 0 | 34.6 ops/s | 51.4 ms | — |
| 4 | 32 | 0 | 48.6 ops/s | 71.7 ms | — |
| 8 | 64 | **0** | **62.1 ops/s** | **87.1 ms** | 239 ms |

`read_consistent = true`, zero unexpected row-count delta, at every level.
Throughput scales 18.1 → 62.1 ops/s (3.43× at 8× workers — sublinear, and the
review notes that rather than rounding it to "scales").

**The earlier failure does not reproduce.** The main run failed 44 of 64
operations at 8 workers with `/dev/shm` exhaustion. The recovery run passes at 8
workers with zero failures. The difference is a settings change
(`max_parallel_workers_per_gather`), so the original failure is
**configuration-induced** and must not be written as a database boundary.

**Why not `VERIFIED`.** The recovery run is **itself unfinished** — `smoke`,
`branch_b` at 2/4/8 workers, the mixed workload, the integrity audit and the
summary are all still `PENDING`, and **no final summary artifact exists**.
Branch B has only worker-1. Completing it requires Docker, which was not running.

### Q10 — Tenant isolation at the database boundary, and end-to-end erasure? — `PARTIAL / NOT IMPLEMENTED / UNKNOWN`

Three different answers to three different sub-questions, and they must not be
merged:

| Sub-question | Verdict | Evidence |
|---|---|---|
| Application-level namespace isolation | `PARTIALLY VERIFIED` (E2–E3) | unit tests plus PG integration checks with explicit predicates on the measured calls |
| **Database-level RLS** | **`NOT IMPLEMENTED`** | **zero** occurrences of `ROW LEVEL SECURITY` / `ENABLE ROW LEVEL` anywhere in the repository — established by search, not assumption |
| Erasure against the **live** database | `PARTIALLY VERIFIED` (E3) | hard purge checks memory row, vector, sources, versions, keys, access rows, entity/relation links, transition references, source-event relation residue, retaining a content-free tombstone |
| Erasure across **backups / exports / telemetry / provider logs** | **`UNKNOWN`** | no `pg_dump` / `pg_basebackup` / `wal_level` / PITR reference outside narrative documentation; retention and legal-hold policy unfixed |

`docs/deletion-semantics.md` already scopes this correctly: the current guarantee
is an **active-database** guarantee, explicitly *not* an end-to-end regulatory
erasure claim. `migrations/` contains `001`–`007`, none of which is an RLS
migration — the absence is structural, not a forgotten file.

### Q11 — Does long-term growth pollute retrieval, or consolidation help? — `NOT TESTED`

No 100→1M controlled quality curve over a fixed query set; no consolidation
ablation (none/simple/session/periodic/rule+LLM). Both remain `NOT TESTED`, i.e.
an executable experiment is known but has not been run. This is a real
architectural blind spot: the system's central risk claim — that it does not
degrade as memory grows — is unmeasured.

### Q12 — Is there a parameterized cost model? — `NOT STARTED`

No model of 1K/10K/100K daily users across embedding calls, LLM tokens, rerank,
consolidation, GB-month storage and compute, with workload assumptions and
sensitivity ranges. `pyproject.toml` declares **no runtime dependencies**
(`dependencies = []`), so an infrastructure-derived cost figure would be
meaningless today.

### Q13 — What happens at 1M scale? — `NOT TESTED` (correctly gated)

Plan P4-P2-1 gates 1M behind a **stable** 100K correctness and operational run
with documented disk/RAM/time guardrails. The 100K run is partial (Q6), so the
gate is correctly closed. No 1M run was attempted, and none should be until Q6
closes.

### Q14 — Is the gate's output expressive enough for the decisions it is asked to make? — `NO` (new finding)

This is the review's most important architectural finding, and it is **not** a
missing feature — it is a contract mismatch discovered by re-reading the scoring
code.

`ops/false_memory_gate_eval.py` scores a **single** boolean,
`decision.accepted`, against **two** oracle axes:

- line 110 — `decision.accepted is oracle.memory_expected`
- line 117 — `decision.accepted is oracle.user_fact_allowed` (when present)

For the event `ec-third-party-residence` (`"Alice lives in Shanghai; do not treat
this as my residence."`) the oracle is `memory_expected = true` and
`user_fact_allowed = false`. **Accepting the event gains one axis and loses the
other; rejecting it does the reverse.** No binary policy can satisfy both, so the
residual false reject is not a gate defect — it is a statement that the gate
cannot express the required decision.

The architecture consequence: the gate's output domain needs a **three-valued
outcome** — *do not remember* / *remember as an entity fact* / *remember as a
user-profile fact* — before this class of event is scorable.

Two further facts about this finding:

1. **The proximate trigger is the sentence-level negation guard.** `do not`
   matches `_NEGATION_CUES`, so `_is_negated` is true for the whole sentence and
   `UNASSERTED_CLAIM` refuses it, discarding the `Alice lives in Shanghai` entity
   fact with it. The new overwrite floor did **not** participate (that event's
   source is `user`, i.e. `trusted`).
2. **The obvious fix was executed, measured and rejected — twice over.** Clause-
   scoped negation (split on `; 。 ! ? `, refuse only if *every* clause is
   negated) yields a clean-looking 34/34 on the labelled suite. Measured further:

   | | `memory_expected` axis | `user_fact_allowed` axis |
   |---|---:|---:|
   | Current | 33/34 | **34/34** |
   | Clause-scoped negation | **34/34** | 33/34 |

   Net effect **+1 / −1 = zero**: it moves the error between axes rather than
   removing it, exactly as the structural argument predicts. And the change
   direction is **permissive**, so its generalisation risk is asymmetric.
   Measured on the real corpus: it would re-admit **18,162 of 246,738 turns
   (7.36%)**, predominantly assistant chatter carrying incidental negations, while
   the negation guard currently refuses **14.39%** of the corpus and is
   load-bearing for formation. Adopting it would likely move the 82.48% formation
   figure and invalidate the 1.90 h benchmark.

   **Decision: keep the guard, keep the residual false reject, record the
   measurement.** The reasoning is preserved in
   `docs/fix-write-gate-semantic-2026-09-21.md` §4.4 so the same proposal is not
   re-made without new evidence.

---

## 4. Claims from earlier documents that this review corrected

Both corrections are in the **understating** direction, which the project treats
as being as harmful as overstating.

| # | Earlier claim | Where | Reality | Action |
|---|---|---|---|---|
| 1 | "Calibrated on **19** hand-labelled probes … no held-out labelled formation set beyond the 19 probes" | `docs/18` §5; `semantic_gate.SemanticWeights` docstring; `ops/calibrate_gate_threshold.py` intro | Probe count is **22** (verified by executing the module); a **29-probe held-out set exists** and scores **0.8276**, Wilson [0.6545, 0.9240]. What is still absent is a held-out **formation** set, which is a different object | corrected in all three files |
| 2 | "P4-P0-3 PostgreSQL 100K: `NOT STARTED`" | `docs/10-roadmap.md`, `docs/19` execution log | Two 100K runs exist on disk: the main run (A–F PASS, G FAILED) and a concurrency recovery run (branch A PASS at 1/2/4/8 workers) | corrected 2026-09-22; standing rule adopted: list `eval/reports/**` before changing any status label |

---

## 5. What changed in the architecture during Phase 4

Additions are listed with what they replaced, because "new module" is not by
itself an architectural statement.

| Change | Kind | Effect on the architecture |
|---|---|---|
| `semantic_gate.py` — prototype-margin gate (`semantic-utility-v2`) | **New decision layer** | Replaces keyword/regex candidacy for the *write* decision. v1 is untouched and remains the default; v2 is opt-in. The gate now owns a calibrated score, thresholds and reason codes |
| Single-valued **overwrite floor** on provenance | **New policy in an existing layer** | Makes the overwrite semantics of `{residence, primary_tool, goal}` enforceable against untrusted/conditional sources; requires a subject check so third-party entity facts are not blocked. `requires_encoding` was updated with it so it remains an honest predictor of encoder work |
| `answering.py` (+ `tests/test_answering.py`) | **New module, wiring** | Closes the reader-side wiring gap found by the benchmark work. Does not by itself produce a QA number |
| `PrototypeIndex.score_many` cache-consistency fix | **Correctness fix** | The bounded-FIFO vector cache could evict a hit *within a single call*; the method must snapshot hits rather than re-read the cache. This defect occurred **twice** and crashed the 500-case sweep |
| `ops/longmemeval_profile.py` | **Instrumentation** | New profiling entry point. Its per-stage timers **overlap** and must not be summed — an instrumentation defect, documented in `longmemeval-500case-profiling.md` §3a |
| GPU execution path (`--device cuda`) | **Runtime option** | Measured 10.78× end-to-end on the full run, with **decision equivalence to CPU** (identical actions/reason codes over 8 probes, score delta ≤ 1.08e-7 = float32 epsilon) |

**Net architectural shape.** The system now has a **two-layer write policy**:
a safety floor (sensitive data, temporary intent, untrusted source, untrusted
single-valued overwrite) that decides *before* the semantic layer is consulted,
and a semantic layer that decides the rest. The ordering is deliberate — relaxing
the semantic layer cannot relax the floor.

---

## 6. Components that must NOT be changed without new evidence

Phase 3 §12 listed the invariants to preserve. Phase 4 adds four, each backed by
a measurement this cycle.

**Carried forward from Phase 3 §12** (unchanged and still correct): the
event/projection boundary; SQLite for offline tests and local development;
provider-neutral interfaces; provenance, deterministic ids and tombstones; strict
HTTP schemas with namespace-first filtering and idempotency; temporal parsing as
a focused module; **no new infrastructure without a failed benchmark**.

**Added by Phase 4:**

1. **The safety-floor-before-semantics ordering.** Sensitive data and temporary
   intent must stay decided before the semantic layer, or a semantic relaxation
   silently relaxes them.
2. **`relations.SINGLE_VALUED_PREDICATES` as the single source of truth.** The
   gate reads it, the resolver reads it. Duplicating the set in the gate would let
   the two drift, and the drift would be silent.
3. **The negation guard's sentence-level scope.** Measured: clause-scoping would
   re-admit 7.36% of the corpus. Reopen only with a corpus-level measurement that
   shows the formation figure does not move.
4. **The bounded-FIFO cache contract in `PrototypeIndex`.** Any method that scores
   many texts must snapshot its hits within the call. Re-reading `_cache` after
   further stores is a latent crash — it has already happened twice.

---

## 7. ADR status after Phase 4

| ADR | Subject | Status after Phase 4 |
|---|---|---|
| ADR-001 | Event sourcing | **Reaffirmed** — no pressure observed to change it |
| ADR-002 | Storage (SQLite default) | **Reaffirmed** — offline suite and local dev depend on it |
| ADR-003 | Graph before graph-database | **Reaffirmed** — ADR-003's "prove it with multi-hop quality/latency first" gate is still unrun |
| ADR-004 | Memory types | **Reaffirmed** |
| ADR-005 | Write timing (defer vs explicit) | **Reaffirmed** |
| ADR-006 | Write gate (v1) | **Superseded in effect, retained as default** — v2 is opt-in; v1 stays the compatibility path |
| ADR-007 | Candidate extraction + write gate | **Reaffirmed**, extended — the gate now also enforces provenance-scoped single-valued rules |
| ADR-008 | Conflict resolution | **Reaffirmed but thin** — Q2 shows the semantic classes are still authored-fixture only |
| ADR-009 | Embedding model selection | **Provisional** — comparison executed (Q5) but not on the preregistered frozen corpus |
| ADR-010 | Hybrid retrieval + fusion | **Unproven, unchanged** — Q3: no naive-vector baseline exists |
| ADR-011 | Context packing + reranking | **Unchanged** — P1 packing ablation not run |
| ADR-012 | PostgreSQL + pgvector | **Provisionally supported** — 100K exact met the provisional target (Q6); RLS, ANN, mixed workload and 1M all open |

No ADR needs to be reversed by Phase 4 evidence. Two need a caveat attached at
their point of use: ADR-009 (provisional) and ADR-012 (partial evidence).

---

## 8. Residual architectural risk

Ordered by what would hurt most if it stays true.

| # | Risk | Why it is an architecture risk, not a backlog item | Blocking condition |
|---|---|---|---|
| 1 | **No grounded reader ⇒ no end-to-end quality signal** | Every quality number in the system is a *gate* or *retrieval* proxy. The architecture cannot be steered by answer quality, so improvements are selected on proxies that may not transfer | reader credentials |
| 2 | **The comparative retrieval claim is untested** | ADR-010's justification rests on hybrid being better than naive vector memory, and that comparison has never been run | none — this is runnable today |
| 3 | **The gate's output domain is too small (Q14)** | One boolean is scored against two opposite expectations; a whole class of events is unscorable by construction. Needs three-valued output | design decision first, then implementation |
| 4 | **Erasure is active-database only** | Deleted personal data may survive in backups and exports, which is a regulatory-exposure risk rather than a feature gap | backup/PITR policy |
| 5 | **No database-level isolation** | RLS is absent (searched, not assumed); application predicates are the only boundary on the measured calls | RLS migration + integration matrix |
| 6 | **No growth-pollution curve** | The claim "memory does not degrade as it grows" is the system's central premise and is unmeasured | fixed query set at increasing corpus sizes |
| 7 | **Evidence artifacts do not all satisfy the contract** | The false-memory artifacts carry no status, timestamps, git state, config hash or environment, so provenance lives in prose. Machine-checkability is an architectural property of an evidence system | `eval-manifest` adoption in the suite runner |

---

## 9. Verdict

1. **Direction confirmed.** Event-sourced evidence, outbox projections, a
   fail-closed write floor and rebuildable derived state remain the right bones.
   Phase 4 found no reason to change any of them.
2. **Formation is materially improved and is now measurable** — 6.14% → 82.48%
   gold evidence-turn coverage on the full official corpus, plus a held-out gate
   calibration at 0.8276 [0.6545, 0.9240].
3. **Two of the three Phase 3 core problems are still open** —
   evolution semantics (Q2) and the comparative retrieval claim (Q3). Retrieval
   *is* now measured; it is simply not compared.
4. **The next architectural move is not a component.** It is the reader: without
   a grounded answer path, every remaining quality decision is made on a proxy.
   If a reader cannot be configured, the highest-value substitute is the
   naive-vector baseline (Q3), because it is the one comparative question that
   needs no external dependency.
5. **One design change is unavoidable before the false-memory suite can be
   honestly closed**: three-valued gate output (Q14).
6. **This review is not a readiness claim.** See `docs/22-phase-4-production-gap-analysis.md`
   and `docs/23-phase-4-readiness-checklist.md`. Phase 4 completion does **not**
   imply production readiness (plan §P4-P2-3).

---

## 10. Evidence index

| Question | Primary artifact |
|---|---|
| Q1, Q4 | `eval/reports/longmemeval-s-retrieval-gpu.json` (`formation`), `gate-formation-semantic.json`, `gate-formation-baseline.json`, `gate-calibration.json` |
| Q2, Q14 | `eval/reports/false-memory-v2-overwrite-floor.json`, `ops/false_memory_gate_eval.py`, `docs/fix-write-gate-semantic-2026-09-21.md` §4.4 |
| Q3 | `longmemeval-s-retrieval-gpu.json` (`retrieval`) |
| Q5 | `eval/reports/embedding-comparison-latest.json`, `docs/benchmark/embedding-model-comparison.md` |
| Q6, Q9 | `eval/reports/postgres-100k/postgres-100k-20260921-084800/` (18 artifacts), `.../postgres-100k-concurrency-20260921-110255/`, `docs/benchmark/postgres-exact-100k-report.md` |
| Q7, Q8, Q11–Q13 | negative results — no artifact exists; that absence is the finding |
| Q10 | `docs/security-threat-model.md`, `docs/deletion-semantics.md`, `migrations/001`–`007` |

Supplementary: `docs/20-phase-4-results.md` (consolidated P0 results),
`docs/18-phase-4-evidence-gap-analysis.md` (§3 matrix, §5 row-level updates),
`docs/benchmark/longmemeval-500case-profiling.md` (performance),
`docs/19-phase-4-experiment-plan.md` (the plan this review closes against).
