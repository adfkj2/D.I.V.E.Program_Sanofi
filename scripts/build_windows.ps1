<#
.SYNOPSIS
    从干净环境重建 Windows 安装包。

.DESCRIPTION
    步骤：准备 Python 3.12 构建环境 → 安装锁定依赖与 PyInstaller → 构建前端 →
    生成图标 → 冻结为 onedir → 调用 Inno Setup 生成
    dist\windows\WeOnceTalked-<version>-Setup.exe。

    版本号来自 pyproject.toml。本机 .venv 是 Python 3.14，PyInstaller 对其支持
    尚不稳定，因此这里始终使用独立的 3.12 构建环境。

    本文件必须保存为 UTF-8 with BOM，否则 Windows PowerShell 5.1 会按 ANSI
    解码中文并导致语法错误。

.PARAMETER SkipFrontend
    跳过 npm ci / npm run build，直接复用已有的 frontend/dist。

.PARAMETER SkipInstaller
    只产出冻结目录 dist\WeOnceTalked，不生成安装包。
#>
param(
    [switch]$SkipFrontend,
    [switch]$SkipInstaller
)
$ErrorActionPreference = 'Stop'

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

function Get-ProjectVersion {
    $pyproject = Join-Path $root 'pyproject.toml'
    $match = Select-String -LiteralPath $pyproject -Pattern '^version\s*=\s*"([^"]+)"' |
        Select-Object -First 1
    if (-not $match) { throw "无法从 $pyproject 读取版本号。" }
    return $match.Matches[0].Groups[1].Value
}

function Find-Python312 {
    $candidates = @()
    if ($env:WOT_BUILD_PYTHON) { $candidates += $env:WOT_BUILD_PYTHON }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        try { $candidates += (& uv python find 3.12 2>$null) } catch { }
    }
    $uvRoot = Join-Path $env:APPDATA 'uv\python'
    if (Test-Path -LiteralPath $uvRoot) {
        $candidates += Get-ChildItem -LiteralPath $uvRoot -Directory -Filter 'cpython-3.12*' -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'python.exe' }
    }
    foreach ($name in @('python3.12', 'python')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $reported = & $command.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($reported -eq '3.12') { $candidates += $command.Source }
    }
    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        $path = $candidate.ToString().Trim()
        if (Test-Path -LiteralPath $path) { return $path }
    }
    throw '找不到 Python 3.12 构建解释器。可安装 Python 3.12，或用 WOT_BUILD_PYTHON 指向解释器路径。'
}

function Invoke-Step([string]$Title, [scriptblock]$Action) {
    Write-Host ""
    Write-Host "==> $Title" -ForegroundColor Cyan
    & $Action
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "$Title 失败（退出码 $LASTEXITCODE）。"
    }
}

$version = Get-ProjectVersion
Write-Host "我们曾聊过 Windows 打包，版本 $version" -ForegroundColor Green
Write-Host "项目目录 $root"

$buildDir = Join-Path $root '.build'
$venv = Join-Path $buildDir 'venv312'
$python = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    $base = Find-Python312
    Write-Host "使用构建解释器 $base"
    New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
    Invoke-Step '创建 Python 3.12 构建环境' { & $base -m venv $venv }
}

Invoke-Step '安装锁定依赖与项目' {
    & $python -m pip install --disable-pip-version-check --upgrade pip
    & $python -m pip install --disable-pip-version-check `
        -c (Join-Path $root 'requirements.lock.txt') `
        -e (Join-Path $root 'vendor\dive-memory') `
        -e $root
}

Invoke-Step '安装 PyInstaller' {
    & $python -m pip install --disable-pip-version-check 'pyinstaller>=6,<7'
}

if (-not $SkipFrontend) {
    $frontend = Join-Path $root 'frontend'
    Invoke-Step '构建前端界面' {
        Push-Location $frontend
        try {
            if (-not (Test-Path -LiteralPath (Join-Path $frontend 'node_modules'))) {
                & npm.cmd ci --cache .npm-cache
                if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw '安装前端依赖失败。' }
            }
            & npm.cmd run build
        } finally { Pop-Location }
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $root 'frontend\dist\index.html'))) {
    throw '缺少 frontend\dist\index.html，请先构建前端。'
}

Invoke-Step '生成应用图标' { & $python (Join-Path $root 'packaging\windows\make_icon.py') }

Invoke-Step '冻结后端与界面（PyInstaller onedir）' {
    & $python -m PyInstaller --noconfirm --clean `
        --distpath (Join-Path $root 'dist') `
        --workpath (Join-Path $buildDir 'pyinstaller') `
        (Join-Path $root 'packaging\windows\we-once-talked.spec')
}
$bundle = Join-Path $root 'dist\WeOnceTalked'
if (-not (Test-Path -LiteralPath (Join-Path $bundle 'WeOnceTalked.exe'))) {
    throw 'PyInstaller 未产出 dist\WeOnceTalked\WeOnceTalked.exe。'
}
$bundleMb = '{0:N1}' -f ((Get-ChildItem -LiteralPath $bundle -Recurse -File |
            Measure-Object -Sum Length).Sum / 1MB)
Write-Host "冻结产物 $bundle（$bundleMb MB）"

if ($SkipInstaller) {
    Write-Host "已跳过安装包，产物：$bundle" -ForegroundColor Green
    exit 0
}

$iscc = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe',
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $iscc) {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { $iscc = $command.Source }
}
if (-not $iscc) {
    Write-Host ""
    Write-Host '未找到 Inno Setup 6（ISCC.exe），已生成冻结目录但未生成安装包。' -ForegroundColor Yellow
    Write-Host '安装后重新运行本脚本即可：winget install JRSoftware.InnoSetup'
    Write-Host '手动安装：https://jrsoftware.org/isdl.php'
    exit 1
}

Invoke-Step '生成安装包（Inno Setup）' {
    & $iscc "/DAppVersion=$version" (Join-Path $root 'packaging\windows\installer.iss')
}

$setup = Join-Path $root "dist\windows\WeOnceTalked-$version-Setup.exe"
if (-not (Test-Path -LiteralPath $setup)) { throw "未生成安装包 $setup" }
$setupMb = '{0:N1}' -f ((Get-Item -LiteralPath $setup).Length / 1MB)
Write-Host ""
Write-Host "安装包：$setup（$setupMb MB）" -ForegroundColor Green
