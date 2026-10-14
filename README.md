# 续研 · DIVE 项目记忆

> 为长期做个人项目、反复与 AI 协作的人，把**重要记录、来源、待办和上次交接**放在一起。
> 记录进展、完成下一步，下次打开直接继续；每个关键结论都能回看到来源和时间。

<p>
<img alt="Python" src="https://img.shields.io/badge/python-3.11%2B%20(3.12%20recommended)-3776AB.svg">
<img alt="Tests" src="https://img.shields.io/badge/tests-1215%20passing-brightgreen.svg">
<img alt="Delivery coverage" src="https://img.shields.io/badge/delivery%20coverage-93.91%25%20lines-brightgreen.svg">
<img alt="LongMemEval dev self-eval" src="https://img.shields.io/badge/LongMemEval%20dev%20self--eval-451%2F500%20(90.2%25)-blue.svg">
<img alt="Status" src="https://img.shields.io/badge/status-research%20prototype-orange.svg">
</p>

![个人项目工作台：待办、上次交接与当前记录（独立测试数据）](docs/assets/workbench-personal-project.jpg)

> **先说结论与边界。** 本项目是**可运行的本地研究原型**。`90.2%（451/500）` 是公开开发集上的
> **非官方开发自评**，不是官方成绩或合规认证；**规则审查（手册 F.1.1/F.1.4）仍未关闭**；
> 真实用户效率收益**尚未**由真实参与者验证。阅读本文时，请把"已实现的工程"与"已证实的价值"分开。

## 目录

- [这是什么](#这是什么)
- [核心特性](#核心特性)
- [快速开始](#快速开始)
- [日常使用流程](#日常使用流程)
- [工作原理](#工作原理)
- [评测现状](#评测现状)
- [项目结构](#项目结构)
- [文档索引](#文档索引)
- [开发与质量门禁](#开发与质量门禁)
- [局限与状态](#局限与状态)

## 这是什么

假设一月你告诉 AI「我住在成都」，几个月后又说「我搬到上海了」。现在问它住哪里，应当答上海；
问去年住哪里，应当能找到成都；问一个从未提过的护照号，应当说**不知道**，而不是猜一个像样的答案。

长期、跨会话的协作难点不在"存聊天记录"，而在**记住、更新和不知道**：哪些话值得记、旧信息何时失效、
回答时该取哪几条记录。本项目用事件溯源 + 有来源的记忆版本实现这条链路，并用
**LongMemEval Oracle（500 题）** 作为可复核的检验场：系统拿到历史对话与问题，自行建立记忆并作答，
参考答案只用于评估，不会提前交给记忆系统。

## 核心特性

- **项目目录**：为自己的每个项目创建名称与目标，旧项目自动发现，同一浏览器下次直接回到上次项目。
- **记录与来源**：保存重要事实、决定和依据；更新原记录时保留旧版本，可随时回看来源与时间线。
- **待办与交接**：持久待办支持完成/重开；离开前保存"本次交接"和"下一次先做什么"，下一步自动进入待办。
- **一致备份**：一键下载完整数据库（含所有项目、记录历史、来源、待办、交接和删除墓碑），并给出停服恢复步骤。
- **离线优先**：默认单用户、本机、离线运行，不需要账号、密钥或回答模型。
- **可复核交付**：每轮结果附带 manifest、源码哈希与成功/失败证据，失败与跳过项不删除、不计为通过。

## 快速开始

### Windows：双击即可（推荐）

在项目文件夹中双击 **[启动续研.cmd](启动续研.cmd)**。启动窗口会检查环境，缺少网页依赖时自动在
`.venv-workbench` 准备独立环境，服务就绪后打开浏览器。停止、重启、打开数据目录和查看日志都在该窗口内。
完整用户说明见 **[开始使用](开始使用.md)**。

> 换电脑时需安装 Python 3.11+（推荐 3.12，含 Tcl/Tk）。首次准备依赖需要网络，之后可离线启动。
> 记录沿用项目根目录的 `memory.db`。双击入口随源码提供，**不是**无需 Python 的独立 EXE。

### 其他系统 / 手工运行

```text
git clone https://github.com/adfkj2/D.I.V.E.Program_Sanofi.git
cd D.I.V.E.Program_Sanofi
python -m venv .venv && . .venv/bin/activate       # Windows: .venv\Scripts\activate
python -m pip install -r requirements/runtime.lock
python -m pip install --no-deps .
dive-memory --db memory.db serve --port 8765
```

打开 <http://127.0.0.1:8765/> 即可使用。完整的在线模式、CLI/API 与恢复说明见
[运行手册](docs/29-runbook.md)。

## 日常使用流程

1. **创建或选择项目** —— 起一个名字，写下目标；以后从左侧下拉框选择，旧项目自动出现。
2. **记录进展** —— 保存重要事实、决定和依据；在"全部记录与修改"按关键词搜索，变化时更新原记录，旧版本保留。
3. **安排下一步** —— 添加具体待办，完成后勾选；需要时可重新打开或编辑。
4. **结束本次工作** —— 保存交接，写下做到哪里、下一次先做什么；"下一步"会自动进入待办。
5. **下次继续** —— 同一浏览器打开即回到上次项目，先看交接和待办；也可复制/下载交接简报带到新的 AI 会话。
6. **保留备份** —— 左侧"备份我的数据"下载整个数据库；恢复步骤见[开始使用](开始使用.md)。

未提交的新增记录、记录修改和交接草稿保留在当前浏览器；正式数据写入本机数据库。待办与交接是**你维护的
工作安排**，不会被自动当成事实证据。

## 工作原理

每道题都独立跑同一条链路。"答案错了"至少有四种不同原因：原话没形成记忆、记忆没被检出、
检出了却没进入有限上下文、模型看到了却误读——因此系统把链路拆成可审计的阶段：

| 阶段 | 做什么 | 为什么 |
|---|---|---|
| 1. 读入与隔离 | 每题独立 `namespace` 与内存库，按真实时间排序写入 | 防止倒序写入把旧住址当最新，防止跨题串记忆 |
| 2. 事件与写入决策 | 每句先成带时间/来源/幂等键的**事件**，抽取候选并记录接受/拒绝理由 | 寒暄与临时内容会淹没证据；一次错误抽取不能变成持久"事实" |
| 3. 更新即加版本 | 单值事实（如居住地）更新时关闭旧版本有效期并标 `SUPERSEDED`，不删除原事件 | 问"现在"答上海，问"去年"仍能追到成都 |
| 4. 时间边界 | 提问时先划定可见时间范围（按"日"精度问题取当日末） | 这是正确性边界，不是排序微调 |
| 5. 候选检索 | BM25 词面 + 谓词/实体/关系通道，RRF 融合后按策略重排 | 人名/日期需精确匹配，同义改写需语义信号 |
| 6. 上下文打包 | 去重、排除过时状态，在 token 预算内装入，保留 `INCLUDE/EXCLUDE/TRUNCATE` 理由 | "已存入""已检出""进入阅读包"是三道不同关卡 |
| 7. 有据作答 | 只收问题、时间与打包证据；返回 `ANSWER/ABSTAIN/ERROR` 并校验引用 | 引用合法**不等于**答案正确 |
| 8. 逐题留痕 | trace 保存各阶段取舍与失败阶段，运行器生成配置清单、汇总与提交 JSONL | 便于断点续跑与归因 |

核心实现见 [`src/dive_memory/`](src/dive_memory/)，设计取舍见[架构](docs/25-delivery-architecture.md)
与[技术研究](docs/01-research-landscape.md)。

## 评测现状

**能声称的：**

| 检查 | 结果 | 边界 |
|---|---|---|
| Oracle 开发自评 R40 | **451/500，90.2%** | 同家族 judge、全 500 题已暴露、含缓存复用；**官方未评估** |
| 独立 reader 工厂对照 | 500/500 输出一致，1110 次缓存命中 | 精确回放，0 新请求；不是新答题成绩 |
| 结构化状态任务 | 120/120 | 完整事件扫描基线也为 120/120，**不证明产品优势** |
| 1 万条记录查询中位时间 | 4.78 s → 0.52 s | 同机同输入、改批量化前后；非 LLM/并发性能 |
| 全部本地回归 | 1215 通过、9 跳过（R46） | PG 服务与真实语义模型检查有环境缺口 |

**不能声称的：** 90.2% 不是官方成绩或合规认证；`docs/26-verification.md` 的历史验证不能当作本轮重跑；
"有引用"不等于"答对了"；120/120 结构化任务不能换算成自然语言盲测优势。
完整验证与全部失败见[验证报告](docs/26-verification.md)。

## 项目结构

```text
.
├── src/dive_memory/          # 生产应用与记忆核心
│   ├── application/          # 应用层：运行时、工作台前端、项目闭环
│   ├── oracle/               # LongMemEval 运行器、阅读器与校验
│   ├── static/               # 工作台前端资源（HTML/CSS/JS）
│   └── *.py                  # 事件、记忆版本、来源、检索、SQLite/PG
├── tests/                    # 单元/边界/质量门禁测试（含 PG 集成）
├── docs/                     # 设计、ADR、评测与运行手册（见下方索引）
├── artifacts/                # 可复核的阶段证据与源码哈希
├── ops/                      # 历史实验、部署(Docker/Compose)与 PG 运维脚本
├── audit_tools/ eval/        # 评测与审计入口（不进入工作台生成依赖）
├── tools/                    # 质量门禁、wheel/workbench 校验、Windows 启动器
└── requirements/             # runtime/dev 精确版本约束
```

## 文档索引

| 主题 | 入口 |
|---|---|
| 五分钟上手 / 用户指南 | [开始使用](开始使用.md) · [运行手册](docs/29-runbook.md) |
| 架构与数据模型 | [交付架构](docs/25-delivery-architecture.md) · [架构决策 ADR](docs/adr/) |
| 部署与运维（镜像/回滚/指标/备份） | [部署与运维](docs/31-deployment-and-operations.md) |
| 评测总账（逐轮目标/改动/费用/失败） | [迭代总账 R1–R46](docs/oracle/ITERATION_LOG.md) |
| 质量与复核入口 | [五项质量证据](docs/32-code-quality-evidence.md) · [代码走读](docs/33-code-walkthrough.md) |
| 验证与全部失败 | [验证报告](docs/26-verification.md) |
| 规则审查与披露 | [合规审查](docs/oracle/39-compliance-audit-20261012.md) · [提交材料](docs/28-submission-and-review.md) |
| 历史 README | [标准化前的 README](docs/history/README-before-standard-layout-20261014.md) · [工程化前的 README](docs/history/README-before-engineering-20261012.md) |

## 开发与质量门禁

本地与 CI 共用同一入口，更新证据请写入新的输出目录：

```text
python -m pip install -r requirements/dev.lock
python -m pip install --no-deps -e .
python -m pytest -q
python tools/check_quality.py --output artifacts/my-quality-run
python eval/run_project_validation.py --output artifacts/my-structured-run
python -m build --wheel
python tools/verify_wheel.py --wheel-dir dist --output artifacts/my-wheel-check
```

`check_quality.py` 包含静态检查、完整回归、行/分支覆盖率门槛与源码绑定；交付层与记忆核心各设
**行覆盖率 ≥90%、分支覆盖率 ≥80%**，任一不足即失败。CI 另含 PostgreSQL 契约、Docker 镜像就绪探针
与浏览器流程。**远程 CI 尚待本次推送触发，不能用配置存在冒充通过。**

最新一轮（R46）结果：完整回归 **1215 通过、9 跳过**；交付层行/分支 **93.91%/89.63%**，
记忆核心 **91.99%/80.56%**；真实浏览器 23 项操作观察（开发者执行，**非**用户研究），
含窄屏、断线重试、服务重启与下载备份恢复。全包行/分支为 73.91%/67.59%，未把局部门槛说成全仓达标。

## 局限与状态

- **定位**：一人、本机、单进程 SQLite 的本地原型。无云同步、无多人权限、无自动文件导入、无独立 EXE。
- **评测**：90.2% 为已暴露开发集的非官方自评；正式 judge、规则认定、天然语言保留集与真实参与者观察仍待完成。
- **环境缺口**：9 项跳过为 8 项可选 PostgreSQL 与 1 项真实语义模型检查；第三方库有 2 条弃用警告。
- **质量**：全源码历史 Ruff 仍余约 113 项诊断；覆盖率说明执行范围，不等于正确率或机制优势。
- **恢复**：手工恢复需先停服再移动文件；完整备份不含浏览器未提交草稿。

价值验证的下一步是观察真实使用者**隔日继续项目的成功率、耗时与维护负担**，不以功能数量替代用户证据。
本 README 只把"已实现"和"已测到"写成事实，不把计划中的优化写成已获得的成绩。

---

第三方依赖与许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
