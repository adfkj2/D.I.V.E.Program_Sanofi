# ADR-006 LLM vs Rule-based Write Gate

## Context

纯规则难以判断未来价值，纯 LLM 容易过度记忆、漂移和产生 false memory。

## Decision

分层 gate：规则先拦截权限、敏感、重复和明确 ephemeral；LLM 只为候选提供结构化 importance/novelty/evidence/temporal 字段；策略服务按阈值决定 accepted/rejected/review。每次决策持久化。

## Consequences

可调阈值和离线回放，减少把模型意见当事实；代价是多一步抽取和需要校准。发布门槛包含 false-memory rate。
