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