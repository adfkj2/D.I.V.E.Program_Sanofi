# 代码质量：五项观察面与复核入口

更新于2026-10-29。目标是代码质量至少19/20；下列证据不等于mentor评分，本人理解仍须现场验证。历史约17/20的条件估计见[R53](oracle/51-handbook-score-review-20261017.md)，本页不按测试数量自动加分。

## 当前工程证据

R71新增可选复核确认协议，通过`resolve_review`注入宿主草稿保留与复核前后来源准入，不改旧默认。最新完整门禁**2172通过、27环境跳过、0失败**；319输入前后不变，全src静态、104文件声明格式、46文件类型及三组覆盖门槛通过。新wheel独立安装、重启/CLI/API/资源/交接通过，独立协议38项含真实SQLite正常访问及两时点硬忘却拒绝；安装后6场景39项、140文件逐字绑定通过；新提交远程检查随后补证。[本轮报告](oracle/66-review-receipt-20261029.md) · [聚合与哈希](../artifacts/review-receipt-20261029/README.md)。工程通过不证明真实答案正确或19/20外部评分。

| 当前范围 | 行覆盖 | 分支覆盖 | 门槛 |
| --- | ---: | ---: | --- |
| 交付层（Agent/application/Web/CLI/指标） | 1604/1692，94.80% | 471/520，90.58% | 90% / 80% |
| 记忆核心（service/store/temporal） | 1136/1224，92.81% | 346/420，82.38% | 90% / 80% |
| 共享阅读层、计算器与工具协议 | 2370/2497，94.91% | 795/898，88.53% | 90% / 80% |
| 全源码（含历史评测与可选后端） | 11000/14149，77.74% | 3055/4208，72.60% | 仅披露，不冒充关键路径覆盖 |

27项本地跳过为25PG、1可选模型依赖、1符号链接权限。前一提交R70的精确`9e16dde`五项远程任务成功、PG25项零跳过；R71须核对自身SHA，不能借前次绿灯。Windows原生任务/管道另有本机实际验证，Linux跳过不当作已执行。以下保留R67及更早证据的原范围与失败历史。

历史R63发布补记：首轮远程4/5，浏览器检查提前导航导致标题隐藏超时；受控延迟复现后修复提交`476ce1c`的[远程五项全部成功](../artifacts/bounded-tool-recovery-20261022/remote-checks.json)，PostgreSQL8通过、零跳过，浏览器20项通过。[失败及修复](oracle/58-bounded-tool-recovery-20261022.md)完整保留，当前R66门禁见下方。

[R67记录](oracle/62-staged-completion-budget-20261026.md)将阶段模式与预算独立注入，持久化终态与总用量，并把阶段参数纳入续跑身份。最新完整本地门禁**1795通过、10环境跳过、0失败**，42文件类型检查，294文件绑定Python、前端、浏览器、锁文件、迁移、部署配置及开发数据。最终新工具夹具审查修正前后均保留完整门禁，最终清单检查前后不变。类型检查仍为局部范围且使用skip导入。真实全量仍为R62的435正确/57错误/8未知；工程验证不能替代95%验收。[门禁](../artifacts/staged-completion-20261026/quality/summary.json) · [精选证据](../artifacts/staged-completion-20261026/README.md)。

| 历史R67范围 | 行覆盖 | 分支覆盖 | 门槛 |
| --- | ---: | ---: | --- |
| 交付层（Agent/application/Web/CLI/指标） | 1596/1684，94.77% | 471/520，90.58% | 90% / 80% |
| 记忆核心（service/store/temporal） | 1083/1177，92.01% | 319/396，80.56% | 90% / 80% |
| 共享阅读层、计算器与工具协议 | 1967/2073，94.89% | 630/710，88.73% | 90% / 80% |
| 全源码（含历史评测与可选后端） | 10469/13567，77.17% | 2840/3966，71.61% | 仅披露，不冒充关键路径覆盖 |

本地8项PostgreSQL因缺服务跳过，R62源码提交`e5e7bc2`的独立远程作业已补证：PostgreSQL8通过、零跳过，浏览器19项通过，五项任务全部成功，[精确提交的逐项记录](../artifacts/scoped-evidence-20261020/remote-checks.json)。普通环境1项真实语义模型测试因缺依赖跳过；R62另在GPU环境加载固定BGE-M3完成同40题配对写入/检索，40/40完成，1题形成与可见来源增加、0来源指标退步。脚本阅读替身不判分；这不是40题准确率。历史4项模型接受/拒绝行为检查与第三方警告仍保留。

R67 wheel已重新构建并在全新环境安装，隔离导入确认来源为site-packages；持久化重启、CLI/API、资源、项目核查与交接导出通过。136个打包文件与当前源码逐项一致；安装包内另以假HTTP验证实际搜证、截断、一次恢复和最终复核，零真实模型调用。[安装记录与wheel哈希](../artifacts/staged-completion-20261026/install/summary.json)。R63历史首次非隔离构建因缺setuptools失败的记录保留。完整src按现有Ruff规则检查；R61的21个历史异常边界及R63新增的两处来源隔离均保留局部理由，未关闭全局规则。格式检查仍仅覆盖声明范围；R66远程结果须发布后按精确提交核验。

R64另外保留1项本机无符号链接权限的测试跳过，本地浏览器27项、独立安装通过。源码提交`0d0cbdf`的[远程五项全部成功](../artifacts/engineering-hygiene-20261023/remote-checks.json)，PG实际8项零跳过、Linux浏览器27项。清理初期16个目录补充只读核验后按同一归档流程完成清理；共移出87个遗留目录，用户数据与原始评测保持。[维护范围](36-workspace-maintenance.md)。

R65记录提交`feee617b256ba2c0f24542a08673fbe73c6e906a`的[五项远程检查均成功](../artifacts/deliberative-evidence-20261023/remote-checks-retry-record.json)，PostgreSQL8项零跳过、浏览器20+7项通过；不能据此称R66最新改动已远程验证。R65后台重试因引用协议失败停止，候选无完整准确率；新来源编号协议只能降低抄写错误风险，语义支持仍须模型判断和真实评测。

R66源码提交`b39afe9ad09c2c55d461733e28fe8597dc42e4a2`已另外完成[五项远程检查](../artifacts/reference-protocol-20261025/remote-checks-source.json)，PostgreSQL实际8项零跳过、浏览器20+7项、Python3.12/3.14及Docker均成功。后续真实开发挑战只属于32条手写阅读场景，不是工程分数或全量Benchmark准确率。

## 手册五项观察面

| 观察面 | 满分 | 已落实的改进与复核入口 | 仍需验证 |
| --- | ---: | --- | --- |
| 机制真实性 | 6 | 事件/版本/来源/遗忘及事务恢复；[7臂同题机制对照](../eval/mechanisms/README.md)保留全部840项与80错误；[边界测试](../tests/test_application_boundaries.py)先写失败再恢复 | 完整系统与事件扫描均120/120，不能宣称自然语言优势；需独立材料与固定条件对照 |
| 架构合理性 | 5 | [共享reading层](../src/dive_memory/reading/reader.py)不依赖评测；[架构测试](../tests/test_architecture_boundaries.py)在新进程阻断Oracle后运行产品；[证据来源协议](../src/dive_memory/reading/adaptive.py)与[存储适配](../src/dive_memory/retrieval_session.py)分离 | 静态/运行时依赖检查不构成安全沙箱；核心存储职责较集中 |
| 工程质量 | 4 | [统一门禁](../tools/check_quality.py)、[集中配置](../pyproject.toml)、[CI](../.github/workflows/quality.yml)；完整src静态检查、三组行/分支门槛、42文件类型检查；模型用量不确定即停，按输入/输出分项预留费用；工具恢复受原调用预算约束 | 全仓严格类型及格式尚未覆盖；异常边界逐项说明，本地跳过单列，远程必须核对精确提交 |
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

R67源码提交`4c40eb19ca06b83639205236c6e787403eb58044`的五项远程任务均已完成并成功：PostgreSQL8项零跳过、浏览器20+7项、两版Python及Docker通过。[运行记录](https://github.com/adfkj2/D.I.V.E.Program_Sanofi/actions/runs/40511425763)。后续记录提交单独核验；工程通过不证明95%准确率。
