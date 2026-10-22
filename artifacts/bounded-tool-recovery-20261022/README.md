# R63 精选证据

本轮0新增模型请求/¥0。这里记录工具恢复修复、合成验证和R62的事后来源诊断；没有新准确率，R62的435正确/57错误/8未知保持原样。

- [完整工程结果](quality/summary.json)：1417通过、9环境跳过；全src静态检查及36文件类型检查通过。
- [运行源码哈希](quality/source-manifest.json)：门禁开始与结束一致，当前源码须按此清单核对。
- [独立安装验证](quality/installed-wheel.json)：持久化重启、CLI/API及交接通过，包含wheel哈希。
- [40个内容错误来源审计](content-audit.json)：20题标注全文可见、12题来源可见但全文部分缺失、4题已形成来源最终不可见、1题未形成、3题不可答误答。全文可见不等于语义充分。
- [离线审计脚本](audit_content.py)：只读历史轨迹和事后标注，不发请求；需要本地原始材料，不能只靠公开包重新运行。
- [修复前失败](failures-and-repairs/contracts-before.txt)、[首次修复](failures-and-repairs/contracts-after-v1.txt)、[扩充边界后的80项回归](failures-and-repairs/contracts-after-v2.txt)。
- [首次构建环境失败](failures-and-repairs/wheel-build.txt)：改用标准隔离构建通过，未降低检查要求。
- [详细报告](../../docs/oracle/58-bounded-tool-recovery-20261022.md)与[文件清单](manifest.json)。

完整轨迹、参考答案、私有检查材料、环境、数据库和wheel仅留忽略目录`eval/reports/bounded-tool-recovery-20261022/`。公开日志只统一UTF-8/LF、去尾空白及替换本机路径，原始/公开哈希和转换说明分别保留。远程结果发布后按提交SHA核验，不能把本地PostgreSQL跳过算成通过。
