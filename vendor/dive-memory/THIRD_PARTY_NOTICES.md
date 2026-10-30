# 第三方来源与许可边界

本文件记录依赖与公共资料来源，不代替各上游许可证。仓库自身尚未声明总许可证，本轮没有替所有者选择开源许可证或推定其拥有品牌授权。

| 组件 / 数据 | 来源 | 用途 |
|---|---|---|
| LongMemEval / Oracle | https://github.com/xiaowu0162/LongMemEval ，论文 arXiv:2410.10813 | 公开开发基准与可移植回放请求；仓库契约记为 MIT，保留上游许可文本 |
| BGE-M3 | https://huggingface.co/BAAI/bge-m3 | 可选真实语义编码器；固定 revision `5617a9f61b028005a4858fdac845db406aefb181`，权重未随应用打包 |
| Sentence Transformers / Transformers / PyTorch | 各项目公开发行包 | 可选模型运行，版本与设备记录在 R40 冻结环境 |
| FastAPI / Starlette / Pydantic / Uvicorn | Python 官方包索引与上游项目 | 本地 API、输入校验与服务；版本固定在 requirements/runtime.lock |
| pytest / Ruff / mypy / build | Python 官方包索引与上游项目 | 测试、风格、类型与 wheel 构建 |
| Playwright / Prettier | npm 官方发行包 | 浏览器操作验证与静态资源格式检查，仅开发依赖 |
| PostgreSQL / pgvector / psycopg | 各项目公开发行包 | 可选后端及服务契约测试；不包含在默认本地运行需求中 |

数据原文件 SHA-256：`821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c`。Oracle JSON 本地大小约 15.4 MB，不随 wheel 分发；取得数据后必须校验哈希。BGE-M3 模型会占数 GB，具体文件清单和体积应以固定 revision 的实际下载为准，不将估计值当已核验大小。

`artifacts/r40-reader-replay.zip` 含公开基准的可见片段及生成响应，属于评测证据，不是运行时知识库。合成业务故事由本项目为机制验证构造，没有使用私人聊天、患者或企业敏感资料。

DeepSeek 是外部在线模型服务，不是可再分发模型权重。模型显示名称缺少不可变服务端版本号，报告保留 `revision=null` 的复现限制。调用前须核对供应商条款与当前价格；API 密钥不应出现在仓库。

该项目使用 AI 辅助代码、测试与文档工作。提交者应能解释核心实现、披露开发过程，并遵守 DIVE 项目组关于原创、署名和品牌使用的最终规范。
