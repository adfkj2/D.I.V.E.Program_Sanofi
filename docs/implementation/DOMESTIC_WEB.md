# 境内联网来源与模型 Key 复用

联网不再限定 Tavily。聊天模型负责人物的思考与表达，联网来源独立提供公开资料；更换联网来源不更换聊天模型。

## 当前适配

| 联网来源 | 凭据方式 | 搜索与正文 |
| --- | --- | --- |
| DeepSeek 搜索（首次配置优先） | 复用已保存的 DeepSeek 官方连接，或单独填写 DeepSeek Key | 独立 Search 返回正文；缺失时同 Key 调用 Fetch |
| Kimi 搜索 | 复用已保存的 Kimi 境内官方连接，或单独填写 Kimi Key | Search 与 Fetch，固定使用 `api.moonshot.cn` |
| 智谱搜索 | 复用已保存的智谱官方连接，或单独填写智谱 Key | Web Search 后使用同 Key 的 Reader 核对正文，可选择搜索引擎 |
| 博查搜索 | 单独填写博查 Key | 当前适配搜索摘要，明确标记其性质，不冒充已读取网页正文 |
| Tavily | 单独填写 Tavily Key，旧配置继续可用 | 保留 Search 与 Extract |

依据：[DeepSeek Search/Fetch 官方指南](https://api-docs.deepseek.com/guides/search/)、[Kimi 境内搜索指南](https://platform.kimi.com/docs/guide/best-practices-for-web-search)、[智谱搜索](https://docs.bigmodel.cn/api-reference/工具-api/网络搜索)与[网页阅读](https://docs.bigmodel.cn/api-reference/工具-api/网页阅读)、[博查官方扩展](https://github.com/Bocha-Labs/bocha-skills/blob/main/bocha-web-search/SKILL.md)。接口权限及真实联网效果由用户点击测试或试聊核验，模拟契约测试不代替真实账号验证。

通义、豆包等已有聊天模型仍可搭配任意已配置联网来源。本轮没有把供应商网页端的联网能力当作 API 能力，也没有把模型生成摘要复制为网页原文。独立搜索工具不要求升级或切换聊天模型。

## 用户流程

在「设置 → 联网查询」选择来源。若已有对应官方模型连接，优先展示「复用已保存的模型连接 Key」，无需再次输入 Key；也可选择单独填写。只有点击「测试并保存」后才发起查询并应用配置，不会自动测试、收费或替用户更换原模型 Key。

切换供应商或凭据方式会清除当前窗口中未保存的 Key 与旧测试结果，防止误发给另一家公司。错误会明确说明当前供应商、鉴权、账户余额或接口读取权限。DeepSeek/Kimi 等 Key 填入 Tavily 时在本地拦截，并提示选择对应来源。

测试只验证搜索接口可达与账号鉴权；某个网站的正文能否读取，仍取决于网站策略和供应商的 Reader/Fetch 权限。正文缺失时标为「资料未读取」，不使用搜索摘要替代。博查的来源卡明确标记「搜索摘要」，不能据此给出录取分数等关键数字。

## 服务边界与兼容

- 新增 `GET /api/v1/web/providers`，返回应用维护的展示信息、凭据复用供应商、查询长度、正文读取能力及可选引擎；端点、HTTP 方法和请求头不接受前端自由配置。
- `POST /api/v1/web/config/validate` 增加 `provider`、`credential_mode`（CONNECTION/SEPARATE）、`connection_id`、`engine`。CONNECTION 不接受另外提交的 Key；SEPARATE 沿用安全存储或仅本次运行。
- `GET /api/v1/web/config` 增加供应商名称、凭据方式、连接引用及查询限制，不返回原始 Key。未指定 provider 的旧 Tavily 调用继续兼容；已有 Tavily 配置不静默切换。
- 复用仅限供应商与官方 HTTPS 域名匹配的连接，阻止第三方代理 Key 被转发到原厂，阻止 Kimi 境内 Key 自动发到国际域名。移除联网配置不删除原模型连接 Key。
- 查询任务绑定供应商、配置版本及所复用连接的变更标识；删除连接、轮换 Key 或修改地址会停止旧批次。历史已发布依据仍保留，私人证据撤回仍执行删除传播。
- 搜索/读取调用独立记账，不伪造模型 token 或未知费用。45秒、三次搜索、五个页面、会话联网额度、全局两个联网槽位及取消规则沿用既有实现。
- 来源新增 `evidence_kind` 与 `verified_body`；PAGE_CONTENT、SEARCH_SNIPPET 与供应商生成摘要分开。录取数字校验只接受已读取正文，不能凭同一个数字出现在摘要中绕过检查。

旧聊天、人物资料、头像、记忆、评估门禁和已发布消息保持兼容。本轮不自动读取或迁移真实 Key 为新联网配置；正式服务仅通过读取状态核验，不后台试聊。Windows、安卓打包与 Release 继续暂停。

## 验证

- 供应商请求/响应契约45项、境内 Key 复用与来源集成38项通过，合计新增83项；所有测试使用隔离数据、模拟模型与模拟搜索传输，未读取也未发出任何真实 Key。
- 后端全量：`728 passed, 1 warning in 167.31s`。唯一警告为既有 Starlette/httpx TestClient 弃用提示，无失败、无跳过。本轮新增 `tests/test_domestic_research.py`，并扩展 `tests/test_research_providers.py` 与 `tests/test_web_integration.py`。
- Ruff（backend、tests、scripts 及浏览器测试服务器）All checks passed；`pip check` 无损坏依赖；TypeScript 检查与 Vite 正式构建通过（39 模块），产物为 `index-ylYMBqm.js`（355.81kB）、`index-CnjWo5yy.css`（100.37kB）。
- 浏览器：`PASSED`，[报告](../../frontend/artifacts/domestic-web-e2e/report.json)，2026-11-01T15:06Z 重跑，3 组检查、16 张截图；1440／768／390／360px 无横向溢出；浏览器异常 0、外部请求 0、真实模型调用 0、真实搜索调用 0，夹具传输测试调用 4 次。使用本机已安装的 Edge 通道运行 headless，未下载新浏览器。
- 本机 `%TEMP%\pytest-of-<用户>` 存在权限故障（WinError 5），全量回归改用 `--basetemp` 指向隔离目录完成，未跳过、未删除任何测试。
- 真实供应商可用性、额度与 Reader/Fetch 权限未在测试中验证，仍需用户用自己的 Key 在「设置 → 联网查询」点击「测试并保存」核验；模拟契约不能代替真实账号结果。
- 本轮未启动正式服务，`.local` 与已保存的模型 Key 未被读取、写入或迁移。
