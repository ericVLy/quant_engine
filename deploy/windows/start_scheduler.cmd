@echo off
rem ============================================================================
rem 启动策略调度器（Windows 单机）
rem
rem 职责：到点触发已发布 Plan -> SuiteRun -> 下单链路。
rem
rem 注意（单机部署约束）：只允许一个调度器实例，多开会重复触发 Plan。
rem 由计划任务 "QuantEngine-Scheduler" 在用户登录时自动调用。
rem
rem 本文件必须为 CRLF 行尾，编码 GBK。
rem ============================================================================
setlocal

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

if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"
set "LOG_FILE=%PROJECT_DIR%logs\scheduler.log"

echo [%date% %time%] 启动调度器（interval=%QUANT_SCHED_INTERVAL%s workers=%QUANT_SCHED_WORKERS%）
echo [%date% %time%] 日志：%LOG_FILE%
"%PROJECT_DIR%.venv\Scripts\python.exe" manage.py run_scheduler --interval %QUANT_SCHED_INTERVAL% --workers %QUANT_SCHED_WORKERS% >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] 调度器已退出（rc=%RC%），日志：%LOG_FILE%
popd
exit /b %RC%
