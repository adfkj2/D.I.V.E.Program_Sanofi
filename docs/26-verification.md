# 验证结果与可复现入口

日期：2026-10-12。所有工程与构造任务结果均不等于官方成绩。

## 已得到的证据

| 检查 | 实测结果 | 解释范围 |
|---|---|---|
| 全部 Python 回归 | 1124 通过，9 跳过 | SQLite/应用/API/现有评测路径；8 个 PG 缺服务、1 个语义模型测试缺依赖 |
| 新应用与故障边界 | 16 通过，包含在上行 | 重启、冲突、历史、幂等、隔离、遗忘、恢复、模型异常、请求限制和批量来源 |
| R40 reader 工厂精确回放 | 500/500 一致，1110 次缓存命中，0 新请求 | 同输入与缓存响应，排除计时；不是新生成或独立判分 |
| 新结构化机制任务 | 30 个故事、120 个任务 | 6 种重复机制模板；数据/规则由同一开发方可见，非盲测 |
| 浏览器操作 | 桌面 1440px、手机 390px，7 项闭环检查通过 | 实际 API、真实修改/删除/导出，无外部网络请求；不替代用户研究 |
| 安装包 | 独立环境检查见交付 manifest | 仅 wheel 安装，清除 PYTHONPATH，跨进程 CLI/API 与静态资源 |

直接证据保存在 `eval/reports/engineering-delivery-20261012/`；可移植 reader 回放包为 `artifacts/r40-reader-replay.zip`，大小约 4.05MB，内含逐文件哈希、500 个公开请求、精确使用的响应与冻结源码。无需下载整个 750MB 历史工作目录。

## 与简单方案的对照

| 方案 | 正确 / 120 |
|---|---:|
| 完整记忆系统 | 120 |
| 从结构化事件重新扫描 | 120 |
| 滚动字段摘要 | 95 |
| 词面检索首条结果 | 90 |
| 最近会话 | 60 |
| 去掉显式更新 | 115 |
| 去掉来源角色门 | 110 |

相同故事、记录、噪声与删除请求提供给全部方案。事件扫描基线保留时间、提议和冲突判断，是有意保留的强基线。所有参考答案只用于生成输出后的确定性比较。

这组结果支持工程机制正确工作，**没有证明产品胜过简单事件扫描**。词面首条方案只是检索代理，不是完整 LLM RAG；题目提供结构化键，没有测自然语言抽取或同义问法泛化；未运行模型，也没有复核链消融。中文/英文仅代表记录文字语言，不能解读成双语 NLP 准确率。

完整逐题输出和所有失败在 `structured-validation/results.json` 与 `failures.json`，可由脚本重新产生。产品目前应定位为可审计记忆模块与继续工作界面，是否值得发展为独立平台取决于任务观察。

## 规模与性能

同一台 Windows 机器、Python 3.14、SQLite 文件、确定性非语义 embedding；每个规模 30 个问题，单进程。后测重用同一组持久化输入。前后不是隔离负载的严格性能实验，不作为跨机器 SLA。

| 事件数 | 单条写入 p50 / p95 | 查询修复前 p50 / p95 | 查询修复后 p50 / p95 |
|---|---|---|---|
| 1,000 | 19.2 / 23.1 ms | 539 / 567 ms | 35.6 / 48.0 ms |
| 10,000 | 20.5 / 23.8 ms | 4783 / 5727 ms | 522 / 583 ms |

一万条查询的中位时间约下降 89%。瓶颈证据与分阶段时间在 `performance/profile.txt`、`performance/summary.json`、`performance-batched/summary.json`。后测没有重跑写入；不声称写入提升。没有真实语义模型生成延迟、并发压力或用户恢复时间结果。

## 重跑

```text
python -m pytest -q
python eval/run_project_validation.py --output artifacts/my-structured-run
python audit_tools/replay_bundle.py --verify artifacts/r40-reader-replay.zip --report artifacts/my-replay.json
python eval/benchmark_application.py --output artifacts/my-performance
python -m build --wheel
python tools/verify_wheel.py --wheel-dir dist --output artifacts/my-wheel-check
```

输出目录需要新建，保留此前失败与测量。浏览器验证需要先用临时 DB 启动工作台，再安装 `npm ci`、`npx playwright install chromium`，执行 `node tools/verify_workbench.cjs artifacts/my-browser-check`。它会创建并修改专用合成项目。可用 `DIVE_BROWSER_EXECUTABLE` 指定本机浏览器。

CI 提供 Python 3.12/3.14、包安装与 PG 服务作业。**配置文件存在不等于远程 CI 已运行**。本机 Docker daemon 不可用，PG 检查未在本轮本机运行，不能把 skip 当通过。
