# ADR-005 Synchronous vs Asynchronous Extraction

## Context

热路径抽取能即时记住关键事实，但增加延迟且易把模型猜测写入；后台整理更完整但有滞后。

## Decision

默认同步追加 raw event，异步 candidate extraction/consolidation；只对用户显式“记住”或安全关键状态启用受限同步确认。后台任务可 dry-run、重试、审阅。

## Consequences

用户对话低延迟，短时间内可能尚未形成 semantic memory；API 返回 event accepted 与 memory ready 两种状态。
