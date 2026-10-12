# v2/v3 Reader 配对验证：先审计证据，再谈答题分数

> 2026-09-27 更新：已完成 DeepSeek Flash 固定 20 题真实配对，见 [实测结果与费用](14-flash-low-cost-pilot-20260927.md)。v2 为 10/20，v3 为 11/20，但存在可答题回退和偏好题判分争议，尚不支持宣布 v3 胜出。以下保留原实验计划；其中“尚未真实配对”的描述是计划制定时状态。

这一步专门回答一个问题：在**相同的记忆与检索结果**面前，v3 的“所问事实及必要前提必须有据”规则，是否比 v2 的“有相关证据就尽量回答”更可靠？它不是检索改进实验，更不是正式 Benchmark 成绩。先保证两臂真正看到了同一份证据，才能把差异归因到 reader 策略。

## 为什么要单独做

历史 D 臂 DeepSeek 本地自评为 309/500，但属于旧时间逻辑、旧代码和 v2 风格提示词；历史 manifest 的策略名称与提示词版本并非完全一致，不能把它当作当前 v2 基线。v3 目前只有离线规则测试，没有相同题、相同模型的真实配对答题结果。覆盖实验的 `formed/retrieved/packed` 只说明证据是否走到模型面前，不等于模型会正确理解、更不等于官方得分。

长交付物还暴露了一个特别危险的预算审计点：`packing.estimated_tokens` 统计的是打包文本；模型实际看到的 `reader_context_text` 可能被重新渲染，字符数和 token 数都不同。脚本同时保存两者的指标、实际模型报告的输入 token，并强制两臂逐题的 `reader_context_text` 指纹一致。它将超出 1,500 的**近似**上下文计数列为审计警报，不假装这个启发式值是供应商精确 token 数；模型输入 token 还包括提示词及重试。

对旧 D 臂做了一次**只读**审计，原始记录是 `eval/reports/oracle-ds-D-500/traces/`，结果存于 `eval/reports/oracle-reader-audit-20260924/legacy-D-context-budget.json`。口径是读取 500 条 trace，对每条保存的 `packing.estimated_tokens` 与**实际保存的** `reader_context_text` 都用项目同一 `utf8-character-heuristic-v1` 字符启发式（约 `len(text)//4`）计数，分别与配置的 1,500 比较。打包估计值中位 1,299、最大 1,499，超预算 0/500；reader 可见文本中位 1,855.5、最大 3,636，**381/500** 超过 1,500。供应商所报 reader 输入均值为 3,244 token，但它包含规则提示、输出格式说明和可能的重试，不能当作纯证据长度。旧 D 臂是 dirty worktree、旧提示词与旧时间逻辑；上述 381/500 是**启发式风险信号**，不等于精确超量次数，不能据此给旧 61.8% 自评分做数学修正，更不能推导正式成绩。可用下列命令复核汇总，工具不读取参考答案、不调用模型：

```powershell
python ops/oracle_reader_pair.py audit `
  --run eval/reports/oracle-ds-D-500 `
  --output eval/reports/oracle-reader-audit-20260924/legacy-D-context-budget.json
```

## 当前零费用可做的事

在项目目录执行：

```powershell
python ops/oracle_reader_pair.py plan `
  --dataset eval/external/longmemeval/longmemeval_oracle.json `
  --size 20 --abstention 4 --seed 20260924 `
  --ids-output eval/reports/oracle-reader-pilot-20.ids
```

会生成仅含题目 ID 的 `.ids` 和相邻的 `.plan.json`，记录数据集 SHA、抽样种子、题型配额与 ID 文件指纹。不可答题特意保留 4 道：v2 的主要风险不是“无证据时不会拒答”，而是有相似主题却缺关键值时猜测。`plan` 只使用题型与 `_abs` 标记做**评测端抽样**；reader 只收到 ID 所定位的原始问句和有界上下文，不收到题型、标准答案或标注证据。

没有 API key 时，到这一步就停止；可以运行现有离线测试，但不要把确定性 reader 或 exact 字符串判分写成“v3 提分”。尤其不能拿旧 D 臂报告与新 v3 报告直接相减。

## 拿到 reader API 后怎么跑

两个运行都从同一份新代码、最好是干净提交开始；各用独立目录。命令中的 endpoint、model 和设备要按实际服务调整，但**两臂完全相同**。不要将 key 写进命令、文档、manifest 或报告；由环境变量提供。

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
$shared = @(
  '--dataset', 'eval/external/longmemeval/longmemeval_oracle.json',
  '--ids-file', 'eval/reports/oracle-reader-pilot-20.ids',
  '--reader', 'openai', '--endpoint', '<reader-chat-completions-url>',
  '--model', '<same-model-id>', '--provider-name', '<same-provider-name>',
  '--api-key-env', 'DIVE_READER_API_KEY',
  '--temperature', '0', '--max-tokens', '512',
  '--gate', 'semantic', '--device', 'cpu',
  '--document-admission', 'all', '--scoring-policy', 'balanced',
  '--limit', '10', '--token-budget', '1500',
  '--embedder', 'deterministic'
)
python -m dive_memory.oracle.runner @shared `
  --reader-policy evidence_first_v2 --output eval/reports/pilot-v2
python -m dive_memory.oracle.runner @shared `
  --reader-policy evidence_first --output eval/reports/pilot-v3
python ops/oracle_reader_pair.py compare `
  --baseline-run eval/reports/pilot-v2 `
  --candidate-run eval/reports/pilot-v3 `
  --dataset eval/external/longmemeval/longmemeval_oracle.json `
  --output eval/reports/pilot-reader-pair.json
```

上例的 `--embedder deterministic` 明确表示 dense 检索仍是非语义占位；如果另一项消融已证实本地语义检索更好，可以**同时**把两臂改成 `--embedder local` 并固定 BGE-M3 修订版。不能只给 v3 换检索器。若机器已有可用 GPU，可两臂同时改设备；输出成本计算也要按实际 API 价格与 token 用量填报，不能预设免费。

比较脚本会检查：数据集 SHA、固定题集、gate/文档准入/检索与预算、reader 供应商/模型/参数、逐题实际提示版本、返回与打包的记忆和来源、打包文本，以及模型实际可见的证据文本。任一不一致，`comparable_reader_ablation=false` 且退出码为 2。两臂证据同样超预算也会被拒绝。9 月 26 日起，Oracle 使用确定性事件 ID，manifest 记录源文件、Python 和依赖的代码快照；dirty worktree 必须有相同且校验有效的快照，才能证明配对代码同一性。只有 commit、没有快照的旧 dirty 运行仍只能用于诊断。

若要使用已完成覆盖筛选的受控文档方案，在上面 `$shared` 中同时给两臂增加 `--document-selection query_excerpt --document-chunk-search controlled_supplement`。它要求 balanced 排名与带注释的 reader 策略。当前 v2/v3 都满足条件；两臂必须采用同一个配置，不能只给 v3 换装包方式。

拒答压力测试题单已生成：`eval/reports/oracle-reader-pilot-50-all-abstention.ids`，seed 为 20260926，含全部 30 道不可答题与 20 道分层可答题。可在完成 20 题接线验证后替换两臂的 `--ids-file`，输出到新的独立目录。该样本刻意增加不可答题比例，不能把其总体正确率外推成完整 500 题结果。

此时报告只有状态、证据同一性和 token 费用线索，`accuracy=null`。不能把 v3 拒答次数下降、格式失败次数下降或证据覆盖变化当作正确率变化。若先只拿到低额度，可先复跑 20 题确认结构化输出、失败率、不可答题；有明显收益且无严重回归，再扩到 50 题，最后才跑 500。20、50 都是筛选性 pilot，不应将其比例线性外推成官方 40 分。

## 何时才有本地正确率

如果已经有**两臂同一评判器**对各自 `submission.jsonl` 逐题判出的 `dive-judge-verdicts-v1` JSONL，可以给 `compare` 增加 `--baseline-verdicts` 和 `--candidate-verdicts`。工具会逐题核对 verdict 绑定的假设答案 SHA、评判器表头和数据集 SHA；缺 verdict 时全题正确率保持 `null`，不能用“已判题分母”掩盖失败。加上 `--dataset` 会在评测端按六类题型报告配对变化，题型与标准答案绝不会进入读侧。输出还包含：两臂全题正确率，以及互斥的“正确作答 / 错误作答 / 正确拒答 / 错误拒答 / 无效输出 / 未评判”六类结果；另保留不可答题与可回答题的专项计数，以及同一题“仅 v2 对 / 仅 v3 对”的转移数。`INVALID`、`ERROR`、`TIMEOUT` 或 `ERROR_FALLBACK` 会优先归为“无效输出”，即使 runner 为保证 submission 行数而写入了规范拒答模板，也不会被误算成 reader 主动做出的正确拒答。JEV、DeepSeek 或 exact 判分都只能标为 `SELF_REPORTED_NON_OFFICIAL`；exact 尤其只能做字符串级接线检查。

最终选择不能只看总正确数。若 v3 救回不可答题，却使可回答的时间/跨会话题大量错拒，应逐题看证据是否足够、推导是否合法、是否触发格式或传输失败；必要时修改策略并重新配对，而非在同一组结果上反复调参后报最高分。正式成绩仍需项目组的 `evaluate_qa.py`、指定 judge 与统一复跑，目前为 `NOT_EVALUATED`。
