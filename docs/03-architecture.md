# 03 Architecture

## 推荐架构

采用事件溯源的单体服务起步：PostgreSQL 作为 source of truth，`pgvector` 保存 embedding，PostgreSQL FTS/BM25 负责稀疏检索，关系表表达实体和时态关系；抽取、整理、索引更新通过任务队列异步执行。所有派生表带 `model_version`、`extractor_version` 和 provenance，可从事件重建。

```mermaid
flowchart LR
  A[Agent / LLM] --> B[Memory API]
  B --> C[Query Understanding]
  C --> D[Retrieval Planner]
  D --> E1[Dense pgvector]
  D --> E2[FTS/BM25]
  D --> E3[Metadata + temporal]
  D --> E4[Entity/relation traversal]
  E1 --> F[Candidate fusion + rerank]
  E2 --> F
  E3 --> F
  E4 --> F
  F --> G[Context budget + consistency]
  G --> A
  A --> H[Raw event ingestion]
  H --> I[Immutable Event Store]
  I --> J[Write gate + extraction worker]
  J --> K[Versioned Memory Objects]
  K --> L[Profile / relation projections]
  K --> M[Index update]
  K --> N[Consolidation / decay jobs]
```

## 写入和读取边界

同步热路径只做事件持久化、必要的高优先级候选和轻量检索；抽取、实体解析、冲突检测、embedding、profile projection 默认异步。用户刚刚明确的事实可通过 `urgency=immediate` 触发受限同步确认，但仍写入事件后再生成 projection。

```mermaid
sequenceDiagram
  participant U as Agent
  participant API as Memory API
  participant ES as Event Store
  participant W as Worker
  participant MS as Memory Store
  U->>API: ingest(messages, metadata)
  API->>ES: append immutable events
  API-->>U: event_id + accepted
  ES-->>W: outbox event
  W->>W: candidate/gate/entity/time/conflict
  W->>MS: append memory version or no-op
  W->>MS: update indexes and provenance
```

## 作用域和权限

namespace = `(tenant_id, user_id, agent_id, project_id, scope)`；默认最小可见范围为 user+agent。共享记忆必须显式 grant。LLM 可以读取被授权对象、提交候选和调用有限工具；不能删除源事件、跨用户读取、修改历史版本或绕过敏感字段策略。

## 扩展路径

100K events 以内优先单 PostgreSQL；到 1M events 先分区、索引和异步 workers，再评估 OpenSearch/Qdrant。只有图遍历 p95 或写入并发成为瓶颈，才引入 Neo4j/Graphiti/FalkorDB；图数据库不是功能目标本身。
