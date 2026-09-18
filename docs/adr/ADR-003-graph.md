# ADR-003 Knowledge Graph

## Context

实体关系和多跳查询适合图，但“为了先进而加图”会扩大 schema、运维和删除传播面。

## Decision

MVP 用实体、关系、时态边的关系表；提供 graph adapter 接口。达到多跳 recall 或 p95 门槛时再试 Graphiti/Neo4j/FalkorDB，并保留 event/provenance 作为真相。

## Consequences

第一版仍能表达 Chengdu→Shanghai 等关系演化；复杂邻域查询可能需要 SQL recursive CTE 或后续图引擎迁移。
