<#
    nginx 反向代理安装（Windows 单机生产部署）

    执行：
        powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install_nginx.ps1

    做的事：
      1. 下载并解压 nginx 官方 Windows 版（已存在则跳过）
      2. 用 deploy\windows\nginx.conf.template 渲染配置到 <NginxDir>\conf\quant_engine.conf
      3. nginx -t 校验语法
      4. 注册开机自启计划任务 QuantEngine-Nginx

    与 install.ps1 的关系：两者**独立**。
        不装 nginx 时，waitress 单端口也能同时提供前端与 API（生产构建的
        VITE_API_BASE_URL 是相对路径 /api，同源直连即可）。
        装 nginx 的收益是：静态资源直出不必回源、支持局域网访问、
        以及 SSE 有独立的缓冲策略。

    nginx 在 Windows 上不是真正的系统服务，因此同样用计划任务在登录时拉起。
#>
param(
    # nginx 安装目录。默认用**用户可写**的位置：C:\Program Files 需要管理员，
    # 普通账户会在解压搬运时报 "Access to the path 'nginx' is denied"。
    [string]$NginxDir = "$env:LOCALAPPDATA\nginx",

    # nginx 对外监听端口
    [int]$ListenPort = 8080,

    # 后端 waitress 端口（须与 start_web.cmd 的 QUANT_PORT 一致）
    [int]$BackendPort = 8000,

    # 允许下载的版本
    [string]$NginxVersion = '1.30.5',

    # 已有 nginx 时跳过下载
    [switch]$SkipDownload
)

$ErrorActionPreference = 'Stop'
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Resolve-Path (Join-Path $scriptDir '..\..')
$template   = Join-Path $scriptDir 'nginx.conf.template'

function Write-Step($msg)  { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Write-Err($msg)   { Write-Host "  [X]  $msg" -ForegroundColor Red }

# ------------------------------------------------------------- 1/5 自检
Write-Step '1/5 自检'
if (-not (Test-Path $template)) {
    Write-Err "缺少配置模板：$template"
    exit 1
}
$nginxExe = Join-Path $NginxDir 'nginx.exe'
Write-Ok "项目目录：$projectDir"
Write-Ok "nginx 目录：$NginxDir"

# 可写性前置检查（放在下载之前，避免白下一份才开始失败）。
# 注意：这里**只警告不阻断**——探针可能因临时占用等原因误报，
# 真正不可写时由后面的解压/搬运给出带指引的错误。
# （上次用户失败在 C:\Program Files：Move-Item "Access to the path 'nginx' is denied"。）
if (-not (Test-Path $NginxExe)) {
    $probeDir = $NginxDir
    if (-not (Test-Path $probeDir)) { $probeDir = Split-Path -Parent $NginxDir }
    try {
        $probe = Join-Path $probeDir '.nginx_write_probe.tmp'
        Set-Content -Path $probe -Value 'probe' -Encoding ASCII -ErrorAction Stop
        Remove-Item $probe -Force -ErrorAction SilentlyContinue
        Write-Ok '目录可写'
    } catch {
        Write-Warn2 "目录可能不可写：$probeDir"
        Write-Host "        $probeDir 若需要管理员权限（如 C:\Program Files），建议改用：" -ForegroundColor Yellow
        Write-Host '          -NginxDir "$env:LOCALAPPDATA\nginx"' -ForegroundColor Yellow
        Write-Host '        先继续尝试，若搬运失败会给出明确提示。' -ForegroundColor Yellow
    }
}

# --------------------------------------------- 2/5 下载并解压（不存在时）
if (-not (Test-Path $nginxExe)) {
    if ($SkipDownload) {
        Write-Err "指定了 -SkipDownload 但 $nginxExe 不存在。"
        exit 1
    }
    Write-Step "2/5 下载 nginx $NginxVersion（Windows 官方版）"
    $zipUrl = "https://nginx.org/download/nginx-$NginxVersion.zip"
    $zip = Join-Path $env:TEMP "nginx-$NginxVersion.zip"
    try {
        Write-Host "  下载：$zipUrl"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing -TimeoutSec 300
        Write-Ok ("下载完成 {0:N2} MB" -f ((Get-Item $zip).Length / 1MB))
    } catch {
        Write-Err "下载失败：$($_.Exception.Message)"
        Write-Host "        可手动下载 $zipUrl 后解压到 $NginxDir 再重跑本脚本。" -ForegroundColor Yellow
        exit 1
    }

    $stage = Join-Path $env:TEMP "nginx_stage_$NginxVersion"
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $stage -Force
    $extracted = Get-ChildItem $stage -Directory | Select-Object -First 1
    if (-not $extracted) { Write-Err '解压结果异常，未找到 nginx 目录'; exit 1 }

    $parent = Split-Path -Parent $NginxDir
    if (-not (Test-Path $parent)) {
        try { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        catch { Write-Err "无法创建上级目录 $parent：$($_.Exception.Message)"; exit 1 }
    }
    if (Test-Path $NginxDir) {
        try { Remove-Item $NginxDir -Recurse -Force }
        catch { Write-Err "无法清理已存在的 $NginxDir：$($_.Exception.Message)"; exit 1 }
    }
    try {
        Move-Item $extracted.FullName $NginxDir
    } catch {
        Write-Err "搬运到 $NginxDir 失败：$($_.Exception.Message)"
        Write-Host '        多为目录权限问题。改用用户可写目录重试：' -ForegroundColor Yellow
        Write-Host "          -NginxDir `"`$env:LOCALAPPDATA\nginx`"" -ForegroundColor Yellow
        exit 1
    }
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "已解压到 $NginxDir"
} else {
    Write-Warn2 "已存在 nginx，跳过下载：$nginxExe"
    Write-Step '2/5 下载 nginx（跳过）'
}

# ------------------------------------------------------- 3/5 渲染配置
Write-Step '3/5 生成 nginx 配置'
$staticfiles = Join-Path $projectDir 'staticfiles'
if (-not (Test-Path $staticfiles)) {
    Write-Warn2 "未找到 $staticfiles（/assets/ 会 404）。请先跑 install.ps1 的 collectstatic。"
}
# nginx 用正斜杠路径
$staticfilesPosix = $staticfiles.Replace('\', '/')

$conf = Get-Content $template -Raw -Encoding UTF8
$conf = $conf.Replace('__BACKEND_PORT__', [string]$BackendPort)
$conf = $conf.Replace('__LISTEN_PORT__', [string]$ListenPort)
$conf = $conf.Replace('__PROJECT_STATICFILES__', $staticfilesPosix)

$confDir  = Join-Path $NginxDir 'conf'
$confPath = Join-Path $confDir 'quant_engine.conf'
# ⚠️ 必须写成 **UTF-8 无 BOM**：
#   - `Set-Content -Encoding UTF8` 在 PowerShell 5.1 下会写入 BOM；
#   - nginx 解析到 BOM 会报 `unknown directive "锘?"`（BOM 被当 GBK 解码后的产物）。
# 因此这里直接用 .NET 的 UTF8Encoding($false)。
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($confPath, $conf, $utf8NoBom)
# nginx.conf 只 include 我们这份，避免与发行版默认站点冲突（默认监听 80 端口）
[System.IO.File]::WriteAllText((Join-Path $confDir 'nginx.conf'), "include quant_engine.conf;`r`n", $utf8NoBom)
Write-Ok "已写入 $confPath"
Write-Ok "监听端口 $ListenPort → 后端 127.0.0.1:$BackendPort"
Write-Ok "静态目录 $staticfilesPosix"

# --------------------------------------------------------- 4/5 语法校验
Write-Step '4/5 校验配置语法'
# ⚠️ nginx 把**所有**输出（含 "syntax is ok"）都写到 stderr。PowerShell 会把
# stderr 行包装成 NativeCommandError，在 $ErrorActionPreference='Stop' 下
# **直接抛异常终止脚本**——即使 nginx 校验成功。
# 因此这里临时放宽错误策略，改用退出码判断成败（nginx 成功返回 0）。
$prevPref = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $testOut = & $nginxExe -t -p "$NginxDir/" -c 'conf/nginx.conf' 2>&1
    $testRc = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevPref
}
$testOut = @($testOut | ForEach-Object { $_.ToString() })
if ($testRc -ne 0) {
    $testOut | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    Write-Err 'nginx 配置校验失败，未注册自启。'
    exit 1
}
$testOut | ForEach-Object { Write-Host "  $_" }
Write-Ok '配置语法正确'

# ----------------------------------------------------- 5/5 注册自启
Write-Step '5/5 注册开机自启计划任务'
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

$taskName = 'QuantEngine-Nginx'
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute $nginxExe `
    -Argument "-p `"$NginxDir/`" -c conf/nginx.conf" -WorkingDirectory $NginxDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description '量化引擎 nginx 反向代理' -Force | Out-Null
    Write-Ok "已注册：$taskName"
} catch {
    Write-Err "注册失败：$($_.Exception.Message)"
    Write-Host '        请以管理员身份重开终端后重试，或手动添加开机启动项。' -ForegroundColor Yellow
    exit 1
}

if (-not $isAdmin) {
    Write-Host '  提示：nginx 监听 80 端口（低端口）需要管理员；本次用的是 8080，故无需提权。' -ForegroundColor Yellow
}

Write-Host @"

  立即启动：
    Start-ScheduledTask -TaskName 'QuantEngine-Nginx'
    或直接运行：& '$nginxExe' -p '$NginxDir/' -c conf/nginx.conf

  访问地址：
    http://127.0.0.1:$ListenPort/

  重要 —— 局域网访问需要补两项配置（改 env.local.cmd 后重启 Web）：
    set ALLOWED_HOSTS=127.0.0.1,localhost,<本机局域网IP>
    set CSRF_TRUSTED_ORIGINS=http://<本机局域网IP>:$ListenPort
  否则浏览器从域名/IP 访问时，POST 会被 CSRF 判为跨站而拒绝。

  日志：$NginxDir\logs\access.log 与 error.log

  停止 nginx：
    & '$nginxExe' -p '$NginxDir/' -c conf/nginx.conf -s quit

"@ -ForegroundColor Yellow