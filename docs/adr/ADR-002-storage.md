# ADR-002 Vector and Primary Storage

## Context

Postgres、Qdrant、Milvus、OpenSearch、Neo4j 均可单点解决部分问题，同时引入会造成双写和运维负担。

## Decision

MVP 使用 PostgreSQL + pgvector + PostgreSQL FTS + relation tables。定义 adapter 和重建作业；在 100K/1M events 压测后依据 p95、写入吞吐和索引维护成本决定是否拆 Qdrant/OpenSearch/graph。

## Consequences

部署简单、事务一致；全文分析和图遍历能力有限。任何新增存储必须给出可测收益和迁移/回放方案。
