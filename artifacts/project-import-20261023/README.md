# 项目文件夹导入 · 本地验收（2026-10-23）

最初功能验收的最终浏览器结果见 [verified/summary.json](verified/summary.json)，桌面与窄屏截图保存在同一目录。较早的根目录/`final/`运行与3份重复合成输入已在R64逐文件校验后归档至本地`eval/reports/engineering-hygiene-20261023/pre-existing-import-evidence.zip`，保留追溯而不重复公开。新浏览器运行的输入使用本次专属临时目录。

完整工程结果见 [engineering/summary.json](engineering/summary.json) 与 [测试日志](engineering/tests.txt)：1439 通过、9 环境跳过，新增导入回归 22 项。交付层覆盖率 94.69% 行 / 90.12% 分支，三组原门槛通过。跳过项为 8 项未配置 PostgreSQL 的服务测试、1 项缺少 sentence-transformers 的语义模型测试，均不计为通过。

[原工作台浏览器回归](../project-import-regression-20261023/summary.json) 20 项通过，导入专用浏览器 5 项通过；本地浏览器均无页面异常、无外部请求。CI 已接入新增脚本，最初验收时尚未推送；后续工程改进与发布结果见[R64](../../docs/oracle/59-engineering-hygiene-20261023.md)。本目录的历史清单对应当时受测Python代码，随后界面、工程脚本与配置的完整绑定在R64重新验证。

没有新增模型请求，没有重测或改变 Benchmark 成绩。支持范围与使用方法见 [使用说明](../../docs/35-project-folder-import.md)。
