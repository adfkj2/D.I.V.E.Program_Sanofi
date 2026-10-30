# 模型连接与秘密边界

`ConnectionManager` 是聊天、评估和虚拟空间共用的本机调用入口。保存连接不发起推理，`test()` 的账本用途为 `connection_test`，实际调用另记用途。未配置连接时，旧聊天入口可以生成明确标记的固定离线演示；`complete()` 不能以演示结果代替真实评估。

| 供应商 ID | 默认协议 | 默认接口根地址 |
| --- | --- | --- |
| `openai` | Responses | `https://api.openai.com/v1` |
| `claude` | Messages | `https://api.anthropic.com/v1` |
| `gemini` | GenerateContent | `https://generativelanguage.googleapis.com/v1beta` |
| `deepseek` | Chat Completions | `https://api.deepseek.com/v1` |
| `qwen` | Chat Completions | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `glm` | Chat Completions | `https://open.bigmodel.cn/api/paas/v4` |
| `kimi` | Chat Completions | `https://api.moonshot.cn/v1` |
| `doubao` | Responses | `https://ark.cn-beijing.volces.com/api/v3` |
| `custom` | Chat Completions | 用户填写 |

模型 ID 从 `/models` 读取；供应商不支持该入口时返回空列表，由用户手动填写并测试。不会用硬编码的“最新模型”替换用户选择。豆包接受模型名或推理接入点 ID。通义地区值为 `cn-beijing`、`international`、`us`、`workspace`；工作空间模式要求 `workspace_id`，使用 `https://{workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。修改地区或工作空间会更新相应预设根地址。

## 配置和凭据

`save(data, connection_id=None)` 接受 `provider`、`model`、`api_key`、`remember`、`base_url`、`region`、`workspace_id`、`label`、`protocol`。编辑时省略 `api_key` 表示保留；浏览器收到的安全摘要只有固定掩码及配置状态。协议枚举为 `responses`、`chat`、`messages`、`generateContent`。外部接口要求 HTTPS；本机 `localhost`、`127.0.0.1`、`::1` 可以 HTTP。地址拒绝内嵌用户名、密码、查询参数和片段。

勾选记住后，秘密写入受支持的系统凭据库，SQLite 仅保留随机 `credential_ref`。默认只接受 keyring 的 Windows、macOS、Secret Service、KWallet 系统后端；不接受第三方纯文本后端。系统后端缺失或失败时明确报错，让用户选择仅本次运行，不会自动落盘。未记住的 Key 仅在进程字典中保存，重启失效。切换为临时保存或移除连接时删除原系统凭据；无法删除时保留当前配置并提示重试。应用关闭时清空临时字典。

人格许可使用的 `config_hash()` 包含供应商、模型、协议、根地址、地区和工作空间，排除 Key、连接名称及其他外观信息。Key 轮换不改变这个指纹。

## 请求与结果

`complete(messages, connection_id=None, max_tokens=400, purpose="chat", temperature=0.4)` 接受纯文本 system/user/assistant 消息，返回文本、输入/输出/总 token、结束原因、供应商、模型、连接 ID、`classification="LIVE"`、`cost=None`。各家缺少可靠总用量时拒绝继续；Gemini 总用量保留供应商报告的思考 token 等额外消耗。Responses 默认禁用服务端会话存储。OpenAI 推理系列不发送其不支持的 temperature 参数。

所有管理器实例共享最多两个推理请求的信号量；不自动重试，避免隐含重复费用。待排队请求获得名额后再次检查连接未被移除。重定向不跟随，鉴权、额度、网络、格式错误都转换为稳定的中文错误，不包含供应商原始响应或异常内容。

`complete()` 还接受可选 `cancelled` 检查函数，在排队前、等待期间和发起请求前检查取消状态。未发送的取消请求不计作实际调用。Claude 缓存读写 token 计入输入及总用量；成功响应意外回显的当前 Key 也会先隐藏，结束原因只允许已知枚举，避免原始字段流入聊天或账本。

持久化 `model_call` 仅含用途、连接/供应商/模型、状态、时间、token 和结束原因，无输入、输出、Key 或原始错误。`last_test` 和 `last_generation` 分开记录。`calls` 只为旧离线演示保留确定性测试数据，真实调用不保存提示词。

## 验证

`tests/test_connections.py` 使用 httpx MockTransport 和进程内假凭据库，覆盖九种预设的请求路径、鉴权和响应，八家 400/401/403/429/500/重定向错误、无可靠用量、网络错误脱敏、多连接、默认切换、临时 Key 重启失效、安全存储失败、地区/工作空间、模型列表、许可指纹和全局两并发。测试不访问真实供应商、不写系统凭据。
