# DeepSeek Reader 接入指南

DeepSeek API 是 **OpenAI 兼容**的，本仓库的 `OpenAICompatibleChatClient`（`oracle/providers.py`）
**零代码改动**即可接入：只需把 `--endpoint` 指向 DeepSeek 的 chat-completions 端点，
`--model` 用 DeepSeek 的有效模型名。

## 官方契约（2026-09-22 核实自 api-docs.deepseek.com）

| 项 | 值 |
|---|---|
| base_url（OpenAI 格式） | `https://api.deepseek.com` |
| chat-completions 端点 | `https://api.deepseek.com/chat/completions` |
| 当前有效模型名 | `deepseek-flash`（推荐，默认）；`deepseek-v4-pro`（推理/编码旗舰） |
| JSON Output | ✓ 支持（`response_format: {"type": "json_object"}`） |
| 鉴权 | `Authorization: Bearer <DEEPSEEK_API_KEY>` |

**已退役，不要使用**：`deepseek-chat`、`deepseek-reasoner`（2026-07-24 退役）、
`deepseek-v4-flash`（旧名，虽仍被接受但由 V4.1-Flash 服务，`deepseek-flash` 才是规范名）。

> 模型名会随 DeepSeek 产品线演进。拿到 API 后以 `https://api-docs.deepseek.com/` 首页的
> "model" 字段为准；本页只是当前快照。

## 凭据到手的即时命令

```powershell
# 1. 把 key 放进环境变量（不会写进 manifest 或 shell 历史）
$env:DIVE_READER_API_KEY = "sk-..."

# 2. 20-case smoke（真实 DeepSeek reader；semantic 门 + GPU 加速）
$env:PYTHONPATH = "src"
$env:HF_HOME = "eval/external/huggingface"
$env:HF_HUB_OFFLINE = "1"; $env:TRANSFORMERS_OFFLINE = "1"   # 本地 bge-m3 强制离线
& ".venv-gpu\Scripts\python.exe" -m dive_memory.oracle.runner `
  --dataset eval/external/longmemeval/longmemeval_oracle.json `
  --output eval/reports/oracle-deepseek-20case `
  --max-cases 20 --allow-partial `
  --reader openai `
  --endpoint "https://api.deepseek.com/chat/completions" `
  --model deepseek-flash `
  --api-key-env DIVE_READER_API_KEY `
  --provider-name deepseek `
  --extra-body '{"thinking":{"type":"disabled"}}' `
  --gate semantic --device cuda
```

> **为什么必须 `--gate semantic`**：默认 v1 关键词门形成率仅 6.14%，检索几乎空转，
> reader 全部无据可答（20/20 ABSTAIN）。semantic 门把形成率提到 82.48%，实测 20 题中
> 5 题产出真实 ANSWER。端到端问答必须用 semantic 门。

> **为什么 `--extra-body '{"thinking":{"type":"disabled"}}'`**：DeepSeek 默认开启
> thinking 模式，答案在 `reasoning_content` 而非 `message.content`（content 为空）。
> 关闭后答案直接落在 `content`，`GroundedChatReader` 才能解析出 JSON。`--extra-body`
> 是 provider-neutral 的请求体注入点，`provider-name` 记入 manifest。

50-case 分层 pilot 与 500 题全量，只需把 `--max-cases 20` 换成 `--stratified 50` 或去掉
`--max-cases`（并确保 clean commit + 配置冻结）。

## GPU 加速实测（2026-09-22）

同一 20 题、同一 semantic 门、同一 DeepSeek reader，只换 device：

| device | 环境 | 总耗时 | 加速比 | reader 结果 |
|---|---|---|---|---|
| cpu | `.venv-models`（torch 2.14.0+cpu） | 246.2 s | 1× | ANSWER 5 / ABSTAIN 15 |
| cuda:0 | `.venv-gpu`（torch 2.14.0+cu126，RTX 4060 Laptop） | 42.4 s | **5.8×** | ANSWER 5 / ABSTAIN 15（完全一致） |

- 门控决策在 GPU/CPU 等价（结果逐题一致），加速来自 bge-m3 编码搬上 GPU。
- ingest 延迟 p50 816.9 ms / p95 1638.7 ms / p99 2196.5 ms（GPU，20 题）。
- key 零泄漏：扫描全部产物文件（manifest/traces/submission/summary）均不含 key 明文。

## 定价模板（供 `evaluate.py --pricing-file`）

```json
{
  "deepseek-flash": {
    "input_per_million": 0.14,
    "output_per_million": 0.28,
    "source": "DeepSeek official rate card, cache-miss input"
  },
  "deepseek-v4-pro": {
    "input_per_million": 0.435,
    "output_per_million": 0.87,
    "source": "DeepSeek official rate card, cache-miss input"
  }
}
```

> 价格为 cache-miss 口径；DeepSeek 对 cache-hit 输入另有大幅折扣，且峰值时段（UTC 周一至周五
> 01:00–04:00 与 06:00–10:00）按双倍计费。**正式运行前以官方 rate card 复核**
> （`api-docs.deepseek.com/quick_start/pricing`），并把实际采用价格写进 `source` 字段。

## 评估命令（自测分，非官方）

```powershell
.venv\Scripts\python -m dive_memory.oracle.evaluate `
  --dataset eval/external/longmemeval/longmemeval_oracle.json `
  --submission eval/reports/oracle-deepseek-20case/submission.jsonl `
  --run-dir eval/reports/oracle-deepseek-20case `
  --output eval/reports/oracle-deepseek-20case/evaluation.json `
  --judge openai --judge-endpoint "https://api.deepseek.com/chat/completions" `
  --judge-model deepseek-flash --judge-api-key-env DIVE_JUDGE_API_KEY `
  --pricing-file eval/reports/pricing-deepseek.json
```

> `--judge openai` 会用本地配置的 LLM judge 模拟判分，结果始终是
> `SELF_REPORTED_NON_OFFICIAL`。旧的 `--judge-official` 标志现已禁用；官方 Overall
> Accuracy 只能由项目组统一复跑得出。历史 A/B/D 的 v1/v2 读侧与当前 v3 不可混称为同一配置。

## 注意事项

- `GroundedChatReader` 强制 `response_format={"type":"json_object"}`，DeepSeek 支持，无冲突。
- reader 的 grounded-answer-v1 契约要求回答带 citation（memory_id + 逐字 quote + 检索集内的
  source_refs）。DeepSeek 需要按 prompt 里的 `output_schema` 输出合法 JSON，否则会被判
  `INVALID`（格式错误）而非当成真答案 —— 这是特性，不是 bug。
- `--provider-name deepseek` 会写进 manifest 的 reader spec，保证可复现披露。
