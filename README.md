<div align="center">

# 续研 · DIVE 长期记忆助手

### 记得上次，也能推进下一步。

有长期记忆的个人项目 AI 助手。<br>
从对话出发，查阅记忆、提出行动、确认执行，再把进展留给下次。

<p>
  <img alt="本地优先" src="https://img.shields.io/badge/Local_first-本地优先-183747?style=flat-square">
  <img alt="DeepSeek 与兼容接口" src="https://img.shields.io/badge/AI-DeepSeek%20%2F%20OpenAI%20compatible-176B5B?style=flat-square">
  <img alt="Python 3.11 及以上" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square">
</p>

[开始使用](#开始使用) · [产品能力](#产品能力) · [技术与验证](#技术与验证) · [文档导航](#文档导航)

</div>

![续研项目助手：连接模型，从当前项目开始对话](docs/assets/project-agent-home.png)

<p align="center"><sub>真实助手首页 · 独立演示项目 · 模型尚未连接</sub></p>

## 让项目有一个可以回来的地方

写论文、做实验、开发一个小工具，往往要经过许多次工作与对话。续研让助手带着“上次做到哪里、为什么这样决定、接下来做什么”继续协作。记忆与项目数据保存在本机，模型负责理解请求和调用明确的项目工具。

| 你想知道 | 续研帮你找到 |
| --- | --- |
| 上次做到哪了？ | 助手读取项目安排、检索长期记忆，并提供可回看的来源。 |
| 接下来怎么推进？ | 对话中梳理下一步，生成可审阅的待办、记忆或交接变更。 |
| 下次还要重新解释吗？ | 对话和确认的记忆持续保存；也可导出交接简报，带到其他 AI 会话。 |

例如，告诉助手：“记住我们采用 MAE 比较误差，安排两种方案的对比，并保存本次交接。”助手提出具体变更；你确认后，它写入并逐项核验。下次询问采用了什么指标，它再从项目记忆中检索依据。

## 开始使用

### Windows：从一个双击开始

1. [下载项目 ZIP](https://github.com/adfkj2/D.I.V.E.Program_Sanofi/archive/refs/heads/main.zip)，解压到准备长期保留的文件夹。
2. 安装 **Python 3.11+**（推荐 3.12，保留默认 Tcl/Tk 组件），双击文件夹里的 **[启动续研.cmd](启动续研.cmd)**。
3. 浏览器打开 **项目助手**，创建项目，填写名称与目标。
4. 点击 **连接模型**，保留 DeepSeek 默认地址 `https://api.deepseek.com` 与模型 `deepseek-chat`，填写 API Key 后开始对话。

启动窗口提供停止、重启、查看日志和打开数据目录。首次准备依赖需要联网。AI 对话需要模型 API，按供应商规则计费；未连接时可离线维护项目资料。网页填写的密钥仅留在服务进程中，重启后需重新连接。这个入口随源码提供，仍需 Python 环境。

**[查看完整使用指南 →](开始使用.md)**

<details>
<summary>查看 Windows 启动窗口</summary>

![续研启动窗口](docs/assets/windows-launcher.png)

</details>

<details>
<summary>macOS / Linux / Windows 手工安装</summary>

先安装 Python 3.11+ 和 Git，然后获取项目：

```sh
git clone https://github.com/adfkj2/D.I.V.E.Program_Sanofi.git
cd D.I.V.E.Program_Sanofi
python -m venv .venv
```

macOS / Linux 可用 `python3` 替代 `python`，并激活环境：

```sh
source .venv/bin/activate
```

Windows PowerShell 无需激活环境，直接使用虚拟环境里的解释器：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements/runtime.lock
.venv\Scripts\python.exe -m pip install --no-deps .
.venv\Scripts\dive-memory.exe --db memory.db serve --port 8765
```

macOS / Linux 在激活后运行：

```sh
python -m pip install -r requirements/runtime.lock
python -m pip install --no-deps .
dive-memory --db memory.db serve --port 8765
```

打开 <http://127.0.0.1:8765/>。自定义数据库、CLI/API 与可选在线模式见[运行手册](docs/29-runbook.md)。

</details>

## 产品能力

### 01 · 先理解项目，再回应你

对话是主要入口。助手读取项目目标、待办和上次交接，按需检索长期记忆、核对来源，再回答问题或提出行动。项目之间隔离，历史回复与已确认事实分别管理。

### 02 · 把建议变成可核验的操作

助手能新增、编辑或完成待办，保存或更新记忆，留下本次交接与下一步。写入前展示完整提案；确认后整批执行并逐项读回核验。重复确认不会重复写入，资料变化时要求重新生成提案。

> **Agent 闭环：** 你的请求 → 读取记忆 → 工具调用 → 审阅提案 → 执行与核验 → 保存进展 → 下次继续。

### 03 · 长期记忆和数据都能保留

正式记录保存在本机 SQLite 数据库。对话、提案、记忆版本、来源、待办和交接都随完整备份保存；恢复前须停止服务，按[恢复步骤](开始使用.md)操作。新一轮对话读取近期历史和相关记忆，重要事实由你确认后进入长期记忆。

模型连接失败会显示原因并保留本次输入，不把失败包装成成功。每轮最多6次模型调用；手动记录、检索、版本回看和导出交接继续可用。

**[查看模型连接与 Agent 使用指南 →](docs/34-project-agent.md)**

<details>
<summary>查看项目资料的窄屏布局（R46）</summary>

<p align="center">
  <img src="docs/assets/workbench-personal-mobile.jpg" alt="续研在窄屏下的项目、待办和交接布局，使用独立测试数据" width="320">
</p>

窄屏展示用于说明布局适配；默认服务只在本机访问，不提供云同步。

</details>

**当前工具范围：** 本地个人项目内的记忆、待办和交接。助手尚不执行代码、联网搜索或修改仓库文件，也不提供云同步与多人权限。发送对话会将当前请求、近期对话和选中的项目资料交给所选模型服务；已完成四个合成项目场景的真实 DeepSeek 验收；真实用户效率与更广泛的回答可靠性仍待验证。

## 技术与验证

助手按四层组织：**模型接口 → Agent 工具循环 → 项目工具适配 → 记忆与存储**。助手编排不依赖 Oracle；Oracle 也不依赖助手。后续可独立迭代 benchmark 的写入、检索和 reader 策略，再把经过验证的通用能力接入产品。

[架构与扩展说明](docs/34-project-agent.md) · [核心交付架构](docs/25-delivery-architecture.md)

### 可复核的工程交付

**R52 远程工程检查已全绿：5/5 任务成功，PostgreSQL 8/8通过、零跳过。** 修复测试数据库初始化、删除路径误用SQLite接口及并发初始化冲突；本地环境跳过不作远程通过。[真实Actions结果](https://github.com/adfkj2/D.I.V.E.Program_Sanofi/actions/runs/38994769337) · [失败与修复记录](docs/oracle/50-ci-postgres-20261017.md)。

**R51：可进入受控个人试用。** 助手现在可以查找初始上下文之外的待办和旧交接；交接可明确沿用已有待办，避免模型改写名称后重复创建。模糊指代先澄清，提议与已确认决定分别处理。真实 DeepSeek 验收从基线6/10、首轮修复5/6，到最终新场景6/6；三组内容不同，前两轮失败完整保留。页面刷新与服务重启后数据恢复已验证，仍需独立用户的长期使用验证。[完整验收、失败与适用范围](docs/oracle/49-product-acceptance-20261016.md)。

提交前另修复任务编号空白、交接与同批次任务更新的冲突边界，并补充工程回归；未重发模型请求，6/6结果与最终版本的差异在报告中说明。

**R51 工程检查：1303通过 / 9环境跳过。** 交付层行1338/1407（95.10%）、分支370/404（91.58%）；记忆核心1080/1174（91.99%）、319/396（80.56%）。全源码行74.70%、分支68.69%。本轮52次模型调用保守估算¥0.291952，共享累计¥32.096522。[证据清单](artifacts/product-acceptance-20261016/manifest.json) · [冻结开发场景](eval/product/README.md)。

R47 的工程测试与浏览器流程使用明确标注的协议替身，完整质量与安装结果见[R47 报告](docs/oracle/45-project-agent-20261015.md)。

**R47 本地最终验证：**

| 验证项 | 已记录结果 |
| --- | --- |
| 自动化回归 | **1,268 通过 / 9 跳过**，较 R46 新增 53 项 |
| 应用交付层（含 Agent） | **94.99% 行 / 91.28% 分支** |
| 记忆核心覆盖率 | **91.99% 行 / 80.56% 分支** |
| 浏览器流程 | **12 项开发者操作观察**，包含提案执行、引用、重启、窄屏及连接失败 |
| 安装验证 | 全新 Python 3.14 安装通过，含安装后 Agent 执行与备份恢复 |

[查看本轮报告](docs/oracle/45-project-agent-20261015.md) · [查看证据清单](artifacts/project-agent-20261015/manifest.json) · [查看 GitHub Actions](https://github.com/adfkj2/D.I.V.E.Program_Sanofi/actions)

以上表格为 R47 历史结果，其全包行/分支覆盖率为 **74.64% / 68.56%**；当前 R51 数值见上文。9 项跳过包括 8 项 PostgreSQL 环境检查与 1 项真实语义模型检查，另有 2 条第三方弃用警告。局部门禁通过不代表全源码风格清理完成。R47 浏览器观察使用本机模型协议替身；R48—R51 另行完成真实 DeepSeek 小样本开发验收。真实用户效果仍待验证。远程 CI 状态以 Actions 为准。

<details>
<summary>研究背景与评测边界 · LongMemEval Oracle</summary>

DIVE 同时研究长期记忆的写入、更新、时间边界与有据作答。评测链路按独立记忆空间读入历史，经候选抽取、版本更新、检索融合与上下文打包后作答，并保存逐题取舍及失败证据。参考答案仅用于评估。

| 检查 | 结果与解释 |
| --- | --- |
| Oracle R40 开发自评 | **451/500（90.2%）**，非官方开发自评；同家族 judge、全开发集已暴露，包含缓存复用。 |
| 独立 reader 工厂对照 | 500/500 输出一致、1110 次缓存命中；是精确回放，不是新成绩。 |
| 结构化状态任务 | 120/120；强事件扫描基线也为 120/120，不能据此证明优势。 |
| 1 万条记录查询中位耗时 | 同机同输入批量化前后为 4.78 s → 0.52 s；不代表模型或并发性能。 |

**90.2% 不是官方成绩或合规认证。** 手册 F.1.1/F.1.4 规则审查尚未关闭，正式判分、独立自然语言保留集与真实参与者观察仍待完成。有引用不等于回答正确。

[验证报告](docs/26-verification.md) · [规则审查](docs/oracle/39-compliance-audit-20261012.md) · [逐轮实验总账](docs/oracle/ITERATION_LOG.md)

</details>

## 文档导航

| 你想做什么 | 从这里开始 |
| --- | --- |
| 连接模型、使用与扩展 Agent | [项目 Agent 指南](docs/34-project-agent.md) |
| 日常使用、备份与恢复 | [开始使用](开始使用.md) · [运行手册](docs/29-runbook.md) |
| 了解架构与设计取舍 | [交付架构](docs/25-delivery-architecture.md) · [架构决策](docs/adr/) |
| 部署、运维与回滚 | [部署与运维](docs/31-deployment-and-operations.md) |
| 评审代码与质量证据 | [五项质量证据](docs/32-code-quality-evidence.md) · [代码走读](docs/33-code-walkthrough.md) |
| 复核迭代、评测与失败 | [证据索引](artifacts/README.md) · [迭代总账](docs/oracle/ITERATION_LOG.md) · [项目进度](PROJECT_IDEA_AND_PROGRESS.md) |
| 查看提交材料与历史说明 | [提交材料](docs/28-submission-and-review.md) · [历史 README](docs/history/README-before-standard-layout-20261014.md) |

<details>
<summary>开发入口与仓库结构</summary>

```text
src/dive_memory/
├── agent/             # 独立模型协议与有限工具循环
├── application/       # 共用运行时、项目工具与持久对话
├── static/            # 工作台 HTML / CSS / JavaScript
├── oracle/            # LongMemEval 运行器、阅读器与校验
└── *.py               # 事件、记忆版本、来源、检索与存储

tests/                 # 自动化测试与集成契约
docs/                  # 用户指南、设计、ADR 与迭代报告
artifacts/             # 精选验证证据与源码哈希
ops/                   # Docker/Compose、PostgreSQL 运维与历史实验
audit_tools/           # 审计工具
eval/                  # 评测入口
tools/                 # 质量门禁、装包校验与 Windows 启动器
requirements/          # runtime / dev 精确依赖约束
```

在已激活的虚拟环境中安装开发依赖。本地与 CI 共用质量检查入口；新的运行结果应写入独立输出目录。

```sh
python -m pip install -r requirements/dev.lock
python -m pip install --no-deps -e .
python tools/check_quality.py --output artifacts/my-quality-run
python eval/run_project_validation.py --output artifacts/my-structured-run
python -m build --wheel
python tools/verify_wheel.py --wheel-dir dist --output artifacts/my-wheel-check
```

质量检查包含静态检查、完整回归、行/分支覆盖率门槛与源码绑定。交付层与记忆核心分别要求 **行覆盖率 ≥90%、分支覆盖率 ≥80%**。CI 配置另包含 PostgreSQL 契约、Docker 就绪探针与浏览器流程。

</details>

---

<p align="center">续研 · 记得上次，也能推进下一步。</p>

第三方依赖与许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
