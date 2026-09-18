# ADR-001 Event Sourcing vs Mutable Memory

## Context

长期记忆需要历史、回放、来源、纠正和删除传播；直接 UPDATE memory 会丢失证据和旧状态。

## Options

1. 可变 memory 单表；2. immutable event + derived projections；3. 全量 event-sourcing + CQRS 从第一天拆服务。

## Decision

选择 2：事件和原始消息 append-only，memory/profile/index 为可重建 projection；MVP 用单服务和 Postgres outbox，不提前拆 CQRS 服务。

## Reasons / Tradeoffs

支持审计、回滚、抽取器迁移和时态查询；代价是 schema、replay 和删除传播更复杂。事件不是无限永久保留的法律承诺，保留策略和 tombstone 必须独立实现。

## Consequences

所有写入带 idempotency 和 extractor version；projection 故障可重建；用户看到的是 memory view，而不是未经处理的事件流。
