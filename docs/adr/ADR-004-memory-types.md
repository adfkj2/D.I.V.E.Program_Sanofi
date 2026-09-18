# ADR-004 Memory Type Design

## Context

MIRIX 的六类和 MemOS 的多层抽象有启发，但拆成六个子系统会使 MVP 难以验证。

## Decision

采用 working、episode、semantic fact、profile、preference、procedural、resource、temporal event、relation、inference 十个逻辑 kind；evidence state 和 durability 独立建模。activation/parameter 留给后续。

## Consequences

查询可按用途规划，数据模型仍统一；未来可把 kind 映射到不同存储，而无需改变 API。
