# Phase 4 Results

Report date: 2026-09-22
Baseline commit for the P0 scope: `16f213464aab60c3e14563ede4109087a14fcaf4`
plus the preserved dirty Phase 3 worktree; the LongMemEval GPU run was executed
at `3b1907afda4c3fdca84870312f32cef16536d66a` with `git_dirty = true`.
Principle: **Evidence > Feature Count**
Supersedes nothing: this report collects the P0 results. Per-experiment detail
stays in the linked benchmark documents.

> **Read this first.** This report contains **no official LongMemEval score**.
> The reader stage was never configured and the official judge never ran, so
> every LongMemEval number below is a **retrieval-stage, memory-level** figure.
> Never present it as an official turn/session baseline or a QA score.

---

## 1. Summary and status board

| Experiment | Status | Headline result | Artifact |
|---|---|---|---|
| **P4-P0-1** Official LongMemEval | `PARTIAL` | Retrieval stage ran on **500/500** cases, 0 errors, 0 timeouts. Formation coverage **739/896 = 82.48%**. **No reader, no QA score** | `eval/reports/longmemeval-s-retrieval-gpu.json` |
| **P4-P0-2** Embedding comparison | `PARTIAL` | Run `COMPLETED` on 32 queries / 48 documents, 5 candidates. E5 highest quality, BGE-M3 close and faster, GTE fastest and smallest. Preregistered frozen-corpus split **not** applied | `eval/reports/embedding-comparison-latest.json` |
| **P4-P0-3** PostgreSQL 100K | `PARTIAL` | Checkpoints **A–F PASS**, **G FAILED**, **H not executed**. 100K exact retrieval **p50 156.12 / p95 165.93 ms**. Concurrency recovery: branch A 1/2/4/8 workers all PASS, 0 failed ops. Recovery run itself **unfinished** | `eval/reports/postgres-100k/postgres-100k-20260921-084800/`, `.../postgres-100k-concurrency-20260921-110255/` |
| **P4-P0-4** False-memory suite | `COMPLETED` (suite + both policies; v2 false accepts closed) | v2 + overwrite floor: agreement **0.9706**, **0 false accepts**, user-fact accuracy **1.0000**. One residual false reject | `eval/reports/false-memory-v1.json`, `-v2.json`, `-v2-overwrite-floor.json` |
| Enabling work (not preregistered): semantic write gate v2 | `COMPLETED` | Offline gate formation **6.14% → 84.82%**; full-corpus **82.48%** | `docs/fix-write-gate-semantic-2026-09-21.md` |
| Enabling work (not preregistered): GPU feasibility + full run | `COMPLETED` | Full 500-case run **6,850.1 s = 1.90 h**, **13.70 s/case**, **10.78×** end-to-end vs the CPU projection | `docs/benchmark/longmemeval-500case-profiling.md` |

**Status vocabulary** (plan §2): `COMPLETED` / `PARTIAL` / `NOT_COMPLETED` /
`FAILED`. A row is `COMPLETED` only if the *whole* preregistered experiment ran.
Three of the four P0 experiments are `PARTIAL`, and the report says why rather
than rounding them up.

---

## 2. Scope, environment, and artifact-contract compliance

### 2.1 Measured environment

| Item | Value |
|---|---|
| OS / platform | Windows 11, `Windows-11-10.0.26100-SP0` |
| CPU | AMD Ryzen 7 8745H, 8 cores / 16 threads |
| RAM | 15.31 GB total |
| GPU | NVIDIA GeForce RTX 4060 Laptop, 8.00 GiB, compute capability 8.9, driver 591.59 |
| Torch (GPU env) | `2.14.0+cu126` (CUDA 12.6), `.venv-gpu` |
| Python | 3.12.14 in the model/gate envs; 3.14.0 for the v1 keyword-gate run |
| Gate encoder | `BAAI/bge-m3` @ `5617a9f61b028005a4858fdac845db406aefb181`, fp32 |
| PostgreSQL / pgvector | see `eval/reports/postgres-100k/.../environment.json` (not repeated here) |

### 2.2 Artifact-contract compliance — measured, with gaps stated

Plan §2 requires each executable run to write a JSON artifact carrying a fixed
field set. Compliance is **uneven**, and the gaps are listed rather than
glossed:

| Required field | LongMemEval retrieval | False-memory suite | Postgres 100K |
|---|---|---|---|
| `schema_version` | present | **absent** | present |
| `run_id` | present (`manifest.run_id`) | **absent** | present |
| `status` | present (`PARTIAL`) | **absent** | present |
| `started_at` / `finished_at` | present | **absent** | present |
| `git_commit` / `git_dirty` | present (`manifest.git`) | **absent** | present |
| `command` | **absent** | **absent** | present |
| `config` / `config_sha256` | present (`manifest.configuration`, `configuration_sha256 = fec87d46…`) | **absent** | present |
| `dataset {name, version, sha256, license, sample_count}` | name / version / path / sha256 present (`LongMemEval_S-cleaned`, `d6f21ea9…`); **license and sample_count absent** | path only | present |
| `environment` | **partial** — `python`, `implementation`, `platform` only; **no OS, hardware, docker, postgres, pgvector** | **absent** | present |
| `models[]` | **absent** — the encoder repo/revision is **not** in the artifact; it is recorded only in `longmemeval-500case-profiling.md` §2 | **absent** | n/a |
| `seed` / `warmup_count` / `measured_sample_count` | **absent** | **absent** | n/a |
| `metrics` / per-case rows / `errors` / `timeouts` | present | metrics present, **no per-case rows** | present |
| `resource_measurements` | **absent** | **absent** | present |
| `limitations` | present | **absent** (a `notes` field carries partial caveats) | present |
| `raw_artifact_paths` | **absent** | **absent** | present |

**The false-memory artifacts are the weakest against the contract**: they are
metric-only files with no status, timestamps, git state, configuration hash or
environment block. Their provenance is therefore carried by this report and by
`ops/false_memory_gate_eval.py` rather than by the artifact itself. Closing this
is a P1 item, not a result.

Because `models[]` is missing from the LongMemEval artifact, the model pin for
that run is **not machine-checkable from the artifact**. Treat the encoder
revision as documented-but-not-attested.

---

## 3. P4-P0-1 — Official LongMemEval (retrieval stage)

**Question (plan).** What does the official benchmark say about this system's
retrieval and final-answer behaviour under the official protocol?

**Answer: the question is only half-answerable with the evidence available.** The
retrieval stage executed in full; the final-answer half did not run at all,
because no reader or judge credentials were configured.

### 3.1 Run facts

| Field | Value |
|---|---|
| Artifact | `eval/reports/longmemeval-s-retrieval-gpu.json` |
| `status` | `PARTIAL` |
| Schema | `dive-longmemeval-retrieval-v2` |
| Started / finished | `2026-09-21T14:46:02Z` / `2026-09-21T16:40:12Z` |
| Duration | **6,850.11 s = 1.90 h** |
| Cases / rows | **500 / 500** |
| Errors / timeouts | **0 / 0** |
| Dataset | `LongMemEval_S-cleaned`, sha256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| Write gate | `semantic-utility-v2.1`, `device=cuda:0`, `cache_size=1024`, `encode_batch_size=8`, `max_sequence_length=512`, `limit=10` |
| Git | `3b1907af…`, dirty worktree |

### 3.2 Formation

| Measure | Value |
|---|---:|
| Non-empty turns ingested | 246,738 |
| Memories formed | 93,774 (0.380 / turn) |
| Gold evidence turns | 896 |
| **Gold evidence turns formed** | **739** |
| **Formation coverage** | **82.48%** |

For contrast, the same corpus under the **v1 keyword gate** formed 55/896 =
**6.14%** (`eval/reports/longmemeval-s-retrieval-latest.json`, 136.3 s). The
change is the enabling work in §7, not a preregistered P0 treatment.

### 3.3 Retrieval — memory-level, **not** the official baseline

Scored cases: **470** (the remaining 30 are the abstention set; see §3.4).

| Metric | Turn-level | Session-level |
|---|---:|---:|
| Recall-any @1 | 0.3660 | 0.5787 |
| Recall-any @5 | 0.5681 | 0.7128 |
| **Recall-any @10** | **0.6319** | **0.7766** |
| Precision @10 | 0.0966 | 0.2496 |
| nDCG @10 | 0.4230 | 0.5989 |
| **MRR** | **0.4520** | **0.6409** |

The v1-gate comparison (turn Recall-any@10 **0.0809**, MRR **0.0557**; session
Recall-any@10 0.7745, MRR 0.6033) shows the same effect as formation: with 6.14%
of evidence turns formed, turn-level recall is near zero. **Session-level recall
was already 0.7745 under v1** because the evidence sat inside retrieved
sessions even when it was never formed into a memory — which is precisely the
failure the formation work targets.

These are **memory-level rankings**. The official LongMemEval baseline scores
raw turns and sessions, which this block does not reproduce.

### 3.4 Latency, abstention, and what is missing

| Measure | p50 | p95 | p99 |
|---|---:|---:|---:|
| Retrieve per case (ms) | 20.09 | 35.50 | 39.40 |
| Ingest per case (ms) | 12,196.03 | 20,187.19 | 21,202.63 |

Ingest dominates the wall clock: the retrieval median is **0.16%** of the ingest
median (20.09 ms / 12,196 ms).

`abstention.status = NOT_EVALUATED` (30 cases; `retrieval_empty_rate = 0.002`).
The artifact's own note is the correct reading: *candidate absence is not
semantic abstention* — evaluating it requires a grounded reader.

`official_scope`: `retrieval_stage = COMPLETED`, `reader_stage =
NOT_EVALUATED`, `qa_generation = NOT_COMPLETED`, `official_qa_judge =
NOT_COMPLETED`, reason "no configured reader/judge model credentials".

**What P4-P0-1 still does not have:** any QA or answer-quality number, any
official overall score, any abstention measurement. The blocking condition is
credential/budget availability for a reader and the official GPT-4o judge — a
`NOT_COMPLETED` under the plan's stop conditions, not a failed experiment.

---

## 4. P4-P0-2 — Embedding model comparison

**Question (plan).** Which real embedding model best serves the bilingual
long-term memory workload under a common retrieval protocol?

**Answer, scoped.** On a 32-query / 48-document project-authored set, with all
candidates sharing one corpus, query set, top-k and metric implementation:

| Candidate | Top-1 | Recall@5 | MRR | nDCG@10 | Query p50 | Model files |
|---|---:|---:|---:|---:|---:|---:|
| Deterministic SHA-256 fixture (non-semantic) | 34.38% | 43.75% | 0.3957 | 0.3890 | 0.04 ms | 0 |
| BM25 word/CJK-bigram reference (lexical) | 65.63% | 84.38% | 0.7438 | 0.7664 | 0.09 ms | 0 |
| **BGE-M3** | 93.75% | 100% | 0.9688 | 0.9769 | 100.6 ms | 2.32 GB |
| **multilingual-e5-large-instruct** | **96.88%** | 100% | **0.9750** | **0.9808** | 132.4 ms | 1.14 GB |
| **gte-multilingual-base** | 87.50% | 100% | 0.9375 | 0.9539 | **43.3 ms** | **628 MB** |

Pins: `BAAI/bge-m3@5617a9f6…` (1024-d), `intfloat/multilingual-e5-large-instruct@274baa43…`
(1024-d), `Alibaba-NLP/gte-multilingual-base@9bbca17d…` (768-d, plus remote code
`Alibaba-NLP/new-impl@40ced75c…`). All on CPU, batch 8, L2-normalized fp32,
exact in-memory cosine.

**Decision as recorded:** ADR-009 makes E5 the provisional quality-first
reference, retains BGE-M3 as the close latency-balanced challenger, and GTE as
the efficiency challenger. No final or production selection is made. No
candidate was rejected on provenance/temporal hard filters because the harness
applies no separate metadata filter.

**Why this is `PARTIAL`, not `COMPLETED`.** Plan §P4-P0-2 preregisters a *frozen
corpus* split by user/session with the test partition untouched during
configuration selection. The executed set covers the eight required categories
(semantic paraphrase, preference, old episodic recall, temporal change,
conflicting facts, near-duplicates, entity ambiguity, long-tail) but **was not
split by user/session, and there is no held-out partition**. So the comparison
is a valid controlled ranking on this set and is **not** the preregistered
protocol.

**Uncertainty is high and must be stated with the numbers.** One query moves an
overall percentage by 3.125 points and a slice result by 25 points. BGE-M3's two
Top-1 misses are both in the four-case conflicting-facts slice (50%); E5's single
miss is a semantic-paraphrase case; GTE's temporal Top-1 is 2/4 against 4/4 for
BGE-M3 and E5. Recall@5 = 100% for all three neural models is **not** evidence
that it holds under memory growth — 48 documents is not a corpus.

Two dependency failures preceded the successful run and are retained in
`docs/benchmark/failures/`: a BGE-M3 snapshot download failure, and a GTE
remote-code `IndexError` under Transformers 5.17 (resolved on the pinned 4.39.1
environment).

---

## 5. P4-P0-3 — PostgreSQL 100K scale

**Question (plan).** What are real 100K PostgreSQL + pgvector exact-path
quality, latency, throughput and resource boundaries?

**Answer: the exact-path latency question is answered; the concurrency and mixed
workload questions are only partly answered, and one checkpoint failed.**

### 5.1 Main run — `postgres-100k-20260921-084800`

`final-summary.json` reports `status = PARTIAL`, `last_successful_checkpoint = F`,
`failed_checkpoint = G`, `not_completed_checkpoint = H`, `failure_mode =
"PostgreSQL dynamic shared memory /dev/shm exhausted at 8 workers"`.
Final integrity: **100,000 memories / 100,000 vectors**, 96 dimensions,
no ANN indexes present.

| Measure | Observation |
|---|---|
| 100K exact retrieval | **p50 156.12 ms / p95 165.93 ms** |
| 10K exact retrieval (comparison) | p50 167.18 ms — the artifact explicitly flags this as **not directly comparable** |
| Filtered retrieval (1% / 10% / 50% selectivity) | p50 20.01 / 30.87 / 81.51 ms |
| Temporal current-fact SQL | p50 301.25 ms (different path from exact; not a pure overhead ratio) |
| Bulk load | COPY **20,279.41 rows/s** |
| Service ingest | 57.49 memories/s (1 writer), 177.31 memories/s (4 writers) |
| Schema size | 167,550,976 B for 96-d vectors |

**The linear-scaling question is answered "no claim supported"**, on the
artifact's own reasoning: the observed 10K→100K ratio was not near 10×, and the
load and projection protocols and runtime state differ. This is the correct
answer to record — not a weak scaling claim.

### 5.2 Concurrency recovery — `postgres-100k-concurrency-20260921-110255`

The 8-worker failure did **not** reproduce once
`max_parallel_workers_per_gather` was changed `2 → 0` (session scope only):

| Workers | Operations | Failed ops | Throughput | Retrieval p50 |
|---:|---:|---:|---:|---:|
| 1 | 8 | 0 | 18.1 ops/s | 49.6 ms |
| 2 | 16 | 0 | 34.6 ops/s | 51.4 ms |
| 4 | 32 | 0 | 48.6 ops/s | 71.7 ms |
| 8 | 64 | **0** | **62.1 ops/s** | **87.1 ms** (p95 239 ms) |

**This reframes the original result.** The `/dev/shm` exhaustion was
**configuration-induced**, not an inherent database boundary. The 44/64 failures
in the main run must not be cited as a PostgreSQL limitation.

**But the recovery run itself is unfinished.** Manifest `status = PENDING`,
last updated `2026-09-21T03:34:55Z`. `inspect` PASS; `branch_a` PASS at
1/2/4/8; `branch_b` worker 1 PASS with **2/4/8 PENDING**; `smoke`,
`mixed_workload`, `integrity_audit` and `summary` all **PENDING**. There is
**no final summary artifact**. Do not cite this as a completed recovery.

### 5.3 What P4-P0-3 does not prove

Taken from the artifact's own `what_this_does_not_prove`: semantic quality,
successful mixed workload, true cold cache, ANN behaviour, 1M scalability,
general PostgreSQL scalability. The 100K corpus was bulk-loaded **without** the
full event/provenance projections, so it is not comparable to the 10K service
baseline.

---

## 6. P4-P0-4 — False-memory test suite

**Question (plan).** Does formation/evolution create or promote unsupported
memory?

**Answer: under the v2 gate plus the single-valued overwrite floor, no false
accept was observed in 34 scored events — and the interval on that zero is wide.**

### 6.1 Results across the three policies

| Measure | v1 keyword | v2 semantic | **v2 + overwrite floor** |
|---|---:|---:|---:|
| Oracle agreement | 22/34 = 0.6471 | 32/34 = 0.9412 | **33/34 = 0.9706** |
| **False accepts** | **12/34 = 0.3529** | 1/34 = 0.0294 | **0/34** |
| False rejects | 0/34 | 1/34 | 1/34 |
| User-fact accuracy | 21/34 = 0.6176 | 33/34 = 0.9706 | **34/34 = 1.0000** |
| Attack-refusal rate | 3/34 = 0.0882 | 15/34 = 0.4412 | 16/34 = 0.4706 |

95% Wilson intervals (computed for this report; **the artifacts do not carry
them**, and plan §P4-P0-4 requires them):

| Measure (v2 + floor) | Rate | 95% Wilson |
|---|---:|---|
| Oracle agreement | 0.9706 | [0.8508, 0.9948] |
| **False-accept rate** | **0.0000** | **[0.0000, 0.1015]** |
| False-reject rate | 0.0294 | [0.0052, 0.1492] |
| User-fact accuracy | 1.0000 | [0.8985, 1.0000] |
| Attack-refusal rate | 0.4706 | [0.3145, 0.6326] |

**Read the zero honestly: `0/34` does not mean the false-accept rate is zero.**
The interval's upper bound is **10.15%**. And the v2 → v2+floor change
(1/34 → 0/34) is **not statistically established at n=34**: the two intervals
(v2 `[0.0052, 0.1492]`, floor `[0.0000, 0.1015]`) overlap. The point estimate
improved and the specific known failure is closed; the *rate* claim is not
separable from noise at this denominator.

### 6.2 Per-category

| Category | v1 | v2 | v2 + floor |
|---|---:|---:|---:|
| entity_confusion | 2/3 | 2/3 | 2/3 |
| extraction_hallucination | 2/3 | 3/3 | 3/3 |
| generated_response_feedback | 1/3 | 3/3 | 3/3 |
| incorrect_merge | 5/6 | 6/6 | 6/6 |
| incorrect_supersede | 3/5 | 5/5 | 5/5 |
| inference_promotion | 0/3 | 3/3 | 3/3 |
| summary_distortion | 3/5 | 4/5 | **5/5** |
| temporal_confusion | 6/6 | 6/6 | 6/6 |

The overwrite floor changed **exactly one cell**: `summary_distortion` 4/5 → 5/5.
Nothing else moved. The change is surgical, not a rebalancing.

### 6.3 The defect that was closed, the bound that was verified, and the one that cannot be closed

**Closed — `sd-summary-overwrites-user`.** A user states "我住在成都"; a
generated summary later states "我住在北京" (`source_type = summary`). The gate
saw a textbook durable personal fact scoring 0.5148 against a 0.495 threshold, so
the previous `effective *= 0.9` damping could not stop it. The failure mode was
**provenance, not content**: a legitimate fact asserted by a non-trusted source
that carries **overwrite semantics**. Fix: a **single-valued overwrite
provenance floor** — a non-`trusted` source may not create a
`residence`/`primary_tool`/`goal` user fact (`relations.SINGLE_VALUED_PREDICATES`).
Multi-valued predicates (`preference`) still accumulate, so the suite's faithful-
summary duplicate control is untouched. `reason_code =
UNTRUSTED_SINGLE_VALUED_OVERWRITE`. Regression tests: 6 in
`tests/test_semantic_gate.py`. Evidence: `eval/reports/false-memory-v2-overwrite-floor.json`.

**Verified, so that no change was needed — LongMemEval is unaffected.** The floor
was scanned across the whole corpus: it refuses **0 of 246,738 turns** (the
single-valued extractor patterns are first-person Chinese; the corpus is
English). So the 82.48% formation figure and the 1.9 h run **did not need
re-running**. Measured overhead: **12.99 µs/call, 1.6 s over 124,331 assistant
turns = 0.024%** of the run.

**Cannot be closed — `ec-third-party-residence` (residual false reject).** Text
`Alice lives in Shanghai; do not treat this as my residence.`, oracle
`memory_expected = true` / `user_fact_allowed = false`.

*Why it is unsatisfiable at the gate:* the harness scores the **single** boolean
`decision.accepted` against **two opposite** expectations —
`decision.accepted is memory_expected` (line 110, expects `True`) and
`decision.accepted is user_fact_allowed` (line 117, expects `False`). Accepting
passes one axis and fails the other; rejecting does the reverse. **No accept/
reject policy satisfies both.** A real fix needs a three-valued outcome ("no
memory" / "entity memory" / "user-profile fact") — architecture work, not a
threshold change.

*Why the obvious local fix was rejected:* the proximate trigger is the
**sentence-level negation guard** (`do not` matches `_NEGATION_CUES`, so the
whole sentence is refused and the Alice fact is lost). Making it clause-scoped
was **measured**:

| | memory_expected axis | user_fact axis | false accepts | false rejects |
|---|---:|---:|---:|---:|
| current | 33/34 | **34/34** | 0 | 1 |
| clause-scoped | **34/34** | 33/34 | 0 | 0 |

It merely moves the error between axes (`+1 −1`, net zero), **and** on the real
corpus it would re-admit **18,162 of 246,738 turns (7.36%)** — overwhelmingly
assistant chatter carrying incidental negations ("I don't have personal
relationships", "they don't typically…", "I'm still unable to find…"). The
negation guard refuses **14.39%** of the corpus and is load-bearing for
formation; relaxing it would likely change the 82.48% figure and void a 1.9 h
benchmark. **Rejected and recorded.** Full record:
`docs/fix-write-gate-semantic-2026-09-21.md` §4.4.

> **Methodology warning for the next person.** The first offline scan of this
> candidate counted only the `memory_expected` axis and concluded it was a clean
> 34/34. Adding the `user_fact_allowed` axis showed the truth. **When scanning a
> candidate gate rule, always score both axes.** And note the asymmetry: a
> tightening change can only refuse more (bounded, acceptable), while a
> loosening change admits false memories — the burden of proof is higher.

### 6.4 Guardrail verdict

The plan's hard guardrail is that an agent-generated or external statement
without user evidence must not become a USER FACT. Under v2 + floor, **0/34
false accepts** and **34/34 user-fact accuracy** were observed. No violation was
recorded in the suite. The residual uncertainty is the interval in §6.1, and the
guardrail is only tested against 34 labelled events — it is a controlled-suite
result, not a population guarantee.

---

## 7. Enabling work (not preregistered)

### 7.1 Semantic write gate v2

The v1 keyword gate formed 6.14% of gold evidence turns, which capped every
downstream retrieval metric. The v2 gate (nearest-anchor margin scoring over
real `bge-m3` vectors, plus source-trust and assertion-strength layers) raises
**offline gate formation 6.14% → 84.82%** and full-corpus formation to
**82.48%**. The two numbers differ because they are different corpora; **82.48%
is the one that belongs in any LongMemEval sentence.** Detail:
`docs/diagnosis-formation-gate-2026-09-21.md`,
`docs/fix-write-gate-semantic-2026-09-21.md`.

The gain is measured **at the gate and at formation**, not end-to-end. There is
no answer-quality evidence that it improves final responses.

### 7.2 GPU feasibility and the full 500-case run

| Measure | Projected | **Actual** |
|---|---:|---:|
| Wall clock, 500 cases | 1.52 h | **1.90 h** (6,850.1 s) |
| Seconds per case | 10.96 | **13.70** |
| Projection error | — | **+25% too optimistic** |
| End-to-end speedup vs CPU projection (20.5 h) | — | **10.78×** |
| Rows / errors / timeouts | — | 500 / 0 / 0 |

**The 1.52 h projection was wrong by 25%, and the first explanation for it was
disproved by the data.** The initial hypothesis (later cases have longer
haystacks, so the 4-case slice was optimistic) is **falsified**: per-case ingest
latency is **not** predictable from turn count (R² = **0.0299**). The leading
(unconfirmed) explanation is FIFO cache saturation late in the corpus. Detail
and the falsification test: `longmemeval-500case-profiling.md` §10.

Two operational facts worth carrying forward: the full run **cannot be
interrupted or partially recovered** (no checkpoint, no per-case write,
`:memory:` store closed per case), and **throughput must be measured through the
production runner path** — a hand-rolled equivalent loop bypasses `warm()` and
moved an extrapolation from 20.5 h to 32.7 h (37% error).

---

## 8. Failure and correction records

Retained deliberately. A failure record is a result.

| # | Event | Handling |
|---|---|---|
| 1 | **Postgres checkpoint G failed** — `/dev/shm` exhausted at 8 workers, 44/64 ops failed | Run stopped safely, partial artifact kept, H not executed. Later shown to be **configuration-induced** (`max_parallel_workers_per_gather`), so it is not a database boundary |
| 2 | **Postgres concurrency recovery run unfinished** | Branch A 1/2/4/8 PASS recorded; `branch_b` 2/4/8, `smoke`, mixed workload, integrity audit and summary remain `PENDING`; no final summary artifact |
| 3 | **LongMemEval 1.52 h projection wrong by +25%** | Measured 1.90 h; the leading explanation was **falsified** (R² = 0.03) and the record says so |
| 4 | **`PrototypeIndex.score_many` lost its own results to cache eviction** (crashed the 500-case sweep) | Fixed and pinned with a test; this bug occurred **twice** |
| 5 | **Reader-side wiring gap** | Fixed and pinned with a test |
| 6 | **Embedding BGE-M3 snapshot download failure**; **GTE remote-code `IndexError` under Transformers 5.17** | Retained under `docs/benchmark/failures/`; resolved on the pinned 4.39.1 environment |
| 7 | **`ops/longmemeval_profile.py` per-stage timers overlap** | Instrumentation defect: `per_case_mean_ms.warm` and `.ingest` **must not be summed**; no per-stage share may be derived from them |
| 8 | **Documentation drift: P0-3 was written as "NOT STARTED" when two 100K runs existed on disk** | Corrected 2026-09-22. The row was wrong **in the direction of understating** completed work — understating is as harmful as overstating |
| 9 | **Documentation drift: `fix-write-gate-semantic-2026-09-21.md` §4.4 judged `sd-summary-overwrites-user` "not fixable at the gate"** | That judgement was **wrong** — it was fixed at the gate. Section revised 2026-09-22 with the reasoning error stated |

**Standing rule adopted from #8:** before changing any status label, list
`eval/reports/**`. The artifact is the source of truth; transcription drifts.

---

## 9. What Phase 4 does not establish

Per plan §6, the unknown items are recorded with their blocking condition
rather than left implicit.

| Not established | Blocking condition |
|---|---|
| Any official LongMemEval score, or any answer-quality metric | No reader/judge credentials configured; reader `NOT_EVALUATED`, judge `NOT_COMPLETED` |
| Semantic abstention behaviour | Requires a grounded reader |
| That the v2 write gate improves **end-to-end answer quality** | Only gate-level and formation-level gains are measured |
| That the full retrieval architecture beats naive vector memory | No naive-vector baseline has been run under the same queries |
| That BGE-M3/E5/GTE is best for this workload in a general sense | 32-query project-authored set; preregistered frozen split not applied |
| That the false-memory rate is 0 | 0/34 observed, 95% Wilson upper bound **10.15%** |
| PostgreSQL concurrency behaviour x mixed workload | Recovery run unfinished; completion needs Docker running |
| ANN behaviour, 1M scalability, cost model, production readiness | P1/P2 scope; not started |

**Phase 4 completion is explicitly not production readiness** (plan §P4-P2-3).

---

## 10. Raw artifacts and reproduce commands

### 10.1 Artifacts

| Artifact | What it is |
|---|---|
| `eval/reports/longmemeval-s-retrieval-gpu.json` | P0-1 retrieval stage, semantic v2 gate, `device=cuda:0`, 500/500 |
| `eval/reports/longmemeval-s-retrieval-latest.json` | P0-1 retrieval stage, v1 keyword gate (formation 6.14%) |
| `eval/reports/embedding-comparison-latest.json` | P0-2 five-candidate controlled comparison |
| `eval/reports/postgres-100k/postgres-100k-20260921-084800/` | P0-3 main run (18 artifacts; A–F PASS, G FAILED) |
| `eval/reports/postgres-100k-concurrency/postgres-100k-concurrency-20260921-110255/` | P0-3 recovery run (branch A PASS; rest PENDING) |
| `eval/reports/false-memory-v1.json`, `-v2.json`, `-v2-overwrite-floor.json` | P0-4 three policies |
| `eval/reports/longmemeval-profile-v1.json`, `-semantic.json` | Stage-split profiles (timers overlap — see §8 #7) |

Supporting documents: `docs/benchmark/longmemeval-500case-profiling.md`,
`docs/benchmark/longmemeval-report.md`, `docs/benchmark/embedding-model-comparison.md`,
`docs/benchmark/postgres-exact-100k-report.md`, `docs/benchmark/postgres-exact-10k-report.md`,
`docs/fix-write-gate-semantic-2026-09-21.md`,
`docs/diagnosis-formation-gate-2026-09-21.md`,
`docs/18-phase-4-evidence-gap-analysis.md`.

### 10.2 Reproduce

```powershell
# P0-1 — retrieval stage, semantic v2 gate, GPU (offline flags required:
# transformers probes the Hub for a PEFT adapter even with the snapshot cached)
$repo = "D.I.V.E.Program_Sanofi"
$env:HF_HOME = "$repo\eval\external\huggingface"
$env:HF_HUB_OFFLINE = "1"; $env:TRANSFORMERS_OFFLINE = "1"
& "$repo\.venv-gpu\Scripts\python.exe" -m dive_memory.longmemeval_benchmark `
      eval/external/longmemeval/longmemeval_s_cleaned.json `
      --output eval/reports/longmemeval-s-retrieval-gpu.json --gate semantic --device cuda

# P0-4 — false-memory suite, both policies
& "$repo\.venv-gpu\Scripts\python.exe" ops\false_memory_gate_eval.py --policy v1 --output eval\reports\false-memory-v1.json
& "$repo\.venv-gpu\Scripts\python.exe" ops\false_memory_gate_eval.py --policy v2 --output eval\reports\false-memory-v2-overwrite-floor.json

# Unit suite (both env switches are required on this machine; see the note below)
$env:PYTHONWARNINGS = "ignore"; $env:CODEBUDDY_SAFE_DELETE_ENABLED = "0"
& "$repo\.venv\Scripts\python.exe" -m pytest -q "--basetemp=$repo\.pt-tmp" -p no:cacheprovider
```

Current test baseline: **348 passed / 9 skipped / 0 errors**.

> `CODEBUDDY_SAFE_DELETE_ENABLED=0` is mandatory here. Without it the harness's
> bulk-delete guard aborts `tmp_path` teardown, producing 31 errors that look
> like code regressions but are sandbox behaviour. `--basetemp` must also point
> at a pure-ASCII directory inside the workspace.

### 10.3 Phase 4 closure status

Plan §6 closes Phase 4 when, after this report, the architecture review
(`docs/21-phase-4-architecture-review.md`), the production gap analysis
(`docs/22-phase-4-production-gap-analysis.md`) and the readiness checklist
(`docs/23-phase-4-readiness-checklist.md`) are published.

**All four closure documents are now published** (2026-09-22):

| Document | Content |
|---|---|
| `docs/20-phase-4-results.md` (this file) | Consolidated P0 results, environment, contract compliance, failure records |
| `docs/21-phase-4-architecture-review.md` | 14 architecture questions answered from executed evidence; 4 preserved invariants; ADR status; residual architectural risk |
| `docs/22-phase-4-production-gap-analysis.md` | 18 readiness domains; 1 `PASS`, 8 `PARTIAL`, 9 `NOT IMPLEMENTED`/`NOT DEFINED`/`UNKNOWN` |
| `docs/23-phase-4-readiness-checklist.md` | 70 item-level verdicts; 12 `PASS`; conditional paths that flip each verdict |

**Phase 4 stops here. Phase 5 does not start automatically** (plan §6).

**Phase 4 completion is explicitly not production readiness** (plan §P4-P2-3).
The overall readiness verdict is `NOT READY`, and `docs/22` §5 lists the
statements that the current evidence does not support.
