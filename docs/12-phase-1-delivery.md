# Phase 1 Delivery: API, Worker and Embedding Contract

本阶段把本地 MVP 从“可调用的 Python service”推进到“可安装、可通过 HTTP 验收、可替换检索模型”的边界。

## 已交付

- FastAPI adapter 增加 `/healthz`、deferred event、统一缺字段错误和 missing event 的 404 语义。
- SQLite connection 支持 FastAPI worker threads，`MemoryService` 使用可重入锁保护同步和 outbox 路径。
- ASGI integration tests 覆盖 event、idempotency、retrieve、context、deferred worker、delete 和错误响应。
- `EmbeddingProvider` contract、离线 deterministic provider 和 OpenAI-compatible `/embeddings` provider。
- embedding model/dimension metadata 和 `reindex_vectors()`；切换模型前必须显式重建索引。
- `OutboxWorker` 提供 `run_once()` 与 `drain()`，后续可以替换成 PostgreSQL queue runner。
- PostgreSQL reference migration 补齐 outbox、vector、tombstone、FTS projection 和索引。

## 验收

```text
33 tests passed in .venv
compileall passed
smoke: recall=0.6667, abstention_accuracy=1.0, false_memory_rate=0.0
```

smoke 集合仍是小型 deterministic fixture，不代表真实 embedding 的线上召回率。

## 下一阶段边界

PostgreSQL migration 已具备 schema contract，但真正的 PostgreSQL repository adapter、事务式 outbox claim/retry/dead-letter、认证授权和真实数据集 benchmark 仍需独立实现和部署环境验证。
