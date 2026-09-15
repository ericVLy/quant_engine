# 量化引擎(DEVELOPING)

> 分支：`develop_backend` · 完整需求与模块设计以根目录 `documents.md` 为准。

## 数据存储说明

- K线数据（日线/分钟线）：使用抽象基类管理，各市场独立建表，历史数据持久化存储
  - 运行时分表命名：`kline_{market}_{code}`（由 `apps/datasources` 动态建表，如 `kline_a_000001`）
- 实时快照（RealtimeSnapshot）：仅保留最新值，用于盘中快速查询
- 数据源配置（DataSource）：第三方数据源连接信息
- 分时监控点（IntradayPoint，模块9）：盘中临时数据（开盘记录 → 收盘清空），分钟级采样

## 模型清单

### datasources

- DataSource: 数据源配置
- RealtimeSnapshot: 盘中实时快照（缓存，仅最新值）
- AbstractKLine: K线抽象基类（不建表）
- AStockKLine: A股K线 (kline_a_stock)
- HKStockKLine: 港股K线 (kline_hk_stock)
- USStockKLine: 美股K线 (kline_us_stock)
- KLineSyncLog: K线同步日志
- FundamentalSnapshot / FundamentalCacheMeta: 基本面快照与缓存元信息

### execution

- SuiteRun: 运行实例（EventLoop）
- Event: 事件记录
- ExecutionLog: 执行结果日志
- Order: 委托单
- NodeRun: 节点级运行实例（执行轨迹回放）
- Alert / AlertChannel: 告警与通知渠道
- AccountFundConfig / FundAllocation: 账户资金配置与分级资金占用

### monitoring（模块9 · 后端与前端均已落地）

- IntradayPoint: 分时监控点（盘中临时数据，(symbol, ts) 分钟级唯一）
- 内部更新器：`updater.py` 随 Django 服务启动（启动回填 → 周期采样 → UTC 23:00 清理）；**无单独更新命令**
- API：`GET /api/monitoring/intraday/` · `GET /api/monitoring/intraday/realtime/` · `GET /api/monitoring/intraday/stream/`（SSE 推送）

## 常用命令

- 运行测试：`python manage.py test`
- 全项目回归：`python manage.py test`（无标签，含 runner）
- 分时采样：随 Django 服务进程内自动执行（`MONITORING_UPDATER_ENABLED`，无单独命令）
- 收盘清理兜底（一般无需手动）：`python manage.py clear_intraday`
- Plan Cron 调度器：`python manage.py run_scheduler --interval 60`
- MCP 服务（SSE，Web 接入）：`.\.venv\Scripts\python.exe .\manage.py run_mcp_server --port 8765`
- MCP 专项测试：`.\.venv\Scripts\python.exe .\manage.py test mcp_server`

## MCP 服务（AI 助手接入 · 模块11）

把既有系统以 **MCP（Model Context Protocol）** 工具形式暴露给 AI 助手 / 前端，用于查询标的、K 线、策略元数据、告警与分时监控。

### 启动

```powershell
# 推荐：SSE（HTTP）常驻服务，客户端按 URL 接入
.\.venv\Scripts\python.exe .\manage.py run_mcp_server --port 8765
# 需要对外暴露时必须配令牌（否则拒绝启动）：
#   --host 0.0.0.0 --auth-token <token>

# 等价包入口
.\.venv\Scripts\python.exe -m mcp_server --transport sse

# 本机 IDE 客户端：stdio 子进程
.\.venv\Scripts\python.exe -m mcp_server --transport stdio
```

端点：`GET /sse`（建连）· `POST /messages/`（消息回传）· `GET /health`（健康检查）。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MCP_TRANSPORT` | `sse` | `sse`（HTTP 服务）或 `stdio`（子进程） |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8765` | 监听地址与端口（`--host` / `--port` 可覆盖） |
| `MCP_AUTH_TOKEN` | 空（不鉴权） | Bearer 令牌；**绑定非回环地址时必填** |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | `127.0.0.1:*,localhost:*` 等 | DNS rebinding 保护白名单 |
| `MCP_CORS_ORIGINS` | 空（不加 CORS 头） | 浏览器直连时允许的来源（逗号分隔） |
| `MCP_ALLOW_TRIGGER` | 空（写操作关闭） | 置 `1` 才允许 `trigger_plan_execution` |

### 客户端配置示例

SSE（推荐；配置令牌时把 `headers` 一并带上）：

```json
{
  "mcpServers": {
    "quant-engine": {
      "url": "http://127.0.0.1:8765/sse",
      "headers": {"Authorization": "Bearer <MCP_AUTH_TOKEN>"}
    }
  }
}
```

stdio（本机 IDE 以子进程方式拉起）：

```json
{
  "mcpServers": {
    "quant-engine": {
      "command": "c:\\Users\\PC\\Documents\\quant_platform\\quant_engine\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server", "--transport", "stdio"],
      "cwd": "c:\\Users\\PC\\Documents\\quant_platform\\quant_engine",
      "env": {"DJANGO_SETTINGS_MODULE": "quant_engine.settings.dev"}
    }
  }
}
```

- 安全：默认只读；`trigger_plan_execution` 仅创建 `pending` `SuiteRun`（实际执行由 `runner` 负责，**MCP 不会直接下单**）；令牌校验失败返回 401；非法 `Host`（DNS rebinding）直接拒绝建连。
- 进程职责：MCP 服务进程不启动分时内部更新器（分时采样只由 Django 服务进程负责），避免多进程重复外部请求与写库。
- 健康检查：`curl -H "Authorization: Bearer <token>" http://127.0.0.1:8765/health`
- 工具清单、安全设计与测试口径见 `documents.md` 模块11。

### mcp_server（模块11 · 模型/端点速览）

- 无自有数据模型；14 个工具复用 `watchlists` / `datasources` / `cases` / `suites` / `plans` / `execution` / `monitoring` 的模型与服务
- 资源：`quant://docs/overview`（系统概览与安全边界）
