# 代码质量：五项观察面与复核入口

更新于2026-10-20。目标是代码质量至少19/20；下列证据不等于mentor评分，本人理解仍须现场验证。历史约17/20的条件估计见[R53](oracle/51-handbook-score-review-20261017.md)，本页不按测试数量自动加分。

## 当前工程证据

[R55–R61记录](oracle/56-shared-reading-and-active-evidence-20261019.md)说明共享阅读层解耦、受限主动查证、全源码静态检查、先失败后修复及真实模型筛选。最终完整本地门禁为**1359通过、9跳过、0失败**；类型检查覆盖34个文件，仍是局部范围且导入使用skip。[门禁与源码绑定](../artifacts/closure-progress-20261019/quality/summary.json) · [复核说明](../artifacts/closure-progress-20261019/README.md)。

| 范围 | 行覆盖 | 分支覆盖 | 门槛 |
| --- | ---: | ---: | --- |
| 交付层（Agent/application/Web/CLI/指标） | 1407/1485，94.75% | 396/434，91.24% | 90% / 80% |
| 记忆核心（service/store/temporal） | 1083/1177，92.01% | 319/396，80.56% | 90% / 80% |
| 共享阅读层与主动查证适配 | 1065/1154，92.29% | 259/306，84.64% | 90% / 80% |
| 全源码（含历史评测与可选后端） | 9576/12682，75.51% | 2513/3626，69.31% | 仅披露，不冒充关键路径覆盖 |

本地8项PostgreSQL因缺服务跳过，须由本次提交的远程作业补证；历史提交ec87f01的5/5成功不能代替新提交结果。普通环境1项真实语义模型测试因缺依赖跳过，另在GPU环境实际离线加载固定BGE-M3，4项接受/拒绝行为全部通过。两条第三方警告保留。

R61最终wheel已重新构建并在全新环境安装，隔离导入确认来源为site-packages；持久化重启、CLI/API、资源、项目核查与交接导出通过。[安装记录与wheel哈希](../artifacts/closure-progress-20261019/quality/installed-wheel.json)。完整src按现有Ruff规则检查；消除111项历史诊断，其中21个既有宽泛异常捕获保留具体理由与局部豁免，未关闭全局规则。格式检查仍仅覆盖声明范围。

## 手册五项观察面

| 观察面 | 满分 | 已落实的改进与复核入口 | 仍需验证 |
| --- | ---: | --- | --- |
| 机制真实性 | 6 | 事件/版本/来源/遗忘及事务恢复；[7臂同题机制对照](../eval/mechanisms/README.md)保留全部840项与80错误；[边界测试](../tests/test_application_boundaries.py)先写失败再恢复 | 完整系统与事件扫描均120/120，不能宣称自然语言优势；需独立材料与固定条件对照 |
| 架构合理性 | 5 | [共享reading层](../src/dive_memory/reading/reader.py)不依赖评测；[架构测试](../tests/test_architecture_boundaries.py)在新进程阻断Oracle后运行产品；[证据来源协议](../src/dive_memory/reading/adaptive.py)与[存储适配](../src/dive_memory/retrieval_session.py)分离 | 静态/运行时依赖检查不构成安全沙箱；核心存储职责较集中 |
| 工程质量 | 4 | [统一门禁](../tools/check_quality.py)、[集中配置](../pyproject.toml)、[CI](../.github/workflows/quality.yml)；完整src静态检查、三组行/分支门槛、34文件类型检查；模型用量不确定即停，按输入/输出分项预留费用；引用按同一片段及来源校验 | 全仓严格类型及格式尚未覆盖；21处既有异常边界局部豁免，本地跳过单列，远程必须核对精确提交 |
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

Oracle全量仍为R40的451/500（90.2%）非官方开发自评。40题开发筛选、工程通过率和覆盖率均不能作为500题95%验收结果；失败候选不替换默认策略。
