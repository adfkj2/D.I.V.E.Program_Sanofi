param([switch]$Install, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$taskProject = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $taskProject
$taskPython = Join-Path $taskProject '.venv\Scripts\python.exe'
if ($Install -or -not (Test-Path -LiteralPath $taskPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败，请安装 Python 3.11 或更新版本。' }
    & $taskPython -m pip install -c requirements.lock.txt -e './vendor/dive-memory' -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw '安装 Python 依赖失败。' }
    Push-Location frontend
    try {
        npm.cmd ci --cache .npm-cache
        if ($LASTEXITCODE -ne 0) { throw '安装前端依赖失败。' }
        npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw '构建前端失败。' }
    } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath 'frontend\dist\index.html')) {
    throw '尚未构建界面，请运行 .\scripts\start.ps1 -Install。'
}
if (-not $env:WOT_DATA_DIR) { $env:WOT_DATA_DIR = Join-Path $taskProject '.local' }
Write-Host '我们曾聊过：http://127.0.0.1:8765   按 Ctrl+C 停止。'
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8765' -WindowStyle Hidden }
& $taskPython -m wot.main
