# 我们曾聊过 · We Once Talked

本机运行的聊天与个人数字分身应用。微信式蓝白聊天界面，内置芒格、巴菲特、张雪峰等十五位解释性 AI 人物。长期记忆内核固定为 D.I.V.E 的一个版本，前后端与产品领域独立重建。

侧栏为「聊天」「我」「设置」。完成真实的严格个人校验并确认报告后，才会显示「虚拟空间」。没有配置模型时，可以浏览资料并使用明确标记的离线聊天演示。

## 项目理念

> 试想一下，如果有另一个自己在自己忙碌的时间里，与你想畅谈的朋友聊天说地，畅谈世间万物，你会好奇他们说了什么吗？你所纠结的，他也会纠结，你感兴趣的，他也会感兴趣，你可以有无数的分身，在有限的时空里，与无数你想交流的人们畅谈，这不是很有趣吗？让我们看看我们聊过什么吧。
>
> 这个时代人们太多的把注意力放在外界，却很少关注自身，本项目理念在把注意力回归本身，让自己真正的看见自己。

仓库：<https://github.com/adfkj2/D.I.V.E.Program_Sanofi>

理念落到工程上，是六条必须兑现、而不是用来装饰的约束：

- **记录，不是塑造**：记忆只保存本人明确授权、并确实说过的话，不推断性格、不合成事实。「他说过这句话」和「他是这样的人」是两件不同的事，本项目只做前者。
- **看见自己，先要资料属于自己**：单机单进程，Key 存操作系统凭据库，调用账本只记用途、状态、模型与用量，不记录提示词、答案或密钥；导出与备份都留在本机。
- **删除必须是真删除**：删证据会清正文、断引用，并让依赖它的画像与许可失效；删会话在仍有在用记忆时会被拒绝；已删除的内容不会被重放、也不会从缓存里复活。
- **分身必须说实话**：所有模拟内容一律标「AI 模拟」，未经验证的能力标 `UNVALIDATED`，费用没有可靠资料就写「费用未估算」。
- **人物是解释视角，不是偶像**：每位内置人物对应其公开的思想方法与资料边界，用来多提供一个看问题的角度，不冒充本人、不代其发言。
- **能力靠证据，不靠数量**：严格个人校验使用冻结答案、匿名判分与本人核验；测试数量只说明契约覆盖，不等于拟真度或实际效果。

## 现有功能

| 模块 | 现状 |
| --- | --- |
| 聊天 | 微信式界面，输入提示、连续消息、主动续话、失败重试、历史上滚、新回复提示与消息提示音 |
| 人物 | 十五位内置人物加自定义导入（Markdown／JSON／纯文本／粘贴 Skill）；质量评估五维等权，综合 80、单项 70 才可添加 |
| 记忆 | 真聊授权 → 证据卡（来源、时间、类型）→ 三轨画像（自我陈述／行为线索／理想方向），模拟内容不进入真实画像 |
| 最近会话 | 单条置顶／取消置顶、删除会话（清正文并同步移除列表）；仍有在用记忆时拒绝删除并说明原因 |
| 严格校验 | 每轮 40 道保留题、四组基线对比、匿名辅助判分、本人核验，达标且超过三个基线才解锁虚拟空间 |
| 虚拟空间 | 双版本参与者、多个独立会话、后台公平推进、默认每场 20 条/30,000 token、只改本轮／真实观点／理想方向 |
| 人物 Agent | 按需联网查证、来源卡与来源分级、独立会话上下文、截断恢复、追加额度 |
| 模型 | 八家供应商、四种原生协议、多连接与默认连接、手动模型名、Key 保存状态透明 |
| 数据 | 三个数据库分离、升级前完整备份、导出、备份与恢复脚本 |

## 快速开始

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

首次使用的顺序：

1. 打开页面，首次会要求配置模型；可以先点「稍后设置」浏览人物与资料。
2. 在「设置 → 模型连接」填入 Key，点「测试并添加」，通过后才会保存。
3. 选一位人物开聊；希望某句话进入长期记忆时，在那条消息上勾选并授权。
4. 想解锁虚拟空间，进「我」完成严格个人校验，并确认报告。

## 模型配置

首次打开会出现配置窗口，也可稍后进入「设置」。支持 OpenAI、Claude、Gemini、DeepSeek、通义千问、智谱、Kimi、豆包及自定义 OpenAI 兼容接口；提供四种原生协议、多连接、默认连接、模型列表及手动模型名称。

API Key 仅提交给本机后端。勾选「记住 Key」时使用操作系统凭据库；系统安全存储不可用时，可选择仅本次运行。数据库保存凭据引用，浏览器不永久保存 Key。点击「测试并添加／测试并保存」会发送一条简短请求，通过后才保存；失败保留表单并说明实际保存状态。成功后展示 Key 保存状态，点击「开始聊天」关闭窗口。

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

## 记忆引擎：D.I.V.E 如何实现

D.I.V.E 以固定版本 vendored 在 `vendor/dive-memory`（`0.4.0+wot.core`），通过 `pip install -c requirements.lock.txt -e './vendor/dive-memory'` 安装并由锁文件约束，不随上游漂移。记忆写入独立的 `memory.db`，与应用库 `wot.db`、私有评估库 `assessment-private.db` 分离。

### 技术栈

| 层次 | 选型 | 说明 |
| --- | --- | --- |
| 语言 | Python 3.11+ | `requires-python = ">=3.11"`，无 C 扩展 |
| 依赖 | 零第三方依赖 | `dependencies = []`，只用标准库，不引入 ORM、向量库或 Web 框架 |
| 存储 | SQLite 单文件 `memory.db` | `PRAGMA foreign_keys = ON`；事件、投影、墓碑同库不同表 |
| 并发 | `threading.RLock` + `_synchronized` 装饰器 | `MemoryService` 的公开方法逐个串行进入，避让 SQLite 写锁 |
| 主键 | `stable_id()` 确定性散列 / `new_id()` 随机 id | 事件 id 由幂等键派生，重复提交是重放而非新增 |
| 召回 | 6 个通道 + RRF 融合 + 可权重排 | 默认启用 `bm25`、`dense`、`predicate`、`temporal`、`entity`、`relation`，`rrf_k=60`；`multi_hop` 与 `document_chunk` 需显式开启；未接嵌入模型时 `dense` 会在融合前被过滤 |
| 应用集成 | `backend/wot/dive_adapter.py` | 全项目唯一调用边界，WOT 不直接读写记忆表 |
| 应用侧 | FastAPI + 直写 SQLite + React/TypeScript/Vite | 本机单进程；Key 走操作系统凭据库 |

### 数据模型

- `events`：一次写入的原始事件，含 `namespace`、`payload`、`observed_at`、`idempotency_key`，并有 `UNIQUE(namespace, idempotency_key)`
- `outbox`：投影队列，含 `status`、`attempts`、`last_error`，用来区分「事件已落库」与「记忆已生成」
- `memories`：记忆条目，含 `evidence_state`、`content`、`importance`、`confidence`、`durability`、`valid_from/valid_to`、`status`、`version`、`supersedes_id`、`contradicts_id`
- `memory_sources`：记忆与来源事件的多对多映射，检索时据此把每条记忆回指到原话
- `memory_versions` / `memory_transitions` / `write_decisions`：版本快照、取代与矛盾迁移、写入决策的可审计记录
- `tombstones`：删除墓碑，只存 `object_type`、`object_id`、`reason` 与时间，不存被删内容
- 其余保留表：`memory_vectors`、`vector_metadata`、`memory_access`、`entities`、`memory_entities`、`relations`、`memory_relations`、`profiles`、`profile_revisions`、`profile_proposals`、`memory_modes`、`jobs`、`document_chunks`

取值：证据状态 `FACT`／`OBSERVATION`／`INFERENCE`；记忆状态 `CANDIDATE`／`ACTIVE`／`REINFORCED`／`SUPERSEDED`／`MERGED`／`ARCHIVED`／`DELETED`／`REJECTED`。WOT 只使用 `OBSERVATION` 这一档。

### 写入链路

1. 你在消息上授权后，WOT 先在应用库写一条 `evidence`（含命名空间、幂等键 `stable_key`、发生时间），同一事务里写一条 `outbox` 待办。
2. 进程内的 outbox 处理取出该条，调用 `core.ingest(namespace, text, key=…, source_message_id=…, source_type=…)`。
3. 适配器先用 `stable_id("evt", namespace, key)` 算出事件 id，查墓碑：命中就拒绝，被删过的内容不能借重试复活。
4. 内核在同一事务里写 `events` 与 `outbox`（状态 `PENDING`），事务返回后才做投影。
5. 内核的 worker 消费 outbox，把候选写成 `memories`（`status=ACTIVE`）并建立 `memory_sources` 映射；失败则 `attempts+1` 并保留错误。
6. 适配器轮询内核 outbox 直到 `DONE`；三次仍未完成就抛错，让 WOT 用同一个幂等键重试。只有确认投影完成，WOT 才回写 `evidence.core_event_id` 与 `memory_ids`，并把应用侧 outbox 标记 `APPLIED`。

### 读取链路

1. 查询先过 `plan_query`：把自然语言拆成 `intent`、`temporal_mode`、`predicates`、`limit` 与 token 预算，并决定是否包含历史版本。
2. `store.search` 按启用配置跑召回：词法 `bm25`、结构化 `predicate`、时间窗 `temporal`、实体 `entity`、关系 `relation`；未接嵌入模型时 `dense` 会在融合前被过滤。
3. 多路结果用 RRF（`rrf_k=60`）融合，避免不同通道的分数量纲不一致。
4. `ScoringPolicy` 在融合分之上做重排，六个可独立调权的分量：语义、关键词、新近度（半衰期衰减）、重要度、时间兼容性、记忆状态。带否决性质的信号（已被取代、已归档）用乘性权重压低而不是删除，所以问历史时仍能看见旧版本。
5. 硬过滤只有两条：命名空间与删除状态；命中后写 `memory_access` 访问记录，结果为空时显式返回 `abstain`，并留下 `retrieval-trace` 说明实际跑了哪些通道。
6. 返回条目带 `memory_id`、`source_event_ids`、`text`、`evidence_state`、`observed_at`、`namespace`，界面据此显示来源并跳回原始消息。

### 遗忘链路

1. 撤回证据时，WOT 先在应用侧排一条 `DELETE` 任务，处理时调用 `core.delete_by_key(namespace, stable_key)`：命中事件就走 `forget_event(hard=True)`，删事件、记忆投影与向量。
2. 应用侧的队列总是先处理删除、再处理写入；若删除先于写入到达内核，就只写一条由同一幂等键派生的不透明墓碑，迟到的 `ingest` 会被 `is_tombstoned` 拒绝。
3. 同时清空幂等响应缓存、递增隐私版本、记录审计，使依赖这份证据的画像、许可与在途回复一起失效。

### 这张记忆表在 WOT 里怎么被用

- **真聊**：只发送本会话未删除的消息与人物上下文，私人记忆不进入真聊请求。
- **虚拟空间**：仅当该参与者被允许使用私有上下文时，按证据所属的三个命名空间分别 `retrieve(topic, limit=12)`，再用返回的 `source_event_ids` 反查证据白名单，最多取 3 条、每条截断 1000 字，并再走一次 `allow_evidence()` 权限检查。
- **画像与评估**：读 WOT 自己的证据表与私有评估库，不经过记忆内核；评估库不挂任何记忆适配器。
- **删除传播**：证据撤回后，应用库的 `evidence` 与内核的 `events`／`memories` 同步失效，任何一侧都不能单独复活内容。

### 关键取舍

**1. 只存原话，不做语义升级。** 适配器用自己的 `_VerbatimObservationExtractor` 替换内核抽取器：一条被授权的本人消息只生成一个 `episode` 候选，`evidence_state` 固定为 `OBSERVATION`，断言语义标记为 `reported_dialogue`。内核里「把对话提炼成事实或性格」的路径在本项目中不启用，因此记忆永远是「你说过这句话」，不会变成「你是这种人」。

**2. 三轨命名空间物理隔离。** 只接受 `user/<id>/real`、`user/<id>/self_reports`、`user/<id>/corrections` 三种命名空间，分别对应真聊、自报与确认更正，来源类型由命名空间推导（`user` / `user_correction`）。越界的命名空间直接报错，所以一条真聊内容不可能被写进自报轨，反向亦然。

**3. 固定策略，而不是自适应打分。** 每次写入的接受与否、重要度、置信度、耐久度都取同一组固定值与固定策略版本（`wot-verbatim-observation-v1`），内核照常把这次决策写进 `write_decisions` 供审计，但不存在「让模型决定哪句话更值得记住」的空间。

**4. 能力保留，行为冻结。** 内核里的画像（`profiles`、`hot_memory`）、记忆合并（`consolidate`）、遗忘曲线（`decay`）、任务调度（`jobs`）都留在 vendor 中但本项目不启用，因为「把对话升级成性格」正好与第一条理念相反。授权与来源判断发生在进入适配器之前：只有本人主动勾选并确认的消息才会走到这里，模拟台词、只改本轮的反馈、模型生成内容都进不去。

## 数据与备份

默认目录为 `.local`，可用 `WOT_DATA_DIR` 指定独立路径。包含应用数据库、记忆数据库、私有评估数据库和重新编码后的头像。当前 schema 为 v6，每次升级前自动保存完整备份，保留旧 ID、会话和证据；旧自定义人物转为待评，旧模拟记录不获得新许可。

先停止服务再备份或恢复：

```powershell
.\.venv\Scripts\python.exe scripts/data.py backup .local ./private-backup
.\.venv\Scripts\python.exe scripts/data.py restore ./private-backup ./restored-data
```

恢复只允许使用新目录，避免覆盖现有删除标记。备份和已经下载的导出包含私人资料，删除操作无法追回旧副本或供应商已经收取的数据。Key 不随备份迁移。

## 验证与设计

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check backend tests scripts frontend/tests
Push-Location frontend
npm.cmd run build
Pop-Location
```

当前后端全量 762 个用例通过（仅 1 条既有 Starlette/httpx 弃用警告），Ruff、TypeScript 检查与构建均无错误。若本机系统临时目录不可写，用 `--basetemp` 指到其他目录，不影响结论。

模型契约及后台运行测试使用隔离数据与模拟供应商，没有开发者发起的真实付费模型调用。浏览器验收与桌面/手机截图见[升级交付记录](docs/implementation/UPGRADE.md)。

最近会话的置顶、取消置顶与删除已覆盖 6 个后端用例和一条浏览器验收步骤：删除会清除正文、取消在途回复、写入审计并递增隐私版本，且拒绝删除仍有在用记忆的会话。

最新的角色表达、回复格式和模型配置修复见[本轮交付记录](docs/implementation/IMMERSION_FIX.md)。Windows／安卓打包、签名和 Release 发布暂停，当前继续提供本地页面测试。

十六位人物的新版表达档案、来源覆盖、匿名同题样片和导入文件见[表达重做交付记录](docs/implementation/VOICE_REDESIGN.md)。内置资料升级为v1.2，真实模型拟真仍待试聊验证。

微信式输入提示、连续消息、主动续话和虚拟空间分组发言见[自然聊天交付记录](docs/implementation/NATURAL_CHAT.md)。

最新独立人物Agent、截断恢复、直接开放的虚拟空间、本人Skill质量准入及追加额度见[角色Agent交付记录](docs/implementation/CHARACTER_AGENTS.md)。

人物 Agent 按需联网查证、报考条件核对、来源查看与联网额度见[公开资料查证说明](docs/implementation/WEB_RESEARCH.md)。在「设置 → 联网查询」选择来源，支持复用 DeepSeek、Kimi、智谱连接的 Key，也可使用博查或 Tavily；普通聊天和虚拟空间共用。境内接口、凭据复用与验证记录见[境内联网适配](docs/implementation/DOMESTIC_WEB.md)。

最近新增代码的录取数据核验、删除传播、并发取消及界面问题修复见[2026-11-02审核记录](docs/implementation/REVIEW_20261102.md)。

人物每条新消息的提示音默认开启，可在聊天顶部或设置中静音；历史不重播，说明见[消息提示音](docs/implementation/MESSAGE_SOUND.md)。

详细说明：[模型连接](docs/implementation/CONNECTIONS.md)、[严格评估](docs/implementation/ASSESSMENTS.md)、[后台虚拟空间](docs/implementation/VIRTUAL_SPACE.md)、[实际架构](docs/implementation/ARCHITECTURE.md)。根目录四份原始产品文档保留为设计来源；本次升级的实际行为以这些实施说明为准。

Skill 资料不授予执行脚本、工具或联网权限。vendor 保留原来源及许可，旧核心未声明统一开源许可证，未擅自改为 MIT。
