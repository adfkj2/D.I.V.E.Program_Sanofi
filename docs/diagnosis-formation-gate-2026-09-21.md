# 诊断报告：LongMemEval 证据轮成记忆率 6.14% 的归因

诊断日期：2026-09-21
诊断范围：只读分析 + 门控离线复算。**未修改任何项目代码，未重跑 benchmark。**
数据来源：
- `eval/reports/longmemeval-s-retrieval-latest.json`（runner 产物，500 case）
- `eval/external/longmemeval/longmemeval_s_cleaned.json`（官方数据集，277 MB）
- 源码：`llm.py`、`extraction.py`、`gate.py`、`service.py` 的 `ingest` 路径

## 0. 结论摘要

| 项 | 结论 | 证据强度 |
|---|---|---|
| 6.14% 的数值可精确复现 | 是，55/896 = 0.0613839 | 实测一致 |
| 失败环节定位在**写入门控**，不在检索 | 是 | 强（见 §2） |
| 门控拒绝的**机制**是 importance 三分支 | 是 | 强（见 §3） |
| 根因是**信号词表与英文语料的系统性失配** | 是 | 强（见 §4） |
| 检索层是二次瓶颈 | 是，条件门槛 | 中（见 §5） |
| 修复后能达到什么水平 | **未知，未验证** | 无（见 §6） |

## 1. 数值复现（校验口径一致）

runner 报告 `formation` 块：
- `nonempty_turns = 246738`，`formed_memories = 57758`
- `gold_evidence_turns = 896`，`formed_evidence_turns = 55`
- `gold_evidence_turn_formation_coverage = 0.06138392857`

我用官方数据集独立数出 gold 证据轮 `has_answer is True` 共 **896** 个，与 runner 一致。
逐行累加校验：`nonempty_turns / formed_memories / gold / evidence` 四项全部吻合。
→ **口径无误，62 个数量级的差异不是统计错误。**

## 2. 失败环节定位（关键分叉）

按 runner 定义，"证据轮成记忆"要求该轮 ingest 后 `memory_ids` 非空。
按 case 统计（`rows` 数组，500 条）：

| 分类 | case 数 | 说明 |
|---|---:|---|
| `turn_gold_count > 0` 的可答 case | 479 | 有 gold 证据轮 |
| 其中证据轮**完全未成记忆**（ev=0） | **425** | 88.7% |
| 部分成记忆（0 < ev < gold） | 32 | 6.7% |
| 完全成记忆（ev >= gold） | 22 | 4.6% |

即：**绝大多数 case 的 gold 证据轮连一条记忆都没写进去**，不是"写进去了但没被检索到"。

检索侧交叉验证（`answerable` 470 case）：
- 命中 ≥1 条相关证据的 case：**38** 个
- 命中 0 条的：**432** 个
- 命中组的平均 `evidence_memory_count` = **1.000**；未命中组 = **0.039**

按 formation 覆盖度分桶计算"被检索到"的比例：

| formation 覆盖度 | case 数 | 检索命中 | 命中率 |
|---|---:|---:|---:|
| 0（未成记忆） | 416 | 0 | **0.000** |
| (0, 0.5) | 13 | 6 | 0.462 |
| [0.5, 1) | 19 | 14 | 0.737 |
| ≥ 1 | 22 | 18 | 0.818 |

→ **未成记忆 ⇒ 检索命中率为 0，无一例外。** 成记忆是检索的前置条件，瓶颈明确在门控，
不在排序。这个结论与 docs/18 里"formation 是最大瓶颈"的判断一致，但这里给出了逐 case 的因果链。

## 3. 门控拒绝机制（代码级）

`service._process_event` → `HeuristicExtractionProvider.extract_outcome`
→ `extraction.extract_candidates` → `gate.decide`。

`gate.decide` 的 `importance` 只有三档：

```
importance = 0.9  if requested        # explicit=True，或命中 EXPLICIT_MARKERS
           = 0.65 if durable_signal   # 命中硬编码词表
           = 0.25 otherwise
if importance < 0.45: -> SKIP / LOW_FUTURE_UTILITY
```

`gate.features` 里虽然计算了 `specificity`（按文本长度）、`token_specificity` 等，
但**它们不参与 accept/reject 判定**——只有 `importance` 决定。因此：

| 输入 | 长度 | 结果 |
|---|---:|---|
| 240 字英文长段落（无信号词） | 240 | **拒绝** importance=0.25 |
| 同一段 + 插入"我使用" | 244 | 通过 importance=0.65 |
| `I like tea.` | 11 | **拒绝** importance=0.25 |
| `I prefer tea over coffee.` | 25 | 通过 importance=0.65 |

→ **判定由关键词表命中与否单点决定，与语义、长度、信息量无关。**

## 4. 根因：信号词表与语料的系统性失配

```python
EXPLICIT_MARKERS = ("记住", "请记住", "remember", "不要忘记", "keep in mind")
durable_signal   = ("我喜欢", "我偏好", "我住在", "我使用", "我的目标", "always", "prefer")
EPHEMERAL_MARKERS= ("今天晚上", "现在临时", "just for now", "今晚", "稍后")
```

在 LongMemEval_S 正式数据集的 **896 个 gold 证据轮**上统计：

- 命中 `durable_signal` 的：**0 个（0.00%）**
- 命中 `EXPLICIT_MARKERS` 的：26 个

而实测门控在全部 896 个证据轮上的判定分布为：

| reason_code | 数量 | 占比 |
|---|---:|---:|
| `LOW_FUTURE_UTILITY` | 841 | **93.862%** |
| `UTILITY_ABOVE_WRITE_THRESHOLD` | 55 | 6.138% |

**离线复算得到 55/896 = 0.06138，与 runner 实测的 55/896 = 0.06138 完全一致。**

全量语料对照：离线在 246,738 个非空轮上的通过率 = 0.234281，
runner 实测 = 57758/246738 = 0.234086（差异 0.000195，源于 runner 中被跳过的空轮判定路径）。

`durable_signal` 里的 5 个中文词组在英文语料里命中率为 0，`always` / `prefer` 覆盖极窄。
**这不是阈值调参问题，是词表与语料语言的系统性错配。**

需要留意的边界：被拒绝并不意味着判定"错误"。门控是为中文口语场景设计的，
其"保守"在安全性上是对的——密码、身份证号、API key 均被 `SENSITIVE_DATA` 正确拦截（已实测）。
问题不在保守，在于**判据与目标语料无关**。

## 5. 检索层：真实的二次瓶颈（条件性）

在"已成记忆"的 22 个 case 中，只有 18 个（81.8%）被检索命中，即仍有约 18% 漏召回。
整体轮级指标：

| 指标 | 值 |
|---|---:|
| turn-level Recall-any@1 | 0.0404 |
| turn-level Recall-any@5 | 0.0723 |
| turn-level Recall-any@10 | **0.0809** |
| session-level Recall-any@10 | 0.7745 |

分题型：

| 题型 | case | 证据轮 | 成记忆数 | Recall-any@10 |
|---|---:|---:|---:|---:|
| single-session-preference | 30 | 44 | 2 | **0.000** |
| temporal-reasoning | 133 | 259 | 11 | 0.039 |
| multi-session | 133 | 327 | 15 | 0.066 |
| knowledge-update | 78 | 144 | 7 | 0.083 |
| single-session-user | 70 | 66 | 7 | 0.094 |
| single-session-assistant | 56 | 56 | 13 | 0.232 |

**判定条件**：只有当 formation 抬升后 Recall-any@10 仍显著低于 1 时，检索排序才是独立瓶颈。
现在 0.0809 这个数字被 formation 的上限（0.0614）压制，**不能作为检索质量的证据**。

## 6. 本次诊断能证明什么、不能证明什么

**能证明（可直接引用）**
1. 6.14% 是数值准确的，且失败环节在写入门控，有逐 case 因果链支撑。
2. 门控判定由三档 `importance` 单点决定，与语义无关——有代码与对照实验双证据。
3. 词表与英文语料失配是直接机制：896 个证据轮中 0 个命中 `durable_signal`。
4. 离线复算与 runner 实测在两项统计上吻合（55/896、全量通过率），说明诊断方法与 runner 一致。

**不能证明（需要新实验）**
1. **修复后能到多少，完全未知。** 未做任何改动，没有 A/B 对照，
   不能声称"换个抽取器就能到 X%"。
2. **假记忆率会不会同步恶化。** `false_memory_v1` 套件尚未运行（P0-4）。
   formation 召回与假记忆抑制是同一门控的两面，放宽判据必然影响两侧，必须同时测。
3. **拒答准确率 0.00%（0/30）的根因。** 当前 30 个 abstention case 全部失败，
   可能与门控相关，也可能与 `predicted_abstain = not bool(result.items)` 这条过简的判定有关，
   **本次未做专门归因，不应下结论**。
4. **官方 QA 分数**。reader/judge 凭据缺失，QA 阶段 NOT_COMPLETED，本轮只有检索级证据。

## 7. 若要动手，建议的验证顺序（尚未执行）

1. 先建对照：修一个可插拔的语义抽取器（现成的 `OpenAICompatibleExtractionProvider` 已具备
   严格 schema 校验，但需处理凭据），在**同一 896 证据轮**上跑门控通过率，与 6.14% 对照。
2. 同步跑 `false_memory_v1`，观察召回提升与假记忆率的关系，避免顾此失彼。
3. 只有第 1 步通过率显著抬升后，rerun LongMemEval 检索阶段，才能重新评价检索层质量。
4. 全程遵守 docs/19 的 artifact 契约：失败产物保留、不硬编码、不重标旧结果。
