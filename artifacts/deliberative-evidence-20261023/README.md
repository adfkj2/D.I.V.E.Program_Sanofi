# R65 · 证据阅读重构的精选证据

2026-10-23开始，2026-10-24完成本轮本地工程核验。新策略尚未真实测得答题准确率，没有推广为默认。本轮新增模型请求 **0**、费用 **¥0**，共享保守累计 **¥49.880440**。以上费用属于联网前工程阶段。2026-10-24用户已批准一套冻结候选的条件验证，最多新增¥42.50、累计上限¥100；[原始冻结提案](validation/proposal.json)保留创建时未授权状态，另有[后续明确授权](validation/authorization.json)。真实模型结果单独记录，当前尚无新Benchmark准确率。

- [完整质量汇总](quality/summary.json)：**1646通过 / 10环境跳过 / 0失败**；全src静态检查、声明范围格式及40文件类型检查通过。10个跳过为8项PostgreSQL服务、1项普通环境缺模型依赖、1项Windows符号链接权限，不计通过。
- [源码与数据清单](quality/source-manifest.json)：286文件，检查前后哈希一致；新增开发数据绑定，仍排除外部数据及原始评测输出。三组90%行/80%分支门槛未降低。
- [独立安装](install/summary.json)：全新环境的持久化重启、CLI/API、项目核查与交接验证通过。新阅读工厂使用脚本模型替身验证；[首次检查脚本字段错误](install/deliberative-smoke.txt)及[修正后成功](install/deliberative-smoke-v2.txt)均保留，不能当作模型质量实测。
- [历史失败聚合](diagnosis/audit-reader-failures-v2.json)与[架构聚合](diagnosis/audit-architecture.json)：来自R62保存轨迹的免费只读审计。R62仍为435正确、57错误、8未知；来源可见及字面冗余不等于语义充分或可修正分数。
- [公开研究核验](research/frontier-review.md)：固定版本来源与协议限制。前沿自报与本项目输入、模型或judge不同，不作同协议排名。
- `development-checks/`：保留引用索引、来源公平性、重复短句语境、跑手中止汇总及开发数据哈希遗漏的红测和后续验证。并非只保留成功日志。challenge-runner-after-v1的49项是该阶段结果，后续新增测试已进入完整1646项门禁。

共享阅读层覆盖率为93.14%行/86.97%分支，交付层94.73%/90.50%，核心92.01%/80.56%；全包76.70%/71.05%分别披露。这里没有按工程测试数折算评分或Benchmark准确率。源码提交`3ceefc8`的五项远程检查已全部成功，PG8项零跳过、浏览器27项通过；[精确提交证据](remote-checks-source.json)。后续记录提交仍需按自身SHA核验。

原始证据继续保存在本地 `eval/reports/accuracy-redesign-20261023/`，没有覆盖。公开包不含原问答、参考答案、模型回复全文、逐题轨迹、数据库、环境或wheel；历史聚合诊断保留原文件SHA256。文本只做路径匿名化、LF和尾空白规范化；JSON另做统一序列化。每份公开文件（manifest自身除外）在[哈希清单](manifest.json)列出原始/公开SHA256与转换；README是新撰说明，没有对应的历史原件。

[完整分析与实现](../../docs/oracle/60-deliberative-evidence-20261023.md) · [迭代总账](../../docs/oracle/ITERATION_LOG.md) · [32条手写开发夹具](../../eval/datasets/reader_challenge_v1/cases.json)

[固定100题清单](validation/pilot-selection.json)按题型与不可答标记分层哈希抽样，不按旧成绩挑题。真实验证驱动19项免费测试通过，原2项失败与修复日志已附；方案各阶段失败即停，不能把授权解释为无限次试跑。

真实挑战已完成：[32条运行汇总](validation/challenge-summary.json)、[逐项语义复核及原始产物哈希](validation/challenge-semantic-review.json)、[费用对账](validation/challenge-reconciliation.json)。32/32通过开发者侧AI语义复核，4道应拒答正确，零观察到的来源越界；独立子代理只复核前20条。97次调用、¥0.388132，累计¥50.268572；一次plan精确引用失败按预定上限修复。此成绩只属于手写阅读场景，不更新Oracle分数；100题同期对照已开始。
