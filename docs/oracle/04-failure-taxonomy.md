# 失败分类方法

## 阶段枚举（`contracts.Stage`）

`LOAD` → `FORMATION` → `RETRIEVAL` → `PACKING` → `READING` → `WRITING`，外加 `UNKNOWN`。

任一阶段异常，`pipeline.run_case` 返回带 `failure.stage` / `error_type` / `message` /
`retryable` 的完整 record；`retryable = isinstance(exc, (TimeoutError, ConnectionError,
OSError))`。失败**不丢失此前结果**。

## 三阶段错误归因（`evaluate.attribute`）

对一道可回答（非拒答）题，用内部 trace 判定错误归谁：

| 归因 | 判定条件 |
|---|---|
| `FORMATION` | gold 证据 turn 未产出任何 memory（`turn_memory_counts[ref] == 0`） |
| `RETRIEVAL` | gold turn 已形成 memory，但返回结果的 `source_refs` 不回溯到 gold event |
| `READING` | gold 证据被检索到，但答案仍被判错 |
| `CORRECT` | 判对 |

拒答题（`should_abstain`）或没有 gold 证据的题：`CORRECT`（判对）或 `READING`（判错）。

## reader 状态与拒答的区分（`contracts.ReaderStatus`）

| 状态 | 含义 | 是否算基础设施失败 |
|---|---|---|
| `ANSWER` | 有引用、可验证的回答 | 否 |
| `ABSTAIN` | 语义拒答（证据不足） | 否 |
| `TIMEOUT` | reader 超时 | **是** |
| `ERROR` | 传输/调用失败 | **是** |
| `INVALID` | 返回格式非法 | **是** |

**"检索为空" ≠ 语义拒答**：检索空是 `NO_RETRIEVAL_CANDIDATES` 的 ABSTAIN；
reader 自己判断证据不足是另一回事；超时/错误/格式非法是三种不同的基础设施失败，
绝不能与真正的拒答混淆。`HypothesisOrigin`（`READER` / `READER_ABSTENTION` /
`ERROR_FALLBACK`）让这种归因在 trace 中显式可查。

## 失败产物

- `failures.json`：所有带 `failure` 的题，含 stage / error_type / message。
- `summary.json`：`failure_stage_counts` 聚合各阶段失败数、`timeouts`、
  `reader_status_counts`、`hypothesis_origin_counts`。
- `on_error=abstain` 时，失败题在 submission 中写规范拒答模板并计数
  `error_fallback_lines`；`on_error=skip` 时省略该行。
