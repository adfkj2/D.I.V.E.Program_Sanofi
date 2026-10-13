# 续研 · DIVE 项目记忆

为持续数周实验、反复与 AI 协作的学生和初级研发工程师，保留**当前有效的约束、采纳决定、未采用原因与来源**。每次新会话开始前，先核查“现在依据什么继续工作”。

本项目是可运行的本地研究原型。用户需求是待验证假设；没有宣称已服务药企或提高真实研发效率。赛诺菲/DIVE 的背景体现在可靠性、可追溯性与工程判断上。

![真实工作台：当前状态、提议、来源与历史](docs/assets/workbench-current.png)

## 五分钟运行

推荐 Python 3.12。在本仓库根目录创建并激活虚拟环境，然后：

```text
python -m pip install -r requirements/runtime.lock
python -m pip install --no-deps .
dive-memory --db memory.db serve --port 8765
```

打开 <http://127.0.0.1:8765/>，点“载入合成演示”。默认**无密钥、无模型调用，只浏览真实证据**。四个视图连接同一持久化应用：继续工作、记忆时间线、回答与证据、验证报告。

演示中 A 已采纳，B 虽有更高的构造分数但未采用，C 只是提议；预算从 20 更新为 40。可以现场改值、回看旧日期、打开来源、添加矛盾、核查未知结果并遗忘偏好。数字均为合成数据，明确字段录入不冒充自动 NLP 抽取。

[完整安装、在线模式、CLI/API 与恢复说明](docs/29-runbook.md) · [实际架构](docs/25-delivery-architecture.md)

## 当前证据

| 检查 | 结果 | 边界 |
|---|---|---|
| Oracle 开发自评 R40 | **451/500，90.2%** | 同家族 judge、全 500 题已暴露、含缓存复用；官方未评估 |
| 独立 reader 工厂对照 | 500/500 输出一致，1110 次缓存命中 | 精确回放，0 新请求；不是新答题成绩 |
| 全部本地回归 | 1124 通过、9 跳过 | PG 服务与真实语义模型检查有环境缺口 |
| 结构化状态任务 | 120/120 | 完整事件扫描基线也为 120/120，不证明产品优势 |
| 1 万条记录查询中位时间 | 4.78 秒 → 0.52 秒 | 同机器与输入、来源批量化前后测量；非 LLM 或并发性能 |
| 浏览器 | 桌面/手机真实操作通过 | 不是用户研究；安装与 UI 证据见交付 manifest |

[完整验证与全部失败](docs/26-verification.md) · [R42 执行报告](docs/oracle/40-engineering-delivery-20261012.md)

2026-10-13 干净副本复验：1123 通过、10 跳过（额外缺少完整 Oracle 原文件），500 题回放与构造对照通过。[最终交付记录](artifacts/engineering-20261012/manifest.json)。代码已作本地真实提交，尚未推送远程；远程 CI 未执行。

**规则审查尚未关闭。** 未发现题号或参考答案硬编码的直接证据，但多轮完整评测反馈和规则选择仍有 F.1.1/F.1.4 风险，须向项目组披露并确认。90.2% 不能标为官方成绩或合规认证。[规则审查](docs/oracle/39-compliance-audit-20261012.md) · [提交披露材料](docs/28-submission-and-review.md)

## 复现，而不依赖历史工作目录

```text
python -m pip install -r requirements/dev.lock
python -m pip install --no-deps -e .
python -m pytest -q
python eval/run_project_validation.py --output artifacts/my-structured-run
python audit_tools/replay_bundle.py --verify artifacts/r40-reader-replay.zip --report artifacts/my-replay.json
python -m build --wheel
python tools/verify_wheel.py --wheel-dir dist --output artifacts/my-wheel-check
```

约 4 MB 的 [R40 reader 回放包](artifacts/r40-reader-replay.zip)包含精确请求响应、逐文件哈希和冻结源码；校验程序不具备网络传输能力。原始提交 SHA-256 为 `3b4b5950b9ff93e189250d4dfea57d8683aba094de63dbb33986d7e2a3cb6eb0`。原始历史文件继续保留，另有 [历史哈希索引](artifacts/history-index.json.gz)。

`dive-memory-oracle --dataset 数据路径 --output 新目录` 默认只打印冷运行计划。真正运行需要显式执行参数、请求/token 上限、已缓存语义模型、密钥及预算核对；本轮没有发起新付费生成。冷入口不读取旧答案或判分文件。

## 为什么可能值得用，也为什么可能不值得

价值假设是减少重新交代背景、误用旧约束和重复失败方案。当前强基线与系统的结构化正确率相同，因此必须进一步比较**正确恢复时间和维护负担**，不能靠功能列表证明需求。

[3–5 人任务观察材料](docs/27-user-study-kit.md)已准备，尚无真实参与者结果。如果一份事件摘要以相近成本完成任务，项目就收缩为记忆模块与摘要助手。

## 开发与资料

- 生产应用：`src/dive_memory/application/`；工作台：`web.py` 与 `static/`；核心：事件、记忆版本、来源、检索与 SQLite。
- `ops/` / `audit_tools/` / `eval/` 分别保留历史实验、审计和评测，不进入工作台的生成依赖。
- CI 配置包含 Python 3.12/3.14、wheel 安装、可选 PG 服务；远程执行尚待推送触发，不能用配置存在冒充通过。
- [工程实施计划](docs/24-engineering-delivery-plan.md)、[迭代总账 R1–R42](docs/oracle/ITERATION_LOG.md)、[历史 README](docs/history/README-before-engineering-20261012.md)、[第三方说明](THIRD_PARTY_NOTICES.md)。

本机原型采用单进程 SQLite。默认启发式抽取与非语义 hash embedding 不等于 Oracle 的语义模型配置；结构化字段核查不等于自由问答。正式 judge、规则认定、自然语言保留集和用户观察仍待完成。
