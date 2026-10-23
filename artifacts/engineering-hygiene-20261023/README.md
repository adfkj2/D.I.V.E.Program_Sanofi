# R64 精选工程证据

本轮0模型请求/¥0，未重测Benchmark。当前材料验证工程修复及文件清理，不能折算为95%准确率或评委19/20。

- [完整门禁](quality/summary.json)：1487通过、10环境跳过、0失败；[270文件绑定](quality/source-manifest.json)覆盖Python、前端、浏览器脚本、依赖锁与部署配置。
- [远程五项成功](remote-checks.json)：绑定源码提交`0d0cbdf`，PostgreSQL实际8通过且零跳过、浏览器27项通过，保留步骤结论和日志摘录。
- [独立安装](quality/installed-wheel.json)：持久化重启、CLI/API、资源和交接通过，包含wheel哈希。
- [原工作台20项](browser-workbench/summary.json)、[导入7项](browser-import/summary.json)与[格式检查](quality/browser-format.txt)：真实本地API、零模型调用，保留原有断言，新增大目录和迟到保存回归。
- [清理摘要](cleanup-summary.json)：首轮70目录/7887文件，补充读取核验后清理剩余16目录，并归档移除旧pytest临时目录与覆盖文件，共87个遗留目录；完整验证后再清4个重建目录。旧测试数据逐文件归档校验，原始分阶段结果保留。
- [修复前与过程](failures-and-repairs/)：源码绑定、清单解析、应用参数、浏览器时序及清理保护的失败均保留。浏览器首次缺依赖、后续测试同步准备错误也单列，不混作产品缺陷。
- [文件清单](manifest.json)分别记录公开与原始哈希及转换；[报告](../../docs/oracle/59-engineering-hygiene-20261023.md)包含完整范围和限制。

本地10项跳过为8项PostgreSQL无服务、1项缺语义模型依赖、1项Windows符号链接权限不足；均不计通过，远程结果按推送SHA单独核验。原始运行、压缩归档、数据库、环境、安装包和3份用户草稿保留在本地，不随精选材料发布。日志只做UTF-8/LF、去尾空白及本机路径替换。
