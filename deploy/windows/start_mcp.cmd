@echo off
rem ============================================================================
rem 量化引擎 MCP 服务（Windows 常驻）
rem
rem 用途：为 AI 助手 / 外部客户端提供 MCP (SSE) 接入端点
rem       默认端点：http://127.0.0.1:8765/sse
rem
rem 随计划任务 "QuantEngine-MCP" 在用户登录时自动启动；重复启动不会多开进程
rem
rem 本文件为 CRLF 行尾 + GBK 编码
rem ============================================================================
setlocal

set "PROJECT_DIR=%~dp0..\..\"

if not exist "%PROJECT_DIR%.venv\Scripts\python.exe" (
    echo [ERROR] 找不到虚拟环境：%PROJECT_DIR%.venv\Scripts\python.exe
    exit /b 1
)
if not exist "%~dp0env.local.cmd" (
    echo [ERROR] 缺少 %~dp0env.local.cmd
    echo         请先运行 install.ps1 完成初始化
    exit /b 1
)

pushd "%PROJECT_DIR%" || exit /b 1
call "%~dp0env.local.cmd"

rem 日志统一写UTF-8：Python 写 stdout 的编码默认随区域设置变化，
rem 固定后用 Get-Content -Encoding UTF8 读即为正常中文。
set "PYTHONIOENCODING=utf-8"

rem 兜底默认值：env.local.cmd 若是旧版（没有 MCP_* 变量）也能起得来
if "%MCP_HOST%"=="" set "MCP_HOST=127.0.0.1"
if "%MCP_PORT%"=="" set "MCP_PORT=8765"

if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"
set "LOG_FILE=%PROJECT_DIR%logs\mcp.log"

rem 写操作开关独立于鉴权；这里只放"无空格"的开关，参数值一律走 env 变量
set "MCP_EXTRA="
if "%MCP_ALLOW_TRIGGER%"=="1" set "MCP_EXTRA=%MCP_EXTRA% --allow-trigger"
if "%MCP_ALLOW_MUTATE%"=="1" set "MCP_EXTRA=%MCP_EXTRA% --allow-mutate"

echo [%date% %time%] 启动 MCP 服务（SSE）监听 %MCP_HOST%:%MCP_PORT%
echo [%date% %time%] 日志：%LOG_FILE%

if "%MCP_AUTH_TOKEN%"=="" (
    "%PROJECT_DIR%.venv\Scripts\python.exe" manage.py run_mcp_server --host %MCP_HOST% --port %MCP_PORT% %MCP_EXTRA% >> "%LOG_FILE%" 2>&1
) else (
    "%PROJECT_DIR%.venv\Scripts\python.exe" manage.py run_mcp_server --host %MCP_HOST% --port %MCP_PORT% --auth-token "%MCP_AUTH_TOKEN%" %MCP_EXTRA% >> "%LOG_FILE%" 2>&1
)
set "RC=%ERRORLEVEL%"
echo [%date% %time%] MCP 服务已退出（rc=%RC%），日志：%LOG_FILE%
popd
exit /b %RC%
