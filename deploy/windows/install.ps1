<#
    Windows 单机生产部署安装脚本（2026-10-14）

    执行：powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install.ps1

    做的事：
      1. 环境自检（venv / MariaDB 服务 / Node 可选）
      2. 生成 deploy\windows\env.local.cmd（**交互式询问数据库口令，口令不写入本脚本**）
      3. 安装 waitress（Gunicorn 不支持 Windows）
      4. 建库（若不存在）+ migrate
      5. 构建前端（static/ 被 gitignore，全新克隆必须先构建）
      6. collectstatic
      7. 注册开机自启计划任务（Web + Scheduler / nginx 可选）
      8. 输出后续步骤（初始化超级管理员）

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

    # 跳过 collectstatic（默认会执行；它与 migrate 相互独立）
    [switch]$SkipStatic,

    # 跳过前端构建（static/ 是构建产物且被 gitignore，全新克隆必须构建）
    [switch]$SkipFrontend,

    # 装依赖时优先使用 uv（快很多）；-NoUv 强制走 pip
    [switch]$NoUv,

    # 按 requirements.txt 全量安装/校准依赖（默认只补装 waitress）
    [switch]$SyncDeps,

    # 计划任务的权限级别：Limited（**默认**，普通账户即可，无需管理员）
    #                     Highest（需管理员；本项目服务本身不需要）
    [ValidateSet('Limited', 'Highest')]
    [string]$RunLevel = 'Limited'
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Resolve-Path (Join-Path $scriptDir '..\..')
$venvPython = Join-Path $projectDir '.venv\Scripts\python.exe'
$envFile    = Join-Path $scriptDir 'env.local.cmd'
$logDir     = Join-Path $projectDir 'logs'
$reqFile    = Join-Path $projectDir 'requirements.txt'
# 隐藏窗口启动器（wscript 壳）；缺失则回退为直接调 cmd.exe
$vbsLauncher = Join-Path $scriptDir 'run_hidden.vbs'

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

function Invoke-Native {
    <#
        调用外部命令并返回其退出码。

        为什么需要它：PowerShell 会把**子进程的 stderr 行**包装成
        NativeCommandError 记录；而 nginx / vite / npm 这类工具**把正常的
        进度与警告也写到 stderr**。在 $ErrorActionPreference='Stop'（本脚本
        顶部设定）下，那会被当成致命错误直接终止脚本——即使命令本身成功。

        实测踩过两次：
          - nginx -t 成功时输出 "syntax is ok"（走 stderr）→ 被中断；
          - vite build 成功（rc=0），但 chunk>500kB 的警告走 stderr → 被中断。

        因此这里临时把错误策略放宽为 Continue，用退出码判断成败，
        执行完在 finally 中恢复。
    #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory,
        [switch]$PassThruOutput
    )

    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        Push-Location -LiteralPath $WorkingDirectory -ErrorAction SilentlyContinue
        try {
            if ($PassThruOutput) {
                & $FilePath @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" }
            } else {
                & $FilePath @Arguments 2>&1 | Out-Null
            }
        } finally {
            Pop-Location -ErrorAction SilentlyContinue
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevPref
    }
}

function Invoke-Django {
    <#
        在载入生产环境变量后执行一条 Django 管理命令，返回是否成功。
        直接 call 真正的 env.local.cmd —— 不再生成临时 .cmd：
        早前版本用 `Get-Content | -join "`n" | Set-Content` 复制一份，
        既会写出 LF 行尾（cmd 解析不可靠），又会因ASCII 编码毁掉中文注释。
    #>
    param([string]$DjangoArgs)

    $errPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & cmd.exe /c "call `"$envFile`" && cd /d `"$projectDir`" && `"$venvPython`" manage.py $DjangoArgs"
        $rc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $errPref
    }
    return ($rc -eq 0)
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

    # uv 与 pip 都可能把进度/警告写到 stderr，同样按退出码判断，
    # 不能让$ErrorActionPreference='Stop' 把它们当成错误。
    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($tool -eq 'uv') { & $uv @cmdArgs } else { & $venvPython @cmdArgs }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevPref
    }
}

# ---------------------------------------------------------------- 1. 自检
Write-Step '1/8 环境自检'

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
Write-Step '2/8 生成 env.local.cmd'

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
# ⚠️ 必须显式指定 **GBK 编码 + CRLF 行尾**：
#   - 编码：文件里有中文注释，需按 ANSI(GBK) 写出，否则 cmd 里显示乱码；
#   - 行尾：here-string 内部换行是 LF，直接 Set-Content 会写出 **LF-only** 的
#     .cmd，而 cmd 对 LF-only 批处理的解析不可靠（start_web.cmd 就栽在这里）。
$envBodyCrlf = $envBody.Replace("`r`n", "`n").Replace("`n", "`r`n")
[System.IO.File]::WriteAllText($envFile, $envBodyCrlf, [System.Text.Encoding]::GetEncoding(936))
Write-Ok "已写入 $envFile（GBK 编码 + CRLF 行尾）"

# ------------------------------------------- 3. 安装 Python 依赖（uv 优先）
Write-Step '3/8 安装 Python 依赖'

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
    Write-Step '4/8 数据库迁移'

    $mariaBin = Get-ChildItem 'C:\Program Files\MariaDB*\bin\mariadb.exe' -ErrorAction SilentlyContinue |
               Select-Object -First 1
    if ($mariaBin) {
        foreach ($db in @('quant_engine', 'quant_kline')) {
            $sql = "CREATE DATABASE IF NOT EXISTS ``$db`` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            $env:MYSQL_PWD = $dbPassword
            $rc = Invoke-Native -FilePath $mariaBin.FullName -Arguments @('-u', $dbUser, '-e', $sql)
            if ($rc -eq 0) { Write-Ok "数据库 $db 就绪" }
            else { Write-Warn2 "数据库 $db 可能已存在或创建失败（继续）" }
        }
        Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
    } else {
        Write-Warn2 '未找到 mariadb.exe，跳过建库（若库不存在请手工创建）'
    }

    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

    if (-not (Invoke-Django 'migrate --noinput')) {
        Write-Err 'migrate 失败，请检查数据库连接配置（口令 / 库名 / 服务是否启动）'
        exit 1
    }
    Write-Ok 'migrate 完成'
} else {
    Write-Warn2 '已跳过 migrate（-SkipMigrate）'
}

# ------------------------------------------------- 5/7 前端构建 + 静态资源
# ⚠️ static/ 目录被 .gitignore 排除（构建产物），**全新克隆里根本没有前端**。
# 不先构建，collectstatic 只能收集到 admin/DRF 的样式，前端会白屏。
# 这一步与 migrate 相互独立：修计划任务时用 -SkipMigrate 重跑，
# 不应连带把前端构建也跳过。
if (-not $SkipFrontend) {
    Write-Step '5/8 构建前端'

    $frontDir = Join-Path $projectDir 'quant-frontend'
    if (-not (Test-Path (Join-Path $frontDir 'package.json'))) {
        Write-Err "未找到 quant-frontend\package.json：$frontDir"
        exit 1
    }

    $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npmCmd) {
        Write-Err '未找到 npm。请安装 Node.js LTS 后重试，或手工在 quant-frontend 目录执行 npm run build。'
        exit 1
    }
    Write-Ok "npm: $((& npm --version))"

    if (-not (Test-Path (Join-Path $frontDir 'node_modules'))) {
        Write-Ok '安装前端依赖（首次较慢）…'
        $rc = Invoke-Native -FilePath 'npm.cmd' -Arguments @('ci', '--no-audit', '--no-fund') `
                            -WorkingDirectory $frontDir -PassThruOutput
        if ($rc -ne 0) {
            Write-Warn2 'npm ci 失败，回退到 npm install'
            $rc = Invoke-Native -FilePath 'npm.cmd' -Arguments @('install', '--no-audit', '--no-fund') `
                                -WorkingDirectory $frontDir -PassThruOutput
        }
        if (-not (Test-Path (Join-Path $frontDir 'node_modules'))) {
            Write-Err '前端依赖安装失败，无法继续构建。'
            exit 1
        }
    } else {
        Write-Ok 'node_modules 已存在，跳过依赖安装'
    }

    # 只跑 vite build，跳过 vue-tsc 类型检查（部署不因类型报错而中断）。
    # 注意：vite 会把chunk 体积警告写到 stderr，必须走 Invoke-Native，
    # 否则成功构建会被误判为致命错误（见 Invoke-Native 注释）。
    $buildRc = Invoke-Native -FilePath 'npx.cmd' -Arguments @('vite', 'build') `
                             -WorkingDirectory $frontDir -PassThruOutput

    $indexPath = Join-Path $projectDir 'static\index.html'
    if ($buildRc -ne 0 -or -not (Test-Path $indexPath)) {
        Write-Err '前端构建失败或未产出 static\index.html。'
        Write-Err '生产环境缺少前端产物会导致页面白屏，不作为"可忽略的警告"处理。'
        exit 1
    }

    # 关键校验：前端产物必须用**绝对路径**引用资源。
    # 若 index.html 里出现 ./assets/（base:'./'），在 history 模式的深层路由
    # （如 /plans/123）刷新时，浏览器会把 ./assets/x.js 解析成
    # /plans/assets/x.js，落进后端 catch-all 拿到 index.html（Content-Type: text/html），
    # 脚本解析失败 → 白屏，且**不报 404**，极难排查。
    $indexHtml = Get-Content $indexPath -Raw -Encoding UTF8
    if ($indexHtml -match '"\./assets/') {
        Write-Err 'static\index.html 里出现相对路径 ./assets/ —— 与 history 路由不兼容。'
        Write-Err '请检查 quant-frontend\vite.config.ts 的 base 是否为 '/'，然后重新构建。'
        exit 1
    }
    Write-Ok "前端构建完成（已校验为绝对路径引用：/assets/…）"
} else {
    Write-Warn2 '已跳过前端构建（-SkipFrontend）'
}

if (-not $SkipStatic) {
    Write-Step '6/8 收集静态资源（collectstatic）'
    if (-not (Invoke-Django 'collectstatic --noinput')) {
        Write-Err 'collectstatic 失败。生产环境缺静态文件会导致前端与 admin 样式失效，'
        Write-Err '不作为"可忽略的警告"处理。请先修复再启动服务。'
        exit 1
    }
    $collected = @(Get-ChildItem (Join-Path $projectDir 'staticfiles') -Recurse -File -ErrorAction SilentlyContinue)
    Write-Ok "collectstatic 完成（staticfiles 下共 $($collected.Count) 个文件）"
} else {
    Write-Warn2 '已跳过 collectstatic（-SkipStatic）'
}

# ------------------------------------------------- 5. 注册开机自启计划任务
Write-Step "7/8 注册开机自启计划任务（触发：$Trigger / 权限：$RunLevel）"

$tasks = @(
    @{ Name = 'QuantEngine-Web';      Script = 'start_web.cmd';      Desc = '量化引擎 Web 服务（waitress）' },
    @{ Name = 'QuantEngine-Scheduler'; Script = 'start_scheduler.cmd'; Desc = '量化引擎 策略调度器' }
)

# 提权自检：RunLevel=Highest（或 OnStart 的 SYSTEM）需要管理员权限，
# 普通账户会抛 0x80070005 Access is denied。先探测，好提前给出可执行的提示。
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$needAdmin = ($RunLevel -eq 'Highest' -or $Trigger -eq 'OnStart')
if ($needAdmin -and -not $isAdmin) {
    Write-Warn2 "当前账户不是管理员，而「$RunLevel + $Trigger」需要管理员权限。"
    Write-Host "        请用**管理员身份**重开 PowerShell 后重跑本脚本，或改用：" -ForegroundColor Yellow
    Write-Host "          .\install.ps1 -RunLevel Limited -Trigger OnLogon" -ForegroundColor Yellow
    exit 1
}
if (-not $isAdmin) {
    Write-Ok '当前为普通账户，使用 RunLevel=Limited（本项目无需管理员权限）'
}

# 隐藏窗口启动器自检：缺失则回退到 cmd.exe（会弹窗，但不至于注册出坏任务）
$useVbs = $true
if (-not (Test-Path $vbsLauncher)) {
    Write-Warn2 "缺少 $vbsLauncher，自启时将弹出控制台窗口。"
    Write-Warn2 '（该文件应随仓库一起提交；若丢失可从 git 恢复）'
    $useVbs = $false
} else {
    Write-Ok '自启将以隐藏窗口方式运行（run_hidden.vbs）'
}

foreach ($t in $tasks) {
    $full = Join-Path $scriptDir $t.Script
    Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue

    # 用 run_hidden.vbs 以隐藏窗口方式启动，避免登录自启时弹出黑色控制台。
    # 该 vbs 会**阻塞等待**子进程结束，任务状态因此保持「正在运行」，
    # 任务设置里的「失败后重启」才会生效（若用异步启动，WScript 立即退出，
    # 任务会被判定为已完成）。
    if ($useVbs) {
        $action = New-ScheduledTaskAction -Execute 'wscript.exe' `
            -Argument "`"$vbsLauncher`" `"$full`"" -WorkingDirectory $projectDir
    } else {
        $action = New-ScheduledTaskAction -Execute 'cmd.exe' `
            -Argument "/c `"$full`"" -WorkingDirectory $projectDir
    }
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel $RunLevel
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    if ($Trigger -eq 'OnLogon') {
        $triggerObj = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    } else {
        $triggerObj = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' `
            -LogonType ServiceAccount -RunLevel Highest
    }

    try {
        Register-ScheduledTask -TaskName $t.Name `
            -Action $action -Trigger $triggerObj -Settings $settings -Principal $principal `
            -Description $t.Desc -Force | Out-Null
    } catch {
        Write-Err "注册 $($t.Name) 失败：$($_.Exception.Message)"
        Write-Host "        多数情况是权限不足（0x80070005）。请用管理员身份重开 PowerShell 后重跑，" -ForegroundColor Yellow
        Write-Host "        或加 -RunLevel Limited -Trigger OnLogon 以普通权限注册。" -ForegroundColor Yellow
        exit 1
    }
    Write-Ok "已注册：$($t.Name)"
}

# ------------------------------------------------------------ 6. 收尾提示
Write-Step '8/8 完成'
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
