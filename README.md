# 我们曾聊过 · We Once Talked

本机运行的聊天与个人数字分身应用。微信式蓝白聊天界面，内置芒格、巴菲特、张雪峰等十五位解释性 AI 人物。保留 D.I.V.E 的固定版本长期记忆内核，前后端及产品领域独立重建。

侧栏为「聊天」「我」「设置」。完成真实的严格个人校验并确认报告后，才会显示「虚拟空间」。没有配置模型时，可以浏览资料并使用明确标记的离线聊天演示。

## 启动

Windows 需要 Python 3.11+、Node.js 20.19+/22.12+：

```powershell
.\scripts\start.ps1 -Install
```

之后双击 `start.cmd`，或执行 `.\scripts\start.ps1`。页面位于 <http://127.0.0.1:8765>，仅支持本机单进程。服务停止后，运行中的虚拟会话会暂停，重启不会自动恢复付费生成。

手工安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements.lock.txt -e './vendor/dive-memory' -e '.[dev]'
Push-Location frontend
npm.cmd ci
npm.cmd run build
Pop-Location
.\.venv\Scripts\python.exe -m wot.main
```

## 模型配置

首次打开会出现配置窗口，也可稍后进入「设置」。支持 OpenAI、Claude、Gemini、DeepSeek、通义千问、智谱、Kimi、豆包及自定义 OpenAI 兼容接口；提供四种原生协议、多连接、默认连接、模型列表及手动模型名称。

API Key 仅提交给本机后端。勾选「记住 Key」时使用操作系统凭据库；系统安全存储不可用时，可选择仅本次运行。数据库保存凭据引用，浏览器不永久保存 Key。连接测试由用户主动触发，保存配置不自动调用模型。

实际推理会将当前功能所需的资料发送给所选供应商。调用账本只保留用途、状态、模型和用量，不记录提示词、答案或密钥。费用无可靠资料时显示「费用未估算」。

## 人物、记忆与严格校验

- 十五位内置人物的本地插画、固定来源版本和 MIT 声明见[人物目录](docs/implementation/PERSONA_CATALOG.md)。它们属于内置角色，不伪造质量评估成绩。
- 可以换头像、恢复头像、移除或从内置目录重新添加。删除标记会保留；旧聊天继续可读。
- 导入 Markdown、JSON、纯文本或粘贴 Skill 后先成为草稿。人物资料质量评估五项等权，综合至少80、每项至少70并通过关键权限检查后，才能明确点击添加。
- 真实记忆仅来自本人主动授权的聊天、自报和确认更正。自我陈述、行为线索与理想方向分开；模拟台词和只改本轮的反馈不进入真实画像。
- 个人严格校验每轮准备40道新的保留情境题，冻结本人答案，比较无画像、静态简介、普通摘要/RAG与用户 Skill。匿名辅助判分后，本人核验全部争议及至少12题抽样，再确认报告。
- 解锁要求综合至少85、每项至少75、安全检查全过，未四舍五入总分严格超过三个基线。演示或合成结果不能授予正式许可。报告只说明本轮样本内的表现，不声称是普遍的人格准确率。

评估答案、预测和匿名判分映射单独存入私有数据库，不进入普通状态、数据导出、记忆检索或模拟上下文。许可绑定画像、协议和模型配置；轮换 Key 或更换昵称/头像不影响许可，删除依赖证据或撤回授权会立即使其失效。

## 虚拟空间

解锁后可创建多个独立 VS 会话，选择两个不同版本的参与者并填写话题。开始前确认双方自动分享本场生成发言，默认每场最多20条或30,000总 token。

本机服务在后台公平推进各会话，最多两个模型请求并发。自动发言间隔为12–25秒，模型较慢则继续等待；所有发言标为「AI 模拟」。自己的分身发言有至少五秒接管窗口，支持只改本轮、确认真实观点、确认理想方向。新资料积累为下一版草稿，长期分身继续使用已校验版本，复评后才切换。

网页关闭后会话仍可运行，前提是本机服务保持运行。提供暂停、继续、停止和全部暂停；已发送的模型请求可能产生费用，暂停会丢弃其输出并记录实际用量。

## 数据与备份

默认目录为 `.local`，可用 `WOT_DATA_DIR` 指定独立路径。包含应用数据库、记忆数据库、私有评估数据库和重新编码后的头像。升级至 schema v3 前自动保存完整备份，保留旧 ID、会话和证据；旧自定义人物转为待评，旧模拟记录不获得新许可。

先停止服务再备份或恢复：

```powershell
.\.venv\Scripts\python.exe scripts/data.py backup .local ./private-backup
.\.venv\Scripts\python.exe scripts/data.py restore ./private-backup ./restored-data
```

恢复只允许使用新目录，避免覆盖现有删除标记。备份和已经下载的导出包含私人资料，删除操作无法追回旧副本或供应商已经收取的数据。Key 不随备份迁移。

## 验证与设计

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check backend tests scripts
Push-Location frontend
npm.cmd run build
Pop-Location
```

模型契约及后台运行测试使用隔离数据与模拟供应商，没有开发者发起的真实付费模型调用。浏览器验收与桌面/手机截图见[升级交付记录](docs/implementation/UPGRADE.md)。

详细说明：[模型连接](docs/implementation/CONNECTIONS.md)、[严格评估](docs/implementation/ASSESSMENTS.md)、[后台虚拟空间](docs/implementation/VIRTUAL_SPACE.md)、[实际架构](docs/implementation/ARCHITECTURE.md)。根目录四份原始产品文档保留为设计来源；本次升级的实际行为以这些实施说明为准。

Skill 资料不授予执行脚本、工具或联网权限。vendor 保留原来源及许可，旧核心未声明统一开源许可证，未擅自改为 MIT。
