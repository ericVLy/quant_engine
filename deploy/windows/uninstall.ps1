<#
    卸载 / 撤销开机自启（Windows 单机生产部署）

    执行：powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1

    默认只移除计划任务，**不动数据库、不删配置**。
    需要连带清理时加 -PurgeEnv（删除 env.local.cmd）。
#>
param(
    [switch]$PurgeEnv
)

$ErrorActionPreference = 'Stop'
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Resolve-Path (Join-Path $scriptDir '..\..')

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

Write-Step '停止并移除计划任务'
foreach ($name in @('QuantEngine-Web', 'QuantEngine-Scheduler', 'QuantEngine-Nginx')) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "  [已移除] $name"
    } else {
        Write-Host "  [跳过] $name（不存在）"
    }
}

# nginx 不会随任务停止而自行退出（它是常驻 master 进程），需显式 quit。
# 安装目录与 install_nginx.ps1 的默认一致（用户可写位置）。
# 注意：nginx 把输出（含正常信息）写到 stderr，必须临时放宽 ErrorActionPreference
# 并按退出码判断，否则"停止成功"也会被当成错误而中断脚本。
$nginxCandidates = @(
    (Join-Path ${env:LOCALAPPDATA} 'nginx'),
    (Join-Path ${env:ProgramFiles} 'nginx'),
    (Join-Path ${env:ProgramData} 'nginx')
)
$prevPref = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    foreach ($dir in $nginxCandidates) {
        $exe = Join-Path $dir 'nginx.exe'
        if (Test-Path $exe) {
            & $exe -p "$dir\" -c 'conf/nginx.conf' -s quit 2>&1 | Out-Null
            Get-Process -Name nginx -ErrorAction SilentlyContinue |
                Where-Object { $_.Path -like '*nginx*' } | Stop-Process -Force -ErrorAction SilentlyContinue
            Write-Host "  [已停止] nginx（$dir）"
        }
    }
} finally {
    $ErrorActionPreference = $prevPref
}

# 兜底：清理可能残留的 python/cmd 进程（仅匹配本项目的命令行特征）
Write-Step '清理残留进程'
$stale = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -like "*$projectDir*" -and $_.CommandLine -match 'waitress|run_scheduler|run_intraday_updater' }
if ($stale) {
    foreach ($p in $stale) {
        Write-Host "  终止 PID $($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host '  无残留进程'
}

if ($PurgeEnv) {
    $envFile = Join-Path $scriptDir 'env.local.cmd'
    if (Test-Path $envFile) {
        Remove-Item $envFile -Force
        Write-Host "  [已删除] $envFile"
    }
} else {
    Write-Host "`n  提示：加 -PurgeEnv 可一并删除 env.local.cmd（含数据库口令）。"
}

Write-Host "`n卸载完成。数据库与代码未改动。" -ForegroundColor Cyan
