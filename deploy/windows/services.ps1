<#
    量化引擎 Windows 服务管理（启动 / 停止 / 重启 / 状态）

    用法：
        # 重启全部（最常用：改了配置或重新部署后执行）
        powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\services.ps1 -Action restart

        # 只重启 Web
        ... -Action restart -Service Web

        # 看状态
        ... -Action status

        # 启动 / 停止
        ... -Action start
        ... -Action stop

    说明：
      - Web / Scheduler 由计划任务 QuantEngine-Web / QuantEngine-Scheduler 拉起
        （经 run_hidden.vbs 隐藏窗口运行）。
      - nginx 是可选组件：装过 install_nginx.ps1 才有计划任务，
        否则退化为直接调start_nginx.cmd / nginx.exe。
      - 停止时会**兜底清理残留进程**：计划任务停止虽会带走进程树，
        但服务异常退出后可能留下孤儿 python / nginx 进程。
    不需要管理员权限（与 install.ps1 的默认 RunLevel=Limited 一致）。
#>
param(
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'status',

    [string]$Service = 'All',

    # start/restart 后等待端口就绪的秒数
    [int]$WaitSeconds = 30
)

$ErrorActionPreference = 'Stop'
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = (Resolve-Path (Join-Path $scriptDir '..\..')).Path

function Write-Step($msg)  { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Write-Err($msg)   { Write-Host "  [X]  $msg" -ForegroundColor Red }
function Write-Info($msg)  { Write-Host "  -    $msg" -ForegroundColor Gray }

# --------------------------------------------------------------- 基础设施
# 与 install.ps1 同一个坑：PowerShell 把子进程 stderr 包装成 NativeCommandError，
# 而 nginx 等工具把正常输出也写 stderr。统一按退出码判断成败。
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory,
        [switch]$Quiet
    )
    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        Push-Location -LiteralPath $WorkingDirectory -ErrorAction SilentlyContinue
        try {
            if ($Quiet) { & $FilePath @Arguments 2>&1 | Out-Null }
            else { & $FilePath @Arguments 2>&1 | ForEach-Object { Write-Host "      $_" } }
        } finally { Pop-Location -ErrorAction SilentlyContinue }
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $prevPref }
}

# 从 env.local.cmd 里读某个变量（不执行该文件，避免副作用）
function Get-EnvVarFromFile([string]$Path, [string]$Name, [string]$Default = '') {
    if (-not (Test-Path $Path)) { return $Default }
    foreach ($line in (Get-Content $Path -Encoding Default -ErrorAction SilentlyContinue)) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*)$") {
            return $Matches[2].Trim()
        }
    }
    return $Default
}

# 按命令行特征找进程并终止（用于清理计划任务没带走的孤儿进程）
function Stop-ProcessByPattern {
    param([string[]]$Patterns, [string]$What)
    $killed = 0
    $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
               Where-Object {
                   $cl = $_.CommandLine
                   if (-not $cl) { return $false }
                   if ($_.Name -match 'powershell|pwsh') { return $false }
                   foreach ($pat in $Patterns) { if ($cl -like "*$pat*") { return $true } }
                   return $false
               })
    foreach ($p in $procs) {
        if ($p.ProcessId -eq $PID) { continue }
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            $killed++
            Write-Info "已结束残留 $What 进程 pid=$($p.ProcessId)"
        } catch { Write-Warn2 "无法结束 pid=$($p.ProcessId)：$($_.Exception.Message)" }
    }
    return $killed
}

function Get-TaskState([string]$Name) {
    $t = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $t) { return $null }
    return $t.State.ToString()
}

function Test-PortListening([int]$Port) {
    if ($Port -le 0) { return $false }
    try {
        return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } catch { return $false }
}

function Wait-Port([int]$Port, [int]$TimeoutSec) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        if (Test-PortListening $Port) { return $true }
        Start-Sleep -Milliseconds 800
    }
    return $false
}

# ------------------------------------------------------------ nginx 定位
# install_nginx.ps1 会把安装目录记在这里；没有则回退到常见候选位置。
function Resolve-NginxDir {
    $marker = Join-Path $scriptDir 'nginx_dir.txt'
    if (Test-Path $marker) {
        $d = (Get-Content $marker -Raw -ErrorAction SilentlyContinue).Trim()
        if ($d -and (Test-Path (Join-Path $d 'nginx.exe'))) { return $d }
    }
    foreach ($c in @((Join-Path ${env:LOCALAPPDATA} 'nginx'),
                     (Join-Path ${env:ProgramFiles} 'nginx'),
                     'D:\nginx')) {
        if (Test-Path (Join-Path $c 'nginx.exe')) { return $c }
    }
    return $null
}

# --------------------------------------------------------------- 服务定义
# --------------------------------------------------------------- 服务定义
# 把 -Service 拆成数组。
#
# 为什么不在参数上写 ValidateSet + [string[]]：PowerShell 的 **-File 调用方式
# 不解析逗号**。从 PowerShell 交互式执行 `-Service Web,Scheduler` 能被解析成数组，
# 但 `powershell -File x.ps1 -Service Web,Scheduler`（例如从 Python 的
# subprocess 调用）传进去的是**一个整体字符串** "Web,Scheduler"，
# 对 [string[]] 只会包成单元素数组，随即被 ValidateSet 拒绝：
#   The argument "Web,Scheduler" does not belong to the set "All,Web,..."
# 这个坑真实出现过：sync_symbols 调start 时服务没起来，脚本却报告"已恢复"。
# 因此这里接收单个字符串、自行按逗号拆分，与调用方式无关。
$serviceList = @(
    $Service -split ',' |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ } |
    Select-Object -Unique
)
foreach ($s in $serviceList) {
    if ($s -notin @('All', 'Web', 'Scheduler', 'Nginx')) {
        Write-Host "  [X]  -Service 取值非法：$s（可选 All / Web / Scheduler / Nginx）" -ForegroundColor Red
        Write-Host "      多项请用逗号分隔，例如 -Service Web,Scheduler" -ForegroundColor Yellow
        exit 1
    }
}

$envFile = Join-Path $scriptDir 'env.local.cmd'
$webPort = [int](Get-EnvVarFromFile $envFile 'QUANT_PORT' '8000')

$defs = @(
    # ⚠️ Web 必须**同时**匹配 run_intraday_updater：
    #   分时采样是 Web 拉起的子进程，且**继承**了 stdout/stderr 重定向，
    #   也就是继承了 logs\web.log 的文件句柄。杀掉 waitress 后它仍会存活，
    #   导致 web.log 一直被锁 —— 下次 start_web.cmd 执行 `>> web.log` 时报
    #   "The process cannot access the file because it is being used by
    #    another process."，waitress 秒退，**重启必失败**（实测踩过）。
    @{ Key = 'Web';       Task = 'QuantEngine-Web';
       Patterns = @('waitress', 'run_intraday_updater'); Desc = "Web(waitress) :$webPort" },
    @{ Key = 'Scheduler'; Task = 'QuantEngine-Scheduler';
       Patterns = @('run_scheduler'); Desc = '调度器' },
    @{ Key = 'Nginx';     Task = 'QuantEngine-Nginx';
       Patterns = @('nginx'); Desc = 'nginx 反向代理 :8080' }
)
if ($serviceList -notcontains 'All') {
    # 支持 -Service Web,Scheduler 这样一次操作多个
    $defs = @($defs | Where-Object { $serviceList -contains $_.Key })
}

# --------------------------------------------------------------- 停止逻辑
# ⚠️ restart 与 stop 必须走**同一个**函数：只Stop-ScheduledTask 是不够的。
#
# run_hidden.vbs 用 WshShell.Run 启动子进程，而该方式创建的过程**不在计划任务的
# 作业对象（Job Object）内**，因此 Stop-ScheduledTask 只终止 wscript.exe 本身，
# 真正的服务进程会活下来变成孤儿：端口仍被占用，随后 Start-ScheduledTask 起不来
# （waitress 绑定端口失败退出 → 任务状态变成 Ready 而非 Running）。
#
# 实测踩过：restart 报「已启动、Web 已就绪」，实际上老进程一个没停。
# 因此停止动作必须包含"按命令行特征清理残留进程"这一步。
function Stop-Def {
    param($Def)
    $state = Get-TaskState $Def.Task
    if ($null -ne $state) {
        Stop-ScheduledTask -TaskName $Def.Task -ErrorAction SilentlyContinue
        Write-Ok "已停止计划任务：$($Def.Task)"
        Start-Sleep -Seconds 2
    }
    if ($Def.Key -eq 'Nginx') {
        # nginx 是常驻 master，必须 -s quit 才会正常退出
        $ng = Resolve-NginxDir
        if ($ng) {
            [void](Invoke-Native -FilePath (Join-Path $ng 'nginx.exe') `
                    -Arguments @('-p', "$ng\", '-c', 'conf/nginx.conf', '-s', 'quit') -Quiet)
        }
    }
    $n = Stop-ProcessByPattern -Patterns $Def.Patterns -What $Def.Key
    if ($n -gt 0) { Write-Ok "已清理 $n 个残留 $($Def.Key) 进程" }
    return $n
}

# ------------------------------------------------------------------ 主流程
Write-Step "量化引擎服务管理 — $Action / $($serviceList -join ',')"

if ($Action -eq 'status') {
    foreach ($d in $defs) {
        $state = Get-TaskState $d.Task
        if ($null -eq $state) {
            if ($d.Key -eq 'Nginx') {
                $ng = Resolve-NginxDir
                if ($ng) { Write-Warn2 "$($d.Desc)：未注册计划任务，但检测到 $ng" }
                else { Write-Warn2 "$($d.Desc)：未安装（跳过）" }
            } else {
                Write-Err "$($d.Desc)：计划任务 $($d.Task) 不存在，请先运行 install.ps1"
            }
            continue
        }
        $running = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                     Where-Object {
                         $cl = $_.CommandLine
                         if (-not $cl -or $_.Name -match 'powershell|pwsh') { return $false }
                         foreach ($pat in $d.Patterns) { if ($cl -like "*$pat*") { return $true } }
                         return $false
                     })
        $portMark = ''
        if ($d.Key -eq 'Web')       { $portMark = if (Test-PortListening $webPort) { "端口 $webPort 已监听" } else { "端口 $webPort 未监听" } }
        if ($d.Key -eq 'Nginx')     { $portMark = if (Test-PortListening 8080) { '端口 8080 已监听' } else { '端口 8080 未监听' } }
        Write-Host ("  {0,-10} 任务={1,-9} 进程数={2} {3}" -f $d.Key, $state, $running.Count, $portMark) -ForegroundColor Yellow
    }
    return
}

# ---------------------------------------------------------------- start
if ($Action -eq 'start' -or $Action -eq 'restart') {
    if ($Action -eq 'restart') {
        Write-Step '停止现有实例'
        foreach ($d in $defs) { [void](Stop-Def -Def $d) }
        # 确认端口真的空了再启动，否则 waitress 会因绑定失败静默退出
        if ($defs.Key -contains 'Web' -and (Test-PortListening $webPort)) {
            Start-Sleep -Seconds 3
            if (Test-PortListening $webPort) {
                Write-Err "端口 $webPort 仍被占用，无法重启 Web。请手工排查占用进程。"
                exit 1
            }
            Write-Ok "端口 $webPort 已释放"
        }
    }

    Write-Step '启动服务'
    foreach ($d in $defs) {
        $state = Get-TaskState $d.Task
        if ($null -eq $state) {
            if ($d.Key -eq 'Nginx') {
                $ng = Resolve-NginxDir
                $startCmd = Join-Path $scriptDir 'start_nginx.cmd'
                if ($ng -and (Test-Path $startCmd)) {
                    Write-Ok "启动 $($d.Desc)（未注册计划任务，直接运行 start_nginx.cmd）"
                    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', "`"$startCmd`"") `
                                  -WorkingDirectory $ng -WindowStyle Hidden | Out-Null
                    Start-Sleep -Seconds 2
                } else {
                    Write-Warn2 "$($d.Desc)：未安装，跳过"
                }
            } else {
                Write-Err "$($d.Desc)：计划任务 $($d.Task) 不存在，请先运行 install.ps1"
            }
            continue
        }
        Start-ScheduledTask -TaskName $d.Task
        Write-Ok "已启动 $($d.Desc)"
    }

    if ($defs.Key -contains 'Web' -and $WaitSeconds -gt 0) {
        Write-Step "等待服务就绪（最多 ${WaitSeconds}s）"
        if (Wait-Port $webPort $WaitSeconds) {
            Write-Ok "Web 已就绪：端口 $webPort 正在监听"
        } else {
            Write-Err "Web 未在 ${WaitSeconds}s 内就绪。"
            Write-Host ''
            Write-Host '      常见原因——logs\web.log 被残留进程锁住：' -ForegroundColor Yellow
            Write-Host '        分时采样子进程 run_intraday_updater 由 Web 拉起，会继承 stdout 重定向，'
            Write-Host '        也就是持有 logs\web.log 的文件句柄。Web 被杀后它若仍存活，'
            Write-Host '        下次 `>> logs\web.log` 会失败：'
            Write-Host '          "The process cannot access the file because it is being used by another process."'
            Write-Host '        waitress 随即秒退，表现为「任务秒退、状态 Ready、端口未监听」。'
            Write-Host ''
            Write-Host '      处理办法（本脚本 -Action stop 已包含该清理；若仍未生效可手工执行）：' -ForegroundColor Yellow
            Write-Host "        Get-CimInstance Win32_Process |"
            Write-Host "          Where-Object { `$_.CommandLine -like '*run_intraday_updater*' } |"
            Write-Host "          ForEach-Object { Stop-Process -Id `$_.ProcessId -Force }"
            Write-Host ''
            Write-Host '      另外请查看：logs\web.log 尾部与计划任务的 LastTaskResult' -ForegroundColor Yellow
        }
    }
    return
}

# ----------------------------------------------------------------- stop
Write-Step '停止服务并清理残留进程'
foreach ($d in $defs) {
    $n = Stop-Def -Def $d
    if ($n -eq 0 -and $null -eq (Get-TaskState $d.Task)) {
        Write-Warn2 "$($d.Desc)：无运行中的实例"
    }
}
Write-Ok '完成'