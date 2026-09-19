# 13 Code Review and Fixes

评审对象：`D.I.V.E.Program_Sanofi`（v0.2.0，工作区内的当前版本）。
评审日期：2026-09-19。

## 1. 范围与方法

工作区根部还存在一份 v0.1.0 的旧快照（`../src`、`../tests`、`../pyproject.toml`、`../README.md`），
它与本目录**不同源、且自身已有失败测试**（见第 6 节）。本目录是唯一可运行、可安装、带
`docs/`、`migrations/` 和 `.venv` 的版本，因此以本目录为评审对象。

方法不是读代码猜问题，而是先建立可复现基线，再用探针脚本逐条验证假设：

1. 跑通原始测试套件 → `42 passed`（与 `docs/12` 声明一致）。
2. 写两轮探针脚本，对每个怀疑点直接观察运行结果（而不是推理）。
3. 按证据修复，再跑探针确认行为改变，并把结论固化为回归测试。
4. 全量测试 + smoke 基准 + `compileall` 三重验收。

> 环境注意：默认 `tmp_path` 目录 `%LOCALAPPDATA%\Temp\pytest-of-<user>` 在本机不可访问
> （`PermissionError: [WinError 5]`），会让 3 个使用 `tmp_path` 的测试报错。这**不是**代码
> 缺陷，而是环境问题。用 `--basetemp` 指向项目内目录即可：

```powershell
.venv\Scripts\python -m pytest -q --basetemp .pytest-tmp
```

## 2. 结论摘要

| # | 问题 | 影响 | 严重度 | 状态 |
|---|---|---|---|---|
| B1 | 所有 predicate 都被当作单值，无关事实互相 supersede | 静默丢记忆 | 高 | 已修复 |
| B2 | `as_of` 用字符串比较 | 历史查询答错/答空 | 高 | 已修复 |
| B3 | 自然问句（"我现在住在哪里"）假拒答 | 存下来的事实取不出来 | 高 | 已修复 |
| B4 | 无标点长 CJK 串无法被检索 | 记忆写进去但查不到 | 中高 | 已修复 |
| B5 | `decay` 传无时区 `now` 抛 TypeError | HTTP 500 | 中 | 已修复 |
| B6 | `decay(dry_run=True)` 仍改写 profile | dry-run 有副作用 | 中 | 已修复 |
| B7 | bm25 负值被截断，所有命中同分 | 词法通道失去区分度 | 中 | 已修复 |
| B8 | `append_event` 冲突后不回滚 | 事务悬挂/半写提交 | 中 | 已修复 |
| B9 | `correct_memory` 可跨 namespace | 越过租户边界写入 | 中 | 已修复 |
| B10 | API 用 `memory.__dict__` 取字段 | 死分支 + 响应缺字段 | 低 | 已修复 |
| B11 | 多处依赖 SQLite 未定义行序 | 行为不确定 | 低 | 已修复 |

## 3. 逐项分析

### B1 冲突判定过度 supersede（高）

`_find_conflict` 只比较 `predicate` 与 `value`，没有区分单值/多值谓词。结果是：两条正文完全
无关的 `statement`、或两条并存的偏好在写入时互相 `SUPERSEDED`。

证据（修复前）：

```
P1 我养了一只猫 / 我对花生过敏 → active=1     # 第一条被顶掉
P2 我喜欢绿茶 / 我喜欢咖啡     → first_status=SUPERSEDED
```

这直接违反 `docs/04`：*"只有在 predicate 相同或规则明确时才 supersede…否则两个事实并存"*。
修复方式是在 `relations.py` 显式声明单值谓词集合（`residence` / `primary_tool` / `goal`），
其余（`preference`、`statement`）累积。

修复后：`P1 active=2`，`P2` 两条均为 `ACTIVE` 且产生 2 条 relation 边。

### B2 as_of 用字符串比较（高）

原实现直接比较 ISO 字符串，文档 `docs/05` 的典型场景（"问 2025 年住哪里用 as-of 过滤"）因此失效。

证据（修复前，`timeline(as_of=...)`）：

```
full_iso            → ['成都']     ✓
'2025-06-01'        → ['成都']     ✓（侥幸）
'2025-01-01'        → []           ✗ 当日生效的事实被排除
'2025'              → []           ✗ 整年查询返回空
```

根因：`"2025" < "2025-01-01T00:00:00+00:00"`，且日期精度不一致时字典序没有语义。新增
`temporal.py`，把 `2025` / `2025-06` / `2025/6/2` / `2025年6月2日` / 裸 ISO（含 `Z`）统一
归一化为 UTC `datetime`，年月日分别解析为**该区间末刻**，并提供 `visible_at()` / `expired()`。

修复后 4 种写法全部返回 `['成都']`；同时 `2026-06-01` → `上海`、`2024-01-01` → `[]`（正确拒答）。

顺带修掉了 `require_instant()`：非法 `as_of`/`now` 现在抛 `ValueError` → HTTP 400，而不是
被静默忽略后回答另一个问题。

### B3 自然问句假拒答（高）

`planner` 会算出 `intent`/`temporal_mode`，但**从不产生检索可用的约束**，所以"存了事实"和
"能回答"完全脱节。

证据（修复前）：`我现在住在哪里` / `我喜欢什么` / `我的目标是什么` / `现在住哪里` 全部返回 `[]`，
只有直接命中词（`上海`、`绿茶`）才能召回。原因是中文问句与 `residence: 上海` 在字典序词法和
哈希向量上都没有重合，得分被 `< 0.15` 阈值过滤 → 拒答。

修复：`planner.query_predicates()` 把"问的是哪个属性"映射为谓词（住/居→residence，
喜欢/偏好→preference，使用/工具→primary_tool，目标→goal），`store.search()` 增加
`predicate` 通道。同时对**查询里的时间表达**调用 `find_period()` 生成 `as_of`。

修复后：`现在住哪→上海`、`2025年住哪→成都`、`2026年住哪→上海`、`以前住在哪→两者`。

**精度护栏**：谓词通道默认会为"火星住哪里"这类**未知主体**的问题返回用户自己的事实——这正是
`docs/06` 禁止的"以相似但不同实体填空"。smoke 基准立刻抓到该回归（`abstention_accuracy`
从 1.0 掉到 0.667），因此加入 `_residual_subject()`：剔除代词/时间词/疑问词/谓词词表及显式
年份后，若仍残留 CJK 字符，说明问题问的是别的对象，谓词通道不生效。修复后 smoke 恢复
`abstention_accuracy=1.0`。

### B4 无标点长 CJK 串不可检索（中高）

FTS5 默认 `unicode61` 分词器把整段连续 CJK 当作**一个 token**（CJK 属于 alphanumeric，
且没有空格可切）。于是 `preference: 咖啡并且每天早上喝咖啡` 变成单个 token，用 `咖啡` 永远查不到。

证据（修复前）：`查 '咖啡' → []`，`查 '喝咖啡' → []`（记忆已落库，但完全不可达）。

修复：新增 `lexical.py`，写入 FTS 时同时索引每个 CJK run 的**字符二元组**，查询侧做同样扩展
（拉丁文本不受影响，本来就按空白/标点分词）。修复后 `咖啡`、`喝咖啡`、`每天早上` 均可召回。

同时发现 `structured_content` 以 `ensure_ascii=True` 写入 FTS 列，中文被转成 `\u5496...`，
等于该列对 CJK 完全无效——已改为 `ensure_ascii=False`。

### B5 `decay` 时区比较崩溃（中）

`apply_decay` 用 `datetime.fromisoformat(now)`，无时区时得到 naive datetime，再与
补了 UTC 的 `observed` 比较 → `TypeError: can't compare offset-naive and offset-aware datetimes`。
`/v1/jobs/decay` 允许调用方传 `now`，因此这是一个可被 HTTP 触发的 500。

修复：统一走 `temporal.parse_instant()`（naive 视为 UTC）。修复后 `now="2025-02-01T00:00:00"`
正常归档。`lifecycle.archive_expired` 的同源字符串比较一并改为 `expired()`。

### B6 dry-run 有副作用（中）

`service.decay()` 无条件调用 `_refresh_profile()`，而 `archive_expired()` 有 `if not dry_run`
保护——同一类作业语义不一致。

证据（修复前）：`decay(dry_run=True)` 把 profile 从 `{"sentinel": 1}` 改写成 `{"preference": "Python"}`。
已加同样的 dry-run 保护。

### B7 bm25 打分退化（中）

`1.0 / (1.0 + max(0.0, bm25(...)))`：SQLite 的 `bm25()` 返回**负值**（越小越相关），
`max(0, ·)` 把所有命中都压成 `0` → 词法分恒等于 `1.0`，通道完全不携带信息。

证据：`raw bm25 = -1.2e-06` → 转换后 `1.0`；两个都命中的文档 `distinct_scores=1`。

修复：改为按 `bm25` 排序后的**序位**打分（`1/(1+position)`），量纲无关、跨查询可比。
配套把阈值从 `score < 0.15` 收窄为"仅纯 dense 命中才受阈值约束"——否则第 2 名之后的词法命中原
会被阈值误杀，反而降低召回。

### B8 事务未回滚（中）

`append_event` 捕获 `IntegrityError` 后只 `return False`，没有 `rollback()`。

证据（修复前）：`in_transaction_after_failure = True`。连接会停留在已中止的事务里，之后任意一次
成功的 `commit()` 都可能把"events 已插入、outbox 未插入"的半写状态提交，使事件被 worker 永久跳过。
已补 `self.db.rollback()`。

### B9 纠正可跨 namespace（中）

`correct_memory(..., namespace="victim")` 会把记忆写进他人的 namespace，并顺带污染对方的 profile
投影。namespace 在本项目里就是隔离边界（`test_namespaces_are_isolated`），因此这是越权。

修复：允许显式重申原名，但跨 namespace 直接 `ValueError`（在删除原记忆**之前**校验，避免先删后报错）。

### B10 / B11 低severity 清理

- `api.py` 里 `item.memory.__dict__ if hasattr(...)` 分支是死代码：`Memory` 是 `slots=True`，
  没有 `__dict__`；而 fallback 又缺 `status`/`observed_at`/`durability`。改为统一的
  `memory_payload()`，并确认响应里不含内部向量。
- `active_memories()` / `memories_for_namespace()` / 检索候选 SQL 增加 `ORDER BY`，消除
  "谁被 supersede/merge、谁在并列时排前"对未定义行序的依赖。

## 4. 变更文件

新增：

- `src/dive_memory/temporal.py` — 时间解析/比较（`parse_instant`、`find_period`、`visible_at`、`expired`、`require_instant`）
- `src/dive_memory/lexical.py` — CJK 二元组索引与 FTS 查询构造
- `tests/test_edge_cases.py` — 19 条回归测试，逐条对应上面的问题（另有 1 条 HTTP 层测试加入 `test_api.py`）
- `docs/13-code-review-and-fixes.md` — 本文

修改：

- `store.py` — 事务回滚、FTS 写入标准化、序位 bm25、`predicate` 通道、确定性排序、`reindex_lexical()`
- `planner.py` — 查询时间→`as_of`、谓词映射、未知主体护栏
- `service.py` — 单值谓词冲突判定、谓词下发、`as_of`/`now` 校验、dry-run 保护、跨 namespace 拦截、`timeline` 时间比较
- `relations.py` — `SINGLE_VALUED_PREDICATES` / `is_single_valued()`
- `decay.py`、`lifecycle.py` — 时区安全与确定性
- `api.py` — `memory_payload()`、`parse_int()`、`ValueError` → 400
- `tests/test_api.py` — 新增 HTTP 层时间校验与载荷测试

## 5. 后续 hardening 状态

本报告之后的 Phase 2 已继续处理上述设计缺口：多值 profile 改为去重列表；纠正生成 version/
supersedes 链并写 `memory_versions`；访问记录在单事务内批量提交；projection 支持确定性 replay；
outbox 支持原子 claim、重试和 dead-letter；write decision、job、memory mode 均持久化；检索改为
RRF + quality rerank，并补英文未知主体、CJK 过召回及 bounded multi-hop 护栏。

当前仍依赖外部环境的工作只有 PostgreSQL repository/pgvector 实例验证，以及 LoCoMo、LongMemEval、
BEAM 等受数据许可和模型配置影响的公开 benchmark。详见 `docs/14-phase-2-hardening.md`。

## 6. 工作区重复副本（需你决策）

工作区根部存在 v0.1.0 旧快照，已确认是**被取代的副本**：

- 根 `tests/test_evaluation.py` **当前就是失败的**：`abstention_accuracy` 断言 1.0，实际 0.5
  （旧版检索没有相关性阈值，"火星" 会返回已存的记忆）。
- 根 `README.md` 指向 `docs/`，但根部没有 `docs/`（`docs/` 只在本目录）。
- 文件时间戳：根部 `00:44`，本目录 `10:57`–`11:48`。根部缺 `migrations/`、`.venv`、
  `baselines.py`、`llm.py`、`worker.py`、`embeddings.py`、`context.py`、`decay.py`、`entities.py`、
  `lifecycle.py`、`planner.py`、`relations.py`、`smoke.py`。

两个副本并存会导致"改错文件"和测试结果互相矛盾。建议二选一：**删除根部旧快照**，或
**把本目录提升为工作区根**（两者都涉及删除/移动文件，需要你确认后我再执行）。

## 7. 验收

```text
pytest                95 passed
compileall src tests  OK
smoke                 recall=1.0  abstention_accuracy=1.0  false_memory_rate=0.0
10K SQLite            retrieve p95≈404ms  false_memory_rate=0.0
```

新增测试除覆盖 B1–B11 外，还覆盖原子投影、retry/dead-letter、hard purge、replay、版本、授权、
严格 API schema、双时间、多跳、持久化 memory mode、敏感信息拒写和评测公式。
