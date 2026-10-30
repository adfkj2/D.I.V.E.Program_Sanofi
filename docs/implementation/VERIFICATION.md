# 实际验证记录

## 最新：微信式聊天与15位人物改版

2026-10-30，本轮最终独立环境输出：**142 passed, 1 warning in 31.47s**；Ruff（backend/tests/scripts及浏览器测试服务器）All checks passed；TypeScript＋Vite正式构建通过（33模块）；pip check无损坏依赖。一个警告仍是Starlette TestClient的httpx弃用提示，没有失败或跳过。

新增验证包括32项人物目录来源/许可/正文隔离测试、15项IM后端/升级/失败恢复测试、历史已删标题修复和15张头像资产检查。浏览器首次发现的SQLite读并发问题已修复并重跑完整流程，不使用旧通过记录代替新验证。

新版浏览器 **9组验收全部通过**，六视图×1440/768/390/360px无横向溢出，27张成功截图，未捕获异常0，外部网络请求0，外部聊天模型调用0。报告为frontend/artifacts/im-e2e/report.json，早期失败保存在其attempts/001-read-race。该测试使用独立mock目录与确定性失败一次的供应商，实际验证已存USER后的失败重试、刷新恢复和原key一致；未写入正式.local。

已实际构建Python wheel并打开验证：15份人物数据、15份完整MIT notice、最新SQLite串行读取代码均在包内。15张插画头像使用内置image_gen独立生成，精确提示词见AVATAR_ASSETS.json；仅做等比缩放，总1,951,469字节，原图保留。图像生成与“外部聊天模型0调用”分别记录，未声称图像工具免费。

实现决策、接口与迁移边界见IM_REDESIGN.md，人物来源核查范围见PERSONA_CATALOG.md。以下为同日初版的历史验证记录，93项旧结果不代表新版测试总数。

---

2026-10-30，Windows 本机，Python 3.14.0 / Chrome。当前交付为可运行本地研究预览，不宣称实现全部目标规范或通过真实人格验证。

## 新产品环境

已在 WeOnceTalked/.venv 从零创建独立环境，安装本地 pinned core 与 WOT，不依赖旧项目环境。后端依赖固定在 requirements.lock.txt，前端固定于 package-lock.json。`pip check`：No broken requirements found。启动脚本通过 PowerShell 语法检查，并用于最终本机运行。

## 后端

```text
.venv/Scripts/python.exe -m pytest -q
93 passed, 1 warning in 28.82s
.venv/Scripts/python.exe -m ruff check backend tests scripts
All checks passed!
```

93 项组成：adapter 21、产品纵向/反例 33、HTTP/并发 11、幂等恢复/权限 11、评测 15、备份恢复 2。无失败、无跳过。一项依赖警告：Starlette TestClient 提示 httpx 兼容方式将弃用，不是运行断言失败。

覆盖可信来源、第三方/模拟不能入真实、愿望与现实分开、目标本人批准/撤回、不可变内容寻址快照、独立角色调用、私人资料用途与公开批准、模拟反馈不回灌、暂停在途请求、用量/回合停止、删除重启不复活、核心故障后的 Outbox 恢复、操作完成但回执失败后的幂等恢复、跨站/DNS rebinding/非本机连接、静态 Skill、两库备份与恢复哈希。

修复了测试真实暴露的更正 namespace 不可检索、删除后旧编译操作碰撞、动作提交后回执缺失导致重复、私人分句转述需公开批准、暂停控制与生成锁竞争等问题。没有删除断言或跳过反例来获得通过。

## 旧核心兼容

原仓库指定基线 210 passed；实际 vendored core 运行 87 项上游记忆/主体/检索/冲突矩阵测试，87 passed。具体命令、首次环境失败及修复记录见 BASELINE_AUDIT.md。没有将上游结果算入新产品 93 项。

## 前端与端到端

`npm run build` 包含 TypeScript 检查和 Vite production build，通过。采用 Vite runner config loader，避免当前受限环境扫描祖先目录失败；未隐藏依赖或类型错误。

浏览器实际运行 `frontend/tests/e2e.mjs`，11 项核心场景通过：冷启动、真聊与记忆授权、三轨镜像、来源更正、Chosen 两步确认、逐轮双角色与公开批准/暂停恢复/反馈、批准现实行动/自报、带披露导出、删除取消与确认/重载不复活、浏览器和网络边界。

六视图 × 1440/768/360px：18 个布局检查无横向溢出，另存空首屏，共 19 张成功截图。已目视检查桌面镜像与手机模拟等布局。浏览器未捕获异常 0，外部网络请求 0，外部模型调用 0。

- 报告：frontend/artifacts/e2e/report.json
- 桌面：frontend/artifacts/e2e/mirror-1440.png
- 手机：frontend/artifacts/e2e/simulation-360.png
- 早期失败留痕：frontend/artifacts/e2e/attempts/（原测试选择器/等待错误，修正后完整重跑通过）

复跑必须对独立、空、mock 数据目录启动服务，并显式设置 WOT_E2E_ALLOW_WRITE=1。脚本拒绝非本机、非空库或 live 模型。正式 .local 没有写入合成用户资料；合成库位于 artifacts/e2e-data*。

## 评测

```text
.venv/Scripts/python.exe -m wot.evaluation --output artifacts/evaluation/synthetic-report.json
Synthetic framework validation: 8/8 paired cases; external calls 0; cost CNY 0.00
Persona fidelity: unvalidated. No DOMAIN_TESTED status can be awarded.
```

这只是四基线、隔离/冻结与配对统计框架验收。没有真实人类盲测、外部付费联调、真实 persona fidelity 增益、长期成长因果收益或 DOMAIN_TESTED 认证。

## 边界

API 实际调用/费用：0 次 / 0 元。没有推送、公开部署或代发消息。旧目录的系统移动限制与数据保留见 CLEANUP.md；旧 Git 状态与真实数据库已核对未损坏。
