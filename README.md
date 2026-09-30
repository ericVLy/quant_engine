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

### dashboard（N-06 · 运行总览）

- 只读聚合，**不持有模型**（无迁移）；统计全部在 DB 侧完成，不在前端拉列表自行统计
- API：`GET /api/dashboard/overview/?window_days=7` · `GET /api/dashboard/execution-trend/?days=14`
  （单对象接口，**不分页**）
- 口径：比率型受 `window_days` 限制且分母只含终态；存量/健康度取全表；金额按 `price × volume`
- 前端：`/dashboard`（KPI 卡片 + ECharts 趋势 + 健康提示条 + 30s 自动刷新），侧边导航首项
- 资金块只输出聚合数值，**不含 `account_id`**（N-05 卫生测试守住）

## 常用命令

> 全部命令在 Linux 下以 `.venv/bin/python` 执行（项目**禁止** `.\.venv\Scripts\python.exe` 等 Windows 风格写法）。

- 运行测试：`.venv/bin/python manage.py test`
- 全项目回归：`.venv/bin/python manage.py test --noinput -v 0`
- 模块专项：`.venv/bin/python manage.py test runner --noinput`
- 分时采样：随 Django 服务进程内自动执行（`MONITORING_UPDATER_ENABLED`，无单独命令）
- 收盘清理兜底（一般无需手动）：`.venv/bin/python manage.py clear_intraday`
- 策略调度器（生产入口，会消费 TaskQueue 执行策略）：`.venv/bin/python manage.py run_scheduler --interval 60 --workers 2`
  - 启动时先收口上次进程遗留的未完成运行 + 过期执行意向（幂等，可用 `EXECUTION_ORPHAN_RECOVERY_ENABLED=0` 关闭）
  - 真实下单务必先确认账户/风控与模拟环境：`.venv/bin/python manage.py run_scheduler --order-broker gm`
- MCP 服务（SSE，Web 接入）：`.venv/bin/python manage.py run_mcp_server --port 8765`
- MCP 专项测试：`.venv/bin/python manage.py test mcp_server`

## 已知边界与限制（务必阅读）

以下为**有意为之的设计取舍**或**当前尚未闭环**的部分，改动相关代码前请先确认约束。

### 执行与重启恢复

- **崩溃后只收口、不自动续跑**：进程被 kill -9 / OOM / 断电后，调度器启动时
  （`apps/execution/recovery.recover_orphaned_runs`）把遗留的 `running` 运行标记为
  `failed`（`error_code=ORPHANED_BY_RESTART`）并补 `suite_failed` 告警，**不会**自动重跑。
  原因：续跑需要节点级幂等（`EventLoop._fired_edges` 等状态仅在内存中），
  重放会重复命中边 → 重复下单。需人工核对事件/节点轨迹后重新触发。
- **执行意向（pending SuiteRun）有有效期**：`EXECUTION_PENDING_MAX_AGE_SECONDS`（默认 300 秒）。
  超期未被调度器消费即收口为 `PENDING_EXPIRED` 并告警——陈旧交易意图**不会**被补投执行。
- **进程内内存态重启即失**：`TaskQueue`（唤醒提示）、`Scheduler._enqueued`（同分钟去重快路径）、
  `EventLoop._fired_edges`。跨重启的唯一保证是数据表：`pending` `SuiteRun` 作为持久化执行意向 +
  `claim_suite_run` 的**原子 CAS 认领**（同一运行不可能被两个 worker / 两个进程执行）。
- **优雅关停不 drain 在途任务**：`run_scheduler` 收到 SIGINT/SIGTERM 后只停止取新任务，
  在途运行被直接取消（`WorkerPool.run_forever` 的 `finally` 只 `cancel` 不 `join` 排空），
  因此会留下与崩溃等价的痕迹——由下次启动的恢复器收口。
- **「已提交券商、未回写」的委托单本地无法闭环**：gm 下单接口无 `cl_ord_id` 幂等键，
  崩在该窗口的 `Order(status='pending', external_order_id 空)` 只能由恢复器统计
  （`unconfirmed_orders`）并**提示人工对账**，禁止本地臆断重投。

### 状态与风控

- **`run_status` 与执行事实是两套状态源**：`SuiteRun` / `NodeRun` / `ExecutionLog` 是执行真相；
  `Plan/Suite/Case.run_status`（`new/running/done/interrupt/failed`）是**每个实体单槽**的
  「编排会话」状态，**执行引擎不驱动** `Case.run_status`（多标的并发执行下单槽无法表达）。
  `Plan.run_status` 会在每次执行结束后按 `SuiteRun` 事实归一（见 `state_sync`）；
  `Suite` / `Case.run_status` 只由 REST 手动 `/start` `/stop` 动作流转（执行引擎不驱动）。
  `start_plan` 已放开重启（`new`/`done`/`interrupt` 可再次启动，仅拒绝 `running`）。
- **Plan 可重复驱动**：`start_plan` 接受 `new` / `done` / `interrupt`（仅拒绝 `running`，
  避免并发执行），跑完后可再次 `/api/plans/{id}/start/`。`Plan.run_status` 在**每次执行结束后**
  按 `SuiteRun` 事实自动归一（`apps/execution/state_sync.py`：有活跃运行 → `running`、
  全部成功 → `done`、含失败/停止 → `interrupt`；无运行记录则不动，避免臆断），
  并在调度器启动期批量修复历史遗留的分叉状态。
- **交易时段按市场时区判定**：`TradeTimeWindow` 使用 `Asia/Shanghai` / `Asia/Hong_Kong` /
  `America/New_York`（按订单 `symbol` 解析，`Symbol.market` 优先）。**禁止**用
  `timezone.localtime(timezone.now())` 当墙钟——`TIME_ZONE='UTC'` 时那是 UTC 时间，
  会把 A 股窗口错成 8 小时前的时刻。默认窗口本身仍是 A 股时段，多市场需自行配置。
- **每日累计金额口径**：`DailyLimitPolicy` 按库内 `Order` 聚合（`price × volume`），
  `build_execution_service` 默认**不启用**（`max_daily_value=None`）；限额通常在 **Plan 级**配置。
- **风控限额在 Plan 上配置**：`Plan.risk_*` 8 个字段（持仓方向 / 单笔数量与金额 / 每日累计 /
  账户可用 / 总仓位数量与金额 / 交易时段窗口），全部留空 = 不限制。限额随 `PlanRegistry`
  热加载，**改限额无需重启调度器**，并随 `PlanVersion` 快照一起发布与回滚。
  整体关闭风控用 `--no-risk-control`。
- **单机部署**：只允许运行一个 Scheduler 实例。多实例分布式去重、租约、领导者选举为 P4。

### 环境

- **部署需安装 tzdata**：`zoneinfo` 依赖系统 tzdata，否则 `America/New_York` 等时区解析失败。
- **secret / token 只经环境变量或 systemd `EnvironmentFile` 注入**，不落仓库、不入日志。
- **PII 脱敏已落地**：`apps/execution/redaction.py` 的脱敏工具 + `RedactionLogFilter` 已在 dev/prod
  全部日志 handler 挂载，告警邮件正文脱敏（不夹带异常栈），`AlertChannel.email_recipients` 仅管理员
  （`tests_redaction.py` 24 例）。新增涉及 PII 的序列化器 / 视图 / 服务时，**必须补 ≥1 个日志卫生测试**。
- **真正会越过「单机」边界的通道只有 MCP**：AI 助手接入后数据离开本机。新增/扩展 MCP 工具时
  **必须字段级白名单**，返回体禁止出现 `account_id`、`auth_info`、密钥、`email_recipients`、
  `User.phone` / `company`。本地服务请固定绑定 `127.0.0.1`（`dev.py` 的 `ALLOWED_HOSTS = ["*"]`
  仅在误用 `--host 0.0.0.0` 时才会把边界推出本机）。


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
| `MCP_ALLOW_TRIGGER` | 空（写操作关闭） | 置 `1` / `true` / `yes` 开启；也可使用启动参数 `--allow-trigger`（仅触发执行：创建 pending SuiteRun） |
| `MCP_ALLOW_MUTATE` | 空（写操作关闭） | 置 `1` / `true` / `yes` 开启配置写（创建/编辑/删除 Case、Suite、Plan，共 10 个工具）；也可使用启动参数 `--allow-mutate`；与 `MCP_ALLOW_TRIGGER` 相互独立 |

### 命令行开启写操作

两个入口、SSE 与 stdio 均支持 `--allow-trigger` 与 `--allow-mutate`（布尔开关，不需要附加值，相互独立）：

```powershell
& 'c:\Users\PC\Documents\quant_platform\quant_engine\.venv\Scripts\python.exe' 'c:\Users\PC\Documents\quant_platform\quant_engine\manage.py' run_mcp_server --port 8765 --allow-trigger
& 'c:\Users\PC\Documents\quant_platform\quant_engine\.venv\Scripts\python.exe' -m mcp_server --transport stdio --allow-trigger

# 额外开放配置写（仍只改 draft，不发布、不启动、不下单）
& 'c:\Users\PC\Documents\quant_platform\quant_engine\.venv\Scripts\python.exe' 'c:\Users\PC\Documents\quant_platform\quant_engine\manage.py' run_mcp_server --port 8765 --allow-mutate
& 'c:\Users\PC\Documents\quant_platform\quant_engine\.venv\Scripts\python.exe' -m mcp_server --transport stdio --allow-mutate
```

- 显式传入时覆盖 `MCP_ALLOW_TRIGGER=0` / `MCP_ALLOW_MUTATE=0`；未传入时保留环境变量配置，未配置则默认只读。
- stdio 客户端可在上述配置的 `args` 数组末尾追加 `"--allow-trigger"` / `"--allow-mutate"`；SSE 客户端需在服务端启动命令中配置，不能通过连接 URL 开启。
- 开关只作用于当前 MCP 进程；修改后需重启服务。`--allow-trigger` 仅允许创建 `pending SuiteRun`，不会直接下单；`--allow-mutate` 只创建/编辑/删除 **draft** 配置（复用 REST 同源校验与删除保护 409 语义），发布 / 启停仍走 REST 动作接口。两个开关都不会绕过非回环绑定的令牌要求。


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
- 变量描述：14 个工具的每个入参都在 `tools/list` 的 `inputSchema.properties.<变量>.description` 中带说明（`server.py` 用 `Annotated[<类型>, Field(description=...)]` 声明，`pydantic` 因此显式登记到 `requirements.txt`）；门面函数 `tools_impl.py` 逐个变量给出 `Args` / `Returns` / `Raises`，命令行 `--transport / --host / --port / --auth-token / --allow-trigger` 与 `MCP_*` 环境变量同样逐个带说明。
- 进程职责：MCP 服务进程不启动分时内部更新器（分时采样只由 Django 服务进程负责），避免多进程重复外部请求与写库。
- 健康检查：`curl -H "Authorization: Bearer <token>" http://127.0.0.1:8765/health`
- 工具清单、安全设计与测试口径见 `documents.md` 模块11。

### mcp_server（模块11 · 模型/端点速览）

- 无自有数据模型；14 个工具复用 `watchlists` / `datasources` / `cases` / `suites` / `plans` / `execution` / `monitoring` 的模型与服务
- 资源：`quant://docs/overview`（系统概览与安全边界）
