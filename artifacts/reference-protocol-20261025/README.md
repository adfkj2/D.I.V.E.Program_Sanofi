# R66 · 来源编号协议的免费工程证据

新可选策略 `deliberative-refs` 让模型选择当前可见来源编号，程序提取原文与出处。旧默认策略保持；合法来源不代表语义支持，完整片段也可能挤占证据预算。已完成32条手写开发挑战的真实生成和逐项AI复核，Oracle候选尚无完整成绩，不能称Benchmark准确率提高。

- [完整门禁](quality/summary.json)：1671通过、10环境跳过、零失败；全src静态检查、声明格式范围与41文件类型检查通过。8项PostgreSQL、1项普通环境缺模型依赖、1项符号链接权限跳过，不计通过。
- [288文件清单](quality/source-manifest.json)：检查前后不变，含源码、测试、配置及开发数据。交付94.74%行/90.50%分支、核心92.01%/80.56%、共享阅读93.40%/87.43%；全包76.82%/71.19%，门槛未降。
- [独立安装](install/summary.json)：新环境的重启、CLI/API、资源与交接验证通过。[新策略替身验证](install/reference-smoke.json)确认安装包内三阶段与精确引文，只用脚本模型、零供应商调用。
- [模块未实现时的收集失败](development-checks/protocol-before.txt)、[测试字段错误](development-checks/protocol-after-v1.txt)和[修正后129项通过](development-checks/protocol-after-v2.txt)均保留。

工程准备阶段新增费用0，当时累计保守¥53.739524；历史未知预留保持。随后单独获准的挑战新增¥0.360072，已对账累计¥54.099596，实际账单未知；正在执行的100题费用另账，不在该快照内。R65停止证据保留在[独立历史包](../deliberative-evidence-20261023/README.md)。最新全新500题仍为435正确、57错误、8未知，未达475确认正确。R66源码提交b39afe9的[远程五项全部通过](remote-checks-source.json)，后续记录提交须独立核验。

原始证据在本地 eval/reports/reference-protocol-20261025/；公开文件仅做路径匿名化、LF、尾空白规范化及JSON重排，不含真实对话、答案、轨迹、密钥、数据库、wheel或环境。每份文件的原始与公开哈希见[清单](manifest.json)。[详细报告](../../docs/oracle/61-reference-selection-protocol-20261025.md) · [总账](../../docs/oracle/ITERATION_LOG.md)。

[独立只读审查](validation/independent-code-review.json)未见协议阻断问题；[新运行入口63项免费检查](development-checks/validation-tests-v3.txt)通过。[冻结提案](validation/validation-proposal.json)绑定288源码文件与374份历史证据，[三个阶段预检](validation/validation-preflight-summary.json)均拒绝未授权执行，没有付费输出。计划依次进行32手写挑战（≥31语义通过且全部拒答正确）、100题新生成/原judge（≥97确认正确及保守对照与拒答门槛）、条件500题新生成（≥475确认正确），上限¥1.50+¥7.25+¥29，累计最坏¥91.489524。执行/费用故障立即停止，不自动重跑。原提案保留approved=false，之后的[明确授权](validation/validation-authorization.json)另存；格式未知保留并不计正确；完整条件见[报告](../../docs/oracle/61-reference-selection-protocol-20261025.md)。

2026-10-26完成挑战复核：[32条生成汇总](validation/challenge-summary.json)、[逐条语义与原产物哈希](validation/challenge-semantic-review.json)、[独立复核](validation/challenge-independent-review.json)和[费用审计](validation/challenge-reconciliation.json)。两位AI审阅者均判32/32语义通过、4条必要拒答正确、零观察到的来源越界；96请求完整记账、零格式修复，38条最终引文可核对。全部挑战未搜索/读取，不证明复杂检索或全量准确率。[后台终态](validation/challenge-terminal.json)为COMPLETED/exit0。按已批准条件，100题候选全部新生成/原judge已启动，费用单列；500题仍未启动，严守原97条确认正确等晋级门槛。
