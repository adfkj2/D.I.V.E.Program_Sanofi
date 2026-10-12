# Oracle 长文档 N / E / EC 零 API 配对消融

日期：2026-09-24。状态：**20 题先导和固定 50 题实验已完成；未评估答题正确率。**

9 月 26 日补充：这里建议的受控补入 ER 已实现并完成同题配对，packed 从 E 的 59 增至 62，三道时间题获益、未见数量回退；代码已接入 runner。见 [最新实现与实验报告](13-implementation-results-20260926.md)。本页保留原始三臂实验的历史口径。

## 结论先行

1. `query_excerpt` 可以保留为下一次真实 reader 配对实验的候选，但现在还不能凭覆盖数据宣布它提高了正确率。50 题上，它在检索候选完全不变的条件下，将装包的 gold 证据轮次从 **58 提到 59**：2 个题获得证据，1 个时间题丢失证据。
2. 对所有问题直接启用 `document_chunk` 通道的 EC 臂应当被否决。它将 retrieved / packed gold 从 E 的 **62 / 59** 降到 **24 / 21**；装包项中文档占比从 **30.5%** 上升到 **80.4%**，说明片段命中作为新的 RRF 投票把大量助手文档推入 top-k，挤掉了跨会话短事实和时间线证据。
3. 下一步不应再扩大文档通道权重，而应当做“**保护基线候选 + 有上限的片段救援 + 按 token 成本选证据**”。实验已经证明，片段索引本身有效，问题在于它如何参与候选竞争。

## 这个实验分离了什么

| 臂 | 文档展示 | 片段检索 | 回答的问题 |
|---|---|---|---|
| N | `prefix` | `off` | 新的“实际 reader 可见证据块”预算下的基线 |
| E | `query_excerpt` | `off` | 不改召回候选，只把文档前缀改成问题相关原文，会发生什么 |
| EC | `query_excerpt` | `all` | 让片段命中参与全局 RRF，能否救回原本未进 top-k 的文档 |

三臂共用每题唯一一次形成结果和同一个内存库，其余配置全部固定：

- semantic write gate：本地 `BAAI/bge-m3@5617a9f...`，CPU，缓存 8192，最大序列长度 512，长交付物准入 `all`；
- retrieval embedder：`deterministic-sha256-v1`，它是可复现的非语义占位向量，因此这不是语义检索实验；
- 重排：`balanced`；top-k = 10；
- 阅读策略：`oracle-grounded-reader-v3`的带注释证据块；
- 证据预算：1500，对 reader 实际会看到的带注释文本计数，而不是对未截断的内存对象计数；
- 网络和 API：强制 Hugging Face / Transformers 离线，0 reader 调用，0 judge 调用；
- gold 边界：每个问题的 N / E / EC 三份运行产物全部完成后，评估器才读取题型和证据轮次标注。不可答题没有正向召回目标，其干扰会话不计入 gold recall。

固定 50 题包含 5 道不可答题。题型分布是：8 道 knowledge-update、13 道 multi-session、6 道 single-session-assistant、3 道 single-session-preference、7 道 single-session-user、13 道 temporal-reasoning。题目 ID 被固定在 `eval/reports/oracle-document-ablation-50.ids`，而不是在运行时根据答案选题。

## 50 题结果

45 道可答题共有 83 个 gold 证据轮次。

| 臂 | formed | retrieved | packed | packed 覆盖 | 平均证据 token | 最大 token | 装包文档 / 全部装包项 |
|---|---:|---:|---:|---:|---:|---:|---:|
| N | 77 | 62 | 58 | 69.88% | 1291.10 | 1499 | 116 / 407 = 28.50% |
| E | 77 | 62 | 59 | 71.08% | 1288.32 | 1500 | 129 / 423 = 30.50% |
| EC | 77 | 24 | 21 | 25.30% | 1337.04 | 1500 | 325 / 404 = 80.45% |

所有 150 个臂级上下文均没有超出 1500，N 与 E 的返回内存 ID 在 50/50 题上完全一致；因此 E 与 N 的差异只能来自可见原文范围和它导致的预算装包变化。

按题型看 packed gold 轮次：

| 题型 | gold | N | E | EC | E - N | EC - E |
|---|---:|---:|---:|---:|---:|---:|
| knowledge-update | 16 | 11 | 11 | 1 | 0 | -10 |
| multi-session | 28 | 19 | 21 | 1 | +2 | -20 |
| single-session-assistant | 6 | 6 | 6 | 6 | 0 | 0 |
| single-session-preference | 3 | 2 | 2 | 2 | 0 | 0 |
| single-session-user | 6 | 6 | 6 | 4 | 0 | -2 |
| temporal-reasoning | 24 | 14 | 13 | 7 | -1 | -6 |

E 对 N 有 2 题增益、1 题回退：

- `36b9f61e`（跨会话，计算奢侈品总支出）：文档可见区间从固定 1200 字符前缀变为 1095 / 1097 字符的问题相关段，释放出的预算让第 8 个候选进入证据块，packed gold +1。
- `c4a1ceb8`（跨会话，统计鸡尾酒中的柑橘类型）：两个文档摘录比固定前缀更短，第 10 个候选因而可以装入，packed gold +1。
- `gpt4_2487a7cb`（时间题，比较两场活动的先后）：E 使一个排名更高的文档摘录从“因预算不足被跳过”变为“可装入”，反而挤掉后面一条 gold 时间证据，packed gold -1。

这个回退非常重要：“摘录更相关”不等于“整个证据包更好”。当前 packer 按排名贪心遍历，一个新能装下的长文档会消耗后续短证据的预算。

EC 的失败则更明确。例如 `2e6d26dc`询问几位亲友最近生了孩子：N / E 的 top-10 包含全部 4 条 gold 证据；EC 的前 7 个可装包候选全是带 `document_chunk` 加成的助手长文档，4 条 gold 全部被挤出 top-10。这是“文档可被检索”与“文档应与所有短事实无限制竞争”之间的差别。

## 这些数字不能说明什么

- 它们不是答题准确率，也不是正式 40 分 benchmark 得分。
- `packed` 只表示 gold 证据轮次对应的内存进入了 reader 证据块。Oracle 没有给出答案在原轮次中的字符偏移，所以它不能证明选中的 1200 字符恰好包含答案句。
- E 在 6 道 assistant 题上 N / E 都是 6/6，只能说证据轮次均被装入；没有字符级 gold span 或真实 reader，不能由此判定 E 看到了更好的答案段落。
- token 计数是项目的字符启发式计数器，覆盖实际带注释证据块，但不是供应商 tokenizer 的精确值，也不包含系统指令和输出预算。

## 性能与复现

一题三臂的中位总耗时是 7.54 秒，其中共享形成的中位耗时为 7.52 秒；N / E / EC 臂本身的中位耗时分别只有 4.99 / 4.69 / 5.70 毫秒。这说明本地 semantic write gate 是离线实验的主要耗时，文档摘录和 SQLite 片段检索的额外耗时很小。

复现命令：

```powershell
python ops/oracle_document_ablation.py `
  --output eval/reports/oracle-document-ablation-pilot-50 `
  --dry-run

python ops/oracle_document_ablation.py `
  --output eval/reports/oracle-document-ablation-pilot-50
```

`--dry-run` 只检查数据 SHA、固定 ID 和本地模型缓存，不加载模型、不形成记忆、不写产物。`--resume` 只接受相同数据、ID 和配置指纹。

产物：

- `eval/reports/oracle-document-ablation-pilot-50/manifest.json`：数据、模型修订版、固定配置、选题和指纹；
- `eval/reports/oracle-document-ablation-pilot-50/traces/*.json`：每题一次形成、三臂候选、装包决策、可见 `start/end`、实际证据块、token 与耗时；
- `eval/reports/oracle-document-ablation-pilot-50/report.json`：事后 gold 覆盖、题型汇总、配对增益/回退和逐题导航。

## 下一步决策

1. 保留 E，作为真实 reader 小样本的候选臂；暂不将“packed +1”写成准确率改善。
2. 不启用全局 EC。下一个零 API 变体应保留基线 top-k 的短事实/时间证据，再最多补入 1–2 个片段命中文档，并给文档总 token 份额设上限；片段通道不应当凭一张额外 RRF 选票无限制替换基线候选。
3. 将 packer 从纯排名贪心改为受约束的预算选择：至少比较“短事实/时间线”与“文档片段”的证据多样性和每 token 收益，避免 `gpt4_2487a7cb` 式的新可装入长文挤掉关键短证据。
4. 只有受控片段变体在同一 50 题上不再丢失 multi-session / temporal 覆盖，才进入同一真实 reader 和 judge 的低成本配对答题。
