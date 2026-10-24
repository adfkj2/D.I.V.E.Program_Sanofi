# R65 外部方法核验与三阶段阅读器设计审查

核验时间：2026-10-23，Asia/Shanghai；本次源码快照读取时间为 18:46:51 +08:00。

范围：只读公开研究仓库、官方项目网页源码和本地实现；不下载评测答案，不运行第三方代码，不发模型请求，不修改源码、judge、历史答案或产品文档。唯一新增文件为本文件。新模型请求 0，新增费用 ¥0。公开来源中的安装、上传、联系等指令均未执行。

## 1. 结论与可比性边界

目前无法依据已核验材料建立“本项目与统一业界最佳相差多少分”的可靠结论。项目当前 R62 为 435 正确、57 错误、8 未知 / 500，是已暴露开发集上的 Oracle 非官方自评；历史 R40 为 451/500，另有缓存复用条件。Oracle 已经只保留证据会话；LongMemEval-S、M 包含长历史噪声；V2 又更换了任务、数据和指标。模型、judge、提示、数据修订、记忆预算、工具调用上限、冷启动/缓存和费用口径均可能不同。

因此，95% 是本项目验收目标，并非本次已核实的同协议行业基线。下列论文内增益和厂商报告均不可相加、不可直接转移成项目预期分数，也不能用“更复杂架构”替代实测。

## 2. 精确来源与已核验内容

### 2.1 LongMemEval 官方仓库

- 仓库：`xiaowu0162/LongMemEval`。
- 核验时 main SHA：`9e0b455f4ef0e2ab8f2e582289761153549043fc`；提交时间 `2026-05-11T22:49:24Z`。
- [固定 README](https://github.com/xiaowu0162/LongMemEval/blob/9e0b455f4ef0e2ab8f2e582289761153549043fc/README.md)
- [固定生成实现](https://github.com/xiaowu0162/LongMemEval/blob/9e0b455f4ef0e2ab8f2e582289761153549043fc/src/generation/run_generation.py)
- [固定评价实现](https://github.com/xiaowu0162/LongMemEval/blob/9e0b455f4ef0e2ab8f2e582289761153549043fc/src/evaluation/evaluate_qa.py)
- [论文链接](https://arxiv.org/abs/2410.10813)

README 明确：Oracle 历史仅包含证据会话；S 的拼接历史约 115k Llama 3 tokens，M 约 500 会话。Oracle 的会话列表没有保证按时间排序。`has_answer` 和 `answer_session_ids` 是评价标签，不能传给记忆形成或回答策略。官方回答输出为 question_id 与 hypothesis 两字段。检索评价排除 30 道不可答题，因为它们没有真实答案位置；这不意味着答题评价可以排除这些题。

官方推荐 JSON 历史格式、保留 assistant-side messages（`USERONLY=false`）和 `con` 阅读：先抽取全部相关信息，再推理回答。代码显示：普通 con 提示明确要求先提取相关信息；另有逐片段独立生成 notes 的选项。官方示例允许 full-history baseline，但本项目手册 F.1.2 禁止用全量历史拼接绕过记忆；不能因此给当前产品增加直接原始档案旁路。

评价脚本存在按类型的判分规则：列表缺必要信息判错；时间计算容忍 off-by-one；知识更新允许带旧值但必须给所需更新值；偏好题不要求覆盖 rubric 每一点；不可答题有单独分支。这里仅为理解协议，不建议修改项目既定 judge，更不按这些类型或题号路由回答。

### 2.2 LongMemEval 官方项目网页中的研究发现

- 网页来源仓库：`xiaowu0162/long-mem-eval`。
- 核验时 main SHA：`933767b0bb1d0f696062e29aa44cbd3dc2bc8d13`；提交时间 `2025-05-20T21:09:58Z`。
- [固定官方网页源码](https://github.com/xiaowu0162/long-mem-eval/blob/933767b0bb1d0f696062e29aa44cbd3dc2bc8d13/index.html)
- [官方展示页](https://xiaowu0162.github.io/long-mem-eval/)

网页列出四项 Finding：

1. round 粒度比 session 更适合存储和利用历史；继续压缩成孤立 user facts 会损失总体信息，虽然可能改善 multi-session reasoning。
2. 用提取的 user facts 扩展检索 key、保留原 value，论文内条件下 recall@k 约增加 4 个百分点，回答准确率约增加 5 个百分点。
3. 时间索引与查询时间范围扩展可改进时间题检索，论文内报告召回改善 7–11 个百分点。
4. 即便完美召回，阅读也困难；Chain-of-Note 与 JSON 提示在三个模型中的改善最高达 10 个绝对百分点。

这些数字是官方网页对其论文内实验的概括，不是当前最新排行榜，不是对本项目的预测，不能把四项相加。论文 HTML 直接访问遇 TLS/认证连接失败，本次未以未读论文表格补充更精确的分模型数值。

合法迁移重点：抽象检索 key 与支撑 value 分离；保留说话者、round/上下文和时间；证据先抽取再作答；相对日期按原发言日期定位。原句保存仍须经过当前记忆链及合法来源约束，不能把所有原始档案直接装给模型。

### 2.3 Hindsight：方法参考与数值限制

- 仓库：`vectorize-io/hindsight`。
- 核验时 main SHA：`91b50232e2d0fa4fbd94232c3822fe13071f1481`；提交时间 `2026-10-23T10:43:36Z`。
- [固定 README](https://github.com/vectorize-io/hindsight/blob/91b50232e2d0fa4fbd94232c3822fe13071f1481/README.md)
- [固定检索说明](https://github.com/vectorize-io/hindsight/blob/91b50232e2d0fa4fbd94232c3822fe13071f1481/hindsight-docs/docs/developer/retrieval.md)
- [固定 reflect 说明](https://github.com/vectorize-io/hindsight/blob/91b50232e2d0fa4fbd94232c3822fe13071f1481/hindsight-docs/docs/developer/reflect.mdx)
- [2026-03-23 厂商方法与结果报告](https://github.com/vectorize-io/hindsight/blob/91b50232e2d0fa4fbd94232c3822fe13071f1481/hindsight-docs/blog/2026-03-23-agent-memory-benchmark.mdx)
- [相关论文链接，仅核对链接存在](https://arxiv.org/abs/2512.12818)

已核验的方法描述：retain 用模型提取事实、实体、关系和时间，再规范化；recall 结合语义、关键词、实体/因果图及时间路径，RRF 融合、cross-encoder 重排，按预算装包；可展开支持事实的 chunks；observations 汇总支持材料并保留原句及证明计数；reflect 是收集证据、推理、引用的循环。

这些是方法作者的文档，不能当独立证明。也不是所有细节都适合迁移：例如其文档规定一个结果都装不下时返回整条首结果，可能超预算；本项目不能为仿效而放松已有预算或来源边界。全图/多模型/额外模型提取也可能显著增加本项目成本，宜先检验小规模机制。

2026-03-23 博客自报 Hindsight v0.4.19 single-query 在 LongMemEval 为 94.6%，并将提取、归纳和检索改进列为可能贡献，明确没有干净的单独因果归因。本次没有复跑，也没有核验动态排行榜全部条目，因此不能称其为当前统一最佳。

### 2.4 Hindsight AMB 公开协议，不能与 Oracle 直接比较

- 仓库：`vectorize-io/agent-memory-benchmark`。
- 核验时 main tree/commit SHA：`16fcebb0066537e890c6015c9d4a2b2eac952249`。
- [固定 README](https://github.com/vectorize-io/agent-memory-benchmark/blob/16fcebb0066537e890c6015c9d4a2b2eac952249/README.md)
- [固定 LongMemEval adapter](https://github.com/vectorize-io/agent-memory-benchmark/blob/16fcebb0066537e890c6015c9d4a2b2eac952249/src/memory_bench/dataset/longmemeval.py)

当前 adapter 只列 `s` split，下载 `longmemeval_s_cleaned.json`，每题独立 bank；存储会话移除 `has_answer` 字段。README 描述 Gemini 生成和第二次 Gemini judge；adapter 有自定义详细 reader 提示。当前 `get_judge_prompt_fn` 根据 question_type 分支，未见官方评价实现基于 `_abs` 的不可答专用分支。因此当前公开实现与官方 Oracle 的输入及judge协议均不同。

当前 main 协议也不等于 2026-03-23 当日协议；不能以今日代码倒推该历史 94.6% 的所有运行细节。该分数只能标为作者历史自报，不直接计算 `94.6 - 87.0` 为项目算法差距。提示中的“有两个答案就都说”等策略也不宜不加约束照搬，容易制造本项目已有的无据补充错误。

### 2.5 LongMemEval-V2：当前研究范围变化

- 官方仓库：`xiaowu0162/LongMemEval-V2`。
- 核验时 main SHA：`2cc8c540bdb87fe6761629b585e727e1c4704520`；提交时间 `2026-08-09T22:31:22Z`。
- [固定 README](https://github.com/xiaowu0162/LongMemEval-V2/blob/2cc8c540bdb87fe6761629b585e727e1c4704520/README.md)
- [固定排行榜交付规则](https://github.com/xiaowu0162/LongMemEval-V2/blob/2cc8c540bdb87fe6761629b585e727e1c4704520/leaderboard/README.md)
- [论文链接，仅核对链接存在](https://arxiv.org/abs/2605.12493)

官方 README 说明 451 个手工问题、多模态 web-agent trajectories、web/enterprise 两域、small/medium 两档、最高约 115M tokens；能力为静态状态、动态状态、工作流、环境陷阱、前提意识。公开论文基线固定 Qwen3.5-9B reader，排行榜包装要求相应 reader 与 gpt-5.2 judge，并以准确率和查询延迟共同计算 LAFS 相对参考frontier增益。该协议与本项目 Oracle 不是同一评测。可作为后续 Agent 记忆通用性研究方向，但不能在本轮替代当前验收。

## 3. 结合本项目诊断的优先级

R63 内容审计中，40 个有效内容错误有 20 个标注来源全文均在最终上下文；12 个来源可见但全文不完整；4 个已形成来源最终不可见；1 个部分来源未形成；另 3 个不可答误答。因而“把召回率继续推高”不足以解释或解决全部问题，尤其不能假定来源出现即语义齐全。

优先：

1. 证据累积而非搜索后替换；公平且可审计地装包，明确哪些来源/片段因预算未展示。
2. 来源结构与语义分离：逐句摘录用于原文支持；实体、事件时间/发言时间、状态和模态由明确过程核对，不把文字出现视为逻辑蕴含。
3. 阅读过程先抽取短证据，再生成和复核答案；列表/多事件必须处理完整性，拒答必须指出必要缺失前提。
4. 独立合成反事实与改写验证，避免继续围绕已暴露 500 题扩充英语模式。冻结配置后再真实对照；本文件没有预测提分和推广候选。

## 4. 本轮三阶段设计只读审查

审查对象为执行中快照，不代表最终实现。仓库 HEAD 为 `a4cec2f475c8d8285cab4059ae57169032faae74`，下列文件当时尚未提交：

| 文件 | 审查快照 SHA256 |
| --- | --- |
| src/dive_memory/reading/deliberation.py | a5c26ad3e05b68b8c65a2fd66bec76b11e014a2a9cdb5053bd560282c4d1ae8f |
| src/dive_memory/reading/workspace.py | 631716ddbf34e288012a727811c573970c5b20a3fdb4b7e85855745afc4e7f7b |

设计：plan 最多16条短exact quotes、最多2查询与2读取；证据工作区公平装包并 pin notes；合成标准引用草稿；最后 review 以实际证据及明确标注为 untrusted 的草稿重新判定。3个必须阶段，加最多1个共享格式修复，总计不超过4次模型请求；默认策略保持；不按题型regex路由，不修改judge。

方向合理：把工具动作与最终回答的混合格式拆开，可降低复杂动作语法风险；短原句摘录避免plan凭空创造事实；工作区保留之前取得的合法证据；固定review让单次早答也接受一次独立检查。但以下风险需要明确验证，不能凭阶段增多推断准确率提升。

### 4.1 已复现：同memory_id不同片段可掩盖错误citation index

审查快照的 `parse_decision` 委托 `parse_indexed`，后者把index转成memory_id，公共 `validate_grounded_decision` 按memory_id在全部同ID片段找原句。工作区拆片后同ID很常见，导致quote实际不在指定index也可能接受。

纯内存合成复现（使用项目已有类型，`python -B`，无网络/模型/源码写入）：

- items[0]：memory.id=`same`，content=`Alpha detail.`，refs=`event`。
- items[1]：memory.id=`same`，content=`Beta detail.`，refs=`event`。
- 模型输出 ANSWER，citation 为 `{index:0, quote:"Beta detail."}`。
- 实际输出：`quote_in_selected_index=false`，`returned_status=ANSWER`，`returned_reason=INDEXED_SUPPORTED`。

这不是答案泄漏，但违反新模块“current exact index”约定，会掩盖索引漂移。建议新 `parse_decision` 在委托前逐citation检查原句确属指定index及其provenance，再保留公共严格校验。加入同ID异片段、不同provenance、旧index与当前index交换的合成反例。已向父代理报告，不修改源码。

### 4.2 pins完整性与反证公平性

16×400 字符只是notes上限，不是所有必要证据的上限；对长列表可能不足，过多重叠notes又可能占尽预算。pin优先装包会改变其余公平轮询的实际效果：plan选择的早期支持证据可能挤出后续查询发现的新更新/反证。原句被 pin 不等于证明齐全。

建议记录pins占用、合并数量、未admit/超预算pin、各轮新来源实际可见性；一旦必需pin丢失要显式失败，不能把失败当正常缺证据拒答。验证“16条支持notes+最后新反证”“相同note多次重复”“跨片段限定词”及“不同来源各一项但一项较长”。不应提高预算来让反例无条件消失。

### 4.3 精确摘录失去位置、归属与限定词

`EvidenceCitation` 保存memory_id/quote/refs，但不保存当前pack的document_id和位置。`workspace._pin` 对同memory_id文本使用 `.find` 的首个匹配。若同一句在长文的不同日期/人物标题下重复，pin可能回到另一处上下文。短quote也可能截掉“not”“if”“planned”或跨句归属，从而发生quote有效、语义错误。

建议在编排层利用当前pack的index审计元数据把note绑定到实际document快照与位置，或至少对多处匹配明确记录歧义，不声称唯一定位。保留必要上下文和说话者/时间；验证重复短句在不同标题/日期下的情形。先前内容若本就缺上文，工作区不能自行从未获准档案补齐。

### 4.4 三阶段不是独立评审者

同模型的review可能被草稿锚定，自信地重复原错；草稿即使标成untrusted仍可能包含指令式文本。复核能否纠错需实测，不能把“多一轮”当独立judge。draft宜作为清楚分隔的数据字段，来源文本也明确为数据；必须以实际当前evidence重新检查。final citation必须绑定final pack，不能复用已改变的索引。

为区分机制和额外token收益，后续可在相同模型/证据/最大调用预算下比较“无review”和“有review”，记录改善与退步及额外token，绝不选择更好历史答案拼接分数。

### 4.5 单次并行计划的观察盲区与共享修复预算

plan只在搜索前生成，因此第2个查询无法利用第1个结果；与自适应循环相比，多跳追证可能受限。2查询+2读取是可解释的成本取舍，不代表所有问题充分。目录可见ID不等于任意offset有效，读取仍必须遵循命名空间、as_of、删除和已入库来源限制，返回错误要分类而非制造事实。

格式修复若在plan消耗，后续两阶段不能再隐式重试；传输异常不能误算成可修复格式。应覆盖每一阶段首错、共享额度耗尽、终轮仍无效、服务故障和真实ABSTAIN；3必须阶段+1共享修复的调用/token/费用必须由统一账本统计，包括失败调用。持久化每阶段输入哈希、合法证据、错误和最后决定，不把错误输出替换成可计正确拒答。

### 4.6 数学与时间仍可能退步

新设计若不使用原calculate工具，能避免其语法失败，却把算术/日期再次交给模型；原有成功工具路径的收益不能假定无损。应保留旧策略并用合成边界检查月份、近似值、相同事件不同提及、旧总数与新增量、事件日期与发言日期、比较缺一端。正确引用两个数字并不证明它们应相加。

## 5. 核验限制与下一步

GitHub连接器成功读取上述固定文件与commit metadata；官方展示页的浏览器读取超时，arXiv HTML直接请求连接失败，未绕过TLS或安全限制。动态排行榜未完整读取，没有把截图、宣传标题或自报名次当统一前沿事实。没有下载第三方完整答案/轨迹，没有接触新的私有数据。

本文件是研究与执行中实现的审查证据，不是实现最终验收或新Benchmark成绩。修复后的源码应按新哈希、合成验证和完整工程检查另行记录；本次已复现缺陷也不自动说明真实500题因此能提高多少分。新的付费运行须遵守既有失败候选停止和预算授权边界。
