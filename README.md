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
- MCP 服务（stdio 传输，由 AI 助手客户端拉起）：`.\.venv\Scripts\python.exe -m mcp_server`
- MCP 专项测试：`.\.venv\Scripts\python.exe .\manage.py test mcp_server`

## MCP 服务（AI 助手接入 · 模块11）

把既有系统以 **MCP（Model Context Protocol）** 工具形式暴露给 AI 助手 / 编码助手，用于查询标的、K 线、策略元数据、告警与分时监控。

- 传输：`stdio`（`.\.venv\Scripts\python.exe -m mcp_server`）；工具清单与契约见 `documents.md` 模块11。
- **默认只读**：14 个工具 + `quant://docs/overview` 概览资源；唯一写操作 `trigger_plan_execution` 需环境变量 `MCP_ALLOW_TRIGGER=1`，且只创建 `pending` `SuiteRun`（真正执行仍由 `runner` 负责），**MCP 不会直接下单**。
- **不参与运行时调度**：`mcp_server` 是入站适配器叶子包，只读调用 `apps.*` 的模型与服务；写操作复用既有 `apps.execution.services`。
- **不承担分时更新**：`mcp_server/bootstrap.py` 默认 `MONITORING_UPDATER_ENABLED=0`，避免 MCP 进程与 Django 服务进程重复采样（外部数据源请求 + 库写入）。

MCP 客户端配置示例（通用 `mcpServers` 结构，路径按本机实际调整）：

```json
{
  "mcpServers": {
    "quant-engine": {
      "command": "c:\\Users\\PC\\Documents\\quant_platform\\quant_engine\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"],
      "cwd": "c:\\Users\\PC\\Documents\\quant_platform\\quant_engine",
      "env": {
        "DJANGO_SETTINGS_MODULE": "quant_engine.settings.dev",
        "MONITORING_UPDATER_ENABLED": "0"
      }
    }
  }
}
```

- 若确需允许 AI 触发 Plan 执行，在 `env` 中追加 `"MCP_ALLOW_TRIGGER": "1"`（仍只创建 `pending` `SuiteRun`）。
- 验证：`.\.venv\Scripts\python.exe .\manage.py test mcp_server`（24 个用例：工具门面、错误契约、写开关、装配层）。
