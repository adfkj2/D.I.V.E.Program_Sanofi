# 05 Data Model

建议 PostgreSQL 16+，向量扩展 pgvector；时间统一 UTC，展示层转换时区。下列是逻辑模型，字段可按实现映射为 SQL/ORM。

| 表 | 关键字段 | 目的 |
|---|---|---|
| users | id, tenant_id, policy, created_at | 用户与保留策略 |
| agents | id, provider, model_policy | 调用方身份和能力声明 |
| sessions | id, user_id, agent_id, started_at, ended_at | 会话边界和 consolidation 单元 |
| messages | id, session_id, role, content_ref, content_hash, created_at | 原始对话，content 可加密/对象存储 |
| events | id, type, payload, observed_at, occurred_from, occurred_to, source_message_id, idempotency_key | immutable source of truth |
| entities | id, namespace, canonical_name, type, aliases, status | 实体解析 |
| memories | id, namespace, kind, evidence_state, content, structured_content, status, importance, confidence, salience, durability, valid_from, valid_to, observed_at, created_at, updated_at, model_version | 当前 memory object 版本 |
| memory_versions | id, memory_id, version, patch, reason, source_job_id, created_at | 可审计历史和回滚 |
| memory_sources | memory_id, event_id, message_id, relation, weight | provenance 多对多 |
| relations | id, namespace, subject_entity_id, predicate, object_entity_id, valid_from, valid_to, confidence, source_event_id | 时态关系边 |
| memory_relations | memory_id, relation_id, relation_type | memory 与关系映射 |
| profiles | user_id, schema_version, current_json, generated_from_version, updated_at | 当前状态投影，可重建 |
| memory_access | memory_id, query_id, used, rank, accessed_at, caller | 质量、强化和审计 |
| jobs | id, type, input_range, extractor_version, status, dry_run, output_summary | 异步作业幂等和可观测 |
| tombstones | object_type, object_id, reason, requested_by, deleted_at, purge_after | 删除传播和合规 |

## 字段决策

`created_at` 是系统写入时间；`observed_at` 是看到证据的时间；`valid_from/to` 是事实在世界中的有效时间；四者不可互换。`confidence` 表示证据可信程度，`importance` 表示对未来任务的价值，`salience` 表示当前事件显著性，`access_count` 只来自 memory_access 聚合。`source` 不用自由文本替代关系表；`supersedes/contradicts` 用显式边便于查询。

## 索引与约束

- `(tenant_id, user_id, agent_id, status)` B-tree，隔离作用域。
- `valid_from/valid_to` range index，支持 as-of 查询。
- FTS generated column + GIN；embedding HNSW/IVFFlat 按数据量选择。
- `(namespace, canonical_subject, predicate, valid_from)` 唯一性只作为候选约束，不能阻止历史版本。
- 所有写入要求 idempotency key；事件和版本只 append。
- 向量、FTS、profile 都标注 source version，可从 memories/event 重建。

## 典型事实

`User --lives_in--> Chengdu (valid 2025-01..2026-02)` 与 `User --lives_in--> Shanghai (valid 2026-03..present)` 并存。问“现在住哪里”筛 `valid_to is null`；问“2025 年住哪里”用 as-of 过滤；问“搬家过程”检索两个版本及 source episodes。
