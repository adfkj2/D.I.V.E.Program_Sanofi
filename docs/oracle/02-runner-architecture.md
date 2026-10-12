# Oracle Runner 架构与数据流

## 模块职责

```
src/dive_memory/oracle/
├── contracts.py    # 边界 allow-list、失败分类、拒答模板、异常类型
├── loader.py       # 严格 schema 校验、500 题完整性、分层抽样
├── reader.py       # provider-neutral reader 契约 + 确定性 fake
├── providers.py    # OpenAI 兼容 HTTP 传输 + Scripted 传输（与领域层解耦）
├── pipeline.py     # 单题 ingest -> retrieve -> pack -> read
├── runner.py       # 原子落盘、断点续跑、manifest、submission JSONL、CLI
├── validator.py    # 独立 JSONL 校验（不共享 runner 代码）
└── evaluate.py     # 自测准确率、错误归因、延迟、token、成本
```

## 单题数据流（`pipeline.run_case`）

```
LongMemEvalCase
  ├─ system_input() ──► assert_writer_visible(6 键)   [LOAD 失败守卫]
  ├─ chronological_turns（真实时间排序，非发布顺序）
  │     └─ ingest_case：按时间序 ingest 每 turn
  │           └─ warm(gate) 分块预热 → service.ingest(...)
  │                 └─ 记录 turn_events / turn_memory_counts  [供归因]
  ├─ service.retrieve(namespace, question, as_of=question_date)
  │     └─ RetrievalConfig.full(name="oracle-default")
  ├─ pack_context(items, token_budget)  [记录 decisions / omitted]
  ├─ build_reader_prompt(request) ──► assert_prompt_is_clean
  │     └─ find_evaluator_only_fields(prompt)  [READING/LeakageError 守卫]
  └─ reader_outcome(reader, request) ──► ReaderOutcome
        └─ resolved_hypothesis（ANSWER 保留原文；否则规范拒答模板）
```

每个阶段独立计时（`ingest/retrieve/pack/read/total`），任一阶段异常都返回带
`failure.stage` 的完整 record（不丢失此前结果）。

## 耐久性模型（`runner.py`）

- 每完成一题，`os.replace` 原子写入 `traces/<id>.json`（同目录临时文件，Windows/POSIX 均原子）；
- `submission.jsonl` 每题后从 traces 原子重建，永不落后；
- `manifest.json` 在首题前写出，中断运行仍携带 dataset hash / config hash / reader spec；
- `--resume` 在 dataset / config / gate / reader / selection 任一变更时拒绝继续
  （`resume_key` 全量比对），防止把两个不同实验合并进一份 submission。

## 可复现性元数据（manifest）

记录：schema_version、run_id、status、起止时间与时长、dataset（name/version/source/
license/path/sha256/size/cases/expected/abstention/type 分布）、configuration + canonical
hash、resume_key、reader spec + fingerprint、versions（adapter/extractor/gate/resolver/
embedding/reader/prompt）、environment（python/platform/machine/cpu/torch/cuda）、
git（commit/branch/dirty）、seed、selection、command、artifact 哈希清单、
known_limitations。

**`dirty: true` 的运行不得标为正式结果**：manifest 会如实记录，但正式 500 题结果
必须来自 clean commit。

## 运行产物目录

```
<output_dir>/
├── manifest.json
├── summary.json
├── submission.jsonl
├── cases.jsonl
├── failures.json
└── traces/<question_id>.json   # 逐题原始记录
```
