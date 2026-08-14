# 安装 Wyckoff 定时调度 daemon 为 Windows 计划任务。
# 装完后关掉 UI，定时任务仍会跑。
#
# 用法（普通 PowerShell，不需要管理员）：
#   powershell -ExecutionPolicy Bypass -File scripts\daemon_install.ps1

$ErrorActionPreference = "Stop"

$TaskName = "WyckoffDaemon"
$RepoDir  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogDir   = Join-Path $env:USERPROFILE ".wyckoff\logs"

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "==> $msg" -ForegroundColor Green }
function Fail($msg) { Write-Host "==> $msg" -ForegroundColor Red; exit 1 }

# 优先用仓库内的 venv，退回 PATH 上的 python
$Python = Join-Path $RepoDir ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { Fail "未找到 Python。先建 venv：uv venv; uv sync" }
    $Python = $cmd.Source
}

Info "仓库:   $RepoDir"
Info "Python: $Python"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# 已存在则先删，否则 Register 会失败
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Info "移除已有任务..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# pythonw.exe 没有控制台窗口；没有它就用 python.exe
$Runner = $Python -replace 'python\.exe$', 'pythonw.exe'
if (-not (Test-Path $Runner)) { $Runner = $Python }

$action = New-ScheduledTaskAction `
    -Execute $Runner `
    -Argument "-m cli daemon --foreground" `
    -WorkingDirectory $RepoDir

# 登录时启动；daemon 自身有 flock 单例保护，重复触发不会跑出两个
$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Wyckoff 定时调度 daemon — 关闭 UI 后定时任务继续运行" | Out-Null

Info "已注册计划任务 $TaskName"

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

$state = (Get-ScheduledTask -TaskName $TaskName).State
if ($state -eq "Running" -or $state -eq "Ready") {
    Ok "daemon 已启动（状态：$state）"
    & $Python -m cli daemon --status
    Write-Host ""
    Write-Host "查看日志:  Get-Content -Wait $LogDir\daemon.log"
    Write-Host "待批准项:  $Python -m cli approve list"
    Write-Host "停止:      Stop-ScheduledTask -TaskName $TaskName"
    Write-Host "卸载:      powershell -File scripts\daemon_uninstall.ps1"
} else {
    Fail "启动失败（状态：$state）。查看 $LogDir\daemon.log"
}
