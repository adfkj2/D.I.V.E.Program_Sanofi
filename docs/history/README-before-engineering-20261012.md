# D.I.V.E.Program_Sanofi
首届 _SCU X Sanofi_ 菲凡极客项目 

2026-10-12审查补充：[规则审查](docs/oracle/39-compliance-audit-20261012.md)复核R40的451/500及500题缓存证据通过，本轮1108测试通过、9跳过、零新增模型调用；但公开评测集反复调参的通用性与手册F.1.4边界尚未关闭，官方成绩仍未评估，不能将缓存审计等同合规认证。[业务定位与工程交付计划](docs/24-engineering-delivery-plan.md)已形成，用户要求先规划，目前未开始产品重构或发布。

Benchmark 的逐轮目标、改动、失败、结果、费用和后续计划统一记录在[迭代总账](docs/oracle/ITERATION_LOG.md)，包含历史报告索引及后续记录模板。

2026-10-12目标完成：[R40最终报告与复核入口](docs/oracle/38-current-state-review-20261012.md)**451/500（90.2%）**，全部判分完成，可答423/470、不可答28/30；500题独立审计通过。对R39改善2/退步0，对R36累计净增12。1108工程检查通过9跳过；保守累计¥31.456754，规划余¥7.543246，低于既定¥39规划线。修复混合句事实漏记、建议类误拒及部分新旧状态误读；49道剩余错误完整保留。成绩为公开开发集上的非官方同家族judge自评，含精确缓存与新生成，非500题全新采样或盲测。

2026-10-11 R36全量完成：[时间拒答限定复核](docs/oracle/34-temporal-refusal-review-20261011.md)**439/500（87.8%）**，相对R33改善13/退步0，不可答28/30，500题独立审计通过；1002工程检查通过9跳过。剩29误拒、31误答、1格式失败，超过90%还差12题。本轮含筛选¥0.664458，累计¥29.771042，规划余¥9.228958。成绩为精确缓存与新增阶段组合的非官方自评。

2026-10-11继续迭代：[R34逐题归因](docs/oracle/32-r33-error-diagnosis-20261011.md)保存全部74错题的来源、可见原句和各阶段输出；R35单纯提高复核推理额度降至73/80，不推广。[R36时间拒答限定复核](docs/oracle/34-temporal-refusal-review-20261011.md)76/80，改善2无退步、不可答28/30，独立审计通过，开始500题验证。累计¥29.256442，规划余¥9.743558；最新确认全量仍426/500。

2026-10-11 R33全量完成：[显式原文受限优先及完整验证](docs/oracle/31-explicit-document-priority-20261010.md)**426/500（85.2%）**，可答398/470、不可答28/30，对R26改善4/退步0（3道内容改善、1道历史执行失败本轮成功）。500题/2518引用/全部判分独立审计通过；结果含缓存复现与新生成，非官方自评。含筛选本轮14请求、¥0.112416，累计¥29.016310，规划余¥9.983690。超过90%须至少451/500，尚差25题。

2026-10-10 R31完成：[语境阅读实验](docs/oracle/29-contextual-reading-20261009.md)72/80（可答45/50、不可答27/30），对R27改善1/退步2，不推广。80题、273引用及全部判分审计通过；含1道中断失败计错，原32题和费用预留保留。新增101请求、保守¥1.412200，共享累计¥28.903894，规划余¥10.096106。R32长文形成修复通过961项工程检查，但免费对照显示上下文未变，未付费；R33继续验证在原文档名额内优先显示已命中的显式记忆原文。全量仍422/500（84.4%），目标至少451/500。

2026-10-09 R30完成：[显式必要前提账本](docs/oracle/28-premise-ledger-20261009.md)固定80题70/80，对R27改善1/退步4，未过门槛、不推广。80题、248引用和判分独立缓存审计通过；新增95请求、¥0.739924。用户增加¥10规划额度后累计授权¥40、规划线¥39，累计已用¥27.491694，规划余¥11.508306。全量仍422/500（84.4%），尚未达标；从R27阅读主干继续迭代。

2026-10-09更新：[R27](docs/oracle/25-precise-modality-terms-20261008.md)减少意向词误挡，筛选71→73/80（2改善0退步）；[R28](docs/oracle/26-statement-provenance-20261009.md)增加来源日期和角色后仍73/80，对R27改善1/退步1，不可答28→27/30，因此不推广、不追加全量。唯一新增正确存在冲突答案被judge判对的争议；13题出现局部文档预算挤占原句，均已记录。775工程检查通过、9跳过，阅读/引用/判分/费用独立审计通过。R28新增¥1.836492，共享累计¥25.287270/¥30，规划余¥3.712730。完整确认成绩仍 **422/500（84.4%）**，90%未达到；下一轮重点为原句装包、事件身份匹配和判分校准。

2026-10-08 R26：[完整复测与逐题审计](docs/oracle/24-asserted-detail-anchors-20261008.md)完成，**422/500（84.4%）**，相对397改善29题、退步4题，净增25（5.0个百分点）；不可答28/30，含1道本地写入失败计错。274题直接复用，226题新输入执行，并非全500题新采样；仍为公开开发数据上的非官方自评，90%还需净增28题。原文细节保留、检索上限60与新增锚点状态约束作为下一轮显式实验基线，通用默认不变。

500题缓存/引用及判分来源独立审计通过，2502引用核验、无证据预算超限。新增593请求、保守费用¥6.236672，累计¥23.408646/¥30，规划剩余¥5.591354并另保留¥1结算余量。全部改善、退步、中断恢复、计数纠正和剩余78道错误均已记录。下一轮优先关键原句保留、相对时间关联与必要前提误拒。

2026-10-07 R25：[保持证据预算扩大检索池](docs/oracle/23-deeper-retrieval-20261007.md)完成。固定80题筛选由R21的68、R24的69提高到70（对R21为3改善/1退步），不可答保持28/30；仍未达净增3题门槛，不推广、不追加全量。两轮参考GitHub方案的迭代新增费用¥1.331942，共享累计¥17.171974/¥20。当前完整最佳仍397/500缓存回放，90%未达到，默认配置不变。两轮结果均通过独立缓存审计；下一步重点是计划/执行和数量快照/增量区分。

2026-10-07 R24：[GitHub方案核对与原文证据保留](docs/oracle/22-source-retention-github-20261007.md)。参考Mem0、Hindsight与LongMemEval的原文来源保留思路，增加默认关闭的人称细节锚点；未移植测试内容特定规则。固定80题筛选68→69（2改善、1退步），不可答保持28/30；未达净增3题门槛，不推广。732项工程检查通过、9项跳过，80题缓存复现及273处引用通过。新增保守费用¥0.524452，累计¥16.364484/¥20；最佳仍397/500缓存回放，90%未达到。

2026-10-07 后续更新：[必要前提复核的两轮筛选](docs/oracle/21-necessary-premise-review-20261007.md) 已完成。相对同题68/80，关闭思考为67/80、开启思考为68/80，均因不可答退步未推广，未追加全量。最佳仍为397/500缓存回放（完整真实运行395/500），通用默认不变。两轮新增保守费用¥0.759040，累计¥15.840032/¥20；706项工程检查通过、9项跳过。103道错误及9道退步已审计，下一轮优先修复必要事实保留。

2026-10-07 更新：[完整续跑与准确率审计](docs/oracle/20-full-resume-audit-20261006.md) 已完成。500 题非官方自评从 366/500（73.2%）提升到 **395/500（79.0%）**；新增格式容错策略的全量缓存回放为 **397/500（79.4%）**，相对原基线 40 题改善、9 题退步，不可答保持 28/30。回放零新增 API 调用，不是独立新一轮模型采样。已修复续跑漏报历史失败；690 项测试通过、9 项跳过。累计保守费用 ¥15.08/¥20；90% 尚差 53 题，通用默认不变。

2026-10-04 更新：[计算、思考额度与证据复核实验](docs/oracle/19-calculation-and-reasoning-20261004.md) 已完成。独立证据复核在相同 80 题开发筛选集上达到 **68/80（85%）**，基线 65/80；可答 40/50、不可答 28/30，3 题改善、0 题退步，通过全量候选门槛。完整已验证成绩仍为 **366/500（73.2%，非官方自评）**，90% 尚未达到。累计保守费用 ¥8.27；全量复测预计另需 ¥8.51，含波动余量约 ¥10.64，超过原定累计 ¥10 预算，故仅完成免费准备，等待提高预算的授权。候选未设为通用默认。

2026-09-29 更新：[冲击高分的实验记录](docs/oracle/18-road-to-90.md)：叙事窗口＋来源多样化＋top-30/6,000 证据预算＋编号引用阅读器，完整 Oracle 非官方自评 **366/500（73.2%）**，上一版为 311/500；73 题改善、18 题退步，不可答 28/30。此轮调整了证据预算和阅读策略，不能将收益全归因于记忆算法。500 条提交及引用上下文均已核验。随后两组 80 题实验（low thinking、混合句＋文档展开）均为 63/80，低于同题旧版 65/80，不推广、未追加全量。当前回归 588 项通过、9 项跳过；90% 目标尚未达到，通用默认不变。

2026-09-28 更新：[结构性改进及完整复测](docs/oracle/17-structural-improvement-results-20260928.md) 已完成。选择性事件原句保留＋本地 BGE 语义检索，在相同 reader 和 1,500 证据预算下将 Oracle 非官方自评从 289/500 提高到 **311/500（62.2%）**；跨会话净增 12 题，不可答保持 23/30。新方案作为推荐实验配置，通用默认不变；补查和扩大预算未被采用。本轮余额观察消耗 ¥2.81，538 项测试通过、9 项跳过，付费调用已结束。

2026-09-27 更新：DeepSeek Flash 的 [500 题完整自评](docs/oracle/15-flash-full-test-20260927.md) 已完成，v3 为 289/500（57.8%，非官方）。随后实现事件时间推导规则和可选来源坐标，[两项改进的 80 题配对复测](docs/oracle/16-v4-improvement-results-20260927.md) 未达到推广门槛，继续保留 v3 默认；本轮新增余额观察消耗 ¥0.26，未追加 500 题付费运行。

2026-09-26 更新：受控文档补入、严格可见预算下的本地语义消融、reader 配对复现、可审阅热档案与关系诊断已实现。最新结果与使用边界见 [五步优化实现及实验报告](docs/oracle/13-implementation-results-20260926.md)。本轮零 API 调用，尚无新的真实答题正确率或官方成绩。

Long-term interactive AI memory system. Research and design documents live in
[`docs/`](docs/). The first local MVP implements an event-sourced memory
service with selective writing, provenance, temporal-aware projections,
lexical/structured retrieval, deterministic test vectors, deletion, and a
layered evaluation harness. Phase 3 design and evidence are in
[`docs/15-phase-3-architecture-review.md`](docs/15-phase-3-architecture-review.md),
[`docs/16-phase-3-design-options.md`](docs/16-phase-3-design-options.md), and
[`docs/17-phase-3-roadmap.md`](docs/17-phase-3-roadmap.md).

## Local development

```powershell
python -m pytest
```

The core package is dependency-free. The optional HTTP adapter can be enabled
with `pip install -e ".[api]"` and served with `uvicorn` after creating an app
from `dive_memory.api:create_app`.

## MVP status

The local MVP now covers event sourcing, atomic and retryable outbox projection,
selective/deferred extraction, persisted write decisions, provenance, version
chains and deterministic replay, bitemporal filters, profiles, entities and
traceable channel retrieval and configurable RRF, context packing, consolidation,
reinforcement/decay, recoverable and hard deletion, export, persistent user
controls, optional namespace authorization, strict candidate validation,
eight-way immutable memory transitions, generation-aware embedding cutover,
five evaluation baselines, and smoke/load benchmarks:

```powershell
$env:PYTHONPATH = "src"
python -m dive_memory.smoke
python -m dive_memory.benchmark --memories 10000 --queries 20
```

The API adapter and integration tests use the optional development environment:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[api,dev]"
.venv\Scripts\python -m pytest -q
```

Embeddings are supplied through a provider interface. The default is the
offline deterministic **test** provider and does not contribute semantic dense
scores. `OpenAICompatibleEmbeddingProvider` and the lazy local
`LocalSentenceTransformerEmbeddingProvider` return explicit model/revision and
degraded metadata; fallback vectors are never scored as semantic. The local
`EmbeddingLifecycle` supports staged, complete backfill, atomic cutover and
rollback. BGE-M3 is a provisional production candidate—not a benchmark-proven
default. A deployment must configure and validate a semantic provider.

Retrieval defaults to BM25 + predicate + temporal + entity/relation signals;
dense contributes only with a healthy semantic provider. Multi-hop is
default-off because the current user-hub topology has not shown adequate
evidence recall. The offline A–H output is committed at
[`eval/reports/p0-offline-latest.json`](eval/reports/p0-offline-latest.json),
with interpretation in
[`docs/benchmark/p0-offline-report.md`](docs/benchmark/p0-offline-report.md).
It is a six-case harness regression, not a quality leaderboard.

The SQLite adapter remains the local/default CI backend. `PostgresStore` is an
opt-in PostgreSQL + pgvector adapter for the online ingest, retrieve and
memory-delete path of the same domain service. Eight real
PostgreSQL integration tests now cover clean/idempotent migrations, concurrent
`SKIP LOCKED` claims, stale retry limits, projection rollback, concurrent
idempotency, namespace isolation, temporal transitions, hard-purge residue and
the exact pgvector cosine operator:

```powershell
.venv\Scripts\python -m pip install -e ".[postgres,dev]"
$env:DIVE_TEST_POSTGRES_DSN = "postgresql://dive:dive_test_password@127.0.0.1:55432/dive_test?connect_timeout=5"
.venv\Scripts\python -m pytest -q tests\integration\postgres --basetemp .pytest-postgres
```

The measured 10K dense-only exact baseline is ingest 38.62 memories/s and
retrieval p50/p95/p99 167.18/176.78/179.36 ms with 96-d synthetic vectors. It
is a plumbing/performance result, not semantic-model or ANN evidence. See
[`docs/benchmark/postgres-exact-10k-report.md`](docs/benchmark/postgres-exact-10k-report.md),
[`docs/adr/ADR-012-postgresql-pgvector.md`](docs/adr/ADR-012-postgresql-pgvector.md)
and [`ops/postgres/README.md`](ops/postgres/README.md).

The strict LongMemEval-cleaned adapter is implemented and tested against a
small synthetic fixture, and a full **retrieval-stage** run over all 500 official
`LongMemEval_S` cases is committed. That run completed with 0 case errors and 0
timeouts, and it is reported as a retrieval result only:

| Stage | Status |
|---|---|
| Official dataset adapter, 500 cases | `COMPLETED` |
| D.I.V.E formation + retrieval | `COMPLETED` (500/500 rows, 0 errors, 6,850 s = 1.90 h; formation coverage 739/896 = 82.48%) |
| Reader answer generation | `NOT COMPLETED` |
| Official GPT-4o judge | `NOT COMPLETED` |

**No official LongMemEval score is claimed.** See
[`docs/benchmark/longmemeval-methodology.md`](docs/benchmark/longmemeval-methodology.md)
for the evidence boundary and
[`docs/benchmark/longmemeval-report.md`](docs/benchmark/longmemeval-report.md)
for the measured numbers.

### Semantic write gate

The default write gate is the rule-based `utility-baseline-v1`. A calibrated
semantic gate (`semantic-utility-v2.1`, nearest-anchor margin scoring over real
`bge-m3` vectors) is implemented and raises gold evidence-turn formation
**offline at the gate** from 6.14% to ~84.82% (82.48% on the full corpus), and
reduces false accepts in the false-memory suite **from 12 to 0**. Both figures
are gate-level measurements, not end-to-end answer quality. See
[`docs/diagnosis-formation-gate-2026-09-21.md`](docs/diagnosis-formation-gate-2026-09-21.md)
and [`docs/fix-write-gate-semantic-2026-09-21.md`](docs/fix-write-gate-semantic-2026-09-21.md).

The gate encoder is the dominant cost of a full benchmark sweep, so the runner
exposes `--device`:

```powershell
$env:HF_HOME = "eval/external/huggingface"
$env:HF_HUB_OFFLINE = "1"; $env:TRANSFORMERS_OFFLINE = "1"   # required: see below
$py = ".venv-gpu\Scripts\python.exe"                          # torch +cu126
& $py -m dive_memory.longmemeval_benchmark eval/external/longmemeval/longmemeval_s_cleaned.json `
      --output eval/reports/longmemeval-s-retrieval-gpu.json --gate semantic --device cuda
```

Measured on this workstation, from the **completed full run** (500/500 cases):
the GPU finished in **6,850 s = 1.90 h at 13.70 s/case**, against a CPU
projection of 20.5 h — an end-to-end **10.78×**. An earlier 4-case sample gave
13.47× / 1.52 h; the full corpus showed that estimate to be **25% optimistic**,
so cite 1.90 h. GPU and CPU gate decisions are equivalent — the same probe set
yields identical accept/skip/review actions and reason codes, with scores
differing by at most 1.08e-7. Full profile and the projection-error analysis:
[`docs/benchmark/longmemeval-500case-profiling.md`](docs/benchmark/longmemeval-500case-profiling.md).

> Local sentence-transformer loading now resolves the pinned checkpoint's
> cached snapshot and passes `local_files_only=True`; a missing checkpoint
> fails locally instead of silently contacting the Hub. Setting
> `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` is still appropriate for other
> model tooling in this repository.

Still unverified: reader QA with the production tokenizer and the official
GPT-4o judge, the preregistered P4-P0-2 embedding comparison on the frozen
project corpus, PostgreSQL RLS/backup erasure, replay/reindex/job/event-delete/
export parity, and the ANN (HNSW/IVFFlat), mixed-workload and 1M experiments.
100K **exact** retrieval *is* measured (p50 156.12 ms) and concurrency is
partially characterized (branch A at 1/2/4/8 workers, 0 failed ops) — but that
concurrency run is unfinished, so it is not a completed result. The repository
must not be described as production-ready until those applicable release gates
pass.

**Phase 4 P0 results are consolidated in
[`docs/20-phase-4-results.md`](docs/20-phase-4-results.md)** — every experiment
with its status, artifact-contract compliance, Wilson intervals for the
false-memory rates, the failure/correction record, and the explicit list of what
Phase 4 does not establish.

Phase 4 is closed as an evaluation milestone. The closure set is
[`docs/21-phase-4-architecture-review.md`](docs/21-phase-4-architecture-review.md)
(14 architecture questions answered from executed evidence),
[`docs/22-phase-4-production-gap-analysis.md`](docs/22-phase-4-production-gap-analysis.md)
(18 readiness domains) and
[`docs/23-phase-4-readiness-checklist.md`](docs/23-phase-4-readiness-checklist.md)
(70 item-level verdicts). **The readiness verdict is `NOT READY`** — 12 of 70
items earn `PASS`, and Phase 4 completion does not imply production readiness.
`docs/22` §5 lists the statements the current evidence does not support.

The HTTP adapter uses strict request schemas and idempotency keys for event and
correction writes. `StaticTokenAuthorizer` provides a service-token namespace
boundary; deployments can supply an IAM-backed implementation of the same
protocol.

## Review status

`docs/13-code-review-and-fixes.md` records a full review of this MVP: eleven
defects (silent supersession of unrelated facts, lexicographic `as_of`
comparison, false abstention on natural questions, unretrievable CJK runs,
a `decay` timezone crash, dry-run side effects, a degenerate bm25 mapping, a
missing transaction rollback, and a cross-namespace correction hole), each with
the evidence that reproduced it and the test that now pins it.

Two helpers back those fixes: `temporal.py` normalises wall-clock input such as
`2025` / `2025-06` / `2025年6月2日` before comparison, and `lexical.py` indexes
CJK character bigrams so memories written without punctuation stay retrievable.
`SQLiteStore.reindex_lexical()` rebuilds the FTS projection, which a database
written before that change needs once.

On Windows hosts whose `%LOCALAPPDATA%\Temp` is not writable, point pytest at a
project-local directory:

```powershell
.venv\Scripts\python -m pytest -q --basetemp .pytest-tmp
```

## Oracle 端到端问答流水线（正式 Benchmark）

严格区分两套评测：`LongMemEval_S-cleaned` 是**记忆形成/检索阶段**实验，而正式的
40 分 Benchmark 是 **`longmemeval_oracle.json`（500 题）的端到端问答**，主指标为
项目组统一 `evaluate_qa.py` + LLM judge 打出的 **Overall Accuracy**
（`Benchmark = Overall Accuracy × 40`）。协议与区别见
[`docs/oracle/00-oracle-protocol.md`](docs/oracle/00-oracle-protocol.md)。

### 唯一推荐路径（新环境可执行）

```powershell
# 1. 虚拟环境
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,models]"
# semantic gate 首次使用还需取得固定版本 BAAI/bge-m3 权重；
# 本机有 CUDA 时可把以下 --device cpu 改为 --device cuda。

# 2. 测试（跳过项与本机可选依赖有关；以实际输出为准）
.venv\Scripts\python -m pytest -q

# 3. synthetic smoke（确定性 reader，纯探针，不产生官方分数）
$env:PYTHONPATH="src"
.venv\Scripts\python -m dive_memory.oracle.runner `
  --dataset eval/external/longmemeval/longmemeval_oracle.json `
  --output eval/reports/oracle-smoke --max-cases 3 --allow-partial

# 4. 配置真实 reader（将 API key 安全注入环境；不要写入仓库或命令历史）
.venv\Scripts\python -m dive_memory.oracle.runner `
  --dataset eval/external/longmemeval/longmemeval_oracle.json `
  --output eval/reports/oracle-20case --max-cases 20 --allow-partial `
  --reader openai --endpoint "<chat/completions URL>" --model "<model>" `
  --api-key-env DIVE_READER_API_KEY --gate semantic --device cpu `
  --document-admission all --scoring-policy balanced --reader-policy evidence_first

# DeepSeek 接入（非官方模拟；端点与当前模型名以供应商文档为准）
.venv\Scripts\python -m dive_memory.oracle.runner `
  --dataset eval/external/longmemeval/longmemeval_oracle.json `
  --output eval/reports/oracle-v3-pilot-20 --max-cases 20 --allow-partial `
  --reader openai --endpoint "https://api.deepseek.com/chat/completions" `
  --model deepseek-flash --api-key-env DIVE_READER_API_KEY --provider-name deepseek `
  --extra-body '{"thinking":{"type":"disabled"}}' `
  --gate semantic --device cpu --document-admission all --scoring-policy balanced `
  --reader-policy evidence_first

# 5. 50-case 分层 pilot / 500 题全量：保留同一模型及上述配置，
#    只改 --stratified 50 或去掉题量限制；使用新的输出目录与 clean commit。
.venv\Scripts\python -m dive_memory.oracle.runner `
  --dataset eval/external/longmemeval/longmemeval_oracle.json `
  --output eval/reports/oracle-v3-pilot-50 --stratified 50 --allow-partial `
  --reader openai --endpoint "https://api.deepseek.com/chat/completions" `
  --model deepseek-flash --api-key-env DIVE_READER_API_KEY --provider-name deepseek `
  --extra-body '{"thinking":{"type":"disabled"}}' `
  --gate semantic --device cpu --document-admission all --scoring-policy balanced `
  --reader-policy evidence_first

# 6. 校验 JSONL（partial smoke 不传 --dataset；500 题终版必须传）
.venv\Scripts\python -m dive_memory.oracle.validator `
  eval/reports/oracle-smoke/submission.jsonl
.venv\Scripts\python -m dive_memory.oracle.validator `
  eval/reports/oracle-final/submission.jsonl `
  --dataset eval/external/longmemeval/longmemeval_oracle.json

# 7. 完整 500 题运行完成后做本地布线检查；exact 不是语义判分或官方分
.venv\Scripts\python -m dive_memory.oracle.evaluate `
  --dataset eval/external/longmemeval/longmemeval_oracle.json `
  --submission eval/reports/oracle-final/submission.jsonl `
  --run-dir eval/reports/oracle-final --output eval/reports/oracle-final/evaluation-exact.json `
  --judge exact
```

### 断点续跑与可复现性

长文档片段检索是**实验性、默认关闭**的选项：`--document-selection query_excerpt`
和 `--document-chunk-search all` 配合 `--scoring-policy balanced`。它保留原始文档，
只把带字符位置的相关片段放入阅读包。新运行也修复了 v2/v3 reader 重新渲染全文
导致的证据块超预算问题，因此不能把旧 D 臂与新运行当成仅改变一个开关的配对
准确率实验。实施细节、381/500 的历史预算审计及零 API 消融设计见
[`docs/oracle/11-document-fragment-and-budget-fix.md`](docs/oracle/11-document-fragment-and-budget-fix.md)。

- 每题原子写入 `traces/<id>.json`；`--resume` 在 dataset/config/gate/reader/selection
  任一变更时**拒绝继续**，防止合并不同实验。
- `manifest.json` 记录 dataset SHA-256、配置 hash、reader fingerprint、git commit/dirty、
  环境与 artifact 哈希。**`dirty: true` 的运行不得标为正式结果。**

### 产物定位

`<output_dir>/` 下的 `manifest.json`、`summary.json`、`submission.jsonl`、
`cases.jsonl`、`failures.json`、`traces/*.json`。详见
[`docs/oracle/02-runner-architecture.md`](docs/oracle/02-runner-architecture.md)、
[`docs/oracle/01-leakage-design.md`](docs/oracle/01-leakage-design.md)（防泄漏）、
[`docs/oracle/04-failure-taxonomy.md`](docs/oracle/04-failure-taxonomy.md)（失败分类）、
[`docs/oracle/05-submission-checklist.md`](docs/oracle/05-submission-checklist.md)（提交清单）、
[`docs/oracle/07-deepseek-reader.md`](docs/oracle/07-deepseek-reader.md)（DeepSeek 接入）。

**当前状态：已有历史 A/B/D 真实 DeepSeek reader 的 500 题提交及非官方评判；D 臂
500 行合法，但运行状态为 PARTIAL（7 个 reader 无效输出），且 manifest 为 dirty。
这些都是旧版 reader v1/v2 与旧时间截止逻辑下的产物，不是当前 v3 的最终复跑。
旧 exact-v1 口径有误，修正版离线重判与前沿选型见
[`docs/oracle/09-frontier-decision-and-no-cost-verification.md`](docs/oracle/09-frontier-decision-and-no-cost-verification.md)。
正式 Overall Accuracy 仍为 NOT_EVALUATED。**
生产级高可用与大规模集群不在本期主线，现有 production-readiness 内容作附录保留。
