# 写入门控修复报告：从 6.14% 到 84.82%

日期：2026-09-21
状态：**核心修复已完成并实测**。本报告区分「已实测」与「已知缺口」。
第 8–9 节记录第 7 节各项的执行结果，其中 8.5 节**推翻**了前文对阈值可信度的乐观表述。

## 0. 结论摘要

| 项 | 修复前 (v1) | 修复后 (v2) | 证据强度 |
|---|---|---|---|
| **证据轮成记忆率 (896)** | 0.0614 (55/896) | **0.8482 (760/896)** | 实测，离线 |
| **端到端 8-case 轮级 Recall-any@10** | 0.125 | **0.750** | 实测，完整管道（第 8.1 节） |
| 端到端 8-case 会话级 MRR | 0.583 | **0.8125** | 实测，完整管道 |
| 假记忆套件 oracle 一致率 | 0.6471 (22/34) | **0.9412 (32/34)** | 实测 |
| 假记忆套件 user_fact 轴 | 0.6176 (21/34) | **0.9706 (33/34)** | 实测 |
| 假记忆错误接受 | **12** | **1** | 实测 |
| 手标对抗样本 | — | **21/22 → 修复后 22/22** | 实测 |
| 全量语料接受率（采样 2544 轮） | 0.2272 | 0.8003 | 实测，非普查 |
| 敏感数据拦截 | 拦截（11/12 形状） | 拦截（12/12） | 实测 |
| 留出集门控准确率（29 条，见 8.5） | — | 0.8276，Wilson 95% [0.655, 0.924] | 实测，**仍未可分** |
| 单元测试 | 219 passed | **282 passed** | 实测 |

**核心意外发现（比原诊断更重要）**：v1 门控的缺陷不是「词表语言失配」单一问题，
而是**缺少来源信任与断言强度两个维度**。在 34 条受控假记忆用例上，v1 有 12 条
错误接受，其中多条文本内容完全合法（如「我住在上海」），仅因来源不可信而必须拒绝。
**内容评分在原理上无法表达来源**，因此修复必须新增该维度，而不是只做语义化。

**第二个必须记住的结论（8.5 节）**：0.495 阈值在留出集上**不可分**。
22 条探针上的完美可分是过拟合。加入言语行为地板后留出集准确率从 0.690
提到 0.828，但 gap 仍为负（−0.042）。**该阈值必须继续标注为 provisional。**


## 1. 修复前基线的精确复现（Task #5）

新增 `ops/gate_formation_probe.py`（只读，不建库），在官方数据集上独立复算：

| 指标 | 本探针 | runner 实测 | 差异 |
|---|---:|---:|---:|
| 证据轮接受 | 55/896 | 55/896 | 0 |
| 证据轮成记忆率 | 0.06138392857142857 | 0.06138392857142857 | 0 |
| 全量非空轮接受 | 57806/246738 | 57758/246738 | 48 轮 |

前两项**逐位一致**，基线锁定可信。全量 48 轮差异源于 runner 对空轮的跳过路径。

产物：`eval/reports/gate-formation-baseline.json`、`...-rejects.json`（400 条拒绝样本）

## 2. 资源盘点（Task #4）

| 资源 | 状态 | 说明 |
|---|---|---|
| bge-m3 权重 | 完整 | `snapshots/5617a9f6...`，2165 MB，pin 到 revision |
| torch | 2.14.0+cpu | CPU-only，无 CUDA |
| sentence-transformers | 3.0.1 | 可用 |
| numpy | 2.5.3 | 可用 |
| 外部 LLM 凭据 | **无** | 环境变量、`.env`、项目内凭据文件均为空 |
| `.venv-models` | 存在 | 独立于 `.venv`，模型依赖只装在这里 |

**关键判断**：由于没有任何外部 API 凭据，修复路线确定为**全本地、无凭据**方案，
用已缓存的 bge-m3 做语义判据。这同时避免了引入网络依赖与不可复现性。

## 3. v2 门控设计（Task #6）

新增 `src/dive_memory/semantic_gate.py`。v1 保留为默认，v2 通过注入启用。

### 3.1 三轴最近锚点边际

第一版实现用**质心**余弦差，实测**线性不可分**：

| 输入 | 质心版 score |
|---|---:|
| `I prefer dark mode in my editor.` (durable) | 0.5745 |
| `I just ate a sandwich.` (transient) | **0.5488** |
| `I graduated with a degree...` (durable, 长句) | **0.4330** |

问题在于 16 条互不相似的锚点取均值后被拉向语料中心，且长度项
（`single_valued`）与耐久性**反相关**：长 durable 句得 0.000，短 transient 句得 0.725。

第二版改为**最近锚点边际**（per-anchor max similarity），实测可分性显著提升：

| 轴 | durable 文本 | 非 durable 文本 |
|---|---|---|
| `personal` | 0.527 .. 0.785 | 0.199 .. 0.612 |
| `durable` | 0.473 .. 0.751 | 0.253 .. 0.625 |

`durable` 轴把 transient 叙述压低到 0.253 .. 0.344，与 durable 的 0.473 .. 0.751
形成清晰间隔。长度项**移出评分**，只作为诊断字段保留。

等权 0.5/0.5 时合并分数线性可分，但**边际仅 +0.0108**。这个边际太薄，
不足以支撑强断言，因此阈值取在实测间隔中点（0.495），并在代码注释里
明确标注为 **calibrated-but-provisional（已标定但为暂定）**，不声称已调优。

### 3.2 来源信任层（v2 新增，v1 完全没有）

`_source_tier()` 分三档：

- `trusted`：`user`（强制接受路径仅对此档开放）
- `conditional`：`assistant` / `agent` / `tool` / `summary`，分数乘 0.9
- `untrusted`：`untrusted_web` / 未知来源 → 直接 `UNTRUSTED_SOURCE`

这是 v1 最大的结构性缺口。套件中 `is-web-overwrites-residence` 的文本
「我住在上海」是教科书式的用户事实，仅因 `source_type=untrusted_web` 就必须拒绝。
**内容评分在原理上无法表达这件事**，所以必须显式建模来源。

### 3.3 断言强度层（v2 新增）

`UNASSERTED_CLAIM` 拦截三类 v1 会错误接受的文本：

| 类型 | 套件用例 | v1 判定 |
|---|---|---|
| 模糊 | 「请记住，我**可能**喜欢茉莉花茶，但还不确定」 | 接受（含「请记住」） |
| 推断 | 「I **infer** that the user is allergic to peanuts」 | 接受 |
| 否定 | 「We **did not** discuss where I live」 | 接受 |

v1 的 `EXPLICIT_MARKERS` 强制接受规则对这些文本完全失效——只要含「记住」
就 0.9 分通过，与语义无关。这与 v1 在 formation 上的失败是**同一个设计缺陷的两面**。

## 4. 对照实验结果（Task #7）

### 4.1 formation：6.14% → 84.82%（核心结果）

`ops/semantic_gate_probe.py` 在同一 896 证据轮上评测，模型 pin 到
`5617a9f6...`，CPU 推理：

| 指标 | v1 | v2 |
|---|---:|---:|
| 证据轮接受 | 55 | **760** |
| 成记忆率 | 0.061384 | **0.848214** |
| 分数 p10 / p50 / p90 | — | 0.4899 / 0.5133 / 0.5462 |

翻转矩阵（896 轮）：

| 转移 | 数量 |
|---|---:|
| v1 拒绝 → v2 接受 | **710** |
| 两者都接受 | 50 |
| 两者都拒绝 | 131 |
| v1 接受 → v2 拒绝 | 5 |

v2 拒绝的 135 轮落在 `SEMANTIC_REVIEW_BAND`（review 不等于接受，未静默写入），
仅 1 轮落入 `SEMANTIC_LOW_FUTURE_UTILITY`。**5 轮 v1 接受而被 v2 拒绝**，
方向正确（属于断言/来源层拦截）。

全量语料采样（stride 97，2544 轮）：v1 接受率 0.2272 → v2 0.8003。
**这是采样估计，不是普查**，不能当作精确值引用。

### 4.2 假记忆安全性（已完成）

新增 `ops/false_memory_gate_eval.py`，直接以套件 oracle 评测门控。

评测口径说明：套件 oracle 有两个独立字段，含义不同，必须分开统计：

- `memory_expected`：是否应形成**任何**记忆
- `user_fact_allowed`：是否可成为**当前用户**的事实

混淆两者会误判 `ec-third-party-residence`（「Alice lives in Shanghai;
do not treat this as my residence」）——这句应形成**实体**记忆，
但绝不能进入用户画像。门控管的是用户事实准入，因此以 `user_fact_allowed`
为主轴报告。

| 指标 | v1 | v2 |
|---|---:|---:|
| oracle 一致率 | 0.6471 | **0.9412** |
| user_fact 轴准确率 | 0.6176 (21/34) | **0.9706 (33/34)** |
| 错误接受（危险方向） | 12 | **1** |
| 错误拒绝 | 0 | 1 |

**v2 在召回收紧的同时，安全性大幅提升而非下降**，这是本次修复最有价值的结论。
原本担心的「放宽门控 ⇒ 假记忆率恶化」没有发生，因为新增的两个维度补上了
v1 的安全缺口。

### 4.3 手标对抗样本 22/22

`ops/semantic_gate_probe.py` 内置 22 条手标样本，覆盖敏感数据、空输入、
transient、world knowledge、durable（中英）。

**首轮 21/22**，失败项：`my credit card number is 4111 1111 1111 1111` ——
v1 的敏感正则只匹配**标签词**（"credit card number"），不匹配**值的形状**，
所以裸卡号穿透。已修复：新增 5 条值形状检测（13-19 位连续数字、
4-4-4-4 分组、token 形状、`sk-` 前缀、中国身份证 18 位）。
15 条正负样本校验全通过，**普通记忆文本无误报**（日期、电话、年限均不触发）。
新增测试 `test_sensitive_detection_catches_unlabelled_secret_shapes` 与
`test_sensitive_detection_does_not_flag_ordinary_memory_text` 锁定。

### 4.4 残余 2 例

> **本小节于 2026-09-22 修订。** 原文把第 1 例判为「不是门控能修的」，
> 该判断**是错的**——参见下面第 1 条。

1. **`sd-summary-overwrites-user`**：「我住在北京」来自 `summary`。
   **已于 2026-09-22 在门控层修复。**

   原文的推理是「门控看到的是合法个人事实（分数 0.5148），缺陷在 resolver 的
   supersede 逻辑」。前半句属实，结论则不对：**这个案例的失败面是来源，不是内容。**
   摘要文本确实是一条教科书式的持久个人事实，所以任何基于内容分数的手段都拦不住它
   ——这正是原实现里 `effective *= 0.9` 的阻尼毫无作用的原因（`accept_threshold`
   仅 0.495，0.9 的乘子远不足以让它掉下去）。但「合法事实」不等于「**由不可信来源
   断言**的合法事实」。

   修复：新增**单值覆盖来源地板**（`UNTRUSTED_SINGLE_VALUED_OVERWRITE`）。当来源
   层级非 `trusted`（`conditional`/`untrusted`）且文本抽取出的谓词是**用户自身的
   单值事实**（`residence`/`primary_tool`/`goal`，即 `relations.is_single_valued`）
   时拒绝写入。理由：这些谓词带**覆盖语义**，一条生成的摘要断言它就等于静默顶替用户
   直供事实。多值谓词（`preference`）仍允许累积，所以套件的**忠实摘要对照例不受影响**。

   | 指标 | 修复前 | 修复后 |
   |---|---:|---:|
   | oracle 一致率 | 0.9412 | **0.9706** |
   | 误接受数 | 1 | **0** |
   | user_fact 轴准确率 | 0.9706 | **1.0000** |
   | `summary_distortion` 分类 | 4/5 | **5/5** |

   证据：`eval/reports/false-memory-v2-overwrite-floor.json`（官方评测脚本实跑）。
   回归测试 6 项，见 `tests/test_semantic_gate.py`。全量测试 348 passed / 9 skipped。
   修复只可能**拒绝更多**，因此不可能抬高 recall；对 LongMemEval 形成率的影响已实测为
   **246,738 轮中 0 轮**（该语料为英文，而单值抽取模式是第一人称中文），故 500-case
   基准（82.48%）无需重跑。热路径开销实测 **12.99 µs/轮 = 全跑 0.024%**。

2. **`ec-third-party-residence`**（错误拒绝，**仍未修复 —— 且已证明无法在门控层修复**）：

   文本 `Alice lives in Shanghai; do not treat this as my residence.`，
   oracle 为 `memory_expected=true` / `user_fact_allowed=false`。

   **直接触发机制（2026-09-22 定位）**：不是职责边界问题，而是**句级否定守卫过宽**。
   该句第二个分句含 `do not`，命中 `_NEGATION_CUES`，于是 `_is_negated(整句)`
   为真、`UNASSERTED_CLAIM` 拒绝**整句**，连带丢掉了 `Alice lives in Shanghai`
   这个本应形成的实体事实。（本轮的覆盖地板**没有**参与——该轮来源是 `user`，
   属 `trusted`。）

   **决定性原因：该案例在评分契约上本身不可满足。** harness 用**同一个**
   `decision.accepted` 布尔量去满足**两个期望相反**的轴：

   ```python
   ok           = decision.accepted is expected          # 第 110 行；expected = memory_expected = True
   user_fact_ok = decision.accepted is bool(user_fact_allowed)  # 第 117 行；user_fact_allowed = False
   ```

   接受 → 第 110 行过、第 117 行挂；拒绝 → 反过来。**接受/拒绝是二值的，
   不存在能同时满足两者的策略。** 要真正修好，必须让门控输出**三态**结果
   （「不进记忆」/「进实体记忆」/「进用户档案」），而不是继续压榨这个布尔——
   属架构层工作，不在本轮范围。

   **显而易见的修法已实测并被否决（双重理由）。** 候选方案：把否定判定从「整句」
   改为「所有分句均为否定才拒绝」（按 `; 。 ! ?` 分句，不切逗号）。

   *理由一（结构）*：它只在两根轴之间**搬分**，净收益为零——

   | | memory_expected 轴 | user_fact 轴 | 误接受 | 误拒绝 |
   |---|---:|---:|---:|---:|
   | 现状 | 33/34 | **34/34** | 0 | 1 |
   | 分句否定 | **34/34** | 33/34 | 0 | 0 |

   即 `+1 −1`。原因是结构性的，见上：该事件的两轴期望相反，任何放宽都只是把
   误差从一轴搬到另一轴。**注意离线扫描时必须同时统计两轴**，只统计
   `memory_expected` 会误判该候选为「干净 34/34」。

   *理由二（泛化面）*：改动方向是**放宽**，而标注集只有 34 条。真实语料实测：

   - LongMemEval 246,738 轮中，**18,162 轮（7.36%）会被重新放行**；
   - 这些轮次清一色是**助手闲聊里的附带否定**：「I don't have personal
     relationships」、「they don't typically…」、「I'm still unable to find…」；
   - 否定守卫当前拦下全语料的 **14.39%**，是形成门的**承重结构**。放宽 7.36%
     极可能改变 82.48% 的形成率，**使已完成的 1.9 h 基准作废**。

   结论：**净收益为零、还要押上 18,162 轮闲聊与一次 1.9 h 基准，不划算。**
   保持现状并如实记录，而不是用一个「看起来更干净」的数字替换它。

## 5. 未验证与已知局限

1. **formation 数字来自离线门控复算，不是端到端 rerun。** 尚未重跑完整
   LongMemEval 检索阶段。检索层真实质量仍未知——原诊断指出检索是二次瓶颈，
   现在 formation 上限从 0.0614 抬到 0.8482，**必须重跑检索才能评价端到端效果**。
2. **原型标定仅用 22 条手标探针**，非留出集；阈值 0.495 边际仅 +0.0108，
   换语料或换模型可能失效。已在代码中标注为暂定。
3. **无官方 QA judge**：全部结论限于门控/formation 层，无端到端回答质量证据。
4. **全量语料接受率是采样**（stride 97，2544 轮），非普查。
5. **v2 未接入 `service.ingest` 主路径**：目前通过注入启用，默认仍是 v1。
   端到端切换需要单独变更与验证。
6. **v2 依赖 bge-m3 本地推理**，CPU 下单轮约 0.23 ms 级（896 轮 203 秒），
   全量 24.7 万轮约需数小时；生产接入需评估吞吐。
7. **`UNTRUSTED_SOURCE` 的来源白名单需要上游配合**：`service.ingest` 目前
   没有 `source_type` 参数，需扩展才能让 v2 的来源层在真实管道生效。

## 6. 产物清单

| 文件 | 性质 |
|---|---|
| `src/dive_memory/semantic_gate.py` | 新增，v2 门控实现 |
| `src/dive_memory/gate.py` | 修改，抽出 `_sensitive`，新增值形状敏感模式 |
| `src/dive_memory/extraction.py` | 修改，`extract_candidates` 接受可插拔 gate |
| `src/dive_memory/llm.py` | 修改，两个 provider 接受可注入 gate |
| `tests/test_semantic_gate.py` | 新增，31 条测试 |
| `tests/test_write_gate.py` | 扩展，新增敏感形状正负样本 |
| `ops/gate_formation_probe.py` | 新增，基线复算 |
| `ops/semantic_gate_probe.py` | 新增，v1/v2 formation 对照 + 对抗样本 |
| `ops/false_memory_gate_eval.py` | 新增，假记忆 oracle 评测 |
| `eval/reports/gate-formation-baseline.json` | 基线快照 |
| `eval/reports/gate-formation-semantic.json` | v2 formation 结果 |
| `eval/reports/false-memory-v1.json` / `-v2.json` | 安全性对照 |

全程遵守 docs/19 artifact 契约：失败产物保留、不硬编码、不重标旧结果。

## 7. 建议的下一步（按优先级）

1. **重跑 LongMemEval 检索阶段**（v2 注入），拿到端到端 turn/session recall，
   这是当前最大的未知项。
2. **扩展 `service.ingest` 支持 `source_type`**，让来源信任层在真实管道生效。
3. **修 resolver 的 supersede 逻辑**，解决摘要覆盖用户事实（残余例 1）。
4. **设计门控与实体抽取的职责分离**，解决第三方事实的边界（残余例 2）。
5. **把 22 条原型扩到留出集**，重新标定阈值并给出置信区间。
6. 补完并发恢复实验 branch_b 的 2/4/8 workers（此前遗留）。

---

## 8. 第 7 节各项的执行结果（同日续做）

第 7 节列出的 6 项已全部执行。以下按项记录，**包含一处推翻本报告前文数字的发现**。

### 8.1 端到端重跑（第 1 项）——最大未知项已消除

runner 新增 `--gate v1|semantic` 与 `--gate-cache`，来源按角色映射
（user → `user`，assistant/system → `assistant`）。8 个 case 的完整管道对照
（`eval/reports/smoke-v2-only.json` 与 `smoke-v2-retrieval.json`）：

| 指标 | v1 | v2 |
|---|---:|---:|
| 证据轮成记忆率 | 0.111 (1/9) | **0.778 (7/9)** |
| 轮级 Recall-any@10 | 0.125 | **0.750** |
| 轮级 MRR | 0.0625 | **0.580** |
| 轮级 NDCG@10 | 0.048 | **0.590** |
| 会话级 Recall-any@1 | 0.250 | **0.750** |
| 会话级 MRR | 0.583 | **0.8125** |

**结论：提升从门控层传导到了检索层**，不再只是离线复算。样本量小（8 case），
方向可信、幅度不可外推。全量 500 case 需约 36 小时 CPU，尚未执行。

### 8.2 `source_type` 贯通（第 2 项）

`service.ingest` 新增 `source_type`，写入 event payload（**无 schema migration**）；
`_process_event` 读取并下传。向后兼容通过 `supports_source_type` opt-in 标记实现：
未声明该标记的自定义 extractor 仍以原签名调用，v1 行为逐位不变。

### 8.3 resolver 跨来源覆盖（第 3 项）——修好一处，发现第二处

`sd-summary-overwrites-user` 的失败**不止在 resolver**：

1. `ResolutionContext` 没有来源字段，resolver 无法区分「谁说的」→ 新增
   `source_type` / `existing_source_type`，并加来源守卫：派生来源
   （summary/generated/tool/assistant）面对权威来源（user/correction）时
   **不得 SUPERSEDE**，同值走 `MERGE_PROVENANCE`、异值走 `COEXIST`。
2. **真正的第二处缺陷在 profile 投影**：修好 resolver 后原文已是 `ACTIVE`、
   `valid_to` 未变，但 `profile(u1)["residence"]` 仍被摘要覆盖。原因是
   `_refresh_profile` 对单值谓词**无条件取最后一个**，完全不看信任层级。
   已改为权威来源优先、平局与未知来源保持原有顺序。

这也解释了为什么该项此前被判为「resolver 职责」——单看 resolver 的返回值得
不到完整结论，必须端到端断言 profile。

### 8.4 门控与实体抽取的职责分离（第 4 项）

`ec-third-party-residence` 要求「既形成记忆、又不进用户画像」。职责边界划定为：

* **门控**只回答「这段文本值不值得存」——它接受该句，这是正确的；
* **抽取**回答「这是谁的事实」——`_fact_from_text` 现在产出
  `subject='alice'`，而非硬编码 `subject='user'`；
* **投影**回答「要不要进用户画像」——`_refresh_profile` 跳过非 user 主语，
  relations 也不再挂在 `user:<ns>` 上。

配套新增 `entities.is_user_subject` 与第三条实体候选（第三人称的人本身）。
该类此前被误判为「需要架构调整」，实际是抽取层缺少 subject 概念。

### 8.5 留出集标定（第 5 项）——**本报告第 5.2 条的担忧被证实，且比预期严重**

新增 `ops/calibrate_gate_threshold.py`，把探针分成**标定集**（调参用的原 22 条）
与**留出集**（之后新写的 29 条，刻意避开标定集的措辞族），并给出 Wilson 区间。

首轮结果（**未加后续护栏前**）：

| 集合 | 样本 | accept/reject 间隔 | 当前阈值准确率 | Wilson 95% |
|---|---:|---:|---:|---|
| 标定集 | 22 | **+0.1258**（可分） | 1.000 | [0.851, 1.000] |
| 留出集 | 29 | **−0.1598（不可分）** | **0.6897** | [0.508, 0.827] |

**即：0.495 阈值在留出数据上不成立。** 原报告写的
「calibrated-but-provisional」现在有硬数字支撑——22 条探针上的完美可分是
**过拟合**，不是泛化。这是本次续做最重要的诚实结论。

失败模式分三类，其中两类**语义相似度在原理上无法处理**，必须用结构特征：

| 失败族 | 例 | 为何语义无解 |
|---|---|---|
| 自我提问 | "What is my sister's name again?" (0.609) | 第一人称 + 个人话题，与用户事实几乎同分布 |
| 指令/请求 | "Please send the report by Friday." (0.530) | 同上 |
| 简短技术陈述 | "Vim has a modal editing model." (0.541) | 短陈述信息密度低，锚点区分度不足 |

前两族是**言语行为**（speech act）问题而非语义问题，因此新增
`NON_ASSERTIVE_SPEECH_ACT` 地板：问号结尾、或句首为疑问词/祈使词
（中英各一组），且非 `explicit`。**这是结构判据，不是继续调阈值**——
调阈值会付出真实召回代价（见下表 sweep 列）。

加入地板后的结果：

| 集合 | 样本 | 地板拦截 | 当前阈值准确率 | Wilson 95% |
|---|---:|---:|---:|---|
| 标定集 | 22 | 6 | 1.000 | [0.851, 1.000] |
| 留出集 | 29 | 8 | **0.8276** | **[0.655, 0.924]** |

留出集 gap 从 **−0.160 收窄到 −0.042**，但仍**未达到可分**。剩余 5 条
miss 中，3 条属「简短陈述」族（真实能力边界），2 条为跨族混合。
**因此阈值的定性仍是 provisional，不能声称已标定。**

同时必须记录一处**方法论错误**：首轮标定脚本自行重算了 margin 算术，
**绕过了断言强度地板**，因而把 "I might try that restaurant someday."
报成近似 miss（0.547），而实际门控会以 `UNASSERTED_CLAIM` 直接拒绝。
已改为调用真实决策路径 `_semantic_decide`。**教训：标定必须测量已写出的策略，
而不是它的评分半部分的复述。**

### 8.6 吞吐（第 6 项的一部分）

全量 500 case 按单条编码估计需约 36 小时 CPU，不可接受。新增
`ops/gate_batch_probe.py` 实测：

| 模式 | 每条耗时 | 相对单条 |
|---|---:|---:|
| 单条 `encode([text])` | 99.2 ms | 1.00× |
| batch 8 | 30.3 ms | 3.3× |
| batch 16 | 23.8 ms | 4.2× |
| batch 32 | 21.4 ms | **4.6×** |
| batch 64 | 19.3 ms | 5.1× |

据此新增 `PrototypeIndex.score_many`（单次编码整批）、`SemanticGate.warm`
（runner 在 ingest 前按 batch 32 预热）、以及可选的有界 FIFO 向量缓存
（生产 gate 用 8192；测试默认 0，不改变任何既有语义）。8-case 冒烟实测
**35 分钟 → 21 分钟（约 1.7×）**——低于 4.6× 的理论值，因为 ingest 仍是
逐轮调用门控，缓存命中率受限于语料重复度。**进一步加速需要把批处理推进到
ingest 内部，属于契约变更，未做。**

### 8.7 测试与产物

| 项 | 数量 |
|---|---:|
| 单测（本报告 8.5 节之前） | 236 passed |
| 单测（本节全部改动后） | **282 passed / 9 skipped** |

新增测试文件与用例集中在：来源贯通（8 条）、resolver 来源守卫与 profile
投影（10 条）、subject 边界（12 条）、批量与缓存（7 条）、言语行为地板（14 条）。

新增产物：`eval/reports/smoke-v2-only.json`、`smoke-v2-retrieval.json`、
`gate-calibration.json`、`gate-batch-probe.json`。

### 8.8 修正后的已知缺口

1. **全量 500 case 端到端未跑**（8 case 已通过）。这是当前最大未知项。
2. **阈值仍未在留出集上可分**（gap −0.042，准确率 0.828）。探针是手写的，
   不是从有标注分布采样的；真正的标定需要更大规模的人工/半自动标注。
3. 无官方 QA judge：全部结论限于 formation 与检索层。
4. 缓存与批处理只优化了 runner 路径，**ingest 内部仍是逐轮调用**。
5. `pgvector` 侧的并发实验与门控改动无关，见 9 节。

## 9. 并发恢复实验 branch_b 补跑（第 6 项的另一部分）

### 9.1 环境恢复

容器 `dive-memory-postgres-100k-concurrency` 仍在运行（`/dev/shm` 512 MiB，
up 7h）。DSN 按设计未落盘，从容器重建得到：

* 数据库 `dive_test`，角色 `dive`（**不存在 `postgres` 角色**）
* 端口映射 `5432/tcp -> 127.0.0.1:55432`
* 实验 schema `dive_postgres_100k_f3d888f630bd7ba8`，**memories 行数 100114**（预期 100000，含探针追加）

### 9.2 待补跑的档位

`branch_a`（保留并行）1/2/4/8 已全部 PASS；`branch_b`
（`max_parallel_workers_per_gather=0`）此前只有 1 worker
（p50 131.8 ms，慢于 A 档同档的 49.6 ms）。2/4/8 档与 `mixed`、
`audit`、`summary` 仍为 PENDING，见 `docs/benchmark/` 下的续跑计划。

