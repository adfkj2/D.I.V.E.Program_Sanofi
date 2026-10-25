# R66 · 来源编号协议的免费工程证据

新可选策略 `deliberative-refs` 让模型选择当前可见来源编号，程序提取原文与出处。旧默认策略保持；合法来源不代表语义支持，完整片段也可能挤占证据预算。尚未进行真实模型验证，不能称准确率提高。

- [完整门禁](quality/summary.json)：1671通过、10环境跳过、零失败；全src静态检查、声明格式范围与41文件类型检查通过。8项PostgreSQL、1项普通环境缺模型依赖、1项符号链接权限跳过，不计通过。
- [288文件清单](quality/source-manifest.json)：检查前后不变，含源码、测试、配置及开发数据。交付94.74%行/90.50%分支、核心92.01%/80.56%、共享阅读93.40%/87.43%；全包76.82%/71.19%，门槛未降。
- [独立安装](install/summary.json)：新环境的重启、CLI/API、资源与交接验证通过。[新策略替身验证](install/reference-smoke.json)确认安装包内三阶段与精确引文，只用脚本模型、零供应商调用。
- [模块未实现时的收集失败](development-checks/protocol-before.txt)、[测试字段错误](development-checks/protocol-after-v1.txt)和[修正后129项通过](development-checks/protocol-after-v2.txt)均保留。

本轮新增费用0，累计保守¥53.739524；历史未知请求预留保持，实际账单未知。新候选付费测试尚未授权，R65停止证据在[独立历史包](../deliberative-evidence-20261023/README.md)。最新全新500题仍为435正确、57错误、8未知，未达475确认正确。R66远程按推送后的精确SHA另核验。

原始证据在本地 eval/reports/reference-protocol-20261025/；公开文件仅做路径匿名化、LF、尾空白规范化及JSON重排，不含真实对话、答案、轨迹、密钥、数据库、wheel或环境。每份文件的原始与公开哈希见[清单](manifest.json)。[详细报告](../../docs/oracle/61-reference-selection-protocol-20261025.md) · [总账](../../docs/oracle/ITERATION_LOG.md)。

[独立只读审查](validation/independent-code-review.json)未见协议阻断问题；[新运行入口63项免费检查](development-checks/validation-tests-v3.txt)通过。[冻结提案](validation/validation-proposal.json)绑定288源码文件与374份历史证据，[三个阶段预检](validation/validation-preflight-summary.json)均拒绝未授权执行，没有付费输出。计划依次进行32手写挑战（≥31语义通过且全部拒答正确）、100题新生成/原judge（≥97确认正确及保守对照与拒答门槛）、条件500题新生成（≥475确认正确），上限¥1.50+¥7.25+¥29，累计最坏¥91.489524。执行/费用故障立即停止，不自动重跑。提案仍未授权，格式未知保留并不计正确；完整条件见[报告](../../docs/oracle/61-reference-selection-protocol-20261025.md)。
