# 长期记忆内核复用基线审计

日期：2026-10-30。此文件描述实际核验和已经实现的适配边界，不表示人格忠实度通过评测。

## 核验来源与版本

- 原 Git 仓库：本工作区 `D.I.V.E.Program_Sanofi/`。
- 远端来源：<https://github.com/adfkj2/D.I.V.E.Program_Sanofi>。
- 本地 HEAD：`eb9898b0a4e4965d8e9d276dc2a76b500461fb68`；项目版本 `0.4.0`。
- 初次核验的 Git 状态：3 个未跟踪的 `docs/oracle/52-disclosure-and-scoring-readiness-20261017.md`、`53-ablation-design-20261017.md`、`54-defense-prep-20261017.md`。本次内核抽取未编辑原库。
- 已读：`AGENTS.md`、`pyproject.toml`、`service.py`、`models.py`、`retrieval_session.py`、`store_contract.py`、`test_architecture_boundaries.py`、`test_application_boundaries.py` 及相关存储/提取接口。
- 使用 `git show <SHA>:<path>` 从固定提交取字节，而非复制可能有本地变化的工作树。

## 真正保留的代码

`vendor/dive-memory/` 保留 `MemoryService`、SQLite 事件/投影/删除、时间有效期、来源链、版本、混合检索、上下文预算及传递依赖。
AST 递归核查后复制了 32 个 Python 文件，另保留原 `THIRD_PARTY_NOTICES.md`。
`PROVENANCE.json` 逐文件记录源路径、SHA-256 和提交；本地修改清单为空。

运行依赖只使用 Python 标准库与 SQLite FTS5。默认确定性向量不是语义 embedding，不将它称为真实语义模型。
原 OpenAI 兼容 provider 定义虽属于保留文件，WOT adapter 未实例化它们；本次验证没有发起外部模型请求。
可安装的 vendor 包版本为 `0.4.0+wot.core`，没有旧产品命令入口和静态前端。

以下旧产品内容没有进入该依赖：`application/`、`agent/`、`web.py`、`api.py`、`cli.py`、`static/`、`oracle/`、`eval_adapters/`、项目 casebook 数据及 benchmark runner。
旧“续研”业务、旧 API/前端、项目工作流和历史评测分数不成为 WOT 产品能力。

## 实际 API 与适配决定

| 原核心真实接口 | WOT 使用方式 |
|---|---|
| `MemoryService(db_path, extractor=..., deterministic_ids=True)` | 独立记忆库，自定义确定性原文 observation extractor |
| `ingest(namespace, text, idempotency_key=..., source_message_id=..., observed_at=..., source_type=...)` | 真实来源经 WOT guard 批准后才调用，显式传来源与稳定消息 ID |
| `retrieve(namespace, query, limit=...)` | 返回原文、memory ID、event IDs、观察时间；不返回人格真值 |
| `get_event()`、`store.get_event_by_idempotency()` | 核验归属、重复键与原始正文/时间/消息 ID 一致性 |
| `process_pending(limit=...)` | 核心 outbox 的真实 claim / retry / 投影事务恢复 |
| `forget_event(event_id, hard=True)` | 删除事件正文、其独立 observation 与索引，留下无正文 tombstone |
| `rebuild_projections()`、`reindex()` | 用测试验证删除后不会重新出现；不作为用户可绕过授权的直通接口 |
| `MemoryEvidenceSource` / `reading.provenance` | 保留可复用来源接口；当前 WOT adapter 不依赖旧 reading 产品编排 |

`MemoryStore` Protocol 很窄，并未覆盖整个 `MemoryService` 实际调用的方法，不能臆造为现成完整插件协议。
`source_type` 是事件 payload 字段，并不独自提供 WOT 所需来源、用途与授权隔离；相关 guard 属于 WOT domain。
原 `get_memory`、`get_event`、`forget_event` 按 ID 工作，adapter 在删除前额外核验 namespace。

## 适配契约

`backend/wot/dive_adapter.py` 公开：

```python
DiveMemoryAdapter(db_path)
adapter.ingest(namespace, text, key=..., message_id=..., observed_at=...)
adapter.retrieve(namespace, query, limit=12)
adapter.delete(namespace, event_id)
adapter.delete_by_key(namespace, key)
adapter.close()
```

只接受 `user/{ASCII安全ID}/real|self_reports|corrections`。这些 namespace 由服务器生成，不由第三方 Skill、模型输出或模拟记录获得写权限。
原文严格作为 `episode / OBSERVATION / reported_dialogue` 保存；不做“希望果断→已经果断”转换，不自动写人物特质或替代 WOT 三轨模型。
同一文本的两个真实来源保留为两个独立 observation，删除一个来源不会误删另一个。

幂等键对应正文、消息 ID 和观察时间；相同键配不同 payload 直接拒绝。事件 ID 使用原核心 `stable_id("evt", namespace, key)`。
原核心有“事件先提交、投影后提交”边界，重复 `ingest` 本身不会恢复失败投影。
adapter 因此检查真实 outbox 状态并调用 `process_pending()`，未成功投影则报告失败，不将空 `memory_ids` 当作完成。
核心默认最多 3 次失败尝试，耗尽后明确报告 pending，不无限自动重试。

WOT 删除可能发生在核心事件提交、WOT 尚未保存 event ID 之间，因此提供 `delete_by_key`。
已存在事件走核心 hard deletion；不存在事件时，在真实核心 tombstones 表写入由 namespace/key 导出的确定性 event 墓碑，阻止未来正文重放。
adapter 对 core outbox 状态和墓碑的少量 SQL 读取/插入是明确记录的兼容边界，并未修改 vendored 源文件。
人格快照、派生缓存与 WOT 应用事件的失效传播由上层存储层负责，不冒充为旧核心已有功能。

## 实际验证

验证环境：本机 Python `3.14`；项目声明最低 `3.11`，未单独验证所有 Python/操作系统组合。

1. 原仓库基线：记忆服务、主体隔离、dialogue retention、批量来源、检索 trace、冲突矩阵、来源链、快照身份与架构边界共 **210 passed**。
   初次运行出现 **209 passed / 1 failed**：架构测试的子进程没有继承源码路径；显式设置 `PYTHONPATH=<原库>/src` 后复跑 210 条全过。未改断言或跳过用例。
2. 实际 vendored 代码：将 `PYTHONPATH` 和 pytest `pythonpath` 明确设到 `vendor/dive-memory/src`，运行原库 `test_memory_service.py`、`test_subject_boundary.py`、`test_retrieval_trace.py`、`test_resolution_matrix.py`：**87 passed**。
3. 新 adapter 契约：`python -B -m pytest -q -p no:cacheprovider tests/test_dive_adapter.py`：**21 passed**，覆盖中文原文、observation 类型、来源、非法 namespace、跨用户读取/删除、重复键冲突、删除后重启/重建/重索引、按键删除、投影事务故障及重启恢复、过期 PROCESSING worker claim、持续失败不可虚报成功、相同正文不同来源、vendor 哈希和旧业务排除。
4. 系统 Python 缺少 `ruff`；随后找到已有原项目 `.venv/Scripts/python.exe`。使用该本地环境执行 Ruff，修正测试 import 排序及一个未使用的 import 后，adapter 与两个新测试文件 **Ruff 通过**，未联网安装依赖。
5. 新 WOT 纵向契约 `tests/test_product_flow.py`：**33 passed**；与 adapter 合并运行 **54 passed**。
   覆盖真聊授权、三轨与愿望隔离、快照、两角色独立上下文、模拟反馈、静态 Skill 注入、用户检查分身发言后显式允许人物读取、删除与跨数据库故障恢复、撤回目标与行动。
   首轮发现更正证据写在 `corrections`，模拟却只读 `real` 的实际缺陷；修复为依照快照证据的真实 namespace 检索后，该反例通过。
   缓存删除保留操作 fingerprint、清空原文 response，验证了删除后重读得到已删状态、相同操作键仍不能改换 payload。

模型 API 请求数：**0**；API 费用：**0**。

## 许可与交付边界

原提交没有总 `LICENSE`；原 `THIRD_PARTY_NOTICES.md` 明确说明未声明总许可证。本次保留原声明并新增 `LICENSE_STATUS.md`，未擅自选择 MIT 或推定品牌授权。
本地授权重构与公开分发的许可状态应分别记录；对外分发前由权利人明确许可。
本审计没有开展或认证 PFE，也不将旧 LongMemEval/Oracle 结果转写为 WOT 人格拟真效果。

安装时先安装本地依赖 `python -m pip install -e ./vendor/dive-memory`，再安装 WOT 本身；仅安装 WOT 的 backend 不会自动包含 vendor 源码。
