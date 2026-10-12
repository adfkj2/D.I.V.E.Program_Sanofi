# Oracle 防泄漏设计

## 目标

`answer`、gold evidence、`question_type`、参考答案位置等评估字段**只能进入 evaluator**。
extractor、write gate、memory store、retriever、context packer、reader prompt、trace、
submission JSONL 均不得接触参考答案或 gold label。

本边界是**代码级隔离 + 哨兵测试**证明的，不是口头约定。

## 结构

三份 allow-list 常量在 `oracle/contracts.py` 中定义，为唯一事实源：

| 常量 | 内容 | 作用 |
|---|---|---|
| `WRITER_VISIBLE_FIELDS` | `namespace/session_id/turn_id/role/content/occurred_at`（6 项） | `system_input()` 只能产出这 6 键，`assert_writer_visible` 逐行断言 |
| `EVALUATOR_ONLY_FIELDS` | `answer/answer_session_ids/has_answer/question_type/evidence_refs/…` 等 | `find_evaluator_only_fields` 递归扫描任一 payload 找泄漏 |
| `SUBMISSION_FIELDS` | `question_id/hypothesis`（2 项） | 提交行恰好两字段 |

## 关键机制

1. **writer 侧**：`LongMemEvalCase.system_input()` 从 turn 构造 6 键行，从**结构上**
   不含任何 gold 字段；`pipeline.run_case` 在 ingest 前 `assert_writer_visible(rows)`，
   未来 adapter 一旦开始转发 gold，会在此处 fail-closed，而非在 500 题运行中静默泄漏。

2. **reader prompt 侧**：`build_reader_prompt()` 只接收 `OracleReaderRequest`
   （question、question_date、as_of、检索到的有限 context），**根本不接收 case 对象**，
   因此无法转发 gold。`assert_prompt_is_clean` 对顶层键与 `output_schema` 键双重校验。

3. **trace 侧**：`pipeline.run_case` 在构造 reader request 后再次
   `find_evaluator_only_fields` 扫描 prompt，命中即 FAILED(READING/LeakageError)。

4. **submission 侧**：`runner._submission_lines` 断言每行 `set(line) == SUBMISSION_FIELDS`，
   否则 `AssertionError`，保证 trace 字段永不泄漏进计分文件。

## 哨兵测试

`tests/test_oracle_pipeline.py::TestLeakageBoundary` 用故意携带 gold sentinel 的 fixture
（`GOLD_SENTINEL = "ZQXGOLDSENTINEL7011"`）证明：

- `system_input()` 输出恰为 allow-list 6 键；
- 携带 `answer` 的行触发 `LeakageError`；
- 携带额外键的行触发 `LeakageError`；
- 嵌套 payload 中的 gold 字段被 `find_evaluator_only_fields` 检出；
- 端到端 trace 与 submission 序列化后**不含 sentinel**；
- 提交行恰为两字段。

## 隔离

每 case 使用独立 `MemoryService`（内存 store）+ 派生 namespace
`longmemeval:<question_id>`。`run_case` 在 finally 中关闭 store，两题之间无状态残留。
`tests/test_oracle_pipeline.py::TestPipeline::test_namespace_isolation_between_cases`
与 `test_per_case_service_is_structurally_disposable` 证明。
