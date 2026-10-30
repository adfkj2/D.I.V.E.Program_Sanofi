# 《我们曾聊过》研究综述、核心论证与不可变引擎规则

> 版本：v1.0（方案基线）｜资料核对：2026-10-30｜状态：**研究支持的设计提案，尚非经过用户实验验证的产品结论**
>
> 本文是 `01_PRD.md`、`02_TECHNICAL_DESIGN.md`、`03_CODEX_PROMPTS.md` 的规范性依据。文中的门槛、样本量和商业指标凡未有实验支持者均标记为“工程预设/待校准”。

## 1. 一句话结论

**《我们曾聊过》应当是一款“由用户主导的数字人格观察、选择与成长产品”，而不是宣称复刻真人或替真人社交的自动化工具。** D.I.V.E 保存可追溯的真实证据；Persona Engine 分别建模“自我陈述”“有证据支持的行为模式”和“用户主动选择的理想人格”；Simulation Engine 允许在明确标识的虚拟空间观察分身交流；Growth Engine 把用户批准的目标转化为现实中的小规模尝试；PFE 独立验证分身在特定情境中的忠实度与不确定性。任何 AI 生成内容均不能自动变成用户的真实经历。

### 1.1 需要被验证的五个假设

- H1 个性化增益：D.I.V.E + 结构化 Persona 在盲测新情境中，优于同一底模的 zero-profile、静态简介、普通摘要/RAG 基线。
- H2 个体差异：跨用户交换人格后，预测性能会下降；避免“所有分身只是同一个礼貌大模型”。
- H3 稳定性：连续对话中可以保持情境相关行为，且不会无依据地固化临时表达。
- H4 用户自主性：用户可以区分“过去如何表现”和“希望如何表现”，能够修改、暂停、撤回目标而不受系统惩罚。
- H5 现实价值：在有对照的纵向试验中，产品对用户**自行选择**的实际行动目标有增量帮助，而不仅是带来新奇、满意度或即时自我感动。

H1–H5 均是待验证假设；研发不得将其写成既成事实。

## 2. 代表性研究与直接设计决策

| 编号 | 证据（原始来源） | 支持的范围 | 不能推出的结论 | 对应设计决策 |
|---|---|---|---|---|
| R1 | Park et al., *Generative Agent Simulations of 1,000 People* (2024 preprint / 2025 Stanford HAI): https://arxiv.org/abs/2411.10109 ; https://hai.stanford.edu/policy/simulating-human-behavior-with-ai-agents | 1,052人、访谈结合LLM；GSS 约达真人两周重测水平的85%（相对比值） | 不意味着任意开放对话85%“像本人” | 真实用户盲测、真人重测参考；“领域局部通过”而非全局复刻 |
| R2 | Peng et al., *Digital twins are funhouse mirrors: Five systematic distortions*, Science Advances (2026): https://pmc.ncbi.nlm.nih.gov/articles/PMC13537255/ | 19项预注册研究、164个结果；数字分身相对基础模型改善有限；跨个体相关低；存在过度理性化等系统性偏差 | 不能将高表面准确率当作个体拟真 | 必测“优于最强非 D.I.V.E 基线”的增益与跨用户区分；报告失败 |
| R3 | Du et al., *TwinVoice*, ACL Findings (2026): https://aclanthology.org/2026.findings-acl.981/ | 对社会、私聊、叙事三场景和观点、记忆、推理、词汇、语气、句法六能力的评价范式 | 问卷一致不等于多轮对话忠实 | 拆分意见/推理/风格/关系，加入固定前缀的对话续写盲测 |
| R4 | Li et al., *PRISM*, EMNLP (2026): https://aclanthology.org/2026.emnlp-main.952/ | 把人设忠实度分成任务框定、人际立场、语言风格以替代单一整体 judge | 不代表通用 API 可直接复现其依赖的概率方法 | 首版用可审计的维度级 rubric 与人工校核，不声称复现原算法 |
| R5 | Samuel et al., *PersonaGym*, EMNLP Findings (2025): https://aclanthology.org/2025.findings-emnlp.368/ | 动态情境与人格相关的评价比只考固定题更合理 | 不能只凭 judge 分高认证用户人格 | 使用人格相关但未暴露的多领域挑战集 |
| R6 | Abdulhai et al., *Consistently Simulating Human Personas with Multi-Turn Reinforcement Learning*, NeurIPS (2025): https://papers.nips.cc/paper_files/paper/2025/hash/4c91443877f8388d8190c938ac5a4d4d-Abstract-Conference.html | 多轮对话易产生角色漂移，且可度量 | 内部自洽不等于符合真实用户 | 分开测“无矛盾”和“与真人下一句一致” |
| R7 | Zhang et al., *CORE*, EMNLP Findings (2026): https://aclanthology.org/2026.findings-emnlp.666/ | 局部证据与持久人格状态应分离，偏好更新应谨慎 | 一句话不能直接更新长期人格 | 证据滞留、冲突、确认、版本和迟滞门 |
| R8 | Hou et al., *PersonaTree*, EMNLP Findings (2026): https://aclanthology.org/2026.findings-emnlp.822/ | 证据到高阶人格声明的支持路径值得显式建模 | “一条总结”不能代替完整证据链 | Observation → Pattern → Trait 三层及支持链 |
| R9 | Wang et al., *Memory-Driven Role-Playing*, ACL Findings (2026): https://aclanthology.org/2026.findings-acl.1175/ | 角色知识检索、选择、边界和应用可拆分评价 | 有记忆不等于实际使用正确 | 记录每轮检索 evidence IDs；检索正确性和生成忠实度分开测 |
| R10 | Wang et al., *PRISK*, EMNLP (2026): https://aclanthology.org/2026.emnlp-main.679/ | 个人化记忆会放大无关引用、同温层和迎合倾向 | 高个性化不等于高质量建议 | 增加反迎合、多视角与最小必要信息检索检查 |
| R11 | Park et al., *Generative Agents*, UIST (2023): https://doi.org/10.1145/3586183.3606763 | 观察—反思—检索—行动是一种可行的模拟架构 | 可爱/逼真的角色不能证明“像特定真人” | 将反思作为有来源的候选，不允许循环自举为事实 |
| R12 | Higgins, *Self-Discrepancy*, Psychological Review (1987): https://doi.org/10.1037/0033-295X.94.3.319 | actual / ideal / ought 自我表征之间存在心理区别 | AI 不可凭差异诊断真实心理状态 | 三轨人格；自选理想与外界应然严格区分；避免差距羞辱 |
| R13 | Ryan & Deci, *Self-Determination Theory*, Psychological Inquiry (2000): https://doi.org/10.1207/S15327965PLI1104_01 | 自主性、胜任感和关系感与动机相关 | AI 不应定义“更优秀的自我” | 用户拥有选择、暂停、撤销与反对的权利 |
| R14 | Pataranutaporn et al., *Future You* (IEEE FIE 2024，2025年页面): https://www.media.mit.edu/publications/future-you-a-conversation-with-an-ai-generated-future-self-reduces-anxiety-negative-emotions-and-increases-future-self-continuity/ | 344人的短期研究支持未来自我互动对自我连续感等即时指标的影响 | 不证明长期现实行为或决策改善 | 虚拟体验与真实成长分开评价，避免“预测未来”措辞 |
| R15 | *The when and how of planning* (2024): https://doi.org/10.1080/10463283.2024.2334563 | 642项检验元分析支持 if-then 执行意图在多种目标上的作用 | 不说明本产品已经有效 | Growth Engine 采用自愿且具体的“如果…那么…”微行动 |
| R16 | Baek et al., *An Evaluation of Data Leakage Risks in Tool-Using LLM Agents*, REALM/ACL (2026): https://aclanthology.org/2026.realm-1.75/ | 非恶意任务同样可能产生权限边界和信息泄漏问题 | 模型提示词无法替代存储/检索层权限 | 端到端数据用途/可见性校验、隐私门和泄漏测试 |
| R17 | Agent Skills 规范及安全建议: https://agentskills.io/specification ; https://learn.microsoft.com/en-us/agent-framework/agents/skills | `SKILL.md` 是可携带指令甚至脚本的第三方包 | 第三方 Skill 不等于可信插件 | 只静态解析白名单字段，不自动执行脚本、工具与外部调用 |
| R18 | 女娲 Skill（设计参考，非人格科学验证）: https://github.com/alchaincyf/nuwa-skill | 强调心智模型、决策启发式、表达 DNA 和诚实边界 | 不能把其自评评分卡当成人格真实性认证 | 借鉴人格结构，而非照搬评分或运行未经审查的 Skill |

**证据强度说明：** 上述来源混合了实验、理论、工具论文和规范，不能等同对待。我们借鉴其问题定义与实验范式，而没有验证那些研究的全部样本或代码。所有“工程门槛”均需本项目单独预注册与校准。

## 3. 严格区分三个自我与三种测量

### 3.1 自我表征（不是三个独立人类）

- **Perceived Self / 自我陈述**：来自用户明确说过的“我认为我是……”；保留“自我叙述”身份，不等同客观事实。
- **Observed Self / 证据推断**：来自用户真实陈述和有授权的现实行为、在具体情境下提炼的模式；必须显示支持与反例、时间、覆盖域及不确定性；AI 不可宣称“比你更懂你”。
- **Chosen Self / 我选择的理想人格**：由用户明确确认的“我想保留/调整为……”目标；不是自动升级版，也不是 AI 认为应有的品德；可拒绝、版本化、撤销。

### 3.2 三个互不替代的指标

- **PFE / Persona Fidelity**：新情境下对真实用户选择、理由、人际态度和表达的预测表现。需要独立基线、盲测、置信区间、领域标签和负例。
- **PAA / Persona Aspiration Alignment**：所生成的 Chosen Self 是否符合用户**明确授权**的目标、反例与约束；检验其目标执行，不等于“更像过去真实用户”。
- **GE / Growth Effectiveness**：是否帮助现实用户完成自己认可的微行动，并在长期对照研究中形成增量效果；短期自我满意不等于现实成长。

不同量表**不得相加成“人格真实性总分”**。安全、隐私和自主性属于硬门，不以高得分抵消。

## 4. Persona Evolution 的正式规则（规范性，不由 LLM 任意解释）

以下 `ER-XX` 为产品不变量，后端以确定性规则、权限校验与测试实现。LLM 只能提出候选，不得直接决定持久化授权。

| 规则 | 引擎约束及不可越界的行为 |
|---|---|
| ER-01 主体绑定 | 每条证据必须携带 owner、subject、source、origin、observed/occurred time、用途/可见性；不明归属拒写个人画像。 |
| ER-02 来源类型 | **真实用户发言**、**用户自报的现实行为**、**用户修正**、**外部 Skill**、**模型推断**、**模拟对话**必须分别标记；来源不能被下游“洗白”。 |
| ER-03 绝对禁止自举 | 模拟 transcript、AI 生成的未来故事、他人 Skill 内容、LLM 自评永不自动升级为用户真实行为证据。 |
| ER-04 三轨隔离 | Perceived/Observed/Chosen 独立命名空间/记录类型；对 Chosen 的改动不重写 Observed 的历史。 |
| ER-05 用户最终决定 | 任何 Chosen Self 目标的新增、启用、重大更改、作废都需要显式用户确认，默认建议状态为 `PROPOSED`。 |
| ER-06 现实行动证据分级 | 自报动作标为 `USER_REPORTED`，不能冒称设备/第三方核验；外部导入须记录授权来源。 |
| ER-07 候选推断非事实 | LLM 输出性格特征仅作为带证据引用的 `HYPOTHESIS`，不能直接写成 FACT；缺证据标 `INSUFFICIENT`。 |
| ER-08 稳定性门槛 | 不能因一条随口表达覆盖跨场景稳定特征；采用多事件、多时间窗口支持、冲突暂存与人工确认。 |
| ER-09 情境条件 | Trait 按领域、对象关系、条件、时间范围表达；不将“工作果断”推成“恋爱果断”。 |
| ER-10 冲突不裁剪 | 相反证据允许并存，生成 `CONFLICTED` 状态，必须能显示反例；不得为使人格一致而捏造解释。 |
| ER-11 版本快照 | Persona 版本不可变；每次发布绑定 evidence snapshot ID、策略版本、模型版本、创建人、授权时间；新版本不悄悄覆盖旧版。 |
| ER-12 检索最小化 | 每个 Agent 只收到自己有权访问、该话题必要的证据；不能凭 prompt 限制替代服务端 ACL。 |
| ER-13 Skill 不可信 | 导入 `.skill`/`SKILL.md` 只作文本/元数据候选；禁止执行第三方代码、自动安装依赖和偷偷联网。 |
| ER-14 外部人物权利 | 真实朋友数字人格必须取得本人适当授权，明示为模拟；公开人物也是“基于资料的解释性角色”，不冒称本人授权或真人发言。 |
| ER-15 会话独立 | 两个 Agent 独立上下文、独立检索范围、独立调用；对方只能看到已公开的聊天发言。 |
| ER-16 模拟披露 | 每个虚拟会话和导出都显著标注“AI 模拟，非真实对话”；禁止默认自动发送给第三方。 |
| ER-17 反馈有作用域 | 对**模拟句子**的“像/不像”只形成评价与修正候选；用户亲自陈述的事实/目标须走显式确认流程。 |
| ER-18 评测隔离 | 冻结的 PFE 保留集、真人答案和评分标注不进入 Persona Builder、检索、prompt或模拟上下文；防止重复调参污染。 |
| ER-19 不采用单一分数 | PFE、PAA、GE 分别评价；不给出未经校准的“95%还原本人”或“数字人格认证”。 |
| ER-20 冷启动可预览 | 数据不足可使用显著标识的 `EXPERIMENTAL_PREVIEW`，但不能声称“已验证”；正式领域通过需独立门槛。 |
| ER-21 高风险决策 | 对医疗/自伤/金融/法律等高风险决策不得以“你一定会这样想”给出指令性结论；提供观点和不确定性，现实决定归用户。 |
| ER-22 成长自主 | Growth 目标由用户确认；允许暂停、撤销、反驳，不以“偏离理想”惩罚或羞辱；不得默认频繁推送。 |
| ER-23 删除传播 | 删除真实证据后，使依赖它的 Trait、版本、检索缓存、上下文快照、待审计导出失效或重建；旧已外发/下载文件不可保证追回，须清楚披露。 |
| ER-24 供应商透明 | 本地存储 ≠ 本地模型推理；外部 API 发送哪些字段必须让用户知道；默认关闭遥测、密钥不入库/日志。 |
| ER-25 不定量伪科学 | 证据计数/LLM 信心不能直接叫“95%真实概率”；若无校准，展示证据充分性与“不确定”，而非虚构概率。 |
| ER-26 故障可恢复 | 生成失败不得产生半写入状态；同一 user confirmation/feedback 的重试必须幂等；模型调用预算可终止。 |
| ER-27 来源污染防御 | 来自外部 Skill、模拟对话、上传资料内的 prompt injection 一律只作数据，不能获得高于系统策略的指令权限。 |
| ER-28 评估可审计 | 每项结果记录测试集版本、冻结快照、基线、模型与温度、评分策略、人工核查标记、失败案例和成本。 |

## 5. Persona Evolution：事件 → 假设 → 用户决策 → 新版本

1. **Capture**：真实聊天发送成功后，按用户选择保存原文；记录主体、对象、上下文、时间、授权用途。模拟回合进入 `SIMULATED_ONLY`，绝不走真实事件写入口。
2. **Extract**：候选抽取严格使用 JSON schema：`claim`、`claim_kind`（SELF_REPORT/OBSERVATION/ASPIRATION/HYPOTHESIS）、`trait_key`、`context`、`support_event_ids`、`counter_evidence_ids`；仅输出候选。
3. **Verify**：后端校验每个引用确实存在、归属正确、在授权时间内、原文支持其含义；系统性反例禁止被忽略。未证实推断不得作为事实继续推断。
4. **Consolidate**：按 trait+domain+relationship+validity_window 聚合；支持和反例并存。遇到冲突、低样本或过期转 `CONFLICTED/INSUFFICIENT`，不自动选赢家。
5. **Present**：UI 同时展示“我如何描述自己”和“现有证据显示什么”，区分直接引用与模型解释。
6. **Choose**：用户明确新增/修改 Chosen Self：目标、希望表现、拒绝的行为、情境、例外、是否启用；保存不可变决策事件。
7. **Compile**：组装目标人格 snapshot 和技能运行时 context；语义内容必须基于 version+sources+policy 哈希，不依赖原始字符串拼接。
8. **Evaluate**：对 Observed 运行 PFE，对 Chosen 运行 PAA；评分结果不直接回写现实事实。
9. **Simulate**：独立多 Agent 轮流发言，检索授权资料，标记模拟；生成内容只存模拟域。
10. **Feedback**：接受用户针对模拟句子的纠正，转成可审阅的变更候选；只有用户明确批准的个人新事实/目标才能入相应真实/Chosen 域。
11. **Grow**：用户选择可撤回的现实 if-then 小行动，完成后自报结果；这些行动不是模型自动代办，也不能证明心理学上的长期成长。
12. **Forget**：删除或撤销授权触发依赖失效图（trait → persona snapshot → session cache → growth suggestion）与安全重建；错误不得复活已删除内容。

### 5.1 状态迁移（后端判定，不在 prompt 内）

- Evidence: `CAPTURED -> CANDIDATE -> ADMITTED|REJECTED|NEEDS_REVIEW -> SUPERSEDED|DELETED`。
- Trait: `HYPOTHESIS -> SUPPORTED|CONFLICTED|INSUFFICIENT -> STALE|RETRACTED`；`SUPPORTED` 不是“真实人格被证明”。
- Chosen goal: `PROPOSED -> USER_APPROVED -> ACTIVE -> PAUSED|COMPLETED|ABANDONED`；已批准目标变更需要新版本。
- Persona snapshot: `DRAFT -> EVALUATING -> PREVIEW_READY|DOMAIN_TESTED -> RETIRED|INVALIDATED`；`DOMAIN_TESTED` 只代表通过已声明的工程/试点协议。
- Simulation: `CREATED -> RUNNING -> PAUSED|COMPLETED|FAILED|CANCELLED`；每轮必须可审计。

## 6. 关键争议与已裁决方案

| 争议 | 不选择 | 选择与理由 |
|---|---|---|
| “理想我”是否应覆盖“真实我” | 让理想 Trait 替换历史数据 | **双轨共存、用户可切换**；历史是回溯证据，理想是自主目标 |
| 评估是否只靠 LLM 打分 | 一次“像不像”评分 | **独立真实回答 + 控制基线 + 结构化 rubric + 人工抽查** |
| 是否先造漂亮产品 | 多 Agent 炫技先行 | **先打通纵向切片和数据隔离，再做精美 UX** |
| 是否全部将聊天纳入记忆 | 全量无差别自动永久记忆 | **透明采集设置、可见证据、受控准入与随时删除** |
| 是否一开始让所有人的 Skill 联网互聊 | 无边界地自动代表用户发言 | **MVP 仅本地模拟、用户观看，任何对外发送需要新授权与后续审查** |
| “通过门槛”是否意味着像本人 | 静态80分通用认证 | **领域、场景、时间条件下的测试标签 + 不确定性**；正式阈值经试点预注册 |

## 7. 评估 protocol 与最低实验纪律

- 参与者需明确告知数据用途、外部模型发送边界、自愿退出机制；小规模 N=10–20 仅属可行性试点，不代表可广泛推断。
- 在收集时就划分**训练/开发/独立保留**数据，最好按会话与时间隔离；相似问题去重；不能运行后再挑有利题目。
- 同一测试题同时对比 Base、Static、Summary/RAG、D.I.V.E；固定模型、温度、最大 token、测试顺序随机种子、时间快照与输出预算。
- 定量指标：选择匹配率、原因要素匹配、适度拒答、生成风格人评、用户间交换负对照、长对话任务；置信区间按参与者/会话聚类计算，避免把同一人的100题当100个独立人。
- 若需要准入：先运行 pilot，**事先冻结**每领域的有效样本下限、主指标、最强基线判定方法、配对 CI、关键失败率与隐私零容忍条件；未满足者保持 Preview，绝不编造普适“人格真实性百分比”。
- `DOMAIN_TESTED` 标签必须展示：测试领域、样本数、测试日期、数据时间截止、效果/误差区间、不能推广的范围。
- 禁止使用评测集来自动优化系统后再报告同一批数据的最终性能；调参后要用新的保留集。
- Growth 有效性需以后续纵向研究、用户自定目标和适当对照为依据，不能拿“用户觉得感动”代替实际行为数据。

## 8. 现有 D.I.V.E 仓库核查（2026-10-30）

- 源仓库：`https://github.com/adfkj2/D.I.V.E.Program_Sanofi`，核对到的 `main` HEAD 为 `eb9898b0a4e4965d8e9d276dc2a76b500461fb68`（2026-10-29 UTC）。新一轮开工时应重新核对 HEAD。
- `pyproject.toml` 当前包名 `dive-memory`、版本 `0.4.0`、Python `>=3.11`。
- `src/dive_memory/service.py` 提供 `MemoryService.ingest(namespace, text, ..., source_type, ...)`；旧接口已有时序、来源、幂等与处理事务线索。`models.py` 有 `Event/Memory/EvidenceState/MemoryStatus`，`store_contract.py` 有 SQLite/PG 共用窄协议。
- `retrieval_session.py` 有 `MemoryEvidenceSource` 按 namespace 和 as_of 控制来源可见性；`application/runtime.py` 是**旧项目工作台业务层**，不是新产品通用 Persona 引擎。
- 现有抽取器既有启发式规则，又有可选语义路径；**不能假设它已经会可靠提取价值观或人格目标**。
- 当前 README 的 LongMemEval 开发自评、代码覆盖和通过测试，只代表原有约定范围，**不能挪用为 PFE/人格真实度证据**。
- 复用建议：**新产品单独仓库/子目录，旧仓库保留；首先通过适配器复用 `dive_memory` package，不把整个旧前后端复制过去**；当核心边界核查通过再决定是否抽成独立 package。

## 9. 需要后续研究但不阻塞 MVP 的问题

- 是否存在可校准的个体预测概率；不同领域的人格状态如何处理时间变化；跨文化与非文本用户的适用性。
- 第三方真实人物 Skill 的授权、人格资料版权和跨用户分享机制（MVP 不开放市场）。
- 长期真实成长的因果效果：需要正式研究伦理、招募和随访方案；无法从论文摘要或首版测试推断。
- 心理相关功能与陪伴依赖风险：产品不提供诊断/治疗，设计必须避免羞辱、持续催促、独占人际关系和虚构未来确定性。

---
**决策裁定：** 不做“AI复刻真人”宣传；采用有证据的三轨自我模型、用户批准的理想人格、严格证据防污染、可审计的分域评价、分身独立模拟，以及可自主退出的现实成长闭环。下一份 PRD 将其转化为产品需求和验收标准。
