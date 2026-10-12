# 成本估算方法

## 原则

- 不生成无法从 artifact 验证的数字。没有价格表或 token 计数时，一律标 `UNKNOWN`。
- 确定性 reader / scripted 传输不产生真实 token 计数，`usage.source` 标 `estimate` 或 `unknown`。

## token 记账

- 真实模型（`OpenAICompatibleChatClient`）从响应 `usage.prompt_tokens` /
  `completion_tokens` 读取，`usage.source = "provider"`。
- 无法取得 provider 计数时，`GroundedChatReader` 用 `ApproximateTokenCounter`
  （utf8-character-heuristic-v1）估算，`usage.source = "estimate"`。
- 确定性 reader 的 `input_tokens = 0` 且 `source = "estimate"`，**不是**真实消耗，
  只是流水线探针。

## 成本公式

`evaluate.estimate_cost(pricing, input_tokens, output_tokens)`：

```
cost_usd = (input_tokens * input_usd_per_million
            + output_tokens * output_usd_per_million) / 1_000_000
```

- `ModelPricing(input_usd_per_million, output_usd_per_million, source)`
  由调用者通过 `--pricing-file` 提供（JSON: `{model: {input_per_million, output_per_million}}`）。
- 缺任一 token 计数或价格 → 返回 `None`，报告写 `UNKNOWN`。

## 报告口径（`evaluate.analyze`）

`cost` 块包含 `currency`、`reader.pricing/estimate_usd`、`judge.pricing/estimate_usd`、
`total.estimate_usd` 与 `total.status`（当前为 `PARTIAL: reader cost only`，
因 judge token 不逐题落盘）。`note` 明确价格是调用者提供、未经厂商核验。

## 全量 500 题成本预估

正式运行前可用 pilot 外推，但必须标注是**预估**：`estimate_cost` 只对已有 token
计数负责，不跨题外推。500 题总成本只能在真实运行后从 artifact 汇总得出。
