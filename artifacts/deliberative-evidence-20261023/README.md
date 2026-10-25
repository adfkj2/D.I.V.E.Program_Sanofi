# R65 · 证据阅读重构的精选证据

2026-10-23开始，2026-10-24完成本地工程核验，2026-10-25补齐停止与续测记录。新策略尚无完整Oracle准确率，没有推广为默认。联网前工程阶段为0请求/¥0，当时累计¥49.880440；之后完成32条开发挑战及100题对照，候选首批中断。一次获准后台重试已按协议停止，最终累计保守 **¥53.739524**，含历史未知请求全额预留。21条输出中20有效、1无效，尚未判分，候选准确率未知。原提案始终保留创建时approved=false，后续授权独立绑定，不覆盖历史状态。

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

真实挑战已完成：[32条运行汇总](validation/challenge-summary.json)、[逐项语义复核及原始产物哈希](validation/challenge-semantic-review.json)、[费用对账](validation/challenge-reconciliation.json)。32/32通过开发者侧AI语义复核，4道应拒答正确，零观察到的来源越界；独立子代理只复核前20条。97次调用、¥0.388132，累计¥50.268572；一次plan精确引用失败按预定上限修复。此成绩只属于手写阅读场景，不更新Oracle分数；随后100题同期对照按下述条件停止。

100题同期对照已按停止条件终止：[停止摘要](validation/control-stopped-summary.json)、[费用](validation/stopped-cost-reconciliation.json)、[判分格式诊断](validation/judge-contract-audit.json)。100题全部生成；判到第14题时两次嵌套output.verdict均不符合原顶层解析协议，保留未知并停止。当前11正确/2错误/1未知/86未判，不能报告100题准确率；新候选与500题均未启动。本轮真实调用共258次，峰值费用¥2.478912，累计¥52.359352。judge、金标和旧分数均不改；任何改变停止条件的续测须新授权，预算余额不自动构成许可。

上述为首次停止时的快照。[随后批准的续测](validation/continuation-authorization.json)只补未尝试86条，完整对照成为[91正确、8错误、1未知](validation/control-completed-summary.json)。原未知不计正确、不缩分母，accuracy保持null。新候选生成4题后进程消失，第13条请求用量未知；[独立中断对账](validation/continuation-stopped-cost-reconciliation.json)保留98完成/1未知的99次续测请求，新增保守¥0.340302、累计¥52.699654。原请求不覆盖，没有把4题当100题成绩。

[跨会话后台实测](validation/background-cross-turn-verification.json)确认180秒任务跨助手轮次持续并正常完成；[短期正常/故障测试](validation/background-runner-feasibility.json)同时保留。它们不证明主机重启恢复。独立重试入口[38项免费检查通过](development-checks/retry-tests-v5.txt)，[字段更名后的旧断言失败](development-checks/retry-tests-v4.txt)保留；运行前两阶段[免费预检](validation/retry-preflight-summary.json)正确拒绝未授权执行。

[一次后台重试提案](validation/retry-proposal.json)与[明确授权](validation/retry-authorization.json)分别保留。100题从头生成，包括旧4条和中断题，旧产物及未知费用继续保留；已完成对照不重判。仍以≥97确认正确、保守对照、6道不可答和零执行失败为全量门槛；通过才做500条全新生成并要求≥475确认正确。新增上限¥36.25、累计最坏¥88.949654，总预算不增加。随后后台按协议停止，未形成完整候选结果，终态与最终对账见下文。

后台重试已结束：[终态](validation/retry-terminal.json)与[候选摘要](validation/retry-stopped-summary.json)记录21条输出、20有效/1无效、79未尝试、0判分，准确率未知。[费用对账](validation/retry-stopped-cost-reconciliation.json)记录64请求全部完成、新增¥1.039870、累计¥53.739524，500题未启动，追加付费授权关闭。R65总计421请求、420完成、1用量未知，新增保守¥3.859084；不把有效输出当作正确答案。

[计划失败逐项审计](diagnosis/retry-plan-failure-audit.json)确认首轮12条编号错配，修正后1条大小写不一致；只读比较与哈希保留，不放宽校验、不改分。[对照8错聚合](diagnosis/control-error-audit.json)用于定位信息丢失、限定词和必要前提等问题，不是人工重判。记录提交`feee617b256ba2c0f24542a08673fbe73c6e906a`的[五项远程检查全部通过](remote-checks-retry-record.json)，其工程结论不等于真实准确率。新协议的免费工程证据独立放在[R66包](../reference-protocol-20261025/README.md)。
