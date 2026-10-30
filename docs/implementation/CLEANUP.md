# 旧产品移除记录

2026-10-30。

## 新工程中的实际移除

React/TypeScript 前端、FastAPI 后端与 WOT 领域服务全部新建，没有旧 static、application/workspace、project agent、Oracle、续研网页或 Sanofi 项目逻辑。新工程仅安装本地 vendor/dive-memory 的 32 文件纯内核依赖闭包，并通过适配器调用。新应用、测试和新虚拟环境不依赖旧目录。

根目录早期 src、tests、pyproject.toml、旧 README/入口、三份旧审计及旧学生手册已从工作区主入口移入 `历史归档/2026-10-30/`。

## 历史与真实数据

尝试将整个旧 D.I.V.E.Program_Sanofi 仓库归档时，Windows 对目录移动返回 Access denied（普通与提升执行均一样）。首次 PowerShell Move-Item 发生部分移动后，已立即恢复全部顶层文件和 .git 内容，核验 HEAD 仍为 `eb9898b0a4e4965d8e9d276dc2a76b500461fb68`，状态仍只有原来三个未跟踪 Oracle 文档，真实 memory.db 仍在原址。未丢弃用户修改，未永久删除真实数据，也未修改目录权限。

故旧库保留在原址作为历史保存；旧 delivery-clean 快照也不再作为产品入口。新独立 Git 仓库位于 WeOnceTalked；没有推送远端。当前完成的是旧业务从新产品中彻底退出，未声称旧目录已被物理永久删除。

后续若需要物理清空旧历史，应先处理目录的系统占用/访问权限并核对真实数据保留要求。新产品运行不受该目录移动限制影响。
