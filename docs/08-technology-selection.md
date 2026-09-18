# 08 Technology Selection

## MVP 选择

| 组件 | A | B | C | 选择与理由 |
|---|---|---|---|---|
| 主库 | PostgreSQL | SQLite | 文档库 | PostgreSQL：事务、JSONB、FTS、时态关系和迁移路径兼顾；SQLite 仅适合单机原型 |
| 向量 | pgvector | Qdrant | Milvus | pgvector：减少运维和双写；到规模瓶颈再拆 Qdrant |
| 稀疏 | PostgreSQL FTS | Elasticsearch/OpenSearch | 独立 BM25 | PostgreSQL FTS 起步；需要复杂分析器/横向扩展时再 OpenSearch |
| 图 | relation tables | Neo4j/Graphiti | FalkorDB | 关系表起步；多跳和 p95 证明需要后引入 Neo4j/Graphiti。Kuzu 当前已弃用 |
| 队列 | Postgres outbox + worker | Redis Streams | Kafka | outbox + worker：保证事件与任务一致，降低 MVP 复杂度 |
| API | FastAPI | gRPC | GraphQL | FastAPI REST，提供 provider-neutral contract；内部可加 gRPC |
| 对象 | encrypted filesystem/S3-compatible | DB blob | 外部 SaaS | 小对象 JSONB，大资源走对象存储；删除协议统一 |

## Scale 方案

事件按 tenant/user 和时间分区；read replicas；pgvector 分片或 Qdrant；OpenSearch 做跨租户全文；图服务只承载关系查询；Redis 仅做短 TTL cache。每个派生索引都有重建命令，不把 cache 当 source of truth。

## 模型策略

抽取器必须支持 JSON schema，低成本模型负责候选，较强模型只处理冲突、时间模糊和 consolidation。embedding 可替换，向量记录模型版本。API 接受 OpenAI/Anthropic/Gemini/local provider 的 adapter，不把模型 SDK 暴露到存储层。

## 成本和锁定

默认允许本地 LLM/embedding 与自托管数据库；所有厂商服务只通过 adapter。每次写入统计 tokens/cost；当异步抽取失败时保留事件并重试，不阻断用户对话。
