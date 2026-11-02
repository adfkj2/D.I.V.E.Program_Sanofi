# Windows 安装包（0.1.0 首版）

本轮只做 Windows 桌面安装包：装好即用，不需要 Python、Node 或命令行。移动端、局域网访问、代码签名与自动更新都不在本版范围内。

## 产物

| 产物 | 路径 | 大小 |
| --- | --- | --- |
| 安装包 | `dist\windows\WeOnceTalked-0.1.0-Setup.exe` | 20.9 MB（21,912,556 字节） |
| 冻结目录 | `dist\WeOnceTalked\`（`WeOnceTalked.exe` + `_internal\`） | 50.0 MB |

两者都不进版本库：`dist/` 与 `.build/` 已在 `.gitignore` 中。

## 用户看到的行为

1. 双击安装包，无需管理员权限，按用户装到 `%LOCALAPPDATA%\Programs\WeOnceTalked`。
2. 写入开始菜单「我们曾聊过」与卸载项，桌面图标为可选任务（默认不勾）。
3. 启动后出现一个控制台窗口，随后默认浏览器打开 `http://127.0.0.1:8765/#chat`；关闭控制台窗口即停止服务。
4. 用户数据落在 `%LOCALAPPDATA%\WeOnceTalked\data`（三个数据库、头像副本、`logs\app.log`）。
5. 卸载只删程序目录，`data` 目录保留；重装后原来的会话、证据与记忆仍在。

## 明确不支持 / 已知限制

- **未签名**：首次运行会被 SmartScreen 拦截，需要「更多信息 → 仍要运行」。没有做代码签名，也不声称已签名。
- **保留控制台窗口**：首版用它显示访问地址、错误与退出码；代价是关掉窗口就停服务。
- **端口固定 8765**：应用的 Host／Origin 白名单与端口绑定。被其他程序占用时打印中文提示并退出（退出码 2），不会静默换端口。
- **安装向导语言**：Inno Setup 6 默认不附带简体中文翻译，脚本在缺少 `ChineseSimplified.isl` 时回退英文向导；安装后的程序界面仍是中文。
- **不含自动更新**，也不含移动端。
- **未改 CI**：本机执行构建，仓库现有的 Linux 测试工作流保持不变。

## 代码改动

| 文件 | 变更 |
| --- | --- |
| `backend/wot/resources.py` | 新增。解析资源根、前端产物、头像目录、默认数据目录与日志目录，是唯一区分「源码态／冻结态」的地方 |
| `backend/wot/desktop.py` | 新增。桌面启动器：日志落盘、单实例复用、端口占用提示、就绪后打开浏览器 |
| `backend/wot/main.py` | `create_app` 走 `default_data_dir()`；静态资源改用 `frontend_dist()`／`public_portraits()`；`main()` 交给 `wot.desktop.run()`，不再用字符串工厂导入 |
| `scripts/start.ps1` | 不再自己 `Start-Process` 开浏览器（避免开两次），改由启动器负责；`-NoBrowser` 映射为 `WOT_NO_BROWSER=1` |
| `packaging/windows/entry.py` | PyInstaller 入口 |
| `packaging/windows/we-once-talked.spec` | onedir 构建：`frontend/dist → frontend_dist`、`wot/data` 与 uvicorn／keyring 后端随包收集 |
| `packaging/windows/make_icon.py` | 用 Pillow 现场生成 16–256 多尺寸 `.ico`，不引入外部素材 |
| `packaging/windows/installer.iss` | Inno Setup 6 脚本，per-user，`PrivilegesRequired=lowest` |
| `scripts/build_windows.ps1` | 一键构建（UTF-8 with BOM，否则 Windows PowerShell 5.1 会误读中文） |
| `tests/test_desktop_resources.py` | 8 个用例：资源根、前端目录查找顺序、数据目录优先级 |
| `tests/test_desktop_launcher.py` | 9 个用例：日志、浏览器开关、健康探针、单实例复用、端口占用 |

资源解析与启动逻辑都用纯函数加依赖注入实现，所以能在不启动真实服务器、不绑定端口的前提下测试。

## 构建

```powershell
.\scripts\build_windows.ps1              # 完整构建
.\scripts\build_windows.ps1 -SkipFrontend   # 复用已有 frontend/dist
.\scripts\build_windows.ps1 -SkipInstaller  # 只产出冻结目录
```

脚本行为：

1. 从 `pyproject.toml` 读版本号，决定安装包文件名。
2. 准备 `.build\venv312`：依次尝试 `WOT_BUILD_PYTHON`、`uv python find 3.12`、`%APPDATA%\uv\python\cpython-3.12*`、`python3.12`／`python`。本机 `.venv` 是 3.14，PyInstaller 对 3.14 支持不稳，所以构建环境固定 3.12.14。
3. 用 `requirements.lock.txt` 约束安装项目与 `vendor/dive-memory`，再装 `pyinstaller>=6,<7`（本次为 6.22.3）。
4. `npm ci`（缺 `node_modules` 时）+ `npm run build`。
5. 生成图标 → PyInstaller onedir → Inno Setup 编译。
6. 找不到 Inno Setup 时只打印 `winget install JRSoftware.InnoSetup` 并退出 1，不会自行安装。

## 验收记录

| 检查 | 结果 |
| --- | --- |
| 源码态全量测试 | `779 passed`，1 条既有 Starlette/httpx 弃用警告 |
| `ruff check backend tests` | 通过 |
| `npm run build` | 通过（`dist/assets/index-*.js` 364.73 kB） |
| 冻结态启动（工作目录为临时空目录） | `/api/v1/healthz` 200、`/` 返回 575 字节 HTML、`/portraits/buffett.png` 200 |
| 数据落点 | `%LOCALAPPDATA%\WeOnceTalked\data` 出现 `wot.db`、`memory.db`、`assessment-private.db`、`avatars`、`logs\app.log`；工作目录内没有 `.local` |
| 静默安装 | `Setup.exe /VERYSILENT /CURRENTUSER` 退出码 0；程序目录、开始菜单两项（启动／卸载）、HKCU 卸载项 `0.1.0` 均存在 |
| 安装后运行 | 从 `C:\` 启动，健康检查 200，创建一条会话成功 |
| 静默卸载 | 退出码 0；程序目录与开始菜单清空，卸载项移除，`data` 目录仍在（三个数据库完好） |
| 重装后回读 | 重装并启动后，卸载前创建的会话仍能在 `/api/v1/state` 读到 |
| 单实例 | 服务已在运行时再次启动会立即退出，并只打开一次浏览器（该次验收用 `WOT_NO_BROWSER=1`，看的是复用分支的日志与退出码 0） |

验收用的 `%LOCALAPPDATA%\WeOnceTalked\data`（由本轮测试新建）已删除，用户首次启动是干净状态。

## 自动化验收提示

设置 `WOT_NO_BROWSER=1` 可关闭自动开浏览器，`WOT_DATA_DIR` 可把数据目录指到临时路径，便于脚本化跑安装包。

## 已知风险

- 未签名 + PyInstaller onedir 仍可能被个别杀软误报，只能靠签名与提交误报解决，本轮未做。
- `keyring` 在冻结环境若回退到非安全后端，应用按既有逻辑降级为「仅本次运行」保存 Key，不会静默写明文。构建清单（`PYZ-00.toc`）确认 `keyring.backends.Windows`、`win32ctypes.pywin32.win32cred` 与 uvicorn 的协议实现都已收集，但未在冻结态实测真实 Key 的写入，因为没有可用的供应商密钥。
- 端口 8765 是硬约束：修改端口需要同步改 Host／Origin 白名单，不在本轮范围。
