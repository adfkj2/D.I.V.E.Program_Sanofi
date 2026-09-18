# 02 Memory Taxonomy

## 设计原则

分类按用途、时间尺度和证据状态三条轴组织。`FACT`、`OBSERVATION`、`INFERENCE` 是证据状态，不是三种可以互相伪装的内容。所有类型都必须带 `source_event_ids` 和 scope。

| 类型 | 作用 | 默认生命周期 | 例子 | 主要存储 |
|---|---|---|---|---|
| Working | 当前请求和最近几轮的临时状态 | 请求/会话 | 当前问题、未完成工具调用 | request context |
| Episode | 一段可复述的经历和结果 | 中长期，可摘要 | “上次部署因 RBAC 失败，补角色后成功” | episode + vector |
| Semantic fact | 可验证的实体/属性/关系断言 | 长期，版本化 | “用户居住在上海” | memory + relation |
| Profile | schema 固定的当前用户/agent 状态视图 | 长期，人工可编辑 | preferred_name、response_style | profile projection |
| Preference | 用户明确或多次观察到的偏好 | medium/long；有置信度 | 喜欢简洁回答 | fact/collection |
| Procedural | 工作方法、规则、成功步骤 | 长期，需成功证据 | 本仓库使用 pnpm | procedure + episode |
| Resource | 外部文件、链接、截图、代码 artifact | 依资源/权限 | 报告 PDF、代码仓库 | resource metadata + object store |
| Temporal event | 带时间的发生或计划 | 从永久到 ephemeral | “2026-09-20 看电影” | event/fact |
| Relation | 实体之间的有向关系及有效期 | 随事实版本 | User→works_on→Project | relation |
| Inference | 系统基于多个证据的推断 | 短/中期，必须可降级 | “可能正在学习 Java” | memory with `inference` |

不在 MVP 中单独建模 activation/KV/parameter memory；它们属于模型运行时优化层，不是可审计的用户事实。

## Memory object

系统真正的 Memory Unit 是 `MemoryObject`，不是 message。它包含规范化内容、类型、状态、时间窗口、证据链、冲突集、权限和检索特征。`Event` 是 source of truth；`Message` 是事件的原始载荷；`MemoryObject` 是可重建的 projection。

## 证据状态

- `FACT`: 用户或可靠工具明确表达，引用具体事件。
- `OBSERVATION`: 系统统计得到的模式，例如 20 次对话中 8 次问 Java；不能写成喜欢 Java。
- `INFERENCE`: 模型推断，必须标注推断规则、证据和置信度；不得在无证据时当作用户事实。

## 持久性类别

`permanent`（身份、用户要求保留、合规审计）、`long_term`（稳定偏好、技能）、`medium_term`（当前项目目标）、`short_term`（近期计划）、`ephemeral`（当前上下文）。持久性是策略输入，不是删除命令；`valid_to`、status 和 retention policy 分开。

## 记忆写入门

候选只有同时满足以下条件之一才进入长期对象：用户显式要求记住；对未来任务有明显复用价值；跨 session 重复且新颖；外部工具产生可验证结果。临时意图、闲聊、模型自己的无证据推断默认不写入。敏感信息需要单独策略，默认拒绝或加密隔离。
