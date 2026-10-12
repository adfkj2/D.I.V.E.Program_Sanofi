# Oracle 协议：正式 Benchmark 与 S-cleaned 的严格区分

## 一、两种评测，不可混淆

本仓库存在两份 LongMemEval 数据，它们**共用行 schema，但除此之外没有可比性**：

| 维度 | `longmemeval_oracle.json`（正式 Benchmark） | `longmemeval_s_cleaned.json`（记忆实验语料） |
|---|---|---|
| 题量 | **500 题** | 500 case（277 MB 长上下文） |
| 输入字段 | `haystack_sessions` / `question` / `question_date` | 同左 |
| 评估专用字段 | `answer`（+ `question_type`、`answer_session_ids`、`has_answer`） | 无官方答案，不产出回答 |
| 用途 | 端到端问答，产出 `submission.jsonl`，由项目组 `evaluate_qa.py` + 统一 LLM judge 打分 | 记忆 **formation** / **evidence retrieval** 阶段实验 |
| 主指标 | **Overall Accuracy**（`Benchmark = Overall Accuracy × 40`） | retrieval recall / formation coverage |
| 是否计分 | **是（40 分）** | 否 |

**核心纪律**：不得把 retrieval recall、formation coverage、`LongMemEval_S-cleaned`
结果或任何内部 fixture 指标冒充 Oracle Overall Accuracy。两者由不同加载器
（`oracle.loader` vs `eval_adapters.longmemeval` 的 S-cleaned 用法）严格隔离。

## 二、提交格式

每行严格两字段：

```json
{"question_id":"...","hypothesis":"..."}
```

- 顺序无关，但 `question_id` 必须唯一且与官方 500 题完全对齐（无缺失、无多余）。
- 除 `question_id` 与 `hypothesis` 外，**不得有任何附加字段**（无索引、无分数、无 trace）。
- `validator.py` 独立校验以上契约，且不与 runner 共享代码。

## 三、评分与诚信边界

- 唯一权威分数来自项目组统一复跑：官方 `evaluate_qa.py` + 统一 LLM judge 对同一
  参考答案打分。本仓库 `evaluate.py` 只能产出**自测分**（`SELF_REPORTED_NON_OFFICIAL`）。
- 默认确定性 reader 是**流水线探针**，其输出不得被描述为模型答案。
- 未运行 / 未评估的内容必须显式标 `NOT_RUN` / `NOT_EVALUATED` / `UNKNOWN`，
  不得用估算或省略代替。

## 四、运行阶段门槛

1. synthetic fixture 验证 loader / 时序 / 隔离 / 泄漏防护 / JSONL；
2. 20-case Oracle smoke（格式、异常、trace）；
3. 50-case 按 `question_type` 分层 pilot（比较 reader/prompt/retrieval 配置）；
4. 冻结配置；
5. 500 题全量。

**只有在 clean commit + 配置冻结之后，才能把 500 题结果标为正式 Benchmark。**

## 五、与 S-cleaned 的区别（详见 `06-s-cleaned-vs-oracle.md`）

`LongMemEval_S-cleaned` 实验完成的是 formation/retrieval 阶段，且 README 中的
`longmemeval-report.md` 写的是 v1 关键词门（形成率 6.14%），不是系统现状。Oracle
流水线复用同一领域层与 adapter，但数据文件、指标、评分契约完全不同。

## 相关文档

- `01-leakage-design.md` — 防泄漏边界（哨兵测试证明，非约定）
- `02-runner-architecture.md` — runner 架构与数据流
- `03-cost-estimation.md` — 成本估算方法
- `04-failure-taxonomy.md` — 失败分类（formation/retrieval/reading 三阶段）
- `05-submission-checklist.md` — 正式提交 checklist
- `06-s-cleaned-vs-oracle.md` — S-cleaned 与 Oracle 的区别
