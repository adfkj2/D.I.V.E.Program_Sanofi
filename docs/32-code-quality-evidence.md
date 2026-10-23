# 代码质量：五项观察面与复核入口

更新于2026-10-22。目标是代码质量至少19/20；下列证据不等于mentor评分，本人理解仍须现场验证。历史约17/20的条件估计见[R53](oracle/51-handbook-score-review-20261017.md)，本页不按测试数量自动加分。

## 当前工程证据

2026-10-23发布补记：R63首轮远程4/5，浏览器检查提前导航导致标题隐藏超时；已用受控延迟复现并修复等待条件。修复提交`476ce1c`的[远程五项全部成功](../artifacts/bounded-tool-recovery-20261022/remote-checks.json)，PostgreSQL8通过、零跳过，浏览器20项通过；[失败及修复](oracle/58-bounded-tool-recovery-20261022.md)完整保留。应用源码和下列Python门禁绑定不变。

[R63记录](oracle/58-bounded-tool-recovery-20261022.md)说明可选阅读器的受限工具恢复、故障隔离和40个内容错误来源审计；0新模型请求，真实全量结果仍为R62的435正确/57错误/8未知，未达95%。最终完整本地门禁为**1417通过、9跳过、0失败**；类型检查覆盖36个文件，仍是局部范围且导入使用skip。[门禁与源码绑定](../artifacts/bounded-tool-recovery-20261022/quality/summary.json) · [复核说明](../artifacts/bounded-tool-recovery-20261022/README.md)。

| 范围 | 行覆盖 | 分支覆盖 | 门槛 |
| --- | ---: | ---: | --- |
| 交付层（Agent/application/Web/CLI/指标） | 1408/1486，94.75% | 396/434，91.24% | 90% / 80% |
| 记忆核心（service/store/temporal） | 1083/1177，92.01% | 319/396，80.56% | 90% / 80% |
| 共享阅读层、计算器与工具协议 | 1391/1516，91.75% | 429/508，84.45% | 90% / 80% |
| 全源码（含历史评测与可选后端） | 9698/12807，75.72% | 2560/3676，69.64% | 仅披露，不冒充关键路径覆盖 |

本地8项PostgreSQL因缺服务跳过，R62源码提交`e5e7bc2`的独立远程作业已补证：PostgreSQL8通过、零跳过，浏览器19项通过，五项任务全部成功，[精确提交的逐项记录](../artifacts/scoped-evidence-20261020/remote-checks.json)。普通环境1项真实语义模型测试因缺依赖跳过；R62另在GPU环境加载固定BGE-M3完成同40题配对写入/检索，40/40完成，1题形成与可见来源增加、0来源指标退步。脚本阅读替身不判分；这不是40题准确率。历史4项模型接受/拒绝行为检查与第三方警告仍保留。

R63最终wheel已重新构建并在全新环境安装，隔离导入确认来源为site-packages；持久化重启、CLI/API、资源、项目核查与交接导出通过。[安装记录与wheel哈希](../artifacts/bounded-tool-recovery-20261022/quality/installed-wheel.json)。首次非隔离构建因缺setuptools失败，标准隔离构建成功。完整src按现有Ruff规则检查；R61的21个历史异常边界及R63新增的两处来源隔离均保留局部理由，未关闭全局规则。格式检查仍仅覆盖声明范围；R63远程结果须发布后按精确提交核验。

## 手册五项观察面

| 观察面 | 满分 | 已落实的改进与复核入口 | 仍需验证 |
| --- | ---: | --- | --- |
| 机制真实性 | 6 | 事件/版本/来源/遗忘及事务恢复；[7臂同题机制对照](../eval/mechanisms/README.md)保留全部840项与80错误；[边界测试](../tests/test_application_boundaries.py)先写失败再恢复 | 完整系统与事件扫描均120/120，不能宣称自然语言优势；需独立材料与固定条件对照 |
| 架构合理性 | 5 | [共享reading层](../src/dive_memory/reading/reader.py)不依赖评测；[架构测试](../tests/test_architecture_boundaries.py)在新进程阻断Oracle后运行产品；[证据来源协议](../src/dive_memory/reading/adaptive.py)与[存储适配](../src/dive_memory/retrieval_session.py)分离 | 静态/运行时依赖检查不构成安全沙箱；核心存储职责较集中 |
| 工程质量 | 4 | [统一门禁](../tools/check_quality.py)、[集中配置](../pyproject.toml)、[CI](../.github/workflows/quality.yml)；完整src静态检查、三组行/分支门槛、36文件类型检查；模型用量不确定即停，按输入/输出分项预留费用；工具恢复受原调用预算约束 | 全仓严格类型及格式尚未覆盖；异常边界逐项说明，本地跳过单列，远程必须核对精确提交 |
| 文档质量 | 3 | 本页统一当前证据与分母；[迭代总账](oracle/ITERATION_LOG.md)保留失败、退步、预算；[提交包](28-submission-and-review.md)区分新生成/缓存、自评/官方及未知指标 | F.1.4认定与指定judge仍需项目组决定 |
| 理解深度 | 2 | [走读与现场练习](33-code-walkthrough.md)要求预测状态、解释来源/时间/事务/费用并实际小改动 | 需要作者本人完成，材料不能证明已掌握 |

## 复现与历史

按README安装开发依赖，在仓库根目录执行：

```text
python tools/check_quality.py --output artifacts/my-quality-run
python -m pytest tests/test_adaptive_reader.py tests/test_architecture_boundaries.py -q
```

输出必须是新目录。完整命令日志、JUnit、逐文件覆盖率、跳过原因、执行前后源码哈希都会保留；源码在检查中改变会失败。门禁清除继承的DIVE配置，默认不发付费模型请求。真实PostgreSQL在独立服务作业执行且强制零跳过。

历史报告保留原范围，不把旧统计覆盖成新成绩：[R45](oracle/43-code-quality-20261014.md)、[R47 Agent](oracle/45-project-agent-20261015.md)、[R51产品边界](oracle/49-product-acceptance-20261016.md)、[R52真实PG修复](oracle/50-ci-postgres-20261017.md)、[R54交付可信度](oracle/55-scoring-readiness-20261018.md)。

最新Oracle全量为R62的435/500（已确认87.0%，另8题未知）非官方开发自评；历史R40为451/500（90.2%）。40题开发筛选、工程通过率和覆盖率均不能作为500题95%验收结果；失败候选不替换默认策略。
