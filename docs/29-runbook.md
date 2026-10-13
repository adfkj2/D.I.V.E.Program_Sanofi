# 安装、运行与恢复

推荐 Python 3.12；本轮同时在 3.14 运行完整本地回归。生产原型只使用 SQLite、单进程、127.0.0.1。无需 Node 即可运行工作台；Node 仅用于格式/浏览器开发检查。

## 从干净目录安装

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

打开 <http://127.0.0.1:8765/>，点“载入真实项目案例”；也可选择合成机制沙盒。默认不下载模型、不需要密钥、不调用 LLM。离线生成安装包须事先下载依赖；首次 pip 安装需要网络。当前远程仓库只有推送后才能取得本轮提交，工作区先验证本地交付。

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
