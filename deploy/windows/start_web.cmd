@echo off
rem ============================================================================
rem 启动生产 Web 服务（Windows 单机）
rem
rem 角色：HTTP/WSGI 服务 + 分时采样子进程的父进程（MonitoringConfig.ready 拉起）。
rem 调度器是独立进程，由 start_scheduler.cmd 启动，两者互不重复采样。
rem
rem 由计划任务 "QuantEngine-Web" 在用户登录时自动调用；也可手工运行。
rem
rem 注意：本文件必须为 CRLF 行尾（cmd 的 ^ 续行依赖 CRLF），编码 GBK。
rem ============================================================================
setlocal

rem 末尾带反斜杠，便于拼接子路径
set "PROJECT_DIR=%~dp0..\..\"

if not exist "%PROJECT_DIR%.venv\Scripts\python.exe" (
    echo [ERROR] 找不到虚拟环境：%PROJECT_DIR%.venv\Scripts\python.exe
    exit /b 1
)
if not exist "%~dp0env.local.cmd" (
    echo [ERROR] 缺少 %~dp0env.local.cmd
    echo         请先运行 install.ps1 完成初始化。
    exit /b 1
)

pushd "%PROJECT_DIR%" || exit /b 1
call "%~dp0env.local.cmd"

rem 日志统一写UTF-8：Python 写 stdout 的编码默认随区域设置变化，
rem 固定后用 Get-Content -Encoding UTF8 读即为正常中文。
set "PYTHONIOENCODING=utf-8"

if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"
set "LOG_FILE=%PROJECT_DIR%logs\web.log"

echo [%date% %time%] 启动 Web（waitress）监听 %QUANT_HOST%:%QUANT_PORT%
echo [%date% %time%] 日志：%LOG_FILE%
"%PROJECT_DIR%.venv\Scripts\python.exe" -m waitress --listen=%QUANT_HOST%:%QUANT_PORT% --threads=%QUANT_THREADS% quant_engine.wsgi:application >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] Web 已退出（rc=%RC%），日志：%LOG_FILE%
popd
exit /b %RC%
