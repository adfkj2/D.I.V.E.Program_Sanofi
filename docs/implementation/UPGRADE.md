# 模型配置、人物评估与虚拟空间升级

实施日期：2026-10-30。运行入口仍为 `http://127.0.0.1:8765`。原核心和仓库历史保留；旧业务前后端不再作为新产品运行依赖。

## 需求与验证位置

| 范围 | 实现 | 核对证据 |
| --- | --- | --- |
| 八家供应商及自定义接口、四协议、凭据隔离、多连接与调用账本 | `backend/wot/connections.py` | `tests/test_connections.py`，97项离线契约、模型发现与取消测试 |
| 草稿导入、质量门槛、版本绑定与权限边界 | `people.py`、`assessments.py` | `tests/test_assessments.py`、`tests/test_upgrade_api.py` |
| 人物删除标记、旧自定义待评、头像重编码/同步/复原 | `people.py`、`catalog.py` | `tests/test_upgrade_api.py`，含重启与图片元数据校验 |
| 我、画像版本与授权本人陈述 | `people.py`、`service.py`、`frontend/src/Upgrade.tsx` | 个人页与版本比较，真实证据和Chosen分别保存 |
| 每轮40道新保留题、四条件、匿名判分、争议与抽样核验、严格许可 | `assessments.py` | 29项隔离测试，包含基线平手不通过、伪造分数、MOCK不发许可 |
| 私有保留答案、实际进度、失败/取消/继续/重启 | 私有评估库及任务记录 | 评估测试和API测试，普通状态及导出不含保留答案 |
| VS双人物、多会话、公平队列、两并发、慢聊和限额 | `virtual.py` | 20项后台运行测试，无浏览器依赖 |
| 接管窗口、三类干预、来源隔离、草稿与已校验版本分开 | `virtual.py`、`people.py` | 虚拟空间测试覆盖真实更正、Chosen与反馈隔离 |
| 证据删除传播、在途回复丢弃与旧模拟门禁 | `service.py`、`upgrade.py`、`main.py` | API回归、隐私竞态测试及虚拟空间失效测试 |
| v3升级前备份与恢复 | `storage.py`、`scripts/data.py` | API迁移测试、备份校验与新目录恢复测试 |
| 桌面/手机、动效减弱、旧链接、聊天草稿与失败重试 | `frontend/src` | `frontend/tests/upgrade-e2e.mjs`，8组流程、49张成功截图 |

## 数据边界

普通状态只暴露安全连接摘要，私有答案与匿名候选映射仅在当前评估详情和本人核验流程读取。它们没有记忆适配器入口。模型调用账本不保留提示词、结果或原始错误。

所有纯模拟发言都带 AI 标记。真实观点需本人明确确认后进入真实更正；理想方向需本人确认后成为Chosen。只有本人真实证据用于行为画像，Chosen在Skill中单列为理想方向。新草稿不改变已有许可指向的版本。

虚拟会话开始前显示20条/30,000总token的默认限额及费用未估算。已发送请求在暂停后可能产生费用，用量仍入账；其回复不会发布。服务退出/重启后会话暂停，需手动恢复。

## 开发验证边界

测试全部使用隔离数据和模拟供应商。合成测试不会向正式个人数据发放解锁许可。没有替用户执行真实连接测试、个人评估或付费会话；各家云服务的实际额度与账户可用性须由用户在配置界面主动测试。

旧模拟记录保留只读入口；即使后来解锁，旧快照也不能继续生成。新会话只能从当前已准入人物和本轮已校验分身中选择。

## 本轮最终检查

TypeScript与Vite构建通过，Ruff通过，wheel构建通过。完整本机回归303项通过（一个既有Starlette/httpx弃用提示），包括97项模型连接、29项评估、20项后台虚拟空间及9项隐私竞态测试。最终提交同时运行[仓库检查](https://github.com/adfkj2/D.I.V.E.Program_Sanofi/actions/workflows/quality.yml)。

浏览器报告为 PASSED：[报告](../../frontend/artifacts/upgrade-e2e/report.json)。8组流程、49张成功截图，八个实际页面与VS视觉夹具覆盖1440/768/390/360px，手机输入框保持可见；浏览器错误、外部请求、真实模型调用均为0。图中的测试用户和对话全部为隔离合成资料。VS与评估画面夹具只存在于浏览器路由，不向后端创建许可。

可直接查看[首次模型配置](../../frontend/artifacts/upgrade-e2e/first-run-model-1440.png)、[桌面聊天](../../frontend/artifacts/upgrade-e2e/chat-1440.png)、[手机个人页](../../frontend/artifacts/upgrade-e2e/me-390.png)、[VS设置预览](../../frontend/artifacts/upgrade-e2e/virtual-vs-setup-fixture-360.png)。后端真实门禁、调度、来源隔离和删除传播由独立测试证明。
