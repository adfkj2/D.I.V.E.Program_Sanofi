# 前端与浏览器验收

React、TypeScript、Vite；本机中文无衬线字体，不加载外部字体。主题和聊天布局见 [DESIGN.md](DESIGN.md)，本次功能与验收见[升级记录](../docs/implementation/UPGRADE.md)。

```powershell
npm.cmd ci
npm.cmd run build
```

## 隔离浏览器测试

每轮使用新的空目录，从工程根目录启动：

```powershell
$env:WOT_DATA_DIR = Join-Path (Get-Location) '.local/e2e-fresh-run'
$env:WOT_PROVIDER = 'mock'
.\.venv\Scripts\python.exe frontend/tests/e2e_server.py
```

另一个终端进入 frontend：

```powershell
npx.cmd playwright install chromium
$env:WOT_E2E_ALLOW_WRITE = '1'
npm.cmd run test:e2e
```

脚本只接受本机地址，校验测试服务器标识和空库后才开始写入。若中途失败，停止该测试服务，再使用新的空目录重跑；不要使用正式数据。可用 `WOT_E2E_CHANNEL=chrome` 或 `msedge` 选择已安装浏览器，默认使用Playwright Chromium。

当前脚本为 `tests/upgrade-e2e.mjs`。输出在 `artifacts/upgrade-e2e`：覆盖首次配置、模型发现、个人资料、质量反馈、保留题、人物管理、聊天重试/草稿/滚动/删除，以及八页四种宽度和VS预览，共49张成功截图。

严格评估与虚拟空间的视觉夹具仅由浏览器路由提供，不保存后端许可，也不发起真实模型请求。真正的评分门槛、后台调度、预算、干预和证据隔离由后端隔离测试覆盖。旧 `im-e2e.mjs` 保留为前一轮流程记录，其中旧模拟创建步骤已被严格门禁替代。
