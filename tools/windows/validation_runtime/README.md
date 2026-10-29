# Windows 冻结评测运行工具

该工具让一个已获授权的评测阶段由 Windows 管理运行。模型策略、预算、原 judge、晋级条件仍由独立的冻结 driver 管理；运行工具不授予花费权限，也不把进程退出0解释为 Benchmark 通过。

## 使用范围

默认只读检查，不创建任务、不读取密钥。显式执行时，启动器先核验 driver 与提案，再从隐藏输入读取密钥；worker 在接收密钥前再次核验。密钥只通过当前用户专属、拒绝远程的一次性命名管道传递，双向校验实际进程 PID 和系统创建时间；任务参数、XML、运行说明中没有密钥。

临时任务使用当前交互用户、Limited 权限、无触发器、无自动重试、12小时执行上限。启动器结束后任务独立运行。工具不自动开始下一阶段，也不恢复已有输出；观察超时后应继续查询同一任务。需要 Windows、PowerShell 7、可用的同环境 `pythonw.exe` 及 driver 所需依赖。系统注销、重启和多小时真实网络调用不在当前离线验证范围内。

## 冻结运行说明

在仓库的 `eval/reports/` 内保存独立 JSON；所有路径都须位于该目录中，输出目录尚不存在。以下哈希仅为占位示例，必须替换为实际冻结值：

```json
{
  "schema_version": "dive-windows-validation-runtime-v1",
  "driver": "eval/reports/my-candidate/driver.py",
  "driver_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "proposal_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "phase": "challenge",
  "output": "eval/reports/my-candidate/runtime-challenge"
}
```

driver 提供 `prepare(phase, execute=False)`，返回包含 `plan` 的对象。`plan.proposal_sha256` 必须匹配；若包含 `driver_sha256`、`phase`，也须匹配。真实执行时 `execution_blockers` 必须为空，driver 必须自行拒绝无效或已结束的授权。`main(["--phase", phase, "--execute"])` 返回0至255的整数，并保留原有请求账本、费用上限与语义门槛。

在仓库根目录、已配置依赖的 Python 环境中运行：

```powershell
python tools/windows/validation_runtime/runner.py --spec eval/reports/my-candidate/runtime-spec.json
# 仅在该冻结方案另获授权后执行；密钥在隐藏提示中输入：
python tools/windows/validation_runtime/runner.py --spec eval/reports/my-candidate/runtime-spec.json --execute
python tools/windows/validation_runtime/runner.py --spec eval/reports/my-candidate/runtime-spec.json --observe
python tools/windows/validation_runtime/runner.py --spec eval/reports/my-candidate/runtime-spec.json --cleanup
```

`--cleanup` 只删除身份、原XML、当前启动时间均匹配且已停止的本次任务，不停止进程、不删除结果或费用账本。外部观察独立保存；缺少 worker 终态时不会补造终态。异常和日志中的完整密钥会被隐藏，但不保证任意变换后的密钥文本或所有 Python 临时内存副本均可识别、擦除。driver 仍须遵守不写入凭据的约束。

`evidence.py` 只读核对账本：未知用量保留完整预留，缺少可定价预留时费用保持未知。执行完整性还需完整分母、worker 终态与完整用量，之后仍须原 judge 和冻结语义门槛；这些检查本身不证明95%。

## 验证

```powershell
python -m pytest tests/test_validation_runtime.py tests/test_validation_runtime_evidence.py tests/test_validation_memory_pipe.py -q
```

前两组可跨平台执行，最后一组使用 Windows 实际进程及命名管道，在其他系统明确跳过。另已完成当前用户临时任务的成功0、捕获异常2、突然退出23与精确清理的本机离线验证。原始任务身份、XML与临时输出保留在忽略的评测目录，公开聚合见[生命周期报告](../../../docs/oracle/64-worker-lifecycle-diagnosis-20261028.md)。
