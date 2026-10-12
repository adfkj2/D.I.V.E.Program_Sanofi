# 正式提交 Checklist

正式 500 题 Benchmark 结果，只有在**全部满足**以下条件时才能声明：

## 数据与配置

- [ ] 数据文件为 `longmemeval_oracle.json`（非 S-cleaned），SHA-256 已记录并匹配。
- [ ] 500 题完整性校验通过（`load_oracle_dataset` 默认 `expected_cases=500`）。
- [ ] 30 道 `_abs` 拒答题齐全。
- [ ] 配置冻结：pipeline limit / token_budget / retrieval config / gate / reader spec 已定，
      并写入 manifest 的 `configuration_sha256` 与 `resume_key`。

## 代码与运行

- [ ] 运行来自 **clean commit**（`git status` 干净，manifest `git.dirty == false`）。
- [ ] 完整测试套件通过（421+ passed，skipped 有解释）。
- [ ] synthetic smoke 可复现。
- [ ] 20-case smoke 成功并生成合法 JSONL。
- [ ] （如预算允许）50-case 分层 pilot 完成，配置选择有依据。

## 提交与校验

- [ ] `submission.jsonl` 500 行，`validator.py` 判定 `VALID`。
- [ ] 每行恰为 `question_id` + `hypothesis` 两字段，无附加字段。
- [ ] question_id 唯一、与官方 500 题完全对齐（无缺失、无多余）。
- [ ] 无空 hypothesis（除 `on_error=abstain` 的显式规范拒答模板，且已计数）。

## 评分边界

- [ ] 未把自测分描述为官方 Overall Accuracy。
- [ ] 未把确定性 reader 输出描述为模型答案。
- [ ] 未把 retrieval recall / formation coverage / fixture 指标冒充 Oracle 分数。
- [ ] 未运行项已标 `NOT_RUN` / `NOT_EVALUATED` / `UNKNOWN`。

## 诚信（手册附录 F）

- [ ] 无逐题硬编码 / 特判 / 规则映射。
- [ ] 未把完整 haystack 直接拼入 reader 上下文。
- [ ] 未用超大上下文模型掩盖 memory pipeline 缺失。
- [ ] 未对测试集逐题调参后宣称泛化能力。
- [ ] 引用了 LongMemEval（arXiv:2410.10813）与相关开源实现。

## 唯一权威分数

- [ ] 最终 `Benchmark = Overall Accuracy × 40` 由项目组统一复跑 `evaluate_qa.py` + 统一
      LLM judge 得出；本仓库只保留 `SELF_REPORTED_NON_OFFICIAL` 自测分与
      `NOT_EVALUATED` 官方分。
