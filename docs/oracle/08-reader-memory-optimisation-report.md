# Oracle 500：Reader 与记忆形成/检索优化报告

> **历史口径提示（2026-09-23）**：本文保存 2026-09-22 的原始实验记录，不代表当前
> reader v3 或已修正的时间截止逻辑。旧 `exact` 是 token-containment v1，会把含参考词的
> 矛盾句判对；旧 `answered_only_accuracy` 误将正确拒答计入分子；旧分题型 `accuracy`
> 仅以成功判题数为分母。数值不得与新版口径混接。修正版 exact-v2 的离线重判、时间诊断
> 和当前选型见 [09-frontier-decision-and-no-cost-verification.md](09-frontier-decision-and-no-cost-verification.md)。

- 日期：2026-09-22
- 数据集：`eval/external/longmemeval/longmemeval_oracle.json`
  （500 题，SHA-256 `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c`）
- 代码版本：`cc3d060` + 未提交工作树（`dirty=true`，见 §11.3）
- 官方分数：**`official_score = NOT_EVALUATED`**（理由见 §2）

---

## 1. 摘要

### 1.1 五条结论

1. **已定位并修复 single-session-assistant 的形成缺口。** 该题型的 gold 证据形成率
   从 **4/56 = 7.14%** 提升到 **52/56 = 92.86%**（+48 轮），进入上下文的 gold 轮
   从 **4 → 51**。机制是"文档准入"（§4）：把助手产出的长交付物作为**文档记忆**准入，
   而不是按 v1 关键词门一律拒绝。
2. **该机制单独启用时是净负的。** 默认 `legacy` 打分下，`packed`（进入上下文的 gold 轮）
   **607 → 524（−83）**：长文档挤占了共享的 1,500 token 预算（均值上下文 573 → **1,276**，
   被排除项 18 → **1,790**）。
3. **已实现但默认关闭的阻尼器把它收回为近似中性。** 加上
   `--scoring-policy balanced`（`document_weight=0.85` + `document_char_budget=1200`）后：
   `retrieved` **+54（+49 / −0，严格单调）**、`packed` **+75（+71 / −2）**；
   相对原始基线的净额为 `packed` **−8**（逐题 +50 / −50）。
4. **所以结论是"必须配套启用"，而不是"不可用"。** 净效应是一次**结构重构**：
   assistant +47 轮 packed，代价是 multi-session −23、temporal −25、knowledge-update −9。
   **是否值得取决于题型权重。** 准确率层已**部分**测出（§9.4、§9.5）：
   `B → D`（只换打分策略）三个 judge 方向一致（+1.80 / +3.80 / +6.80 pp），
   但**在唯一无判题失败的 `exact` judge 上不显著**（McNemar **p = 0.2624**，
   95% CI [+0.00, +4.59]），且 500 题对本效应量的**最小可测效应为 3.99 pp**
   → **准确率层既不足以确认、也不足以否认阻尼器的作用**（§9.5）。
   而**准入单独的准确率效应仍未测**（`A → D` 被 reader prompt v1→v2 混淆，
   去混淆所需的 A′ 臂因 API 余额耗尽未产出）。
5. **本轮不产生任何官方分数。** `official_score = NOT_EVALUATED`。§9.4 的准确率数字是
   **自测**（DeepSeek judge；模型不符合官方要求的 `gpt-4o-2024-08-06`），
   覆盖量更不是准确率（§2、§9.4）。

### 1.2 2×2 消融总表（500 题，GPU，semantic 门，确定性 reader，非语义嵌入）

| 臂 | 准入 | 打分 | `formed` | `retrieved` | `packed` |
|---|---|---|---|---|---|
| **A** | off | legacy | 730 (0.8239) | 607 (0.6851) | 607 (0.6851) |
| **B** | all | legacy | **781 (0.8815)** | 554 | 524 |
| **C** | off | balanced | 730 (0.8239) | 607 (0.6851) | 607 (0.6851) |
| **D** | all | balanced | **781 (0.8815)** | **608 (0.6862)** | **599 (0.6761)** |

四个差分（含逐题方向）：

| 差分 | `formed` | `retrieved` | `packed` |
|---|---|---|---|
| A→B（只加准入，`legacy`） | **+51**（+51/−0） | −53（+51/−85） | **−83**（+49/−107） |
| B→D（再加阻尼） | 0（500 题**逐题不变**） | **+54**（+49/**−0**） | **+75**（+71/−2） |
| C→A（只换打分，无文档） | 0 | 0 | 0（500 题**逐题不变**） |
| **A→D（净效应）** | **+51** | **+1** | **−8**（+50/−50） |

三条结构性事实：

1. **`formed` 只受准入影响**，阻尼对形成**完全无影响**（B→D 的 formed 差额为 0，
   且 500 题逐题不变）。
2. **损失全部发生在下游**：B→D 在 `retrieved` 上是**严格单调改善**（+49/−0），
   在 `packed` 上只让 2 题变差。
3. **C→A 恒等**证明 `balanced` 的时近性/时间窗项**在覆盖层面是惰性的**，
   因此 B→D 的增益可**完全归因于文档阻尼本身**，而不是打分策略的整体换血。

机制层面（`packing.decisions` 全量统计，500 题）：

| 臂 | INCLUDE | TRUNCATE | EXCLUDE | 出现截断的题数 | 移除字符 |
|---|---|---|---|---|---|
| A | 3,581 | 0 | 18 | 0 | 0 |
| B | 2,733 | 0 | 1,790 | 0 | 0 |
| C | 3,581 | 0 | 18 | 0 | 0 |
| D | 4,003 | **1,315** | 520 | 439 | **1,440,990** |

即：字符上限**把 1,270 次"排除"转换成了"截断"**，这正是 `packed` 从 524 回升到 599、
平均入上下文条数从 5.47 回到 8.01 的直接原因（§7）。

### 1.3 分题型（`packed` / gold）

| question_type | gold | A (off/legacy) | B (all/legacy) | D (all/balanced) | A→D |
|---|---|---|---|---|---|
| knowledge-update | 144 | 109 | 93 | 100 | −9 |
| multi-session | 323 | 237 | 170 | 214 | **−23** |
| single-session-assistant | 56 | 4 | 51 | 51 | **+47** |
| single-session-preference | 44 | 39 | 34 | 39 | 0 |
| single-session-user | 66 | 57 | 55 | 59 | +2 |
| temporal-reasoning | 253 | 161 | 121 | 136 | **−25** |
| **合计** | **886** | **607** | **524** | **599** | **−8** |

净额 **−8 = +47 −23 −25 −9 +2 +0**。所以正确的表述不是"更好"或"更差"，
而是：**把 multi-session / temporal / knowledge-update 的覆盖，换成了 assistant 的覆盖。**

### 1.4 准确率层（§9.4，真实 DeepSeek reader，自测）

| 臂 | reader prompt | 准入 | 打分 | exact | ds-legacy | ds-rubric | 拒答率 |
|---|---|---|---|---|---|---|---|
| A（基线）| v1 | off | legacy | 0.3480 | 0.4180 | 0.4360 | 0.510 |
| B | v2 | all | legacy | 0.4160 | 0.5380 | 0.5500 | 0.156 |
| D | v2 | all | balanced | **0.4340** | **0.5760** | **0.6180** | 0.132 |
| A′ | v2 | off | legacy | — | — | — | **未产出（余额耗尽）** |

- **唯一干净对比**：`B → D` 只换打分策略，三个 judge **方向一致为正**
  （+1.80 / +3.80 / +6.80 pp）。**但在功效约束下这不足以确认**（§9.5）：
  唯一无判题失败的 `exact` judge 上 **McNemar p = 0.2624**、95% CI [+0.00, +4.59]（含 0），
  且 500 题对该效应量的**最小可测效应为 3.99 pp**（> 观测到的 1.80 pp）。
  `ds-rubric` 的 +6.80 pp 另有**仪器污染**：两臂判题失败数不等（34 vs 12），
  最多 4.40 pp 与记忆配置无关。→ **既不能确认、也不能否认。**
- **`A → D` 不可归因**：同时混入 reader 策略 v1→v2 与记忆侧配置，本报告**拒绝单侧归因**。
- **取舍**：D 把拒答从 255 降到 66，但真·不可答题判对数从 27/30 掉到 **15/30**，
  `answered_only_accuracy`（exact）0.7102 → **0.5000** —— 收益来自"敢于作答"而非"答得更准"。
- **账单**：`all` 让 reader 输入 token 涨 **2.4–2.9×**（562,290 → 1,370,819 / 1,622,123）。

（注意区分两个计数：A→D 有 **50 题**至少失去一个层级（§1.2 的逐题方向），
而 A→B 有 **107 题**。这里说的是"题数"，不是"gold 轮数"。）

---

## 2. 评测口径与合规边界

### 2.1 三个必须区分的口径

`src/dive_memory/oracle/evaluate.py` 与 `diagnose.py` 统一采用：

| 口径 | 定义 | 用途 |
|---|---|---|
| `full_set_accuracy` | 分子=判对，**分母=全部 500**；ABSTAIN 计为未答对 | **头条指标** |
| `answered_only_accuracy` | 分子=判对，分母=reader 实际作答的题 | 次指标，**与上者并列，不替代** |
| `abstained_rate` | ABSTAIN 占比 | 必须与上两者同时披露 |

**ABSTAIN 的真实机制**：拒答、超时、错误、格式非法统一映射到同一条
canonical abstention hypothesis；judge **跳过** ABSTAIN（hypothesis 为空）。
所以 `answered_only_accuracy` 的分母更小也更宽松——若只报它，
"少答以抬高分数"会被奖励。因此 40× 投影一律基于 `full_set_accuracy`。

### 2.2 `official_score = NOT_EVALUATED` 的理由

官方判分要求 `gpt-4o-2024-08-06`（官方 `print_qa_metrics.py` 有 assert 强制该版本）。
本地可用的 judge 是 DeepSeek，**不符合官方判分模型要求**，因此：

- 凡本项目自跑的 500 题，产物中的 `official_score` 一律为 `NOT_EVALUATED`
  （`oracle/runner` 的 summary schema 内已固定该字段，见 §11.2）。
- 三份 judge 产物均标注 `SELF_REPORTED_NON_OFFICIAL`。
- **官方 Benchmark（= Overall Accuracy × 40）只能由项目组统一复跑得出。**

### 2.3 已发布基线三列自测的**口径修正**（同一份 500 题 submission）

原先记录的三列自测数存在一个**评测侧缺陷**，本轮已定位并修正。三列分母现在统一为 500：

| judge | 旧记录 | 旧判题数 | **修正后** | 修正后判题数 | 修正后 `judge_errors` |
|---|---|---|---|---|---|
| exact 确定性匹配 | 0.348 | 500 | **0.348（不变）** | 500 | 0 |
| DeepSeek 两档指令（legacy） | 0.4005 | 427 | **0.4180** | 489 | 11 |
| DeepSeek 官方 rubric | 0.4225 | 426 | **0.4360** | 492 | 8 |

**缺陷是什么**：旧产物里 `calls.judge = 500`（调用确实都发生了）却只有 427 / 426 题
被计入 `judged`，差额 **73 / 74 题**既不计入判题数、也不计入 `judge_errors`
（旧记录写 `judge_errors: 0`），且旧口径分母是 `judged` 而非 500。
这 73 题**不是** reader 拒答题：500 条 hypothesis **无一为空**（其中 255 条是 canonical
ABSTAIN 文本），故"judge 跳过 ABSTAIN"的解释与数据不符。真实成因是**未被重试的判题失败**
被静默归入 `NOT_EVALUATED`。

**修正方法与证据**：用当前 `evaluate.py` 重判同一份 submission，并在失败时重试
（最多 4 次、指数退避）。判题数回到 489 / 492，**残余永久失败 11 / 8 题**被如实计入
`judge_errors`（这些题在 `full_set_accuracy` 中按未答对处理，故修正值是**偏保守的下界**）。
judge 的 `configuration` 块（`provider` / `model` / `prompt_version` / `temperature` /
`max_tokens` / `official`）与旧产物**逐字节相同**，唯一差异是重试策略——
**增量可归因于判题可靠性，而非 prompt 或模型变化**。

**独立对照**：`exact` 确定性 judge 不走网络、不会失败，其数值**原样不变（0.348）**。
因此"修正后 DeepSeek 数字变高"**不是系统变好，而是旧记录被判题失败拉低了**。
`exact` 仍是 paraphrase 的严格下界，故数值最低。

### 2.4 基线诊断事实（决定"瓶颈在哪"的那一组数）

来自 `eval/reports/oracle-failure-analysis-v2/`（对已发布 DeepSeek 500 题的诊断，
**不是**本轮消融的产物）。它同时给出本报告最重要的动机。

**拒答的实际形状**：500 题中 reader 作答 245、拒答 255（`abstain_rate = 0.51`）。
但 255 次拒答里 **228 次发生在"可回答"的题上**，只有 **27/30** 道真正不可回答的题被
正确拒答；`abstain_precision_on_refusals = 0.1059`。**拒答不是保守，是误伤。**

拒答根因（`classify_abstain` 的决策表，首个命中者胜，属诊断分组）：

| 根因 | 计数 |
|---|---|
| `TEMPORAL_AMBIGUITY` | 74 |
| `SUFFICIENT_EVIDENCE_REFUSED` | 54 |
| `NO_EVIDENCE_RETRIEVED` | 31 |
| `ASSISTANT_MEMORY_MISSING` | 30 |
| `GOLD_EVIDENCE_NOT_FORMED` | 22 |
| `PREFERENCE_AGGREGATION_FAILURE` | 14 |
| `WEAK_EVIDENCE` | 3 |
| `UNDER_ABSTENTION_ON_UNANSWERABLE` | 3 |

阶段归因：`CORRECT 172 / ABSTAIN 142 / READING 94 / FORMATION 72 / RETRIEVAL 20`，
`STORAGE 0`、`RERANK 0`。**`FORMATION 的 72 中有 52 是 single-session-assistant`**
——这正是 §4 文档准入的目标，且它与 `ASSISTANT_MEMORY_MISSING 30` 互相印证。

> ⚠️ 产物内固定写入的免责声明（原文）：
> *"Stage and abstain cause are inferred from the run's own bookkeeping. There is no
> benchmark label for this axis, so neither field is ground truth and neither may be
> reported as an accuracy."*
> **本节任何数字都不得作为准确率引用。**

逐题型（`accuracy_all_cases` / `accuracy_answered_only` / 该题型"至少一轮 gold 被形成"的题占比）：

| question_type | 题数 | 作答 | 拒答 | all-cases | answered-only | gold formed（逐题） |
|---|---|---|---|---|---|---|
| single-session-user | 70 | 52 | 18 | 0.7286 | **0.9808** | 58 / 70 |
| knowledge-update | 78 | 62 | 16 | 0.6026 | 0.7581 | 68 / 78 |
| multi-session | 133 | 68 | 65 | 0.3684 | 0.7206 | 122 / 133 |
| temporal-reasoning | 133 | 45 | 88 | 0.1805 | 0.5333 | 128 / 133 |
| single-session-assistant | 56 | 5 | 51 | 0.0536 | 0.6000 | **4 / 56** |
| single-session-preference | 30 | 13 | 17 | **0.0** | **0.0** | 27 / 30 |

最后两行是两个不同的故事，必须分开看：

- **assistant**：证据**根本没形成**（4/56）→ 记忆侧问题 → §4 处理。
- **preference**：证据**已经形成并进入上下文**（27/30 题、约 90%），
  但 all-cases 与 answered-only **同时为 0** → 这是纯粹的 **reader/聚合问题**，
  不是检索问题。**这解释了为什么文档准入修不了 preference。**

### 2.5 一个必须显式声明的单位差异

`diagnose` 产物的 `gold_formed_rate` **分母是题数**（"该题至少一轮 gold 被形成"），
而本报告 §1 的 `formed_rate` **分母是 gold 轮数**。两者都对，但不是同一个量：

| question_type | diagnose（逐题） | 本报告（逐轮） |
|---|---|---|
| multi-session | 122 / 133 = 0.9173 | 273 / 323 = 0.8452 |
| temporal-reasoning | 128 / 133 = 0.9624 | 236 / 253 = 0.9328 |
| knowledge-update | 68 / 78 = 0.8718 | 120 / 144 = 0.8333 |

本报告**只用逐轮口径**（§9 的四臂是同一口径，故可比）。
（附带验证：本报告的分析器在"逐题至少一轮形成"这一口径上，与 `diagnose`
**6 位小数完全一致**，两个独立实现互相印证。）

---

## 3. 范围、约束与"可泛化"审计

### 3.1 本轮的硬约束

| 约束 | 本轮的遵守方式 |
|---|---|
| 不优化 judge | 只改 reader 与记忆侧；三份 judge 产物均为既有，未调参 |
| `gold`/`answer`/`question_type` 是 EVALUATOR_ONLY | 见 §3.2；离线覆盖分析器从 dataset 读 gold，**只在评测后使用** |
| 禁止 question_id 特判 / 答案硬编码 / 关键词特判 | 本轮所有改动都是按**谓词类别**（`relations.SINGLE_VALUED_PREDICATES`）、**来源信任层级**、**形状**判定的机制；无一处引用具体题号或答案 |
| 禁止把全量 haystack 拼进 prompt | 上下文仍受 `token_budget=1500` 与 `limit=10` 约束 |
| 禁止使用 gold answer / 针对单题 patch / 反复调 prompt | reader prompt 未改（`oracle-grounded-reader-v1`） |
| 增量为准，不破坏现架构 | 新增评分策略默认 `legacy`，**逐公式复现旧行为**（§6）；`content_caps` 缺省为空映射时输出与原先**逐字节相同**（§7） |

### 3.2 边界的两侧

- **写入侧**：`LongMemEvalCase.system_input()` 只 yield `WRITER_VISIBLE_FIELDS`，
  gold 字段根本不进入 ingest。
- **读取侧**：reader 只看到 question、question_date 与预算受限的检索上下文；
  `configuration.gold_visibility` 在 manifest 中逐字声明了这条边界。
- **离线分析器**：`ops/oracle_formation_coverage.py` 读 dataset 的
  `evidence_refs` 与 run 的 traces 做**事后**比对，不回流进 ingest/retrieval。
  这在 §11.1 有明确的方法学声明。

---

## 4. 记忆形成侧 A：文档准入（document admission）

### 4.1 问题

v1 关键词门把助手的长交付物（表格、方案、推荐清单）整体拒绝 → assistant 题型的
gold 证据形成率只有 **7.14%**。这不是检索问题：**记忆根本不存在**。

### 4.2 机制

`semantic_gate.DocumentAdmissionPolicy` + `admit_document()`：
长 non-trusted 交付物以**文档记忆**准入，但**只作为文档**：

- `subject = <source type>`、`predicate = artifact_document`
  （`models.DOCUMENT_PREDICATE`）、`assertion = unstructured_document`、
  `kind = episode`、`evidence_state = OBSERVATION`；
- **没有标量值槽位**（无 `normalized_value`）→ 不参与 supersession
  （`relations.SINGLE_VALUED_PREDICATES`），也不参与冲突检测
  （`oracle.evidence.DOCUMENT_PREDICATES`）；
- 全部硬地板仍然生效，且顺序为：禁用 → 来源非 `conditional` →
  `DOCUMENT_TOO_SHORT` → `DOCUMENT_TOO_LONG` → 空/敏感数据 → 形状不可交付
  → 单值用户事实 → 非受信来源；
- **只有 `conditional` 来源可准入**；`untrusted`（抓取页、CRM 记录）保持 v1 拒绝。
- 准入时 `reason_code = DOCUMENT_FROM_NON_TRUSTED_SOURCE`——**这是接受码，不是拒绝码**
  （读日志时容易误判）；importance = 0.25、salience = 0.35、durability = `long_term`。

### 4.3 效果（§9 消融实测）

| 指标 | off | on |
|---|---|---|
| `formed` (assistant) | 4 / 56 = 0.0714 | **52 / 56 = 0.9286** |
| 每非空轮形成记忆数 | 0.517 | **0.995** |
| 总形成记忆数 | 5,666 | 10,900 |

形成阶段 **+51 / −0**：严格单调增益。

### 4.4 代价（同一消融实测）

长文档与短标量事实**共享**同一个 `limit=10` 结果位与 `token_budget=1500` 预算：

| 指标 | off | on |
|---|---|---|
| 平均上下文 token | 573.1 | **1,275.8** |
| 平均 packed 条数 | 7.16 | 5.47 |
| 被排除项总数 | 18 | **1,790** |
| `packed` gold 轮 | 607 | **524（−83）** |

→ **在默认 `legacy` 打分下，该机制净负。** 缓解手段已实现但默认关闭（§9.2）。

---

## 5. 记忆形成侧 B：单值用户事实的覆盖防护

（此机制在上一阶段落地，本节只做与本报告口径一致的复核，不重复其完整论证。）

**规则**：非 trusted 来源**不得创建"单值"用户事实**（`residence` / `primary_tool` /
`goal`）。`preference` / `statement` 可累加，不受限。

**为什么不能"一律禁 conditional 来源"**：忠实重复用户事实的摘要**必须被接受**，
一刀切会把一个假接受换成一个假拒绝。区分点是**单值 vs 多值**，不是来源本身。

**必须带 subject 判断**：第三方的 `residence`（"Alice lives in Shanghai"）是实体事实，
不是用户档案覆盖，**不能拦**。

**代价已实测**：`_is_single_valued_user_fact` 约 **12.99 µs/次**；全语料 124,331 条
合计 1.6 s，占全量 6,850 s 的 **0.024%**。LongMemEval 246,738 轮中该门**拦截 0 条**
→ 82.48% 形成率不需重跑。

**未决**：`ec-third-party-residence` 仍是 false_reject，**且在评分契约上不可满足**
（同一布尔被两个相反期望的轴引用 → 二值输出无解，需三态）。见 §10.2。

---

## 6. 检索侧：可配置评分策略与冲突检测修复

### 6.1 `ScoringPolicy`（`retrieval.py`）

新增 `ScoringPolicy` dataclass 与 `scoring_policy(name)` 解析器，两个内置策略：

| 策略 | 关键参数 | 行为 |
|---|---|---|
| `legacy`（**默认**） | 全部新增项为中性值 | **逐公式复现旧行为**，保证零回归 |
| `balanced` | `semantic=0.08`、`keyword=0.04`、`recency=0.10`、`temporal=0.08`、`superseded=0.6`、`archived=0.8`、`document_weight=0.85`、`document_char_budget=1200` | legacy + 时近性 + 时间窗契合 + **文档阻尼** |

`store._scoring_quality()` + `_recency_factor()` + `_temporal_fit()` 实现，
`policy.document_weight` 只对 `predicate == artifact_document` 生效。

**为什么需要 `document_weight`**：一旦长交付物被准入为文档记忆，它们**既多又长**
（语料均 2,166 字符 vs 1,500 token 预算），单靠时近性/重要性重排会让它们把短标量事实
挤出 10 个结果位。这正是 §9.2 观测到的损失。

### 6.2 证据冲突检测修复

`oracle/evidence.py` 的 `DOCUMENT_PREDICATES = {"statement", artifact_document}` 与
`_scalar_value()` 返回 `None` 的规则：**文档之间不是同一槽位的两个竞争值**。
把它们纳入冲突分组会让**每一对**已形成的陈述都看起来互相矛盾——即把一个冲突检测器
退化成常量 `CONFLICTING_EVIDENCE` 信号。修复后冲突分组只对 CURRENT 且**有标量值**的
记忆成立。

---

## 7. 上下文打包：`content_caps`

`context.pack_context(..., content_caps: Mapping[str, int] | None = None)`：
按谓词给出**字符上限**；超限的记忆被**截断而非丢弃**，并记录
`decision = TRUNCATE`、`reason = content_cap`（含 `cap` 与 `original_chars`）。
截断尾部追加 `...[truncated to fit the context budget]`，使 reader 至少知道它存在及其开头。

- 映射由策略派生而非硬编码：`pipeline.packing_content_caps(scoring)` 只在
  `document_char_budget > 0` 时返回 `{DOCUMENT_PREDICATE: budget}` →
  `legacy` 下为空映射，**输出与改动前逐字节相同**。
- **本轮的状态**：该路径**只在 D 臂激活**。A / B / C 三臂 `document_char_budget = 0`，
  截断次数均为 **0**；D 臂产生 **1,315 次 `TRUNCATE`**（439 / 500 题，移除 1,440,990 字符），
  同时把 `EXCLUDE / token_budget` 由 1,790 压到 520。
  **因此它现在既由单元测试覆盖（§10.1 的 7 项），也由一次真实 500 题运行覆盖**，
  而不再只是纸面机制。
- 反过来说，**B 臂的 `packed` 损失发生时阻尼器是关闭的**——这是理解 §9.2 与 §9.3
  差异的前提。

---

## 8. Reader 侧

- `ReaderPolicy`：`strict` / `evidence_first`（两臂均用 `evidence_first`，
  见 manifest `configuration.oracle_pipeline.reader_policy`）。
- **拒答重试**：`max_refusal_retries = 1`——首次拒答时以增强的 evidence 提示重试一次，
  用于减少"证据充分却拒答"（基线诊断中 `SUFFICIENT_EVIDENCE_REFUSED = 54`）。
  **触发条件很窄**：必须同时满足 `max_refusal_retries > 0`、检索有 items、状态是 ABSTAIN、
  `reason_code` 非非法响应、且证据评估**不是** `NO_EVIDENCE`。
  重试**不能凭空造答案**：第二次尝试仍须通过 `grounded-answer-v1`，
  即"由拒答变成作答"必须逐字引用某条已返回记忆。
  （两臂消融用确定性 reader，未走这条路径 → 它的效果**本轮未被测量**。）
- provider 中立：`OracleReaderRequest` 是契约，`DeterministicReader`（抽取式，
  answer = top memory content 原文）用于消融隔离机制；`GroundedChatReader`
  用于真实 API。两臂消融用确定性 reader，**因此消融只反映记忆侧机制，不含 reader 能力**。

---

## 9. 消融实验：文档准入 × 评分策略（2×2 全因子）

### 9.1 实验设计

| 项 | 值 |
|---|---|
| 设计 | 2×2：`--document-admission` ∈ {off, all} × `--scoring-policy` ∈ {legacy, balanced} |
| 数据集 | oracle 500，SHA-256 `821a2034…`（四臂同值） |
| 门 | `semantic-utility-v2.1` |
| reader | `deterministic-extractive-v1`（隔离记忆侧） |
| embedder | `deterministic-test-provider`（**非语义**，见 §11.3） |
| 检索 | `limit=10`、`token_budget=1500`（四臂同值） |
| 完成度 | 四臂各 **500/500 COMPLETED，0 failed，0 timeouts** |
| 耗时 | A 352.6 s ／ B 394.3 s ／ C 379.1 s ／ D 357.8 s |
| 配置哈希 | A `6025c41e…` ／ B `482f73cf…` ／ C `b60b1328…` ／ D `692417f5…` |
| 测量 | `ops/oracle_formation_coverage.py` 三层覆盖；`ops/oracle_coverage_delta.py` 求差 |

**隔离性已机器核对**（逐臂比对 manifest 的每个配置键，只列差异）：

| 配对 | 差异键 |
|---|---|
| A ↔ B | 仅 `document_admission` |
| A ↔ C ／ B ↔ D | 仅 `oracle_pipeline`（其中含 `scoring_policy`） |
| C ↔ D | 仅 `document_admission` |

即每个差分**只有一个自变量**，"2×2"是干净的全因子设计而非四个随意运行。

### 9.2 结果一：文档准入的独立效应（A → B）

- `formed` **+51 / −0** → 形成阶段**严格单调增益**，从不丢失已形成的 gold 轮；
- `retrieved` +51 / −85、`packed` +49 / −107 → 损失全在下游；
- **107 题**至少失去一个层级（明细见 `oracle-coverage-delta-admission.json`
  的 `regressed_cases`）；
- **净为负：`packed` −83**。

### 9.3 结果二：阻尼器（B → D）与净效应（A → D）

**B → D（准入固定 `all`，只换打分策略）**：

| 层级 | before | after | Δ | 逐题方向 |
|---|---|---|---|---|
| `formed` | 781 | 781 | **0** | 0 / 0（500 题全不变） |
| `retrieved` | 554 | 608 | **+54** | **+49 / −0**（严格单调） |
| `packed` | 524 | 599 | **+75** | +71 / **−2** |

阻尼器**几乎把损失全部收回，且不损失形成**。逐题型收益分布均匀
（multi-session +44、temporal +15、knowledge-update +7、preference +5、user +4、
assistant 0——assistant 已在 B 臂拿满）。

**A → D（净效应：原始基线 vs 准入+阻尼）**：

| 层级 | before | after | Δ | 逐题方向 |
|---|---|---|---|---|
| `formed` | 730 | 781 | **+51** | +51 / −0 |
| `retrieved` | 607 | 608 | **+1** | +51 / −46 |
| `packed` | 607 | 599 | **−8** | **+50 / −50** |

**结论与边界**：

1. 阻尼器把 `packed` 的 **−83 收回为 −8**，即从"明确净负"变为"**近似中性**"。
2. 但净效应**不是均匀改善，而是一次结构重构**：assistant 的 packed gold 轮
   **4 → 51（+47）**，代价是 multi-session **−23**、temporal **−25**、
   knowledge-update **−9**；preference 与 user 基本不变（0 / +2）。
3. **因此不能宣称"改进"**：在 `packed` 这个"reader 能用上什么"的上界口径上，
   整体几乎持平（−8 / 886 = −0.9%），50 题变好、50 题变差。
   它换来的是一件事：**最差题型（assistant 0.0536 all-cases accuracy，§2.4）
   的证据可得性大幅提升**。
4. **准确率层面已部分测出，见 §9.4**（`official_score = NOT_EVALUATED` 的定性不变）。
   用真实 DeepSeek reader 复跑后给出 `full_set_accuracy` 的**自测**读数：
   `B → D`（只换打分策略）三个 judge 一致为正；`A → D` 因 **reader prompt 版本混淆**
   **不可归因**，去混淆所需的 A′ 臂因 API 余额耗尽未产出。
5. **默认配置建议**：`--document-admission all` **不应**单独启用；
   若启用，**必须**同时 `--scoring-policy balanced`。是否启用则取决于
   "是否愿意用 multi-session / temporal 的覆盖换取 assistant 的覆盖"——
   这是一个需要题型权重（或端到端分数）才能回答的决策，不是本报告能单方面下的结论。
   （§9.4 补充：`balanced` 本身在准确率层是三 judge 一致的正效应，上句的"对冲"因此至少
   在读侧被证实为真实对冲，而非仅覆盖计数上的权衡。）

### 9.4 准确率层：真实 reader 复跑（哪些结论成立、哪些被余额中断）

先把覆盖层的"结构重构"翻译成"分数变化"：用**真实 DeepSeek reader**（`deepseek-flash`，
`--extra-body '{"thinking":{"type":"disabled"}}'`）复跑新臂。按用户决定**放弃 C 臂**
（`off`+`balanced`），故精度层为 **A 基线 + B + D** 三点，**A′ 缺**。

| 臂 | reader prompt | admission | scoring | 状态 |
|---|---|---|---|---|
| A | `oracle-grounded-reader-v1` | off | legacy | ✅ 已发布基线，本轮用**同版判分器**重判（§2.3）|
| B | `oracle-grounded-reader-v2` | all | legacy | ✅ 500/500（493 C + 7 DEGRADED），422 答 / 72 拒 / 6 无效 |
| D | `oracle-grounded-reader-v2` | all | balanced | ✅ 500/500（493 C + 7 DEGRADED），434 答 / 59 拒 / 7 无效 |
| **A′** | `v2` | **off** | **legacy** | ❌ **未产出** |

**A′ 为什么必需**：A 的 manifest **没有 `reader_policy` 键**（该字段晚于基线运行），
其 `prompt_version` 是 `v1`；而当前默认是 `evidence_first` / `v2`
（`reader.py` 注释写明 `strict` "逐字节复现 v1 行为"）。**A → D 同时变了读侧策略与记忆侧配置**，
本轮未能补跑 A′ 去混淆，原因见 §9.4.4。

**结果（`full_set_accuracy`，分母 500；各臂判分器版本与配置完全一致，故可比）**：

| 臂 | exact | ds-legacy | ds-rubric | 拒答率 | 作答数 | reader 调用 | reader 输入 token |
|---|---|---|---|---|---|---|---|
| A（v1）| 0.3480 | 0.4180 | 0.4360 | 0.510 | 245 | 500 | 562,290 |
| B（v2）| 0.4160 | 0.5380 | 0.5500 | 0.156 | 422 | 577 | 1,370,819 |
| D（v2）| **0.4340** | **0.5760** | **0.6180** | 0.132 | 434 | 560 | 1,622,123 |

#### 9.4.1 唯一干净的对比：B → D（同 v2、同 admission，只换打分策略）

| judge | B | D | Δ |
|---|---|---|---|
| exact | 0.4160 | 0.4340 | **+1.80 pp** |
| ds-legacy | 0.5380 | 0.5760 | **+3.80 pp** |
| ds-rubric | 0.5500 | 0.6180 | **+6.80 pp** |

- 三个 judge **方向一致**（2 个独立 LLM 判分 + 1 个确定性判分），无一例外。
- 与覆盖层的 `packed` **严格单调 +75**（§9.3）方向一致，量级也同向。
- 同时 `retrieved` +54（+49/−0）、拒答率 0.156 → 0.132。
- **但"方向一致"不等于"可确认"**（§9.5）：
  - 唯一无判题失败的 `exact` judge 上 **McNemar p = 0.2624**，95% CI [+0.00, +4.59]（含 0），
    实现功效仅 **24.2%**；500 题的**最小可测效应为 3.99 pp** > 观测到的 1.80 pp。
  - `ds-rubric` 的 +6.80 pp 受**仪器污染**：两臂判题失败数 34 vs 12，
    而 `full_set_accuracy` 把失败按答错计 → 最多 **4.40 pp** 与记忆配置无关，
    真实效应只能界定在 **[+2.40, +6.80]**。
- → 正确表述是：**`balanced` 的作用方向在三种判分下都没有出现反号，
  但其量级在 500 题规模下不可分辨**；据此**不能**宣称"已证明净正"。
  决定性证据仍在覆盖层（精确算术），而不在这一层。

#### 9.4.2 不可归因：A → D

| judge | A | D | Δ |
|---|---|---|---|
| exact | 0.3480 | 0.4340 | +8.60 pp |
| ds-legacy | 0.4180 | 0.5760 | +15.80 pp |
| ds-rubric | 0.4360 | 0.6180 | +18.20 pp |

数字方向一致且幅度大，但**同时混合了 (i) reader 策略 v1→v2 与 (ii) 记忆侧配置**。
**本报告不对该差值做任何单侧归因**，只把它作为"合计效应"记录。

#### 9.4.3 混淆范围的量化（零成本证据）

基线的拒答根因（`eval/reports/oracle-failure-analysis-v2/`）给出一个关键事实：
**`PARSER_OR_STRUCTURED_OUTPUT_FAILURE = 0`** —— v1 的 **255 次拒答不是**结构化输出失败，
而是**真实读侧拒答**。其根因构成（首命中决策表，属诊断分组）：

| 根因桶 | 计数 | 明确归属 |
|---|---|---|
| `TEMPORAL_AMBIGUITY` | 74 | 混合 |
| `SUFFICIENT_EVIDENCE_REFUSED` | 54 | **读侧**（gold 已进上下文仍拒答）|
| `NO_EVIDENCE_RETRIEVED` | 31 | 记忆侧 |
| `ASSISTANT_MEMORY_MISSING` | 30 | 记忆侧 |
| `GOLD_EVIDENCE_NOT_FORMED` | 22 | 记忆侧 |
| `PREFERENCE_AGGREGATION_FAILURE` | 14 | 混合 |
| `WEAK_EVIDENCE` / `UNDER_ABSTENTION_ON_UNANSWERABLE` | 3 / 3 | — |

记忆侧明确桶合计 **83 / 255 = 32.5%**；读侧明确桶 **54 / 255 = 21.2%**。

→ 拒答率 **0.510 → 0.132** 的跌幅里，**两侧都有实质贡献**，且读侧的
`SUFFICIENT_EVIDENCE_REFUSED`（"证据已在上下文里、读者仍拒答"）这一类正是 v2 策略
明确要修的。**这解释了为什么必须跑 A′ 才能谈归因，也解释了为什么本报告拒绝归因。**

#### 9.4.4 中断原因（如实记录）

补跑 A′ 时 DeepSeek 返回：

- 第一次 **HTTP 429**：`Your current concurrency is 6 ... based on your remaining balance`
  —— 该账号并发额度受**余额**限制；触发原因是**判分（8 并发）与 reader 臂同时运行**。
- 第二次 **HTTP 402：`Insufficient Balance`** —— 余额耗尽，所有后续调用失败。

A′ 已跑到 58/500（有 checkpoint，可 `--resume` 续跑）。**续跑命令**（余额恢复后）：

```powershell
$env:DIVE_RUN_TAG="oracle-ds-A2-500"; $env:DIVE_ADMISSION="off"; $env:DIVE_SCORING="legacy"
& .venv-gpu\Scripts\python.exe <Claw>\_run_ds_A2.py     # 内含 --resume
# 判分（务必与 reader 错开，workers ≤4，避免再次 429）：
$env:DIVE_EVAL_RUN_DIR="<repo>\eval\reports\oracle-ds-A2-500"; $env:DIVE_JUDGE_WORKERS="4"
& .venv\Scripts\python.exe <Claw>\_judge_arm_parallel.py
```

#### 9.4.5 新出现的取舍（必须与 `full_set_accuracy` 并列披露）

D 把拒答从 255 降到 66，**代价是真·不可答题的拒答纪律变差**：

| 指标 | A | D |
|---|---|---|
| 30 道不可答题判对（ds-legacy / ds-rubric）| 27 / 27 | **15 / 15** |
| `answered_only_accuracy`（exact）| 0.7102 | **0.5000** |
| `answered_only_accuracy`（ds-rubric）| 0.8898 | 0.7120 |

即：**收益来自"敢于作答"，不是"答得更准"**——多答的题平均质量更低。净效应仍为正
（`full_set_accuracy` 上升），但这正是 §2.1 强调"两个口径必须并列读"的实例：
只看 `full_set` 会把它读成纯粹的进步。

#### 9.4.6 判分可靠性（保守性声明）

本轮判分对失败**重试**（最多 4 次、指数退避）。残余永久失败 B 臂 **34** 题、D 臂 **12** 题
（`judge_errors`），这些题在 `full_set_accuracy` 中按**未答对**处理
→ 表中 D 的 `ds-rubric = 0.6180` 是**保守下界**，不是乐观估计。
两臂失败数不等这一点在 §9.5 会变成一条**独立的削弱理由**。

---

### 9.5 统计功效：本实验能分辨多大的效应

§9.4 的每一条结论都必须在功效约束下读。做法：用**确定性 `exact` judge**
重建**逐题**判定 —— 它是本地纯函数、零判题失败，故可对**任意臂**免 API 复算 ——
再对配对二元结果做 **McNemar 精确检验**。

**该复算已逐题校验**：与流水线自己落盘的 `judge_verdicts-exact.jsonl`
（B、D 两臂）比对，**各 500/500 一致、0 处不符**；聚合成 `full_set_accuracy`
也与各臂 `evaluation-exact.json` 完全相同（0.416 / 0.434）。

| 对比 | 归因性 | Δ | 95% CI | McNemar p | 功效 | n=500 最小可测效应 | @80% 所需题数 |
|---|---|---|---|---|---|---|---|
| **B → D** | **干净** | **+1.80 pp** | [+0.00, +4.59] | **0.2624** | 24.2% | 3.99 pp | **2,469** |
| A → B | 混淆 | +6.80 pp | [+2.85, +10.75] | 0.0011 | 91.5% | 5.70 pp | 351 |
| A → D | 混淆 | +8.60 pp | [+4.65, +12.55] | 0.0000 | 98.7% | 5.73 pp | 220 |

→ **唯一干净的对比不显著（p = 0.2624），95% 置信区间含 0。**
而且这不是"样本运气不好"，是**设计层面的不可分辨**：
在观测到的翻转率下，500 题的最小可测效应是 **3.99 pp**，而效应只有 **1.80 pp**
—— **即使把 A→D 的混淆彻底消除，500 题的 Oracle 集合也答不出这个问题**；
要分辨 1.80 pp 需要约 **2,469 题 = 本集合的 5 倍**。
标准误 **1.43 pp** 与效应 **1.80 pp** 同量级。

**两条显著的效应（A→B、A→D）恰好都是不可归因的那些。**

B → D 的翻转在题型间**方向不一致**，故它是**重排而非一致改善**
（与 §9.3 覆盖层的"结构重构"一脉相承）：

| 题型 | n | 增益 | 损失 | 净 |
|---|---|---|---|---|
| multi-session | 121 | 14 | 6 | +8 |
| knowledge-update | 72 | 3 | 8 | **−5** |
| temporal-reasoning | 127 | 6 | 3 | +3 |
| single-session-assistant | 56 | 4 | 1 | +3 |
| single-session-user | 64 | 1 | 0 | +1 |
| abstention | 30 | 2 | 3 | −1 |
| single-session-preference | 30 | 0 | 0 | 0 |

**第二条独立削弱：判分失败的不对称污染。**

`full_set_accuracy` 分母固定 500，**判题失败按答错计**。B 臂 rubric 失败 34 次、
D 臂 12 次 → **22 题纯属仪器差**，最多贡献 **22/500 = 4.40 pp**。
即 `+6.80 pp` 里最多有 4.40 pp 与记忆配置无关，真实效应只能界定在
**[+2.40 pp, +6.80 pp]**，无法收窄（要收窄必须有逐题 rubric verdict，当前未落盘）。

**据此得到的诚实结论**：

1. 本实验**不能**确立"记忆侧配置提升准确率"。唯一无混淆的对比 B→D = +1.80 pp、
   p = 0.2624，且该量级在 500 题规模下**原则上不可分辨**（MDE = 3.99 pp）。
2. 能**确定性**成立的是**覆盖层**结论（C≡A 逐题恒等、准入 +51/−0 `formed`、
   B→D `packed` +75、D 臂 1,315 次截断）—— 它们是**精确算术，不含抽样噪声**。
   → **对这个效应量，确定性覆盖层比准确率层是更灵敏的仪器**；
   代价是覆盖是**上界代理**，不等于准确率。
3. 能**显著**成立的两条（A→B +6.80 pp、A→D +8.60 pp）**恰恰不可归因**。
4. §9.3 的"覆盖重构 ≠ 改进"在准确率层**被再次证实**：B→D 的净 +9 翻转由
   +30 / −21 相抵而来，且题型间方向相反。
5. 因此本轮的可靠产出是**基础设施与方法学**（500/500 合法提交、断点续跑、
   口径缺陷发现、三层覆盖记账、成本账），**不是**一条关于记忆配置的因果结论。

产物：`eval/reports/oracle-accuracy-significance.json`。

---

## 10. 测试、回归与失败记录

### 10.1 测试

**本文件报告的测试结果均为本轮实测。**

| 套件 | 结果 |
|---|---|
| `tests/test_context_packing.py` | **12 passed**（原 5 + 新增 7，覆盖 `content_caps`） |
| `tests/test_oracle_v2_quality.py` | **11 passed** |
| **全量回归** | **439 passed / 9 skipped / 0 failed**（94.9 s） |

较本阶段记录在案的基线 **432 passed** 增加 7 项，增量**全部**来自 §7 的
`content_caps` 测试，无其它计数变化 → 无回归。

新增 7 项覆盖（`content_caps` 此前**完全没有测试**）：

| # | 测试 | 断言要点 |
|---|---|---|
| 1 | `test_document_content_cap_truncates_without_dropping_the_memory` | 截断而非丢弃；记忆对象**不被就地修改**；`TRUNCATE` 决策含 `cap`/`original_chars` |
| 2 | `test_capped_document_no_longer_crowds_out_a_later_fact` | **机制的存在理由**：未设上限时长文档独占预算并挤掉后续标量事实；设上限后两者共存 |
| 3 | `test_absent_or_empty_content_caps_leave_packing_byte_identical` | `None`/`{}`/`{pred: 0}` 三种"无上限"输入与改动前**逐字节相同** |
| 4 | `test_content_cap_above_the_content_length_emits_no_truncate_decision` | 未超限不产生 `TRUNCATE` |
| 5 | `test_content_cap_does_not_leak_to_unmapped_predicates` | 上限**不泄漏**到未映射谓词 |
| 6 | `test_packing_content_caps_is_derived_from_the_scoring_policy` | `legacy` → `{}`；`balanced` → `{artifact_document: 1200}` |
| 7 | `test_balanced_policy_truncates_a_corpus_average_document_where_legacy_does_not` | 语料均值 2,166 字符文档：`legacy` 不截断、`balanced` 截到 1,200 |

### 10.2 未决事项

| 事项 | 状态 |
|---|---|
| `ec-third-party-residence` false_reject | **评分契约上不可满足**：同一布尔须同时等于 `memory_expected(=True)` 与 `user_fact_allowed(=False)` → 二值无解，需三态门控（架构层） |
| 「分句否定」放宽 | **已实测否决**：标注集上只在两轴间搬分（净零）；真实语料会重新放行 246,738 轮中的 **18,162 轮（7.36%）**，而否定守卫是承重结构（拦下全语料的 14.39%）→ 会改变 82.48% 形成率并让 1.9 h 基准作废 |
| 阻尼臂 | **已完成**（§9.3）：`balanced` 把 `packed` 由 **−83 收回为 −8**，但净效应是**结构重构而非整体改善**。**仍缺**：准确率层面的验证（需真实 reader 复跑） |

---

## 11. 复现、产物与下一步

### 11.1 覆盖分析的方法学声明

- **不是准确率**。`formed` / `retrieved` / `packed` 是对"reader 能用上什么"的
  **上界**：记忆存在 → 才可能被检索 → 才可能进入上下文。报告的 `interpretation`
  块内固定写入该声明（`is_accuracy: false`）。
- **单位已核实**：`retrieval.returned_source_refs` 携带 **event id**（`evt_*`），
  不是 turn ref；gold turn ref 经 `formation.turn_events` 映射。此点是对照已发布运行
  实测确认的，不是假设。
- `packed` 的取法在两臂**刻意保持一致**：`returned_memory_ids` 与
  `returned_source_refs` 是同一次 comprehension 产出的平行列表，zip 后与
  `packing.packed_memory_ids` 求交。**不采用 `evidence.views`**（仅新 trace 有），
  因为"两臂之间悄悄换单位"就不是对照。
- 分析器**逐位复现了已发布基线**（§11.3），这同时验证了它的口径。
- **两个 gold 分母不可混用**：`per_case[].gold_turns` 统计**全部 500 题**，合计 **896** 轮；
  `overall.gold_turns` 与 `by_question_type` **排除 30 道 `_abs` 拒答题**，为 **886** 轮；
  被排除的 **10** 轮（multi-session 4、temporal-reasoning 6）在
  `abstention_cases` / `abstention_by_question_type` 中单独报告。
  产物内含 `reconciliation` 块给出恒等式：
  `by_question_type[t] + abstention_by_question_type[t] == by_question_type_all_cases[t]`。
  **按 `per_case` 求和却期望得到 886 会得到 896——这是分母不同，不是数据错误。**
  （该恒等式在写入前被脚本逐题型反算校验过。）

### 11.2 产物

| 路径 | 内容 |
|---|---|
| `eval/reports/oracle-formation-500-off/` | 臂 A（off/legacy），500/500，352.6 s，`configuration_sha256 6025c41e…` |
| `eval/reports/oracle-formation-500-on/` | 臂 B（all/legacy），500/500，394.3 s，`482f73cf…` |
| `eval/reports/oracle-formation-500-off-balanced/` | 臂 C（off/balanced），500/500，379.1 s，`b60b1328…` |
| `eval/reports/oracle-formation-500-on-balanced/` | 臂 D（all/balanced），500/500，357.8 s，`692417f5…` |
| `eval/reports/oracle-coverage-baseline.json` | 已发布 DeepSeek 500 的三层覆盖 |
| `eval/reports/oracle-coverage-{off,on,off-balanced,on-balanced}.json` | 四臂各自的三层覆盖 |
| `eval/reports/oracle-coverage-delta-admission.json` | **A→B** 准入消融差 |
| `eval/reports/oracle-coverage-delta-scoring-off.json` | **C→A** 打分策略消融差（无文档时的对照） |
| `eval/reports/oracle-coverage-delta-scoring-on.json` | **B→D** 阻尼消融差 |
| `eval/reports/oracle-coverage-delta-net.json` | **A→D** 净效应差 |
| `ops/oracle_formation_coverage.py` | 三层覆盖计算（离线，无 API key） |
| `ops/oracle_coverage_delta.py` | 两报告求差（逐层 + 逐题方向 + 回归清单） |

**准确率层（§9.4，真实 DeepSeek reader）**：

| 路径 | 内容 |
|---|---|
| `eval/reports/oracle-deepseek-500/` | 臂 A = 已发布基线（v1 prompt / off / legacy），本轮重判为同口径 |
| `eval/reports/oracle-ds-B-500/` | 臂 B（v2 / all / legacy），500/500，422 答 / 72 拒 / 6 无效 |
| `eval/reports/oracle-ds-D-500/` | 臂 D（v2 / all / balanced），500/500，434 答 / 59 拒 / 7 无效 |
| `eval/reports/oracle-ds-A2-500/` | 臂 A′（v2 / off / legacy）——**未完成**，58/500 检查点，余额耗尽 |
| `…/evaluation-{exact,ds-legacy,ds-rubric}.json` | 三 judge 产物（当前 `evaluate.py`，失败重试） |
| `…/evaluation-*.oldcode.json` | 旧口径产物**原样保留**，供 §2.3 的口径修正对照 |
| `eval/reports/oracle-accuracy-significance.json` | **§9.5** 的配对显著性 / 功效产物（McNemar、CI、MDE、仪器污染） |

**命名说明（避免误读）**：§9.4 与 §9 的 A/B/C/D 按
**(`admission`, `scoring`) 组合**命名，**不是**按目录命名。同一个"臂 B"在覆盖层落在
`oracle-formation-500-on/`（确定性 reader，用于隔离记忆侧），在准确率层落在
`oracle-ds-B-500/`（真实 DeepSeek reader）。两者**记忆侧配置完全相同**，
差别只在 reader——这是刻意的两层设计，不是两次不同的实验。

每份 delta 产物都含 `regressed_cases` 逐题明细（哪一题在哪个层级变差、差多少），
因此"净额"永远可以拆回具体题目，不必相信汇总。

产物中 `official_score` / `self_reported_accuracy` 均为 `NOT_EVALUATED`
（`dive-oracle-submission-v1` schema 内固定）。

### 11.3 可复现性

**四臂共有且已核对一致**：`git.commit = cc3d060`、`dirty = true`、
dataset SHA-256 `821a2034…`、`seed = 0`、gate `semantic-utility-v2.1`、
embedder 确定性、`limit = 10`、`token_budget = 1500`、`reader_policy = evidence_first`。
每臂相对其它臂的差异键已在 §9.1 列全——**每一对只差一个自变量**。

**打分策略的钉扎方式**：四臂 manifest 只记录 `oracle_pipeline.scoring_policy`（名字），
**但 trace 额外记录了名字 + 64 位指纹**，且两枚指纹都能从代码精确复现：

| 策略 | trace 内指纹（前 16 位） | 关键参数 |
|---|---|---|
| `legacy` | `3a4a0b1038ed50a8` | `document_char_budget = 0`、`document_weight = 1.0` |
| `balanced` | `63bc17c39cf49228` | `document_char_budget = 1200`、`document_weight = 0.85` |

所以"生效的字符上限是 **1200**"**可还原**（名字 → 指纹 → `SCORING_POLICIES`），
只是**没有内联进产物**；还原需要代码版本，而代码版本只以 `dirty=true` 记录。
这是一个**已知、非阻塞**的缺口，不是"上限不可知"。

**残留不足（如实记录）**：

1. **`dirty = true`**：运行发生于未提交工作树，**仅凭 commit 不可重建**。
   缓解：本报告记录覆盖相关源文件的 SHA-256（前 16 位）作为锚点——
   `context.py 06d047856efb6f62`、`retrieval.py 7089ea4ce6341a44`、
   `store.py aa2720621a3a8233`、`semantic_gate.py 06dc03f98bb5f173`、
   `extraction.py 00524b7081cbf2fd`、`models.py 776892754b56280f`、
   `oracle/pipeline.py 3fe73142343c33bc`、`oracle/evidence.py f9a01214fcdd7f90`、
   `oracle/runner.py 0fa97ffa82514b37`、`oracle/reader.py d4fee1a1182b8ff5`。
2. **off 臂（A）与已发布基线的逐题差异**：`overall` 与 `by_question_type`
   **逐字节相同**（含 `memories_formed=5666`、`nonempty_turns=10960`、
   `encoder_inputs=3734`、`mean_context_tokens=573.064`、`total_omitted_items=18`）；
   500 题中 **498 题逐字段相同**，仅 2 题（`gpt4_ab202e7f`、`0bc8ad92`）
   在 `context_tokens` 上相差 **±1**（826↔827、742↔741），且这两题的
   `formed`/`retrieved`/`packed` 计数**完全一致**。
   该 1 token 差异**不影响任何报告的覆盖计数**；成因尚**未**确证
   （候选解释：跨进程的等分候选排序不稳定，或基线工作树与当前树的微小代码漂移），
   下一步实验已在 §11.4 给出。
   **一个相关的解读陷阱**：C 臂与 A 臂的 `INCLUDE`/`EXCLUDE` 计数完全相同
   （3,581 / 18），但 `mean_context_tokens` 差 0.3（572.8 vs 573.1）。
   → **计数相同不等于成员相同**；只在计数层面比较两个臂会漏掉成员置换。
   （两臂的 gold 覆盖仍逐题一致，故本报告的结论不受影响。）
3. **嵌入器非语义**：两臂 `embedder = deterministic-test-provider (non-semantic)`，
   即**记忆侧稠密通道是 hash 桩**，检索主要由关键词/BM25 驱动。因此 §9 度量的是
   记忆侧机制在非语义检索下的效应，**不可外推为语义检索下的效应**。
   （已发布 DeepSeek 500 基线与 off 臂逐字节一致 → 该基线同样运行在非语义嵌入下，
   两臂与基线的可比性因此成立。）
4. **覆盖 ≠ 准确率**：§9.4 已补产**部分**端到端自测准确率（真实 DeepSeek reader）。
   `B → D` 是唯一无混淆的对比，三种 judge 方向一致，但**在功效上不足以确认**：
   §9.5 的 McNemar **p = 0.2624**、CI 含 0、500 题的**最小可测效应 3.99 pp**
   > 观测效应 1.80 pp。`ds-rubric` 的 +6.80 pp 另受两臂判题失败数不等（34 vs 12）
   的仪器污染，最多 4.40 pp 与记忆配置无关。
   `A → D` 因 reader prompt v1→v2 混淆**不可归因**；去混淆所需的 A′ 臂因 API 余额耗尽未产出。
   三份 judge 产品的下界性质（§2.3）不变。
5. **本轮的可靠产出是基础设施与方法学，不是因果结论**：
   500/500 合法提交、断点续跑、`judged`/500 口径缺陷的发现（§2.3）、
   三层覆盖记账（§11.1）、成本账（§11.5）。
   对这个效应量，**确定性覆盖层（精确算术、无抽样噪声）比准确率层是更灵敏的仪器**；
   代价是覆盖只是**上界代理**。

---

### 11.4 下一步（按优先级）

1. **补跑 A′ 臂（`off`+`legacy`，当前 v2 reader 策略）——当前唯一决定性的实验。**
   它正是把 `A → D` 的 **+8.60 / +15.80 / +18.20 pp** 拆成"读侧策略 v1→v2"与
   "记忆侧配置"两部分所缺的那一个点；§9.4.3 已把混淆范围量化到
   记忆侧明确桶 32.5%、读侧明确桶 21.2%，**两侧都不小**，故不补 A′ 就无法归因。
   **补 A′ 后可得两个干净对比**：`A → A′`（只换 prompt v1→v2）、
   `A′ → B`（只换准入 off→all）。
   **但要先接受功效约束（§9.5）**：500 题的最小可测效应约 **4–6 pp**，
   故只有当任一侧效应达到该量级时结论才会显著；若真实效应是 2 pp 量级，
   则 A′ 跑完**仍将不显著**——那是**基准规模**的限制，不是执行问题。
   A′ 已有 **58/500 检查点**，可 `--resume` 续跑；命令与判分注意
   （`DIVE_JUDGE_WORKERS ≤ 4`，**勿与 reader 并行**以免再次 429）见 §9.4.4。
   **C 臂已按用户决定放弃**（覆盖层已证 C≡A），补 A′ 后精度层即为完整的三点对照。
2. **确证 §11.3 第 2 条的 1 token 差异**：用 `--ids-file` 只跑那 2 题，
   在两个独立进程中各跑一次。若两次彼此一致 → 属代码漂移；若不一致 →
   属跨进程排序不稳定，应固定 `PYTHONHASHSEED` 并写入 run 的复现指引。
3. **语义嵌入下的重测**（§11.3 第 3 条）：以
   `--embedder local --embedding-model BAAI/bge-m3` 重跑 A/D 对照。
   当前被选中的证据轮主要由关键词通道决定，换成语义嵌入后
   D 臂的增益幅度**可能显著不同**。
4. **把生效的评分参数内联进 manifest**：现在需要"名字 → 指纹 → 代码"三步
   才能得到 `document_char_budget = 1200`。在 `runner` 的
   `configuration.oracle_pipeline` 里直接写入 `scoring_policy.to_dict()` 即可闭环。
   （本轮**有意未改**：产出证据之后再去改产出者，会让"哪棵树产出了这个产物"更难回答。）
5. **不要单独启用** `--document-admission all`；若启用，**必须**同时
   `--scoring-policy balanced`。理由是**覆盖层**的精确算术（准入单独启用使
   `packed` −83，阻尼收回为 −8）——这是本轮唯一不含抽样噪声的证据。
   准确率层只提供了**方向一致的弱支持**（三 judge 同号但 `exact` 上 p = 0.2624，
   见 §9.4.1 / §9.5），**不足以单独支撑该建议**。
6. **额度纪律（本轮教训）**：`--document-admission all` 让 reader **输入 token 涨 2.4–2.9×**
   （562,290 → 1,370,819 / 1,622,123），这是"覆盖换分数"的直接账单。
   且该账号**并发额度随余额下降**：余额低时并发上限降到 6，判分 8 并发 + reader 并行
   会直接把 reader 打挂（HTTP 429），余额耗尽后是 HTTP 402。
   → 判分与 reader **必须错开**，`DIVE_JUDGE_WORKERS ≤ 4`。
   更完整的成本拆解与四条杠杆见 §11.5。

### 11.5 低成本评测流程（本轮实测成本拆解）

**先问"贵在哪"**（本轮实测，单位 token）：

| 环节 | 用量 | 占比 |
|---|---|---|
| reader（B 臂 `all`） | 1,457,573 | — |
| reader（D 臂 `all`） | 1,709,742 | — |
| reader 合计（含 A′ 的部分） | ≈ 3,167,315 | **≈ 80%** |
| judge 合计（A 重判 + B + D 判分） | ≈ 787,234 | **≈ 20%** |

→ **判分不是大头，reader 才是**；但判分里仍有约六成是纯浪费。

**四条杠杆，按实测收益排序**：

| # | 杠杆 | 实测依据 | 收益 | 风险 |
|---|---|---|---|---|
| 1 | **条件准入**代替全局 `all` | 准入增量 1,059,833 token 全量摊在 500 题上；只对需要文档的题（assistant 56 题）准入 → D 臂输入 1,622,123 → **≈680,991** | **reader 输入 −58%** | 需改 `admit_document` 触发条件（代码改动，非开关） |
| 2 | **配对判分**（只判 hypothesis 变化的题） | B vs D **297/500 = 59.4% 的 hypothesis 逐字节相同**；判分 prompt 是 `(instruction, question, reference, hypothesis)` 的纯函数且 `temperature=0` | **judge −59.4%** | 无（口径不变，已端到端验证：reuse 297/500 后 `full_set` 仍为 **0.434**） |
| 3 | **覆盖层先筛（免费）** | `--reader deterministic` 的 §9 消融**零 API 成本**；本轮所有候选配置本可先过这一层 | 避免对错误配置付费 | 无 |
| 4 | **分层小样本先验** | 项目已有 `--stratified`；100 题 ≈ 1/5 成本 | **−80%**（方向性验证） | **1.80 pp 在 500 题上标准误 ≈2.2 pp** → 小样本**只能证明大效应**，不能证明 1.80 pp |

另有两项次要杠杆：判分**批量化**（固定开销 ≈64 / 133 token ≈ **48%**，摊薄约 −38%，
但需抽样验证 verdict 一致性）；`--token-budget` 由 1500 降到 900–1200。

**已落地的工具支持**（`Claw\_judge_arm_parallel.py`）：

```powershell
# 1) 花钱前先看账：只报告复用/需付费的条数与估算 token，不发任何请求
$env:DIVE_EVAL_RUN_DIR="<arm>"; $env:DIVE_JUDGE_DRY="1"; python _judge_arm_parallel.py
# 2) 配对判分：复用 B 臂的 verdict，只判 D 臂变化的题
$env:DIVE_JUDGE_REUSE="<arm B>"; $env:DIVE_JUDGE_ONLY="exact,ds-legacy,ds-rubric"; python _judge_arm_parallel.py
# 3) 先只跑免费 exact，方向不对就不花钱
$env:DIVE_JUDGE_ONLY="exact"; python _judge_arm_parallel.py
```

判分结果按 `(judge, question_id, sha256(hypothesis))` 落盘为
`judge_verdicts-<judge>.jsonl`（含 judge 身份 + 数据集 SHA 表头，身份不符即拒绝复用）。
**这同时修掉了一个真实浪费**：本轮为验证聚合口径而重判基线，白花约 **245k token**
——当时没有任何 verdict 落盘，所以"只判变化的题"根本无从下手。

**推荐流程（把一次消融从 ≈4M 压到 ≈0.8M）**：
覆盖层消融（0）→ 分层小样本真 reader（≈1.2 万）→ `DIVE_JUDGE_DRY=1` 估账
→ 先跑免费 `exact` → 配对判分全量。
