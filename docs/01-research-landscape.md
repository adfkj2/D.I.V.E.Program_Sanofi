# 01 Research Landscape

整理日期：2026-09-19。公开资料最后核验：2026-09-18/19。

## 结论先行

当前最稳定的共识不是“把聊天切块后做向量搜索”，而是：原始交互应保留为可回放事件；记忆是带来源、时间窗口、作用域和生命周期的派生对象；检索需要按问题选择信号；写入应当选择性进行。不同项目对 agent 自主管理记忆的权限取舍不同，生产系统应把“提出候选”和“提交变更”分开。

## 方案对比

| 方案 | 核心思想/表示 | 写入 | 检索 | 时间/图 | 更新、遗忘、agent 权限 | 借鉴与限制 |
|---|---|---|---|---|---|---|
| Letta / MemGPT | agent 状态；memory blocks；当前 Letta 使用 Git-backed MemFS | agent 可显式写入；Dreaming 在后台整理 | 核心 memory + 归档/工具；实现依版本而异 | 时间主要由版本和提交表达；不是专门时态图 | /remember、/doctor、sleep-time；可审阅提案 | 借鉴分层、版本化、后台整理；不要让模型无限制改源事实 |
| Mem0 | user/session/agent scope 的记忆集合；最新仓库展示 ADD-only 与实体链接 | 候选抽取、ADD-only 单遍写入；平台与 OSS 能力要分开 | semantic + BM25 + entity，多信号融合，top-k | 时态检索支持；图不是核心存储 | 以追加和来源保留避免覆盖；删除/更新仍需按 API 实测 | 借鉴多信号和作用域；官方数字是 managed platform 自报，不能直接当 OSS 结果 |
| Zep / Graphiti | episode → entity/fact temporal context graph | 增量 episode ingestion；关系事实有有效窗口 | semantic + keyword + graph traversal | 显式时间有效区间、历史失效而不删除、来源回溯 | 图中自动失效旧事实；自定义 ontology | 借鉴时态边和 provenance；MVP 先用关系表，避免过早引入图数据库 |
| A-MEM | Zettelkasten note：内容、关键词、标签、上下文、邻居 | 新 note 触发历史关联和属性演化 | Chroma 语义检索 + 动态链接 | timestamp；不是双时态真相模型 | agent 驱动演化；研究结果需按模型和数据复现 | 借鉴邻域和链接；演化历史须保留版本，不能原地覆盖 |
| MIRIX | Core、Episodic、Semantic、Procedural、Resource、Knowledge Vault 六类 | 专门 agent 从对话/工具/屏幕轨迹提取；auto-dream | PostgreSQL BM25 + vector，多模态 | episodic/resource 可含时间；图能力不是其唯一重点 | auto-dream 合并、冲突处理；本地优先 | 借鉴资源/工具记忆与显式 consolidation；MVP 不拆六个服务 |
| MemOS | MemCube：payload + provenance/version/governance/usage；plaintext/activation/parameter 三层 | MemReader、MemScheduler、MemLifecycle；异步 MemScheduler | 图、全文、向量和策略调度 | 版本链、TTL、权限与迁移 | 资源级调度、反馈纠正、可导入导出 | 借鉴统一元数据和调度概念；参数/激活记忆不进入本项目 MVP |
| LangMem | semantic collection、profile、episodic、procedural；namespace | hot-path active 或后台 subconscious；manager 可 upsert/delete | store direct/semantic/metadata | 依应用实现 | collection 可合并；profile 更新当前状态；工具可让 agent 操作 | 借鉴 API 抽象和 active/background 双路径；不把 LangGraph 作为服务耦合 |
| SelfMem (2026) | agent 通过 memory tools 和反馈探索自己的策略 | 自优化策略，结果来自 BEAM 实验 | 策略可变 | 取决于 memory backend | 研究性质，权限和稳定性仍需验证 | 作为 Phase 10 实验，不放入 MVP 的默认路径 |

## 证据与可比性

| 证据 | 观察 | 使用方式 |
|---|---|---|
| Mem0 GitHub README（2026-09-18，仓库显示 2026-04 新算法） | LoCoMo 92.5、LongMemEval 94.4、BEAM 1M 64.1；注明 managed platform、top_200、单遍检索 | 仅作为方向性参考；内部复现实验必须锁定模型、数据版本、judge、top-k、token budget |
| Mem0 memory-benchmarks（2026-09-18） | OSS 与 Cloud 可分别跑；默认 answerer/judge gpt-4o；说明 embedding、LLM、top-k 会显著影响结果 | 采用其 ingest→search→evaluate 结构，禁止直接横向比较不同配置 |
| LoCoMo 官方仓库/论文（ACL 2024） | 10 个长对话，QA、事件摘要、证据 dialog id；数据是成本可控子集 | 作为早期回归集，不宣称覆盖生产规模 |
| LongMemEval 官方仓库/论文（ICLR 2025；cleaned 2025-09） | 500 问题，信息抽取、跨 session、知识更新、时间、abstention；S 约 115k token，M 约 500 sessions | 作为核心对话记忆评测，并保留 abstention |
| LongMemEval-V2（arXiv 2605.12493，2026-08 更新） | 451 问题、最多 115M token、多模态网页/企业轨迹、准确率+查询延迟 | 作为后期 AgentRunbook 轨迹评测，不冒充旧版对话 benchmark |
| BEAM（arXiv 2510.27246；仓库） | 100 对话，128K/500K/1M/10M token，10 种能力；LLM judge | 先跑 128K/500K subset；10M 作为扩展成本项 |
| OmniMemEval（2026 仓库） | 统一 user-memory API 与 agent-memory 两条轨道，含 PersonaMem v2、HaluMem | 用于适配器和泄漏检查；结果仍按固定配置记录 |
| Graphiti GitHub（当前） | episode、entity、fact validity、provenance、hybrid retrieval；Kuzu 标为 deprecated | 采用概念，不在 MVP 依赖 Kuzu |
| Letta docs（当前） | MemFS、Git-backed memory、Dreaming、Agent reviews | 借鉴版本化和后台审阅，不照搬 agent 写权限 |
| A-MEM arXiv v11（2025-10-08） | 动态索引、链接和 memory evolution | 作为链接实验来源，需独立复现 |
| MemOS arXiv 2507.03724（2025-07-04） | MemCube、provenance/version、scheduling 的系统抽象 | 借鉴元数据设计；参数/激活迁移延后 |

## 研究判断

1. Event sourcing + derived memory 比可变的单表 memory 更适合作为 source of truth：能回放、重跑抽取器、审计和满足删除传播。
2. Profile 只表示当前状态；历史事实放 collection/event/fact，不能被 profile 覆盖后丢失。
3. 图关系在多跳、实体关系和时间线问题上有价值，但 MVP 可由 `entity`、`relation`、`memory_relation` 关系表实现；只有查询和规模证明需要时才迁移图引擎。
4. “agent-controlled memory”应限制为候选提案、显式工具调用和可审计 patch。源事件 immutable，写入 gate 和策略服务拥有最终提交权。
5. Decay 应作用于召回优先级和存储层级，而不是无条件删除。用户明确要求、身份/安全、稳定偏好采用永久或人工确认策略。

## 参考链接

- https://github.com/letta-ai/letta
- https://docs.letta.com/configuration/memory
- https://github.com/mem0ai/mem0
- https://github.com/mem0ai/memory-benchmarks
- https://github.com/getzep/graphiti
- https://github.com/agiresearch/A-mem
- https://github.com/Mirix-AI/MIRIX
- https://github.com/MemTensor/MemOS
- https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
- https://github.com/snap-research/locomo
- https://github.com/xiaowu0162/LongMemEval
- https://github.com/xiaowu0162/LongMemEval-V2
- https://github.com/mohammadtavakoli78/BEAM
- https://github.com/MemTensor/OmniMemEval
- https://arxiv.org/abs/2502.12110
- https://arxiv.org/abs/2507.03724
- https://arxiv.org/abs/2607.03726
