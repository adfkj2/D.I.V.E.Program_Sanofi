param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$repositoryPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$launcherPath = Join-Path $PSScriptRoot 'launcher.py'

function Find-LauncherPython {
    $candidates = @(
        (Join-Path $repositoryPath '.venv-workbench\Scripts\python.exe'),
        (Join-Path $repositoryPath '.venv\Scripts\python.exe')
    )
    $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonLauncher) {
        foreach ($version in @('-3.12', '-3')) {
            try {
                $candidate = & $pythonLauncher.Source $version -c 'import sys; print(sys.executable)' 2>$null
                if ($LASTEXITCODE -eq 0) { $candidates += $candidate }
            } catch { }
        }
    }
    $pathPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pathPython -and $pathPython.Source -notlike '*\WindowsApps\*') {
        $candidates += $pathPython.Source
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            & $candidate -c 'import sys, tkinter; assert sys.version_info >= (3, 11)' 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { }
    }
    return $null
}

try {
    $pythonPath = Find-LauncherPython
    if (-not $pythonPath) {
        throw '需要先安装 Python 3.12（保留安装器默认的 Tcl/Tk 组件），完成后再次双击“启动续研”。下载地址：https://www.python.org/downloads/windows/'
    }
    if ($CheckOnly) {
        @{ ready = $true; python = $pythonPath; launcher = $launcherPath } | ConvertTo-Json -Compress
        exit 0
    }
    $windowPython = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
    if (Test-Path -LiteralPath $windowPython) { $pythonPath = $windowPython }
    # The bootstrap console is hidden; the interactive Tk window must be visible.
    # Native invocation preserves spaces without passing SW_HIDE to the GUI itself.
    Set-Location -LiteralPath $repositoryPath
    & $pythonPath $launcherPath
} catch {
    if ($CheckOnly) { Write-Error $_; exit 1 }
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, '续研 · 启动提示', 'OK', 'Error') | Out-Null
    exit 1
}
