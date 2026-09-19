# Phase 2 Hardening and MVP Compliance

日期：2026-09-19。

## 本阶段完成

- 事件 projection、关系、profile、索引和 outbox DONE 状态在一个 SQLite transaction 内提交。
- worker 用原子 claim；失败记录 attempts/last_error，三次后保留为 dead-letter；旧 PROCESSING claim 可恢复。
- projection memory id 由 event id + candidate index 确定生成，支持全量或 namespace replay。
- `memory_versions`、supersedes/version 链、write decision、job audit、model/extractor version 已持久化。
- soft delete 保留审计；hard purge 删除 memory content、versions、vector、FTS、relation、access 和 event payload。
- profile 支持多值谓词；consolidation 把全部 source provenance 合并到 survivor。
- retrieval 使用 namespace 过滤后的 RRF 和 quality rerank；支持 predicate、entity/relation、bounded
  multi-hop、first/latest、valid-time as-of 和 observation-time cutoff。
- 默认 deterministic hash vector 不再单独支撑答案，避免规模增大后的随机 dense collision。
- FastAPI 使用 strict schema；event/correction 写入要求 namespace-scoped idempotency key；普通事件默认异步，显式记忆即时处理。
- 补齐 candidate、search、reindex/replay、范围 forget、version、reinforce 和 job audit API。
- memory mode 持久化；可选 `StaticTokenAuthorizer` 隔离 namespace，并支持替换为 IAM authorizer。
- 敏感 secret 默认拒写；非法 observed/occurred/as-of/interval 在 HTTP 层返回 400。
- stale worker claim 消耗 retry budget；孤立 outbox 自动清理，避免无限重试。
- version API 返回完整 supersession 链；终态 memory 拒绝纠正和强化，避免分叉历史。
- tombstone 持久化 namespace，硬删除 event/memory 后仍可安全按租户导出审计记录。
- projection replay 按解析后的 UTC instant 和稳定 tie-breaker 排序，兼容混合时区 offset。

## 验收结果

```text
pytest:       95 passed
system:       87 passed, 1 skipped (optional FastAPI/httpx test module)
compileall:   passed
diff check:   passed
smoke:        recall=1.0, abstention=1.0, false-memory=0.0, provenance=1.0
10K SQLite:   retrieve p50≈376ms, p95≈404ms, p99≈416ms
               recall=1.0, abstention=1.0, false-memory=0.0, provenance=1.0
```

10K 数字来自本机 SQLite development adapter、20 次查询；它证明本地 MVP 门槛，但不能替代
PostgreSQL/pgvector 的发布环境报告。运行方式：

```powershell
.venv\Scripts\python -m dive_memory.benchmark --memories 10000 --queries 20
```

## 与规划的对应

`docs/09` 周期 1–5 的本地功能和内部验收已覆盖；周期 6 的内部 benchmark、延迟、false-memory 和
provenance 指标已可重复运行。PostgreSQL schema 在 `001_initial.sql`，事务式 worker SQL 在
`002_outbox_claims.sql`，既有数据库的 tombstone scope 升级在 `003_tombstone_namespace.sql`。

以下项目必须在外部环境完成，不能由单机 SQLite 测试替代：

1. PostgreSQL repository adapter 对真实 PostgreSQL 16 + pgvector 的集成与故障注入测试。
2. LoCoMo、LongMemEval、BEAM、LongMemEval-V2 数据集适配、许可确认及固定 reader/judge 报告。
3. 部署环境的 IAM、加密/KMS、备份恢复、连接池、监控告警与多实例压测。

在这些外部验证完成前，准确标签是“本地 MVP 完成、生产部署待验证”，不能标记为生产就绪。
