# LongMemEval 500-case feasibility: measured bottleneck profile

Date: 2026-09-21; full-run section updated 2026-09-22
Status: `VERIFIED` for feasibility — **the full 500-case GPU run COMPLETED**
(500/500 rows, 0 errors, 6,850 s = 1.90 h). The CPU-side profile remains
`PARTIALLY VERIFIED`. No LongMemEval *quality* claim is made here.

Scope of this document: **what the full 500-case semantic-gate run costs and
where the time goes, with the projection error measured rather than hidden.**
It is a profiling report, not a benchmark result.

## 0. The full run — headline result

The run that this whole document was written to plan has now been executed.
Configuration pinned to match the CPU baseline: `--gate semantic`,
`cache_size=1024`, `encode_batch_size=8`, `max_sequence_length=512`, `limit=10`,
all 500 cases, `device=cuda:0`.

| Measure | Projected before the run | **Actual** |
|---|---:|---:|
| Wall clock, 500 cases | 1.52 h | **1.90 h** (6,850.1 s) |
| Seconds per case | 10.96 | **13.70** |
| Projection error | — | **+25.0% too optimistic** |
| Rows completed | — | **500 / 500** |
| Errors / timeouts | — | **0 / 0** |
| Gate decisions changed by the device | — | no (see §7a) |

**The 1.52 h projection was wrong by +25%.** §10 diagnoses why, because the
first explanation I reached for was disproved by the data.

Other end-to-end figures from the same artifact:

| Measure | Value |
|---|---:|
| Non-empty turns ingested | 246,738 |
| Formed memories | 93,774 (0.380 / turn) |
| Gold evidence-turn formation coverage | **739 / 896 = 82.48%** |
| Ingest latency p50 / p95 / p99 | 12,196 / 20,187 / 21,203 ms |
| Retrieve latency p50 / p95 / p99 | 20.1 / 35.5 / 39.4 ms |
| Full-run GPU speedup vs CPU projection | **10.78×** |

Formation coverage is **82.48%** here against the **84.82%** measured offline
over the semantic-gate micro-suite. These are consistent — different corpora,
and 82.48% is the number that belongs in any LongMemEval sentence.

> **What this run is not.** `official_scope.reader_stage = NOT_EVALUATED`,
> `qa_generation = NOT_COMPLETED`, `official_qa_judge = NOT_COMPLETED`, and
> `abstention.status = NOT_EVALUATED`. There is **no** LongMemEval QA score
> here and none may be inferred. The `retrieval` block is a memory-level
> ranking summary, not the official turn/session baseline.

## 1. Answer in one line

On this machine the current code needs **≈20.5 h** for 500 cases on CPU, and
**99.99% of that is turn encoding inside ingest**; retrieval is 13.9 ms/case.
Moving the encoder to the RTX 4060 was **measured** at **10.78× end-to-end**,
giving **1.90 h measured** — not the 1.52 h first projected.

## 2. Measured environment

| Item | Value |
|---|---|
| CPU | AMD Ryzen 7 8745H, 8 cores / 16 threads |
| RAM | 15.31 GB total, 4.42 GB free at measurement time |
| GPU | NVIDIA GeForce RTX 4060 Laptop, 8.00 GiB, compute capability 8.9, driver 591.59 |
| Torch (CPU env) | 2.14.0+cpu, 8 threads, `cuda.is_available() = False` |
| Torch (GPU env) | **2.14.0+cu126** (CUDA 12.6), `.venv-gpu` |
| Python | CPython 3.14.0 for API/mock runs; 3.12.14 in the model envs |
| Model envs | `.venv-models` (CPU) and **`.venv-gpu` (CUDA)** |
| Gate model | `BAAI/bge-m3` @ `5617a9f61b028005a4858fdac845db406aefb181`, fp32 |

## 3. Stage split — real runner path

Measured by calling `run_longmemeval_retrieval` itself (the production path,
including the runner's bounded `warm()` pre-pass and the gate vector cache),
with `max_cases=4`, `limit=10`, `cache_size=1024`, `encode_batch_size=8`,
`max_sequence_length=512`.

| Measure | Result |
|---|---:|
| Wall clock (4 cases, incl. 67.1 s model load) | 590.5 s |
| **Per case** | **147.6 s** |
| Ingest per case (mean) | 176,562 ms |
| **Retrieve per case (mean)** | **13.9 ms** |
| Mean non-empty turns per case | 517.5 |
| Mean formed memories per case | 182.0 |
| Gate load (one-off per process) | 67.1 s |

Ingest share ≈ **99.99%**; retrieval ≈ **0.008%**.

> The 4-case retrieval metrics produced by this run are **not** reported as
> results. They are n=2 scored cases, both `single-session-user` (the easiest
> published type), and the artifact labels them high-uncertainty.

### 3a. Known instrumentation defect: the stage split double-counts

`ops/longmemeval_profile.py` records `per_case_mean_ms` with **per-stage
timers that overlap**, so the components **cannot be summed**. On the GPU
semantic profile (`eval/reports/longmemeval-profile-semantic.json`):

| Field | Value (ms/case) |
|---|---:|
| `per_case_mean_ms.warm` | **10,761.75** |
| `per_case_mean_ms.ingest` | 1,807.77 |
| `per_case_mean_ms.retrieve` | 12.19 |
| `per_case_mean_ms.segment` | 48.48 |
| `wall_clock_seconds` / 3 cases | **12,630** |

`warm + ingest = 12,569 ms/case` already consumes the whole wall clock; adding
`retrieve` and `segment` overshoots it. The two numbers therefore **cannot both
describe disjoint intervals**.

Root cause, from `longmemeval_benchmark._evaluate_case`
(`src/dive_memory/longmemeval_benchmark.py:133-168`):

- `ingest_ms` starts its `perf_counter` **before** the warm+ingest loop and
  stops it **after**, so it *includes* every `gate.warm()` call;
- the profile's own `warm` field accumulates the individual `gate.warm(...)`
  durations from inside that same loop.

The two are measured at different scopes over the same work.
**Do not present `warm` and `ingest` as additive shares of the wall clock**, and
do not compute a "bottleneck percentage" from them. The correct statement is
narrower: *the warm pre-pass is where the semantic gate spends its time inside
ingest*, which §4-§6 already establish by an independent breakdown on the
**un-warmed** path (`service.ingest` measured directly, no runner warm-up).

This defect is the reason the §7 table is anchored on the **runner's whole-case
wall clock** (147.63 s CPU / 10.96 s GPU) rather than on summed sub-stages: the
wall clock is the only figure here that cannot double-count.

> Correction status: the timer scopes in `ops/longmemeval_profile.py` are **not
> yet fixed**. They should be made non-overlapping (either exclude warm time
> from `ingest_ms`, or emit the warm pre-pass as a separate interval) before any
> future head-to-head stage comparison is published. Recorded here rather than
> silently patched, because the CPU and GPU profile artifacts that already
> exist were produced by the current instrumentation.

## 4. Ingest internal breakdown

Two configurations, same 1,564 turns, measuring `service.ingest` directly
(no runner warm-up — this deliberately exposes the un-warmed cost):

| Bucket | v1 keyword gate | semantic gate |
|---|---:|---:|
| extractor + gate | 0.36 s (31.8%) | **685.4 s (99.9%)** |
| embedding (deterministic sha256) | 0.38 s (33.1%) | 0.12 s |
| `add_memory` sqlite work | 0.13 s (11.4%) | 0.15 s |
| `append_event` | 0.04 s (3.8%) | 0.07 s |
| other store calls | 0.08 s (7.2%) | 0.18 s |
| **wall clock** | **1.15 s** | **686.2 s** |

The semantic gate costs **~1,900× the v1 gate**. Nothing else in ingest changed.

## 5. Root cause: the `max_seq_length` pin does not reach the model call

Measured attributes and tensor shapes:

```
tokenizer.model_max_length        = 8192
encoder.max_seq_length            = 512
encoder.tokenize(batch8).input_ids.shape = (8, 512)
tokenizer(..., max_length=512, truncation=True, padding=True) -> (8, 512)
```

`encoder.max_seq_length = 512` is set on the `SentenceTransformer` wrapper. What
actually shapes the batch is `tokenizer(..., max_length=512, truncation=True)`,
and **padding is `longest`** — so a batch containing one 724-token turn pads all
eight rows to 724, then the model forward pass runs on that.

Forward-pass cost, measured with synthetic tensors (batch 8):

| seq | total ms | ms/item |
|---:|---:|---:|
| 64 | 803.7 | 100.5 |
| 128 | 1,586.7 | 198.3 |
| 256 | 3,462.7 | 432.8 |
| 512 | 7,686.5 | 960.8 |
| 1024 | 19,670.5 | 2,458.8 |
| 2048 | 56,814.7 | 7,101.8 |

Batch 1: 512 → 1,003 ms; 1024 → 2,335 ms; 2048 → 6,651 ms; 4096 → 22,092 ms.

### Two optimisations that look obvious and are measured to be wrong

1. **"Cap the sequence shorter."** Setting `max_seq_length=128` changed a
   batch-8 encode from 7,864 ms to 7,989 ms — *no improvement*. The pin is not
   consulted. A real 256-token cap measured 457 ms/encode × 1.286 encodes/turn
   projects to **43.9 h**, i.e. **worse** than today.
2. **"Enable batching."** `_embed_many` / `_embed_one` call
   `encoder.encode(prepared, normalize_embeddings=True)` **without
   `batch_size`**, so sentence-transformers applies its default and re-batches
   internally. Measured sequential-8 = 3,883 ms vs batched-8 = 7,742 ms →
   **0.50×** (batching is slower, not faster, at these lengths).

Neither is a fix. Do not implement them expecting a speedup.

## 6. Encoder call amplification

| Measure | Value |
|---|---:|
| Encodes per turn | **1.286** (whole turn + recovered assertion span) |
| Texts that reached the encoder decision | 1,331 / 1,035 turns |
| Cache hits | **0** |
| Gate wall time (un-threaded ingest loop) | 311 ms/turn |

With the runner's bounded 32-turn `warm()` block the same work amortises to
**≈285 ms/turn** (147.6 s ÷ 517.5 turns), which is why the runner figure is
lower than the raw loop figure.

## 7. Projection to 500 cases

> **Superseded for option E.** The table below is the projection as it stood
> *before* the full run. Option E has since been executed and measured:
> **1.90 h, not 1.52 h.** See §0 and §10. The other rows remain projections.

All rows derive from the measured constants above; no new computation.

| Option | 500-case wall clock | Basis |
|---|---:|---|
| **A. Current code, CPU, single process** | **20.5 h** | measured runner throughput |
| **E. RTX 4060 GPU (`--device cuda`)** | ~~1.52 h~~ → **1.90 h MEASURED** | projected at 13.47×; **actual run 10.78×** |
| D. Same, 4 cases in parallel (8 cores) | ≈5.2 h | 4× throughput, needs per-worker thread cap (projected) |
| F. Revert to v1 keyword gate | 3.4 min | but gold evidence-turn formation falls back to 6.14% |
| G. One-off prefill, then reuse | pay 1.90 h once on GPU | subsequent retrieval sweeps cost ~20 ms/case |

Options B and C ("cap at 256", "cap at 256 + batch 8") are **43.9 h / 41.6 h** —
slower than A. They are listed only to record that they were measured and
rejected.

## 7a. GPU speedup — measured, not assumed

The earlier revision of this document carried "≈1.4 h, **ASSUMED 15×, NOT
MEASURED**" as an E0 assumption. It has now been replaced by a measurement on
the **same 4 cases and the same `run_longmemeval_retrieval` entry point** as the
CPU baseline, so the two figures are directly comparable.

| Measure | CPU baseline | GPU (`cuda:0`) | Ratio |
|---|---:|---:|---:|
| Device | 8-thread CPU | RTX 4060 Laptop | — |
| Gate load (one-off) | 67.1 s | 15.8 s | **4.2×** |
| Wall clock, 4 cases | 590.5 s | **43.8 s** | **13.5×** |
| **Seconds per case (4 cases)** | **147.63** | **10.96** | **13.47×** |
| Ingest per case | 176,562 ms | 11,947 ms | **14.8×** |
| **Projected 500 cases** | **20.5 h** | ~~1.52 h~~ **1.90 h actual** | **10.78× actual** |
| CUDA peak allocated | n/a | 2.297 GiB | — |

The 4-case figures in this table are what produced the 13.47× ratio. **The full
run delivered 10.78×**, because the 4-case slice was an optimistic sample —
see §10. The 13.47× number is retained here only as the record of what was
measured at that point, not as the speedup to cite.

Single-encode latency, batch = 1 (GPU vs the CPU curve in §5):

| Input chars | CPU ms | GPU ms | Ratio |
|---:|---:|---:|---:|
| 50 | 167 | 17.3 | 9.7× |
| 200 | ~420 | 16.5 | ~25× |
| 800 | ~1,570 | 23.6 | ~67× |
| 3,200 | ~22,092 (@4,096) | 54.3 | ~400× |

The GPU is **flat below ~400 chars** (~17 ms, launch-bound) and only then grows.
This is why the *end-to-end* speedup (13.5×) is far below the peak per-encode
speedup: the real workload is dominated by many short turns, where the win is
bounded by kernel-launch overhead, not by FLOPs.

### Batching still does not help

| Configuration (8 real turns) | GPU ms |
|---|---:|
| Sequential, one at a time | 414.1 |
| `batch_size=8` | 463.9 |
| `batch_size=16` | 381.9 |
| `batch_size=32` | 385.1 |

The best batched figure is **1.075×** over sequential — consistent with the CPU
finding in §5. Batching is not the lever; the device is.

### GPU does not change the decisions

A speedup is only usable if it does not change what the gate stores. cuBLAS does
not accumulate in the same order as the CPU kernel, so the scores cannot be
expected to be bit-identical. The property that matters was measured directly:
the **same 8 probe texts** were scored on both devices with `cache_size=0`.

| Check | Result |
|---|---|
| Accept/skip/review action, all 8 probes | **identical** |
| `reason_code`, all 8 probes | **identical** |
| Max absolute score difference | **1.08 × 10⁻⁷** (float32 epsilon) |

So the GPU run is decision-equivalent; quality metrics do not need
re-baselining, and a GPU number may be compared against the CPU baseline.

> Caveat, stated plainly: this equivalence check covers 8 probes, not the
> 246,738 turns of the full corpus. A probe near a decision threshold could in
> principle flip on a 1e-7 difference. The honest control is to re-run the
> micro-suite and compare gate decisions, which has not yet been done at corpus
> scale.

### Memory

| Item | Value |
|---|---:|
| Model weights, fp16 | ≈1.2 GB |
| Worst attention tensor, batch 8 × seq 2048 (16 heads, fp32) | ≈1.0 GiB |
| OOM observed during 1,564-turn semantic run | **none** |
| Free RAM during measurement | 4.42 GB (other processes resident) |

The memory risk is the **padding-to-longest** path (a batch containing a long
turn inflates all rows), not the model itself.

## 8. What this report does and does not establish

**Establishes** (E4-scope, this machine, this configuration):

- the ingest/retrieval cost split is ~99.99% / ~0.008%;
- the per-case wall clock for the semantic gate is 147.6 s on CPU, hence 20.5 h for 500;
- the `max_seq_length` pin and the encode-batch parameter are **both inert** on
  the call path, with the specific mechanism and tensor shapes shown;
- two plausible optimisations are slower, measured;
- **the full 500-case GPU run completed**: 500/500 rows, 0 errors, **6,850 s =
  1.90 h, 13.70 s/case**, end-to-end speedup **10.78×** against the CPU
  projection;
- **gold evidence-turn formation coverage is 739/896 = 82.48%** at full corpus
  scale (offline micro-suite: 84.82%);
- **GPU and CPU agree on every gate decision** over 8 probes, with scores
  differing only at float32 epsilon.

**Does not establish:**

- **any LongMemEval quality metric.** The reader stage is `NOT_EVALUATED`, QA
  generation and the official judge are `NOT_COMPLETED`, and abstention is
  `NOT_EVALUATED`. The `retrieval` block is a memory-level ranking summary.
- multi-process throughput (projected, not measured);
- that the semantic gate improves *final answer* quality — only that it raises
  evidence-turn formation;
- decision equivalence at full corpus scale (only 8 probes were compared);
- a per-stage *share* breakdown of the wall clock: the profile's per-stage timers
  overlap (§3a) and must not be summed;
- a per-turn linear cost model: turn count explains almost none of the
  per-case variance (§10).

## 8a. Run status and interrupt semantics

The 500-case GPU run (`--gate semantic --device cuda`, all 500 cases) ran
`2026-09-21 22:45:34` → `2026-09-22 00:40:15` local as PID 38384 against
`.venv-gpu\Scripts\python.exe`, and **completed successfully**.

Recorded here because it governed how the run had to be handled: **the run
could not have been resumed or partially recovered.** Verified by inspection,
not assumed:

| Property | Finding |
|---|---|
| Artifact write | single `OUT.write_text(...)` **after** the runner returns |
| Per-case checkpoint | **none** in `longmemeval_benchmark.py` or the driver |
| Progress file | written once before the run starts, never updated per case |
| In-case store | `MemoryService(db_path=":memory:")`, closed in a `finally` block per case |
| Temp files | `%TEMP%\divetmp` and `%TEMP%\LongMemEval-*` hold only unrelated pytest fixtures (17:40) |

`_evaluate_case` is deliberately case-isolated and entirely in memory, so there
is **no intermediate state on disk to salvage**. Terminating the process before
it returned would have discarded all completed cases; any interruption would
have had to be written up as "the run was abandoned", never as a partial result.
This is why the process was left undisturbed for the full 1.90 h.

## 10. The projection was 25% too optimistic — and my first explanation was wrong

The 1.52 h figure came from a 4-case measurement extrapolated to 500. The real
figure is 1.90 h: **+25.0%**, or 13.70 s/case against 10.96 s/case. Recording
the miss matters more than the number, so here is the diagnosis.

### The hypothesis I formed first, and why it is false

My initial explanation was: *the first 4 cases are unrepresentative because
LongMemEval turn counts grow through the file, and short turns sit in the GPU's
flat launch-bound region, so later cases are proportionally more expensive.*

**The data rejects this.** Turn counts do not grow across the dataset:

| Position | Turns | Ingest ms |
|---|---:|---:|
| case 0 | 550 | 13,851 |
| case 125 | 515 | 11,568 |
| case 250 | 489 | 11,123 |
| case 375 | 521 | 12,419 |
| case 450 | 454 | 17,563 |
| case 475 | 474 | 17,985 |
| case 495 | 513 | 20,264 |
| case 499 | 542 | 18,308 |

Turn counts are **flat** (mean 493.5, min 396, max 616, median 491); the
first-4 mean is 545 and the last-4 mean is 508.5 — the opening cases actually
have *more* turns, not fewer. So the "turn counts grow" premise is simply wrong.

### What the data does say

Regressing per-case `ingest_ms` on `nonempty_turns` over all 500 cases:

| Quantity | Value |
|---|---:|
| slope | 18.18 ms/turn |
| intercept | 4,706 ms |
| **R²** | **0.0299** |

Turn count explains **3.0%** of the per-case latency variance. A linear
per-turn cost model is therefore **not** supported — the ~12 s/case cannot be
written as "turns × cost/turn".

The residual pattern is visible in the table above: the first ~75% of the
dataset runs at 11–14 s/case, then from roughly case 450 onward it climbs to
17–20 s/case **while turn counts stay flat**. That is a sharp late-stage
regime change, not a gradual trend.

Its most likely cause is **the gate's bounded FIFO vector cache saturating.**
`cache_size=1024` is shared across the entire run and the corpus is 246,738
turns, so in the early phase every `warm(batch)` call still finds its texts
resident, and later — once the working set exceeds 1,024 entries — each block
misses and re-encodes. This would produce exactly the observed shape: a flat
opening regime followed by a step up in per-case cost at constant turn count.

> **Status of that explanation: hypothesis, not measurement.** The artifact does
> not record cache hit/miss counters, so I cannot confirm it from this run
> alone. It is stated as the leading candidate because it is the only mechanism
> I have identified that explains a flat-turn-count latency step, and it is
> falsifiable: re-run a slice of the cases from the 450+ region in isolation and
> compare against the same cases with `cache_size=0` or a much larger cache. If
> the late-run penalty disappears, the cache is confirmed as the cause.

### What is established regardless of the cause

- the **measured** cost of a full run is **1.90 h**, and 1.52 h must not be
  quoted again;
- a 4-case opening slice **underestimates** the full-corpus per-case cost by 25%;
- per-case latency is **not** predictable from turn count (R² = 0.03);
- therefore no future projection should be built from a leading slice or from a
  per-turn constant.

## 9. Raw artifacts

| Artifact | Path |
|---|---|
| **Full 500-case GPU run (the real result)** | **`eval/reports/longmemeval-s-retrieval-gpu.json`** (1.26 MB, 500 rows) |
| Run progress log | `_run500_progress.txt` |
| Run stdout | `_run500.log` |
| Ingest breakdown, v1 gate | `eval/reports/longmemeval-profile-v1.json` |
| Ingest breakdown, semantic gate | `ops/longmemeval_profile.py` output, `_ingest_breakdown_semantic.json` |
| Encoder call profile | `_gate_call_profile.json` |
| Attention/padding audit | `_pad_audit.txt` |
| Runner throughput, CPU | `_runner_throughput.txt` |
| **Runner throughput, GPU** | `_gpu_speedup.json`, `_gpu_run3.txt` |
| **GPU/CPU decision equivalence** | `_gate_equiv_cpu.json`, `_gate_equiv_gpu.json`, `_gate_equiv_report.txt` |
| Projection arithmetic | `_projection.json` |
| **Projection-vs-actual analysis** | `_analysis500.txt` |

Reproduce the full run (GPU) — offline flags are required because
`transformers` probes the Hub for a PEFT adapter even when the snapshot is fully
cached, and the proxy answers `502`:

```powershell
$repo = "D.I.V.E.Program_Sanofi"
$env:HF_HOME = "$repo\eval\external\huggingface"
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
& "$repo\.venv-gpu\Scripts\python.exe" -m dive_memory.longmemeval_benchmark `
      eval/external/longmemeval/longmemeval_s_cleaned.json `
      --output eval/reports/longmemeval-s-retrieval-gpu.json `
      --gate semantic --device cuda
```

Expect **≈1.9 h** wall clock and **500 rows / 0 errors**. A result materially
faster than that from the same command would indicate a configuration
difference (most likely `cache_size`), not a speedup.

Reproduce (CPU profile):

```powershell
$py = "D.I.V.E.Program_Sanofi\.venv-models\Scripts\python.exe"
$env:HF_HOME = "D.I.V.E.Program_Sanofi\eval\external\huggingface"
& $py ops/longmemeval_profile.py eval/external/longmemeval/longmemeval_s_cleaned.json `
      --cases 5 --gate semantic --output eval/reports/longmemeval-profile-semantic.json
```

> Note: the same command with `--device cuda` under `.venv-gpu` produced
> `longmemeval-profile-semantic.json`. Remember §3a before quoting its
> `per_case_mean_ms` fields in any comparison.

