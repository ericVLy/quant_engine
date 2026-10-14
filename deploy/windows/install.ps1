<#
    Windows 单机生产部署安装脚本（2026-10-14）

    执行：powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install.ps1

    做的事：
      1. 环境自检（venv / MariaDB 服务 / Node 可选）
      2. 生成 deploy\windows\env.local.cmd（**交互式询问数据库口令，口令不写入本脚本**）
      3. 安装 waitress（Gunicorn 不支持 Windows）
      4. 建库（若不存在）+ migrate + collectstatic
      5. 注册开机自启计划任务（Web + Scheduler）
      6. 输出后续步骤（初始化超级管理员）

    幂等：重复执行安全；已注册的任务会先删除再重建。
#>
param(
    # 计划任务触发方式：
    #   OnLogon = 用户登录时启动（**默认**）
    #   OnStart = 系统启动时以当前用户运行（未登录也起，但 gm 终端通常不可达）
    [ValidateSet('OnLogon', 'OnStart')]
    [string]$Trigger = 'OnLogon',

    # Web 监听端口
    [int]$Port = 8000,

    # 监听地址。默认只听本机；如需局域网访问改为 0.0.0.0（注意 ALLOWED_HOSTS 需同步）
    [string]$Host_ = '127.0.0.1',

    # 跳过 migrate（仅重装服务时用）
    [switch]$SkipMigrate,

    # 装依赖时优先使用 uv（快很多）；-NoUv 强制走 pip
    [switch]$NoUv,

    # 按 requirements.txt 全量安装/校准依赖（默认只补装 waitress）
    [switch]$SyncDeps
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Resolve-Path (Join-Path $scriptDir '..\..')
$venvPython = Join-Path $projectDir '.venv\Scripts\python.exe'
$envFile    = Join-Path $scriptDir 'env.local.cmd'
$logDir     = Join-Path $projectDir 'logs'
$reqFile    = Join-Path $projectDir 'requirements.txt'

function Write-Step($msg)  { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Write-Err($msg)   { Write-Host "  [X]  $msg" -ForegroundColor Red }

# ------------------------------------------------------------ 依赖安装器
# uv（若可用）比 pip 快一个数量级；本项目的 .venv 本身就是 uv 创建的
# （pyvenv.cfg 含 uv = <version>），因此优先走 uv，找不到再回退 pip。
function Resolve-UvPath {
    if ($NoUv) { return $null }
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($cand in @(
            "$env:USERPROFILE\.local\bin\uv.exe",
            "$env:APPDATA\Python\Scripts\uv.exe")) {
        if (Test-Path $cand) { return $cand }
    }
    return $null
}

function Install-Deps {
    <#
        安装 Python 依赖。$packages 为空表示按 requirements.txt 全量同步。
        uv 与 pip 的命令行不同（uv 用 --python 指定解释器；pip 用 -m pip），
        因此参数分别构造，不要混用——混用会让 pip 把 --python 当成文件名。
    #>
    param([string[]]$packages)

    $uv = Resolve-UvPath
    $targets = if ($packages -and $packages.Count -gt 0) { $packages } else { @() }

    if ($uv) {
        $tool = 'uv'
        $cmdArgs = @('pip', 'install')
        if ($targets.Count -eq 0) { $cmdArgs += @('-r', $reqFile) }
        else { $cmdArgs += $targets }
        $cmdArgs += @('--python', $venvPython)   # uv 专有：精确装进目标解释器
    } else {
        $tool = 'python -m pip'
        Write-Warn2 '未找到 uv，改用 pip（速度较慢但结果一致）'
        $cmdArgs = @('-m', 'pip', 'install', '--disable-pip-version-check')
        if ($targets.Count -eq 0) { $cmdArgs += @('-r', $reqFile) }
        else { $cmdArgs += $targets }
        # pip 不认 --python：已由 $venvPython 这个可执行文件本身决定目标环境
    }

    if ($tool -eq 'uv') { & $uv @cmdArgs } else { & $venvPython @cmdArgs }
    return $LASTEXITCODE
}

# ---------------------------------------------------------------- 1. 自检
Write-Step '1/6 环境自检'

if (-not (Test-Path $venvPython)) {
    Write-Err "找不到虚拟环境：$venvPython"
    exit 1
}
Write-Ok "虚拟环境：$venvPython"

$mariaSvc = Get-Service -Name 'MariaDB' -ErrorAction SilentlyContinue
if (-not $mariaSvc) {
    Write-Err '未找到 MariaDB 服务。生产部署需要它（或改用 SQLite，但不推荐）。'
    exit 1
}
if ($mariaSvc.Status -ne 'Running') {
    Write-Warn2 "MariaDB 服务当前为 $($mariaSvc.Status)，尝试启动…"
    Start-Service -Name 'MariaDB'
    Start-Sleep -Seconds 3
}
Write-Ok "MariaDB 服务：$($mariaSvc.Status)（StartType=$($mariaSvc.StartType)）"

# ------------------------------------------------------- 2. 生成环境变量文件
Write-Step '2/6 生成 env.local.cmd'

$dbUser     = Read-Host 'MariaDB 用户名 [root]'
if ([string]::IsNullOrWhiteSpace($dbUser)) { $dbUser = 'root' }

# 口令用 SecureString 读取，不回显、不进脚本内容
$securePwd = Read-Host 'MariaDB 口令（输入不回显）' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePwd)
try {
    $dbPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ([string]::IsNullOrWhiteSpace($dbPassword)) {
    Write-Err '口令为空，终止（避免生成连不上库的部署）。'
    exit 1
}

$klineUser     = Read-Host 'K 线库用户名 [root]'
if ([string]::IsNullOrWhiteSpace($klineUser)) { $klineUser = 'root' }
$klineSecure   = Read-Host 'K 线库口令（留空=与主库相同）' -AsSecureString
$klineBstr     = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($klineSecure)
try {
    $klinePassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($klineBstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($klineBstr)
}
if ([string]::IsNullOrWhiteSpace($klinePassword)) { $klinePassword = $dbPassword }

$gmServAddr = Read-Host 'gm 终端服务地址，如 192.168.1.10:7001（留空=本机终端）'
$allowedHosts = if ($Host_ -eq '0.0.0.0') { '127.0.0.1,localhost' } else { "$Host_,localhost" }

$envBody = @"
@echo off
rem 由 install.ps1 自动生成于 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
rem ** 含数据库口令，请勿提交到 git（已在 .gitignore 中）**
set DJANGO_SETTINGS_MODULE=quant_engine.settings.win_production
set USE_SQLITE=0
set SETUP_ENABLED=0
set DB_NAME=quant_engine
set DB_USER=$dbUser
set DB_PASSWORD=$dbPassword
set DB_HOST=127.0.0.1
set DB_PORT=3306
set KLINE_DB_NAME=quant_kline
set KLINE_DB_USER=$klineUser
set KLINE_DB_PASSWORD=$klinePassword
set KLINE_DB_HOST=127.0.0.1
set KLINE_DB_PORT=3306
set GM_SERV_ADDR=$gmServAddr
set FUNDAMENTALS_ENABLED=1
set MONITORING_UPDATER_ENABLED=1
set MONITORING_UPDATER_INTERVAL=60
set ALLOWED_HOSTS=$allowedHosts
set QUANT_HOST=$Host_
set QUANT_PORT=$Port
set QUANT_THREADS=8
set QUANT_SCHED_INTERVAL=60
set QUANT_SCHED_WORKERS=1
"@
# 用系统 ANSI 代码页（中文 Windows = GBK/936）写出，避免 cmd 里中文注释乱码
Set-Content -Path $envFile -Value $envBody -Encoding Default
Write-Ok "已写入 $envFile（GBK 编码，cmd 可正常显示中文注释）"

# ------------------------------------------- 3. 安装 Python 依赖（uv 优先）
Write-Step '3/6 安装 Python 依赖'

$uvPath = Resolve-UvPath
if ($uvPath) {
    $uvVer = (& $uvPath --version) -join' '
    Write-Ok "检测到 uv：$uvVer（安装器优先使用 uv，比 pip 快一个数量级）"
} elseif ($NoUv) {
    Write-Warn2 '按 -NoUv 要求跳过 uv，使用 pip'
} else {
    Write-Warn2 '未找到 uv，使用 pip。可安装：powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
}

if ($SyncDeps) {
    Write-Ok '按 requirements.txt 全量同步依赖…'
    $rc = Install-Deps -Packages @()
} else {
    $rc = Install-Deps -Packages @('waitress')
}
if ($rc -ne 0) {
    Write-Err '依赖安装失败。可手工重试：uv pip install -r requirements.txt --python .venv\Scripts\python.exe'
    exit 1
}
Write-Ok 'WSGI 服务器 waitress 就绪（Windows 上 Gunicorn 不可用，故用 waitress）'

# ------------------------------------------------------ 4. 建库 + migrate
if (-not $SkipMigrate) {
    Write-Step '4/6 数据库迁移与静态资源'

    $mariaBin = Get-ChildItem 'C:\Program Files\MariaDB*\bin\mariadb.exe' -ErrorAction SilentlyContinue |
               Select-Object -First 1
    if ($mariaBin) {
        foreach ($db in @('quant_engine', 'quant_kline')) {
            $sql = "CREATE DATABASE IF NOT EXISTS ``$db`` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            $env:MYSQL_PWD = $dbPassword
            & $mariaBin.FullName -u $dbUser -e $sql 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { Write-Ok "数据库 $db 就绪" }
            else { Write-Warn2 "数据库 $db 可能已存在或创建失败（继续）" }
        }
        Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
    } else {
        Write-Warn2 '未找到 mariadb.exe，跳过建库（若库不存在请手工创建）'
    }

    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

    # migrate 需要生产环境变量
    $tmpEnv = Join-Path $env:TEMP 'quant_env_probe.cmd'
    $probe  = (Get-Content $envFile -Encoding Default) -join "`n"
    Set-Content -Path $tmpEnv -Value $probe -Encoding ASCII

    cmd /c "call `"$tmpEnv`" && cd /d `"$projectDir`" && `"$venvPython`" manage.py migrate --noinput"
    if ($LASTEXITCODE -ne 0) { Write-Err 'migrate 失败，请检查数据库连接配置'; exit 1 }
    Write-Ok 'migrate 完成'

    cmd /c "call `"$tmpEnv`" && cd /d `"$projectDir`" && `"$venvPython`" manage.py collectstatic --noinput"
    if ($LASTEXITCODE -eq 0) { Write-Ok 'collectstatic 完成' }
    else { Write-Warn2 'collectstatic 失败（前端仍可由 static/ 提供）' }

    Remove-Item $tmpEnv -ErrorAction SilentlyContinue
} else {
    Write-Warn2 '已跳过 migrate（-SkipMigrate）'
}

# ------------------------------------------------- 5. 注册开机自启计划任务
Write-Step "5/6 注册开机自启计划任务（触发：$Trigger）"

$tasks = @(
    @{ Name = 'QuantEngine-Web';      Script = 'start_web.cmd';      Desc = '量化引擎 Web 服务（waitress）' },
    @{ Name = 'QuantEngine-Scheduler'; Script = 'start_scheduler.cmd'; Desc = '量化引擎 策略调度器' }
)

foreach ($t in $tasks) {
    $full = Join-Path $scriptDir $t.Script
    Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue

    $action = New-ScheduledTaskAction -Execute 'cmd.exe' `
        -Argument "/c `"$full`"" -WorkingDirectory $projectDir
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    if ($Trigger -eq 'OnLogon') {
        $triggerObj = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    } else {
        $triggerObj = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    }

    Register-ScheduledTask -TaskName $t.Name `
        -Action $action -Trigger $triggerObj -Settings $settings -Principal $principal `
        -Description $t.Desc -Force | Out-Null
    Write-Ok "已注册：$($t.Name)"
}

# ------------------------------------------------------------ 6. 收尾提示
Write-Step '6/6 完成'
Write-Host @"

  立即启动（不必等下次登录）：
    Start-ScheduledTask -TaskName 'QuantEngine-Web'
    Start-ScheduledTask -TaskName 'QuantEngine-Scheduler'

  查看状态：
    Get-ScheduledTask -TaskName 'QuantEngine*' | Get-ScheduledTaskInfo

  访问地址：
    http://${Host_}:$Port/

  首次使用：打开前端会自动跳 /setup 引导页，创建超级管理员。
  完成初始化后建议把 env.local.cmd 里的 SETUP_ENABLED 保持为 0（已是）。

  日志目录：$logDir
    web-*.log / scheduler-*.log

  注意（单机约束）：
    - 调度器只能有一个实例，多开会重复触发 Plan；
    - 分时采样子进程由 Web 进程自动拉起，**不要**再单独跑 run_intraday_updater；
    - MariaDB 服务需为自动启动（当前：$($mariaSvc.StartType)）。

"@ -ForegroundColor Yellow
