# 安装、运行与恢复

推荐 Python 3.12；本轮同时在 3.14 运行完整本地回归。生产原型只使用 SQLite、单进程、127.0.0.1。无需 Node 即可运行工作台；Node 仅用于格式/浏览器开发检查。

## 从干净目录安装

Windows 可优先双击根目录的 `启动续研.cmd`。完整用户说明见[开始使用](../开始使用.md)。它先寻找项目的 `.venv-workbench`、`.venv` 或系统 Python；缺少网页依赖时在 `.venv-workbench` 创建独立环境并安装 `requirements/runtime.lock`。需 Python 3.11+ 及 Tk，推荐 3.12；不需要 Node，不更改全局 PowerShell 执行策略。

启动器使用根目录 `memory.db`，工作状态和日志保存在忽略提交的 `.workbench/`。同一项目采用操作系统锁避免重复启动；服务实际就绪并匹配实例身份后才打开浏览器。8765 被占用时由操作系统分配可用本机端口，并保持套接字直到服务接管。只停止启动器自己创建的进程，不按端口或进程名停止其他服务。关闭窗口后服务通过父子管道正常退出；异常退出或超时仅处理自己持有的子进程。

快捷入口固定 `evidence_only`；已有 `DIVE_WORKBENCH_TOKEN` 仍生效。需要在线模型或其他数据库时，使用下面的手工入口。复制数据库前先在启动窗口停止服务。

```text
git clone https://github.com/adfkj2/D.I.V.E.Program_Sanofi.git
cd D.I.V.E.Program_Sanofi
python -m venv .venv
```

激活环境（Windows PowerShell 为 `.venv\Scripts\Activate.ps1`，macOS/Linux 为 `source .venv/bin/activate`），然后：

```text
python -m pip install -r requirements/runtime.lock
python -m pip install --no-deps .
dive-memory --db memory.db serve --port 8765
```

打开 <http://127.0.0.1:8765/>，创建自己的项目、记录进展、添加待办，结束时保存交接；侧栏仍可打开真实案例或合成机制沙盒。默认不下载模型、不需要密钥、不调用 LLM。离线生成安装包须事先下载依赖；首次 pip 安装需要网络。当前远程仓库只有推送后才能取得本轮提交，工作区先验证本地交付。

`requirements/*.lock` 是确切版本约束，不宣称是带 wheel 哈希的供应链锁。Python 3.12 的实际安装结果由独立 wheel 检查保存。构建使用 setuptools；GPU/真实语义模型环境与默认原型分离，历史冻结环境仍按 R40 记录复现。

## 同一数据库的命令行操作

```text
dive-memory --db memory.db demo --project public-demo
dive-memory --db memory.db snapshot --project public-demo
dive-memory --db memory.db lookup --project public-demo --key 预算上限 --as-of 2026-10-02
dive-memory --db memory.db ask --project public-demo "当前基线依据是什么？"
dive-memory --db memory.db recover
```

明确记录用 JSON 文件，例：`{"key":"预算上限","value":"50 元","category":"constraint","disposition":"confirmed","reason":"明确调整","session":"session-4"}`。运行 `record 文件.json --project 项目名 --idempotency-key 唯一请求键 --replaces 旧记忆ID`；省略 replaces 表示新声明，可能形成冲突。数据库路径选错等于打开另一个项目库，不是数据丢失。

常用 API：`/healthz`、`/api/readiness`、`/api/snapshot`、`/api/records`、`/api/events`、`/api/answer`、`/api/lookup`、`/api/sources/{id}`、`/api/events/{id}`、`/api/traces/{id}`、`DELETE /api/memories/{id}`。字段定义见 `/openapi.json`，不依赖外部 Swagger CDN。

## 在线回答

在当前终端的环境中配置以下变量，密钥通过本机的安全输入方式设置，不写进命令历史、仓库或报告：

| 变量 | 含义 |
|---|---|
| `DIVE_MODE` | `live`；默认是 `evidence_only` |
| `DIVE_READER_ENDPOINT` | 完整兼容 chat/completions URL |
| `DIVE_READER_MODEL` | 供应商实际支持的模型名 |
| `DIVE_READER_API_KEY` | 密钥，服务端读取，浏览器不接收 |
| `DIVE_READER_PROVIDER` | 一般 `openai-compatible`；DeepSeek 使用 `deepseek` |
| `DIVE_READER_STRATEGY` | `indexed` 或显式历史策略 `r40` |
| `DIVE_WORKBENCH_TOKEN` | 可选本地访问令牌；与供应商密钥不同 |

默认 hash embedding 不是语义模型，中文/英文自然问答检索质量有边界；真实语义模型适配器在已有核心与 Oracle 配置中提供，不能把 Oracle 的 90.2% 移植成工作台默认成绩。

## Oracle 冷启动与历史回放

```text
dive-memory-oracle --dataset eval/external/longmemeval/longmemeval_oracle.json --output artifacts/cold-run
```

以上只输出计划，**零请求**。执行时另外传 `--execute --max-calls 数量 --max-reserved-tokens 数量 --device cpu或cuda`，要求新的输出目录、已缓存且固定版本的 BGE-M3、DeepSeek 密钥。先核对共享预算账本剩余额度和当前费率。token 限制是保守字节估计，不是金额保证。请求前持久化预留；失败/不确定请求中止整个运行，不自动重发。此冷入口尚未做全量付费生成，不能声称已复现 90.2%。

历史验证无需模型/密钥/全部旧结果：

```text
python audit_tools/replay_bundle.py --verify artifacts/r40-reader-replay.zip --report artifacts/replay-check.json
```

包内的冻结源码用于审阅旧实现；新工厂的精确回放只排除计时。完整 R41 旧审计还包含 judge 与引用检查，不能把其 1116 次命中和这里的 1110 次 reader 命中混算。

## 故障与恢复

没有记忆：打开写入依据，确认被写入门保留以及 project/db 一致。冲突：明确更新对应旧值，或删除错误来源，系统不会擅自选值。读取旧 trace 返回 404：可能因更新、遗忘、100 条上限或服务重启失效，重新提问即可。

投影异常：停止写入，备份 DB，运行 `recover`，每次最多 100 个待处理事件，核心重试次数有限；保留错误记录后定位根因，不循环强制重试。导出不包含供应商密钥，但可能含用户输入；公开分享前应由使用者确认内容。

停止服务后复制 DB 是此单机版本最简单的一致备份方式。需要运行中备份时使用 SQLite backup API。不要仅拷贝一个仍在写入的 WAL 数据库主文件。没有承诺多人权限、远程部署、HA 或数据备份服务。

## 0.4 项目恢复入口

默认从工作台载入真实项目案例，或在同一数据库使用下面的命令。案例为人工整理的仓库来源，核查和简报不调用模型。

```text
dive-memory --db memory.db casebook --project research-case
dive-memory --db memory.db brief --project research-case --format markdown
dive-memory --db memory.db brief --project research-case --since 2026-10-10 --format markdown
dive-memory --db memory.db consult --project research-case "官方成绩"
```

`brief` 可用 `--as-of` 查看历史状态，用 `--since` 明确比较起点；未指定比较起点时不会猜测用户上次何时工作。`consult` 支持受控项目任务或记录名称，对提议、未采用和冲突保留状态。`ask` 仍是独立的检索/模型入口。界面可复制或下载同一简报；刷新页面通过 URL 保留选定项目，不将访问令牌写入浏览器存储。

原文摘录是载入时保存的来源快照，附路径和冻结哈希。新增或改写记录会使用新来源事件，不能借用旧报告哈希为自己的改动背书。日期只有日精度的案例明确采用日末归一化，并不表示真实发生时分秒。更多任务见[案例说明](30-project-resumption-demo.md)。


## 开发质量检查

按 README 安装 `requirements/dev.lock` 后，执行 `python tools/check_quality.py --output artifacts/my-quality-run`；每次使用新的目录。该入口与 CI 相同，包含静态检查、全部回归、行/分支覆盖门槛及源码绑定。门禁清除继承的在线配置，环境缺口会列为跳过；PG 的真实服务检查另行运行。范围与故障排查见[质量证据页](32-code-quality-evidence.md)。

## 个人工作安排与备份

工作台通过 `/api/projects`、`/api/project`、`/api/tasks`、`/api/checkpoints` 管理项目目录、待办和交接。写入共用应用锁和SQLite事务；待办revision拒绝过期覆盖；交接与自动新增的下一步原子提交。request_id用于同一次请求重试。工作安排独立于事实检索，不会自动进入模型证据。

`GET /api/backup` 在应用锁内通过SQLite备份接口产生一致副本，包含WAL已提交数据；响应完成后删除服务器临时文件。使用既有API访问令牌边界，备份覆盖所有本地项目，不是单项目导出。恢复请按[开始使用](../开始使用.md)先停服务、保留旧库及附属文件，再放入备份，避免在线覆盖。CLI的核心投影重建不会重建或删除工作安排表。
