# 11 API Design

API 与模型供应商解耦；所有请求带 `tenant_id`、`user_id`、`agent_id` 和 idempotency key。响应不把内部 embedding 或 prompt 暴露给调用方。

| Method | Path | 作用 |
|---|---|---|
| POST | `/v1/events` | 追加一批原始 message/tool/resource 事件；返回 event ids 和 processing status |
| POST | `/v1/memories/candidates` | 受信任调用方提交候选，仍需 gate |
| POST | `/v1/retrieve` | query、scope、intent、as_of、token_budget；返回 memory、evidence、scores、degraded |
| POST | `/v1/search` | 调试/管理用候选搜索，默认不直接作为 prompt context |
| POST | `/v1/jobs/consolidate` | 指定 session/time range，支持 dry_run |
| POST | `/v1/jobs/reindex` | 从 projections 重建指定索引 |
| GET | `/v1/memories/{id}` | 查看当前版本和 provenance |
| PATCH | `/v1/memories/{id}` | 用户纠正；生成新 version，不覆盖历史 |
| DELETE | `/v1/memories/{id}` | 写 tombstone 并触发派生清理；支持 recoverable/hard policy |
| GET | `/v1/users/{id}/memories` | 分页、kind/status/time filter |
| GET | `/v1/users/{id}/timeline` | as-of 或时间区间的事件/事实时间线 |
| POST | `/v1/users/{id}/forget` | 按 source event、topic 或时间范围传播删除 |
| GET | `/v1/export` | 导出可读 JSONL/ZIP，包含 provenance 和版本 |
| POST | `/v1/memory-mode` | 用户启用、禁用或限制记忆写入；不删除既有数据 |

`retrieve` 返回：`items[]`（id、kind、content、validity、confidence、source_refs）、`query_plan`、`budget_used`、`warnings`。当没有足够证据时 `items=[]` 并返回 `abstain_reason`，而不是猜测。
