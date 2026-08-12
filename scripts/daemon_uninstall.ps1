# 卸载 Wyckoff 定时调度 daemon（Windows 计划任务）。日志和待批准队列保留。

$ErrorActionPreference = "Stop"

$TaskName = "WyckoffDaemon"

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "==> $msg" -ForegroundColor Green }

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Info "停止任务..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Info "已移除计划任务 $TaskName"
} else {
    Info "任务未注册"
}

# Windows 上 SIGTERM 不是真信号；残留进程用 taskkill 收掉
$procs = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -like "*cli daemon*" }
foreach ($p in $procs) {
    Info "结束残留进程 PID $($p.ProcessId)"
    taskkill /PID $p.ProcessId /F 2>$null | Out-Null
}

Ok "已卸载。定时任务现在只在 UI 打开时运行。"
Write-Host "日志保留在 ~\.wyckoff\logs\，待批准队列保留在 ~\.wyckoff\approvals.db"
