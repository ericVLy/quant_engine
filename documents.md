# 量化交易系统 · 全模块需求文档

> 版本：v2.13
> 日期：2026-09-21
> 状态：实现基线已稳定 · API 统一分页已落地 · 以代码为准，文档已同步校正 · 多实例 Scheduler 治理按单机部署目标由 P1 降为 P4 · 分时监控模块9 全部完成（后端 54 个专项测试 + 前端 ECharts 分时监控页 · gm SDK 分时数据源 · 启动完整性回填） · 策略快速创建向导（模块10）全部完成（3 步向导一键生成 Case/Suite/Plan 并发布 · 前端编排既有 API · 后端零新增接口） · MCP 服务（模块11）已落地（`mcp_server` 包：**SSE（HTTP）为主传输** · 14 个工具 + 1 个概览资源 + `/health` · 默认只读 · 令牌鉴权与非回环绑定 fail-fast · 写操作支持 `MCP_ALLOW_TRIGGER=1` 或 `--allow-trigger` · **MCP-19 变量描述：14 个工具逐变量带 `inputSchema` 描述，CLI 参数与 `MCP_*` 配置项逐个带说明** · 46 个专项测试通过 · 全量回归 442 个测试通过）


## 一、项目概述

### 1.1 项目目标

构建一套**本地优先、模块解耦**的量化投研与交易系统，实现从"公开数据采集"到"策略自主决策"的闭环。系统核心定位为：

- **编辑与执行分离**：策略的"设计编排"与"运行调度"是两条独立的流程
- **事件驱动架构**：所有执行由事件（Event）触发，Suite 即 EventLoop
- **图形化策略配置**：用户通过拖拽连线设计策略，配置文件存储于数据库
- **多市场支持**：A股、港股、美股（通过抽象基类实现）

### 1.2 核心设计原则

| 原则 | 说明 |
|------|------|
| **Django 仅作数据层与管理界面** | 不参与策略运行时调度，执行引擎为独立异步进程 |
| **TestCase / TestSuite / TestPlan 隐喻** | 单条策略对标 TestCase，工作流对标 TestSuite，调度对标 TestPlan |
| **Suite 即 EventLoop** | 每个 SuiteRun 拥有独立事件队列，所有 Case 由事件触发 |
| **文件链路 → 数据表链路** | 策略拓扑由数据库表（节点表 + 边表）定义，图形化界面操作 |
| **事件类型集中管理** | 系统内置 + 用户自定义，通过注册中心统一校验 |
| **K线持久化存储** | K线数据直接入库（各市场独立建表），不存放于缓存 |


## 二、整体架构

### 2.1 分层架构

```
┌─────────────────────────────────────────────────────┐
│                    接入层                           │
│  Web 管理界面（Vue3 + 画布）  │  行情网关（被动接收） │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│                 核心服务层（Django）                 │
│  ┌───────────────────────────────────────────────┐  │
│  │  可视化策略设计器（图形化配置 → 数据表）      │  │
│  ├───────────────────────────────────────────────┤  │
│  │  策略仓储 & 版本管理（Case/Suite/Plan 表）    │  │
│  ├───────────────────────────────────────────────┤  │
│  │  数据编织器（统一数据查询门面）               │  │
│  ├───────────────────────────────────────────────┤  │
│  │  风控 & 委托转换单元                          │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
                        │ (共享数据库)
                        ▼
┌─────────────────────────────────────────────────────┐
│               独立异步引擎（runner）                 │
│  Scheduler → Worker Pool → SuiteRunner → CaseExecutor│
│              (EventLoop 事件循环)                    │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│                 基础设施层                          │
│  PostgreSQL（主库）│ InfluxDB（时序）│ Redis（缓存） │
└─────────────────────────────────────────────────────┘
```

### 2.2 模块依赖关系

```
users（用户权限）
   │
   ├── watchlists（标的/分组/自选池）
   │      │
   │      ├── datasources（K线存储/数据源配置）
   │      │        └── monitoring（分时监控，临时数据 · 多市场时区）
   │      │
   │      └── cases（原子策略节点）
   │             │
   │             └── suites（工作流编排/DAG）
   │                    │
   │                    └── plans（调度管理）
   │                           │
   │                           └── execution（执行日志/委托单/事件/告警）
   │                                  │
   │                                  └── runner（独立异步引擎）
   │
   └── （所有模块均依赖 users）
```


依赖方向补充（模块11）：`mcp_server` 为**入站适配器叶子包**，只依赖 `apps.*` 的模型与服务，`apps.*` 不反向依赖它；与 `runner` 同处「编辑与执行分离」架构之外（`runner` 出站执行、`mcp_server` 入站查询），不影响「Django 仅作数据层」基线。

## 三、关键数据契约与 JSON 白名单

为避免用户随意填写自由 JSON 导致事件网关、调度器和策略执行链断裂，系统在后端和前端都实施了“固定字段名 + 固定结构”的校验。

### 3.1 Case.params 白名单

允许字段仅为：

- `trigger`
- `period`
- `threshold_oversold`
- `threshold_overbought`
- `direction`
- `result`
- `order`

其中：

- `trigger` 必须是对象，且只允许 `event_type`；对应事件必须已在 `EventRegistry` 中注册。
- `period` 必须是大于等于 1 的整数。
- `direction` 必须是 `-1`、`0` 或 `1`。
- `result` 必须是对象。
- `order` 必须包含：`direction`、`price`、`volume`，且 `direction` 仅允许 `buy` / `sell`，`volume` 必须大于等于 1 的整数。

已实现的深层校验还包括：

- `result` 仅允许 `direction`、`payload`、`order`；`direction` 必须为 `-1`、`0` 或 `1`，`payload` 必须是对象。
- `result.order` 和顶层 `order` 使用相同的订单结构校验；价格必须为大于 0 的有限数值，数量必须为大于等于 1 的整数。
- `filter` 仅允许 `op`、`field`、`threshold`、`value`；`op` 仅允许 `keep` / `drop`，提供 `field` 时必须同时提供阈值。
- `verdict` 仅允许 `method`、`components`；`method` 仅允许 `weighted_sum` / `vote`，组件必须是非空对象数组，且每个组件必须提供 `indicator` 或 `calculation`。
- `indicator` / `calculation` 必须来自因子引擎目录：`ma`、`sma`、`mean`、`ema`、`macd`、`rsi`、`kdj`、`boll`、`roc`、`momentum`、`pct_change`、`volatility`，或兼容旧接口的 `last`、`compare`。
- 周期、MACD 参数和权重等数值字段会校验类型、有限性和最小值，并拒绝布尔值伪装成整数；MACD 要求 `fast < slow`。
- RSI/KDJ 阈值必须处于 `0` 到 `100`，超卖阈值必须小于超买阈值；verdict 组件不允许重复指标，权重总和必须大于 `0`。

### 3.2 Plan.symbol_scope 白名单

允许字段仅为：

- `type`
- `group_ids`
- `symbol_codes`

`type` 仅允许：

- `all`
- `groups`
- `symbols`

且：

- `all` 只能包含 `type`
- `groups` 必须提供 `group_ids` 数组
- `symbols` 必须提供 `symbol_codes` 数组

### 3.3 Suite Edge.event_condition 白名单

允许字段仅为：

- `event_type`
- `case_id`
- `next_event`

其中：

- `event_type` 必须存在且为非空字符串
- `case_id` 若提供，必须为整数
- `next_event` 若提供，必须为非空字符串

> 这些规则已写入后端序列化器，并同步纳入前端表单校验逻辑，避免“字段随意扩展”造成运行时错误。


### 3.4 API 统一分页契约（N-01）

从 v2.6 起，所有列表接口统一返回分页结构（DRF `PageNumberPagination` 扩展，实现位于 `quant_engine/pagination.py`，通过 `REST_FRAMEWORK.DEFAULT_PAGINATION_CLASS` 全局生效）：

```json
{
    "count": 123,
    "next": "http://host/api/cases/?page=2&page_size=20",
    "previous": null,
    "page": 1,
    "total_pages": 7,
    "results": [ ... ]
}
```

- 分页参数：`page`（页码，从 1 开始，默认 1）、`page_size`（每页条数，默认 20，最大 500）。
- 兼容旧客户端：`limit` 作为 `page_size` 的别名（此前前端固定使用 `{ limit: 6 | 500 }` 拉取下拉/面板数据）。
- 覆盖范围：
  - 所有 ModelViewSet / ReadOnlyModelViewSet 列表接口：`cases`、`suites`、`plans`、`execution/*`（event-types/runs/events/logs/orders/fund-allocations/alerts/alert-channels）、`watchlists/*`（symbols/groups）、`datasources/*`（snapshots/sync-logs；~~sources~~ 已随 D-01 移除）；
  - 自定义列表动作：`cases/{id}/versions/`、`plans/{id}/symbols/`、`plans/{id}/versions/`、`execution/event-types/list-all/`、`datasources/kline/query/`。
- 旧客户端字段兼容：前端 `quant-frontend/src/api/index.ts` 的 axios 响应拦截器将分页结构解包为 `results` 数组，既有页面直接 `response.data` 作为数组使用的代码无需改动；列表调用默认携带 `page_size: 500` 以保持“一次加载全量”的原有体验。
- 单对象 / 详情接口（如 `watchlist`、`topology`、`alerts/statistics`、各类 POST 动作）不属于列表接口，保持原返回结构不变。


## 三、模块详细需求

### 模块1：`users`（用户与权限）✅ 已完成

| 属性 | 说明 |
|------|------|
| **状态** | ✅ 已完成（5 个测试全部通过） |
| **优先级** | P1 |
| **依赖** | 无（基础模块） |

#### 功能需求

| 编号 | 需求描述 |
|------|----------|
| U-01 | 用户注册、登录、注销（JWT 或 Session 认证） |
| U-02 | 用户信息管理（扩展字段：手机号、公司） |
| U-03 | 权限分组（管理员/普通用户/只读用户） |
| U-04 | 各模块资源的访问控制（如"仅管理员可同步全市场标的"） |

#### API 端点

| 方法 | 端点 | 功能 |
|------|------|------|
| POST | `/api/users/register/` | 用户注册并加入普通用户组 |
| POST | `/api/users/login/` | Session 登录 |
| POST | `/api/users/logout/` | 注销当前 Session |
| GET/PUT/PATCH | `/api/users/profile/` | 当前用户信息查询/更新 |
| POST | `/api/users/{user_id}/roles/` | 管理员调整用户角色 |

当前实现文件：`models.py`、`serializers.py`、`views.py`、`urls.py`、`admin.py`。角色使用 Django `Group`，注册用户默认加入 `user` 组，管理员可分配已存在的角色组。

#### 数据模型

```python
class User(AbstractUser):
    phone = models.CharField(max_length=20, blank=True)
    company = models.CharField(max_length=100, blank=True)
```


### 模块2：`watchlists`（标的与自选池）✅ 已完成

| 属性 | 说明 |
|------|------|
| **状态** | ✅ 已完成（15 个测试全部通过） |
| **优先级** | P0 |
| **依赖** | `users.User` |

#### 功能需求

| 编号 | 需求描述 | 实现文件 |
|------|----------|----------|
| W-01 | 标的 CRUD（代码、名称、交易所、市场分类） | `models.py`, `views.py` |
| W-02 | 标的搜索（按代码/名称模糊搜索） | `views.py` (search_fields) |
| W-03 | 标的过滤（按市场/交易所精确过滤） | `views.py` (filterset_fields) |
| W-04 | 批量导入标的（JSON 数组） | `views.py` (batch_import) |
| W-05 | 同步公开市场基础标的信息（按市场/交易所更新 symbol 基础数据） | `services.py` (sync_market_data) |
| W-06 | 分组 CRUD（名称唯一） | `models.py`, `views.py` |
| W-07 | 分组内批量添加/移除标的 | `views.py` (add_symbols, remove_symbols) |
| W-08 | 用户自选池（绑定分组列表，每个用户仅一个） | `models.py`, `views.py` |
| W-09 | 解析 symbol 编码与名称（供前端和 Plan 调用） | `views.py` (`resolve-name`), `services.py` |

#### API 端点

| 方法 | 端点 | 功能 |
|------|------|------|
| GET/POST | `/api/watchlists/symbols/` | 标的列表/创建 |
| GET/PUT/DELETE | `/api/watchlists/symbols/{id}/` | 标的详情/更新/删除 |
| GET | `/api/watchlists/symbols/resolve-name/?code=000001&market=A` | 根据 code 解析中文名称 |
| POST | `/api/watchlists/symbols/sync/` | 全市场同步（管理员） |
| POST | `/api/watchlists/symbols/batch-import/` | 批量导入 |
| GET/POST | `/api/watchlists/groups/` | 分组列表/创建 |
| GET/PUT/DELETE | `/api/watchlists/groups/{id}/` | 分组详情/更新/删除 |
| POST | `/api/watchlists/groups/{id}/add-symbols/` | 分组添加标的 |
| POST | `/api/watchlists/groups/{id}/remove-symbols/` | 分组移除标的 |
| GET/PUT | `/api/watchlists/watchlist/` | 当前用户自选池 |

#### 数据模型

```python
class Symbol(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    exchange = models.CharField(max_length=20, blank=True)
    market = models.CharField(max_length=10, choices=[('A','A股'),('HK','港股'),('US','美股')])

class Group(models.Model):
    name = models.CharField(max_length=50, unique=True)
    symbols = models.ManyToManyField(Symbol, related_name='groups', blank=True)

class Watchlist(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    groups = models.ManyToManyField(Group, related_name='watchlists', blank=True)
```


### 模块3：`datasources`（数据源与K线存储）✅ 已完成

| 属性 | 说明 |
|------|------|
| **状态** | ✅ 已完成（24 个测试全部通过；~~D-01 用户自配第三方数据源~~ 已于 2026-09-15 **移除**——系统无法适配第三方源异构数据结构，K 线/快照/同步链路固定走 akshare（ashare 兼容层）+ gm SDK，不读用户配置） |
| **优先级** | P0 |
| **依赖** | `watchlists.Symbol` |

#### 功能需求

| 编号 | 需求描述 | 实现文件 |
|------|----------|----------|
| ~~D-01~~ | ~~数据源配置 CRUD（AkShare/TuShare/TDX/YFinance）~~ | ❌ **已移除（2026-09-15）**：用户自配第三方源不可行（系统无法适配异构数据结构）；`DataSource` 模型与 `/api/datasources/sources/` 已删除（迁移 0004），数据获取收敛为内置 ashare + gm 适配层；前端同步清理——`/datasources` 页移除数据源配置表格/新增/编辑对话框与 `datasourcesApi.sources/createSource/updateSource/deleteSource` 封装（页面保留快照/同步日志/K 线查询工具），`vue-tsc -b` 0 错误 + `vite build` 通过 |
| D-02 | 实时快照存储（仅保留最新值，`OneToOneField`） | `models.py` (RealtimeSnapshot) |
| D-03 | K线抽象基类（定义公共字段，不建表） | `models.py` (AbstractKLine) |
| D-04 | A股 K线表：按标的编码创建独立分表，运行时建表 | `models.py`, `services.py` |
| D-05 | 港股 K线表：按标的编码创建独立分表，运行时建表 | `models.py`, `services.py` |
| D-06 | 美股 K线表：按标的编码创建独立分表，运行时建表 | `models.py`, `services.py` |
| D-07 | K线增量同步：按 symbol 生成表名并去重插入 | ✅ 完成（`services.sync_kline_for_symbol`；**2026-09-15 增量优化**：拉取前先查区间内已入库日期——区间首尾均已有数据则**跳过远端拉取**（0 流量）；头部已覆盖则拉取窗口收窄为 `(最新一条, end]`（ashare count 按缺口天数计算，减小流量）；头部可能缺口时保持全量拉取由逐行去重兜底，显式传入早于缺口的 start_date 可补历史；API 未传 `start_date` 时走增量语义，`KLineSyncLog` 仍记录完整请求窗口） |
| D-08 | K线同步日志（记录每次拉取状态） | `models.py` (KLineSyncLog) |
| D-09 | K线查询接口（按标的 + 日期范围查询对应分表） | `views.py`, `services.py` |
| D-10 | K线同步触发接口（单标的 / 全部） | `views.py`, `services.py` |

#### 分表设计

当前 K 线存储实现采用“按 symbol 运行时分表 + 兼容 legacy 表”的模式，实际代码中通过 `get_kline_table_name()` 与 `ensure_kline_table()` 动态创建或复用独立分表：

- 默认主库：`default`，保留 Django 业务数据（用户、策略、案例等）
- 运行时 K 线库：默认使用 `kline` 别名，当前本地环境由 SQLite 提供；可在设置中切换到 MySQL/MariaDB
- 表名规则：`kline_{market}_{symbol_code}`，例如 `kline_a_000001`
- 运行时创建：首次同步或查询某个 symbol 时，自动检查并创建该 symbol 对应的表
- 查询时按 `symbol_id + date range` 定位目标表
- 兼容策略：代码中优先查询运行时分表；若分表为空，则回退到 legacy 业务模型（`AStockKLine` / `HKStockKLine` / `USStockKLine`）
- 生产切换：当前实现通过 `settings.KLINE_DB_ALIAS` 及 `connections[db_alias]` 统一接入，不强依赖硬编码表名

#### 分表查询约束审计

运行时表名已经由 `market + symbol.code` 唯一确定（如 `kline_a_000001`），且 `Symbol.code` 为全局唯一。因此，单张运行时分表设计上只属于一个标的，读取时再使用 `symbol_id` 作为 `WHERE` 条件属于重复过滤。

- 运行时分表查询仅按 `date BETWEEN start_date AND end_date` 过滤
- `symbol_id` 继续保留在表结构中，用于写入、去重、数据追溯和兼容既有数据
- 写入阶段仍按 `symbol_id + date` 检查重复记录
- 若未来改为多标的共表，必须恢复查询约束并重新评估唯一索引

#### API 端点

| 方法 | 端点 | 功能 |
|------|------|------|
| GET/POST | `/api/datasources/sources/` | ~~数据源配置 CRUD~~（**已移除**，2026-09-15） |
| GET | `/api/datasources/snapshots/` | 实时快照列表 |
| GET | `/api/datasources/snapshots/{symbol_id}/` | 指定标的快照 |
| GET | `/api/datasources/sync-logs/` | 同步日志列表 |
| GET | `/api/datasources/kline/query/?symbol=&start=&end=` | K线查询 |
| POST | `/api/datasources/kline/sync/` | 触发同步 |

#### 数据模型

```python
class AbstractKLine(models.Model):
    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE)
    date = models.DateField(db_index=True)
    open, high, low, close = models.DecimalField(max_digits=12, decimal_places=4)
    volume = models.BigIntegerField()
    amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    class Meta: abstract = True

# 运行时分表命名规则：kline_a_000001 / kline_hk_00700 / kline_us_aapl
# 逻辑层通过 symbol.id + market + code 识别表名，存储时以原生 SQL 自动创建
# 对外 API 不变，仍通过 symbol + start + end 查询对应标的 K 线。
```

#### 3.1.1 开发补充：A 股数据源从 AkShare 迁移到 Ashare 并保持兼容

2026-09-01 至 2026-09-02 期间，数据源层完成一次关键修正：将 K 线抓取适配由 AkShare 兼容层重构为 Ashare 适配层，同时保持历史调用入口与返回字段契约不变。

##### 1. 目标

- 以 `ashare` 实现替代原有 `fetch_kline_from_akshare` / `stock_zh_a_hist` 依赖路径
- 对外仍保留原有 `fetch_kline_from_ashare` 与 `fetch_kline_from_akshare` 接口入口
- 统一对齐 `date / open / high / low / close / volume / amount / adj_factor / turnover_rate` 等字段
- 兼容真实接口返回值中 index-based DataFrame、空 payload、日期类型混合、腾讯返回 `param error` 等情况

##### 2. 关键实现

- `apps/datasources/services.py`
  - 新增/增强 `ashare_get_price()` 封装层，统一处理 `start_date`、`end_date`、`count` 与 `frequency`
  - 强化 `_normalize_ashare_kline_dataframe()`，对缺失 `date` 列、DatetimeIndex、空值和类型转换做兼容处理
  - `fetch_kline_from_ashare()` 继续以 ashare 为实际数据源，并在同步阶段调用 `sync_kline_for_symbol()` 实现去重入库
- `apps/datasources/ashare.py`
  - 兼容 `get_price_sina()` 和 `get_price_day_tx()` 的真实返回结构
  - 处理腾讯接口返回 `{"code":0,"msg":"param error","data":[]}` 的空数据情况
  - 修复 `datetime.date` 与 `datetime.datetime` 混用导致的 `TypeError`
  - 当结果是 index-based DataFrame 时，确保 `date` 可从索引中恢复并继续插入分表

##### 3. 运行时问题与修复

本次修复覆盖了真实运行环境中发现的几类问题：

- `KeyError: 'date'`：在 ashare 返回 DataFrame 仅存在索引而无 `date` 列时触发
- `TypeError: unsupported operand type(s) for -: 'datetime.datetime' and 'datetime.date'`：日期对象混用导致
- `TypeError: list indices must be integers or slices, not str`：腾讯接口在无有效数据时返回空列表，原代码直接按 dict 访问
- `NoneType` / empty DataFrame 返回：mock 与真实返回路径需统一确保结果可被调用方安全消费

##### 4. 验证方式

已执行回归验证命令：

```bash
cd c:\Users\linye\Documents\quant_engine
.\venv\Scripts\python.exe .\manage.py test apps.datasources.tests -v 1
```

验证结论：

- 22 个测试全部通过
- 关键覆盖：K线查询、异步同步、数据源 CRUD、ashare schema 规范化、真实接口兼容

##### 5. 说明

- 动态模型重复注册 `RuntimeWarning` 仍会出现在测试日志中，但不影响功能执行，也不属于当前业务错误
- 该兼容层已改为“保留接口名称、切换数据实现”，后续其他模块可继续按已有 `Symbol + start/end` 的协议调用，不需要大面积改动上层代码


### 模块4：`execution`（事件与执行基础设施）🟢 执行闭环与告警已完成

| 属性 | 说明 |
|------|------|
| **状态** | 🟢 同步执行闭环、事件路由与告警管理（Alert / AlertChannel / 通知服务）已完成，真实交易回报验证待完善 |
| **优先级** | P0 |
| **依赖** | `plans.Plan`, `suites.Suite`（外键允许空）, `users.User`（告警处理人） |

#### 事件设计：类和对象模式

`execution.events` 已从“字符串常量中心”扩展为“类 + 对象”双层模型：

- `EventType`：定义系统内置事件类型常量，如 `SUITE_INIT`、`CASE_COMPLETED`、`PRICE_SURGE` 等。
- `BaseEvent`：定义事件对象的通用结构，包括 `source`、`payload`、`metadata` 以及 `to_dict()`。
- 具体子类：如 `SuiteInitEvent`、`SuiteStartEvent`、`CaseCompletedEvent` 等，表示某一类事件的定义。
- 事件实例：`SuiteInitEvent(source='plan', payload={'symbol': '000001'})` 表示一条真实的具体事件，有明确的事件类型和业务参数。

这意味着上层代码既可以继续传递字符串事件类型，也可以直接传递事件对象实例；`enqueue_event()` 统一做类型归一化和 payload 合并，最后写入数据库的 `Event` 表仍保持单条事件记录结构。实际实现中，事件对象通过 `BaseEvent.payload` + 子类专属属性共同承载业务字段，并以 `event_condition` 做简单键值匹配进行路由。

此外，针对不同事件类型已设计专属字段与方法，便于事件分发和策略判断：

- `SuiteInitEvent`: `plan_id` / `suite_id` / `symbol`，用于启动一轮执行；`summary()` 返回初始化摘要。
- `CaseStartEvent`: `case_id` / `case_name` / `trigger_event`，用于追踪某个 Case 的触发来源。
- `CaseCompletedEvent`: `case_id` / `result` / `execution_time_ms`，用于统计执行结果与时长。
- `TimerEvent`: `trigger_time` / `interval_seconds` / `cron`，并提供 `is_due()` 判断是否触发。
- `PriceSurgeEvent` / `PriceDropEvent`: `symbol` / `market` / `price` / `change_pct` / `volume`，并提供 `is_upward()` / `is_downward()`。
- `VolumeSpikeEvent`: `symbol` / `current_volume` / `avg_volume` / `spike_ratio`，并提供 `is_spike()`。
- `MacroCpiEvent` / `MacroInterestEvent`: `country` / `value` / `policy` / `rate`，便于宏观事件路由和研究策略触发。

#### 功能需求

| 编号 | 需求描述 | 实现状态 | 实现文件 |
|------|----------|----------|----------|
| EX-01 | 系统内置事件类型常量（EventType） | ✅ 完成 | `events.py` |
| EX-02 | 事件对象基类与具体事件类（BaseEvent + 子类） | ✅ 完成 | `events.py` |
| EX-03 | 事件类型注册中心（缓存 + 校验 + 列表） | ✅ 完成 | `registry.py` |
| EX-04 | 自定义事件类型注册表模型（EventTypeRegistry） | ✅ 完成 | `models.py` |
| EX-05 | 执行实例模型（SuiteRun） | ✅ 完成 | `models.py` |
| EX-06 | 事件模型（Event） | ✅ 完成 | `models.py` |
| EX-07 | 执行日志模型（ExecutionLog） | ✅ 完成 | `models.py` |
| EX-08 | 委托单模型（Order） | ✅ 完成 | `models.py` |
| EX-09 | 事件类型管理 API（CRUD + list-all） | ✅ 完成 | `views.py`, `serializers.py` |
| EX-10 | SuiteRun 只读 API | ✅ 完成 | `views.py` |
| EX-11 | Event 只读 API | ✅ 完成 | `views.py` |
| EX-12 | ExecutionLog 只读 API | ✅ 完成 | `views.py` |
| EX-13 | Order CRUD API | ✅ 完成 | `views.py` |
| EX-14 | Admin 后台注册所有模型 | ✅ 完成 | `admin.py` |
| EX-15 | **事件循环基础处理** | ✅ 完成 | `services.py`；独立 runner 负责 Case 执行与异步编排 |
| EX-16 | **事件匹配逻辑（Event → Edge 路由）** | ✅ 完成 | `services.py`；支持事件类型、目标 Suite 与后续事件传递 |
| EX-17 | **Plan 触发接口（创建 SuiteRun）** | ✅ 完成 | `views.py`, `services.py` |
| EX-18 | **SuiteRun 状态流转逻辑** | ✅ 完成 | `services.py` |
| EX-19 | **委托单状态回写（对接交易接口）** | ✅ 已接入 gm 适配器；受理→部分成交→完全成交、拒单、撤单与重复回报幂等去重已完成；真实生产交易回报仍需沙盒/实盘联调 | `runner/gm_adapter.py`；关联开发任务：5.1.2-4、5.1.4-任务1 |
| EX-20 | **告警模型（Alert）** | ✅ 完成 | `models.py` |
| EX-21 | **告警渠道配置模型（AlertChannel）** | ✅ 完成 | `models.py` |
| EX-22 | **告警服务（创建/通知/渠道加载）** | ✅ 完成 | `alerts.py`；提供 `alert_service` 全局单例 |
| EX-23 | **告警查询 API（只读 + 操作动作）** | ✅ 完成 | `views.py`, `serializers.py`, `urls.py` |
| EX-24 | **告警渠道配置 API（CRUD + 重载）** | ✅ 完成 | `views.py`, `serializers.py`, `urls.py` |
| EX-25 | **告警 API 认证与授权** | ✅ 完成（DRF `IsAuthenticated`；未认证返回 403） | `views.py` |
| EX-26 | **告警统计与通知重发接口** | ✅ 完成 | `views.py` |

#### API 端点

| 方法 | 端点 | 功能 |
|------|------|------|
| GET/POST | `/api/execution/event-types/` | 事件类型注册表 CRUD（管理员） |
| GET | `/api/execution/event-types/list-all/` | 列出所有事件类型（含内置） |
| GET | `/api/execution/runs/` | SuiteRun 列表 |
| GET | `/api/execution/events/` | Event 列表 |
| GET | `/api/execution/logs/` | ExecutionLog 列表 |
| GET/POST/PUT/DELETE | `/api/execution/orders/` | Order CRUD |
| GET | `/api/execution/alerts/` | 告警列表（只读）；支持 `alert_type`/`severity`/`status`/`plan` 过滤与 `search` 搜索 |
| GET | `/api/execution/alerts/{id}/` | 告警详情（只读） |
| POST | `/api/execution/alerts/{id}/actions/` | 告警操作（`acknowledge` / `resolve`，可附 `note`） |
| POST | `/api/execution/alerts/{id}/resend-notifications/` | 重新发送告警通知 |
| GET | `/api/execution/alerts/statistics/` | 告警统计（`overview` + `by_type`） |
| GET/POST/PATCH/DELETE | `/api/execution/alert-channels/` | 告警渠道配置 CRUD |
| POST | `/api/execution/alert-channels/reload/` | 重新加载告警渠道配置（供 `alert_service` 生效） |
| POST | `/api/execution/trigger/` | 按 `plan_id` 和 `symbol/symbols` 创建 SuiteRun |
| GET | `/api/execution/run/{run_id}/` | SuiteRun 状态查询 |
| POST | `/api/execution/run/{run_id}/start/` | 启动 SuiteRun |
| POST | `/api/execution/run/{run_id}/process/` | 消费队列中的下一个事件 |

#### 数据模型

```python
class SuiteRun(models.Model):
    plan = models.ForeignKey(Plan, null=True, blank=True)
    suite = models.ForeignKey(Suite, null=True, blank=True)
    symbol = models.CharField(max_length=20)
    status = models.CharField(choices=[('pending','待启动'),('running','运行中'),('completed','已完成'),('failed','失败'),('stopped','已停止')])
    event_queue = models.JSONField(default=list)   # 事件ID列表
    started_at, ended_at = models.DateTimeField(null=True, blank=True)

class Event(models.Model):
    run = models.ForeignKey(SuiteRun, on_delete=models.CASCADE)
    event_type = models.CharField(max_length=50, db_index=True)   # 无 choices 限制，由注册中心校验
    source = models.CharField(max_length=100, blank=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(choices=[('pending','待处理'),('processing','处理中'),('done','已完成'),('failed','失败')])

class EventTypeRegistry(models.Model):
    name = models.CharField(max_length=50, unique=True)
    scope = models.CharField(choices=[('system','系统内置'),('plugin','插件定义'),('user','用户自定义')])
    description = models.CharField(max_length=200, blank=True)
    payload_schema = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

class Alert(models.Model):
    ALERT_TYPE_CHOICES = [('order_failed','订单失败'),('suite_failed','策略执行失败'),('plan_failed','计划执行失败'),('risk_violation','风控违规'),('system_error','系统错误')]
    SEVERITY_CHOICES = [('low','低'),('medium','中'),('high','高'),('critical','紧急')]
    STATUS_CHOICES = [('pending','待处理'),('acknowledged','已确认'),('resolved','已解决')]
    alert_type = models.CharField(max_length=50, choices=ALERT_TYPE_CHOICES)
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='medium')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    plan = models.ForeignKey(Plan, null=True, blank=True)            # 关联计划
    suite_run = models.ForeignKey(SuiteRun, null=True, blank=True)   # 关联运行实例
    order = models.ForeignKey('Order', null=True, blank=True)        # 关联委托单
    title = models.CharField(max_length=200)
    message = models.TextField()
    error_code = models.CharField(max_length=50, blank=True, null=True)
    in_app_notified = models.BooleanField(default=False)
    email_notified = models.BooleanField(default=False)
    notification_error = models.TextField(blank=True)
    acknowledged_by / resolved_by = models.ForeignKey(User, null=True, blank=True)
    acknowledged_at / resolved_at = models.DateTimeField(null=True, blank=True)
    created_at / updated_at = models.DateTimeField(auto_now_add=True / auto_now=True)

class AlertChannel(models.Model):
    CHANNEL_TYPE_CHOICES = [('in_app','应用内通知'),('email','邮件通知')]
    channel_type = models.CharField(max_length=20, choices=CHANNEL_TYPE_CHOICES, unique=True)
    is_enabled = models.BooleanField(default=True)
    email_recipients = models.JSONField(default=list, blank=True)       # 邮件收件人列表
    email_subject_prefix = models.CharField(max_length=50, default='[量化交易系统]')
    min_severity = models.CharField(max_length=20, default='low')        # 最低告警级别
    alert_types = models.JSONField(default=list, blank=True)            # 类型白名单（空=全部）
    # should_send_alert(alert): 按 is_enabled + min_severity + alert_types 判定是否投递
```

#### 当前执行服务

`apps/execution/services.py` 已提供以下基础能力：

- 校验已发布 Plan 并创建 SuiteRun，同时注入 `SUITE_INIT` 事件。
- 启动、停止和完成 SuiteRun，并记录开始/结束时间。
- 校验事件类型、持久化 Event，并维护 `event_queue` 中的事件 ID。
- 消费队列事件时使用行级锁，按 `Edge.event_condition` 匹配事件类型和载荷，并将目标 Suite 与后续事件传递到队列。
- 事件处理异常时，将 Event 和 SuiteRun 标记为失败。

该实现是执行层的同步事件消费服务；独立 runner 负责真实 `CaseExecutor`、生产数据上下文和异步编排。订单回报适配已支持外部 ID、状态字符串/枚举、成交价和撤单状态归一化，但生产交易环境的完整回报字段仍需使用沙盒或实盘联调验证。

#### 告警服务

`apps/execution/alerts.py` 提供 `alert_service` 全局单例，负责告警创建与多渠道通知：

- 类型常量：`AlertType`（订单失败 / 策略失败 / 计划失败 / 风控违规 / 系统错误）、`AlertSeverity`（低 / 中 / 高 / 紧急）。
- `create_alert(...)`：统一创建 `Alert` 记录并可选地立即发送通知。
- 快捷工厂方法：`create_order_failed_alert`、`create_suite_failed_alert`、`create_risk_violation_alert`、`create_system_error_alert`，各自生成含业务上下文的标题与多行消息。
- `send_alert_notifications(alert)`：遍历启用的 `AlertChannel`，按 `should_send_alert()`（启用状态 + 最低级别 + 类型白名单）过滤后分发到应用内通知（落库 `in_app_notified`）与邮件通知（`django.core.mail` 模板渲染）。
- `reload_channels()`：渠道配置变更或调用 `POST /alert-channels/reload/` 后重新加载渠道缓存。

邮件通知基于 Django `EMAIL_*` 设置（开发默认 `console.EmailBackend`），邮件主题由渠道的 `email_subject_prefix` + 告警标题拼接，正文包含告警类型、级别、标题、时间、错误代码与详情，并附带后台处理链接。


### 模块5：`cases`（原子策略节点）✅ P0 能力已完成

| 属性 | 说明 |
|------|------|
| **状态** | ✅ 已完成 CRUD、触发校验、发布快照、删除保护、指标目录和深层语义校验 |
| **优先级** | P0 |
| **依赖** | `execution.EventRegistry`（校验触发事件类型） |

#### 功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| C-01 | Case 模型（名称、节点类型、参数 JSON、版本、状态、**运行状态 run_status**） | ✅ 完成 |
| C-02 | Case CRUD API（列表、详情、创建、更新、删除） | ✅ 完成 |
| C-03 | Case 发布（版本号 +1，状态改为 published） | ✅ 完成 |
| C-04 | `params` 中的 `trigger` 配置校验（调用 `EventRegistry.validate`） | ✅ 完成 |
| C-05 | 删除保护（被 Suite 引用时返回 409 Conflict） | ✅ 完成 |
| C-06 | Case 搜索/过滤（按名称、节点类型、状态） | ✅ 完成 |
| C-07 | Case 版本历史记录（发布快照） | ✅ 完成 |
| C-08 | 节点类型定义（signal / filter / verdict / executor） | ✅ 完成 |
| C-09 | 参数 JSON Schema 校验（根据节点类型校验参数完整性） | ✅ 完成（结构与指标语义校验） |

#### 数据模型

```python
class Case(models.Model):
    NODE_TYPE_CHOICES = [
        ('signal', '信号节点'),
        ('filter', '过滤器'),
        ('verdict', '裁决节点'),
        ('executor', '执行器'),
    ]
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('published', '已发布'),
        ('archived', '已归档'),
    ]
    name = models.CharField(max_length=100)
    node_type = models.CharField(max_length=20, choices=NODE_TYPE_CHOICES)
    # params 结构示例：
    # {
    #   "trigger": {"event_type": "SUITE_INIT", "source_case_id": null},
    #   "period": 14,
    #   "threshold_oversold": 30,
    #   "threshold_overbought": 70
    # }
    params = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

#### API 端点

| 方法 | 端点 | 功能 |
|------|------|------|
| GET/POST | `/api/cases/` | Case 列表/创建 |
| GET/PUT/DELETE | `/api/cases/{id}/` | Case 详情/更新/删除 |
| POST | `/api/cases/{id}/publish/` | 发布 Case |

当前实现文件：`models.py`、`serializers.py`、`views.py`、`urls.py`。`Suite.cases` 已建立多对多引用，用于删除保护和后续工作流调度。


### 模块6：`suites`（工作流编排）🟢 编排核心能力已完成

| 属性 | 说明 |
|------|------|
| **状态** | 🟢 已完成 CRUD、拓扑读写、DAG 校验、发布校验、发布拓扑快照（SuiteVersion）、子 Suite 递归执行、树形运行时聚合、并行分支 join；画布前端对接（策略设计器 `/designer`）已完成 |
| **优先级** | P0 |
| **依赖** | `cases.Case` |

#### 功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| S-01 | Suite 模型（名称、聚合方式、父 Suite、状态、版本、**占用资金 allocated_capital**、**运行状态 run_status**） | ✅ 完成 |
| S-02 | Edge 模型（源 Suite → 目标 Suite、条件、事件条件、权重） | ✅ 完成 |
| S-03 | Suite CRUD API | ✅ 完成 |
| S-04 | DAG 无环校验（发布前检查） | ✅ 完成 |
| S-05 | 拓扑获取接口（完整树形结构，供前端画布渲染） | ✅ 完成 |
| S-06 | 拓扑更新接口（批量增删节点和边） | ✅ 完成 |
| S-07 | Suite 发布（递归校验所有引用 Case/Suite 已发布） | ✅ 完成 |
| S-08 | 删除保护（被 Plan 引用时返回 409 Conflict） | ✅ 完成 |
| S-09 | 条件路由支持（`Edge.event_condition` 匹配事件类型） | ✅ 完成（快照驱动路由，出边按 CASE_COMPLETED 匹配递归触发） |
| S-10 | 聚合方式支持（加权求和 / 投票 / 逻辑与 / 逻辑或） | ✅ 完成（节点内 Case 聚合 + 父子 Suite 树形聚合，分支结果按边权重汇合） |
| S-11 | 并行节点执行支持 | ✅ 完成（parallel 模式多分支线程并发执行，join 汇合；fail_stop 失败传播已贯彻） |

#### 数据模型

```python
class Suite(models.Model):
    AGGREGATE_CHOICES = [
        ('weighted_sum', '加权求和'),
        ('vote', '投票'),
        ('and', '逻辑与'),
        ('or', '逻辑或'),
    ]
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('published', '已发布'),
        ('archived', '已归档'),
    ]
    name = models.CharField(max_length=100)
    aggregate_method = models.CharField(max_length=20, choices=AGGREGATE_CHOICES, default='weighted_sum')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

class Edge(models.Model):
    from_suite = models.ForeignKey(Suite, on_delete=models.CASCADE, related_name='out_edges')
    to_suite = models.ForeignKey(Suite, on_delete=models.CASCADE, related_name='in_edges')
    condition = models.JSONField(default=dict, blank=True)          # 保留，用于扩展
    event_condition = models.JSONField(default=dict, blank=True)    # 事件触发条件
    weight = models.FloatField(default=1.0)
```

#### API 端点

| 方法 | 端点 | 功能 |
|------|------|------|
| GET/POST | `/api/suites/` | Suite 列表/创建 |
| GET/PUT/DELETE | `/api/suites/{id}/` | Suite 详情/更新/删除 |
| GET | `/api/suites/{id}/topology/` | 获取完整拓扑 |
| POST | `/api/suites/{id}/topology/` | 更新拓扑 |
| POST | `/api/suites/{id}/publish/` | 发布 Suite |

当前实现文件：`models.py`、`serializers.py`、`services.py`、`views.py`、`urls.py`。拓扑更新在事务中替换 Case 关联和当前 Suite 出边，并在提交前执行 DAG 校验。发布时同步生成不可变拓扑快照（`SuiteVersion`，含递归子树 Case/边/聚合方式），运行引擎只读快照保证执行一致性；执行期每个节点（Suite/Case）落 `NodeRun` 运行实例支持轨迹回放。`final_direction` 语义为根节点完整聚合（本节点 Case 结果 + 所有子 Suite 分支结果按边权重汇合）。


### 模块7：`plans`（调度管理）✅ P0 能力已完成

| 属性 | 说明 |
|------|------|
| **状态** | ✅ 已完成 CRUD、触发校验、标的解析、发布、配置刷新、持久化 Cron 调度、重试策略校验和版本回滚 |
| **优先级** | P0 |
| **依赖** | `suites.Suite`, `watchlists`（解析 symbol_scope） |

#### 功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| P-01 | Plan 模型（名称、根 Suite、触发方式、Cron 表达式、标的范围、执行模式、重试策略、状态、版本、**运行状态 run_status**、**Suite 启动模式 suite_start_mode**） | ✅ 完成 |
| P-02 | Plan CRUD API | ✅ 完成 |
| P-03 | Plan 发布（校验根 Suite 已发布 + 创建版本快照 + 通知异步引擎热加载） | ✅ 完成 |
| P-04 | 标的范围解析（all / 分组 / 指定列表 → 调用 `watchlists.services.resolve_symbol_scope`） | ✅ 完成 |
| P-05 | Cron 表达式校验 | ✅ 基础完成（5 字段和字符校验） |
| P-06 | Plan 删除保护（已有执行记录时返回 409 Conflict） | ✅ 完成 |
| P-07 | 触发方式支持（时间驱动 / 事件驱动 / 手动触发） | ✅ 完成 |
| P-08 | 执行模式支持（串行 / 并行 / 失败停止） | ✅ 完成（runner 执行链已支持） |
| P-09 | 重试策略配置（重试次数 + 延迟秒数） | ✅ 完成（参数校验 + WorkerPool 执行） |
| P-10 | Plan 历史版本回滚 | ✅ 完成（回滚生成新发布版本） |

#### 数据模型

```python
class Plan(models.Model):
    TRIGGER_CHOICES = [
        ('time', '时间驱动'),
        ('event', '事件驱动'),
        ('manual', '手动触发'),
    ]
    EXEC_MODE_CHOICES = [
        ('serial', '串行'),
        ('parallel', '并行'),
        ('fail_stop', '失败停止'),
    ]
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('published', '已发布'),
        ('archived', '已归档'),
    ]
    name = models.CharField(max_length=100)
    root_suite = models.ForeignKey(Suite, on_delete=models.PROTECT)
    trigger_type = models.CharField(max_length=20, choices=TRIGGER_CHOICES)
    cron_expr = models.CharField(max_length=100, blank=True, null=True)
    event_type = models.CharField(max_length=50, blank=True, null=True)
    symbol_scope = models.JSONField(default=dict)   # 示例: {"type":"all"} 或 {"type":"groups","group_ids":[1,2]}
    exec_mode = models.CharField(max_length=20, choices=EXEC_MODE_CHOICES, default='serial')
    retry_policy = models.JSONField(default=dict, blank=True)   # {"max_retries": 3, "delay_seconds": 5}
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
```

#### API 端点

| 方法 | 端点 | 功能 |
|------|------|------|
| GET/POST | `/api/plans/` | Plan 列表/创建 |
| GET/PUT/DELETE | `/api/plans/{id}/` | Plan 详情/更新/删除 |
| POST | `/api/plans/{id}/publish/` | 发布 Plan |
| GET | `/api/plans/{id}/symbols/` | 解析 Plan 的标的范围 |
| GET | `/api/plans/{id}/versions/` | 查询 Plan 历史版本 |
| POST | `/api/plans/{id}/rollback/` | 将指定历史版本恢复为新的发布版本 |

当前实现文件：`models.py`、`serializers.py`、`services.py`、`views.py`、`urls.py`、`management/commands/run_scheduler.py`。Plan 只能在发布时要求根 Suite 已发布，创建和编辑阶段允许保存草稿配置。

#### 持久化 Cron 调度与配置刷新

- `runner.scheduler.Scheduler.run_forever()` 提供可停止的常驻轮询循环，默认每 60 秒检查一次。
- `runner.registry.PlanRegistry.sync_from_database()` 每轮从数据库加载已发布 Plan，按 `version` 刷新配置，并移除已归档或取消发布的 Plan。
- `apps/plans/management/commands/run_scheduler.py` 提供 Django 进程入口：

```bash
.\venv\Scripts\python.exe .\manage.py run_scheduler --interval 60
```

- 同一分钟内同一 `(Plan, Symbol)` 任务只入队一次；进程收到停止信号后退出轮询。
- 当前能力覆盖自动 Cron 触发、配置热刷新、重试策略校验和历史版本回滚。
- **单机部署评估（2026-09-10）**：当前部署目标为单机，同一时刻只运行一个 Scheduler 实例，进程内 `_enqueued` 去重已保证“同一分钟同一 `(Plan, Symbol)` 只入队一次”。跨进程分布式去重、租约/领导者选举和任务幂等键仅在多机/多实例场景才必要，因此该治理任务由 P1 降为 **P4**（见 5.1.2）；单机部署阶段无需开发，未来扩展多机部署时再评估。


### 模块8：`runner`（独立异步引擎）✅ P0 核心能力已完成

| 属性 | 说明 |
|------|------|
| **状态** | ✅ EventLoop、SuiteRunner、Scheduler、TaskQueue、WorkerPool、真实行情数据上下文、技术指标因子引擎、基础风控、热加载注册中心和执行日志链路已完成；真实交易回报、总仓位风控和基本面扩展属于 P1 增强 |
| **优先级** | P0 |
| **依赖** | `execution.SuiteRun`, `execution.Event`, `cases.Case`, `suites.Suite`, `plans.Plan` |

#### 功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| R-01 | **Scheduler（调度器）**：定时扫描 Plan，按 Cron 表达式触发执行 | ✅ 完成 |
| R-02 | **Task Queue**：任务入队（每个 `(Plan, Symbol)` 为一个独立任务） | ✅ 完成 |
| R-03 | **Worker Pool**：固定数量协程并发执行任务 | ✅ 完成（并发、重试和失败传播） |
| R-04 | **SuiteRunner**：加载 Suite 拓扑，创建 SuiteRun 实例 | ✅ 完成；优先读取发布快照（SuiteVersion），支持子 Suite 递归执行 |
| R-05 | **EventLoop**：消费 SuiteRun.event_queue，匹配事件 → 执行 Case → 产出新事件 | ✅ 完成；快照驱动编排：子 Suite 递归执行、树形聚合、parallel 分支并发 join、fail_stop 失败传播、NodeRun 轨迹记录 |
| R-06 | **CaseExecutor**：执行单个 Case 的运算逻辑（因子计算/过滤/裁决） | ✅ 技术指标引擎已接入（MA/EMA/MACD/RSI/KDJ/BOLL/ROC/波动率/涨跌幅 + 过滤 + 综合裁决）；兼容声明式 result |
| R-07 | **数据夹具（Fixture）**：为 Case 执行提供数据上下文（K线/基本面/实时快照） | ✅ DB 分表 K线 + RealtimeSnapshot 实时快照 + gm SDK 行情回退 + AkShare 基本面基础字段已接入（`DataContextBuilder`） |
| R-08 | **风控拦截器**：在 Executor 节点输出前校验仓位/资金限制 | ✅ 单向持仓（long_only/short_only/flat）、单笔数量/金额上限、每日累计金额上限、交易时段校验已接入（`RiskController`） |
| R-09 | **热加载**：Plan 发布后自动刷新内存中的 DAG 配置 | ✅ `PlanRegistry` 已固化可执行快照并支持冷启动自愈；Scheduler 经注册中心读取已发布 Plan |
| R-10 | **执行日志写入**：将执行结果写入 ExecutionLog 表 | ✅ 完成 |
| R-11 | **委托单生成**：将 Executor 节点的输出转换为 Order 记录 | ✅ 完成 |

当前实现文件：`runner/executor.py`、`runner/engine.py`、`runner/queue.py`、`runner/scheduler.py`、`runner/gm_adapter.py`、`runner/fundamentals.py`。Case 可通过 `params.result` 声明 direction、payload 和 order；`GmBrokerAdapter` 已封装 gm SDK 的 `set_token`、`subscribe`、`history`、`history_n`、`schedule`、`order_volume`、`get_orders` 及订单状态回调。真实因子、行情 Fixture、风控和交易回报的生产策略仍可在该适配边界上继续扩展。

#### 基本面数据上下文设计

当前基本面数据源选择 **AkShare**，原因是项目已有 AkShare 依赖、当前业务以 A 股为主，且 `stock_individual_info_em` 可直接返回个股基础信息和市值字段。适配器位于 `runner/fundamentals.py`，不让 AkShare 中文字段名进入 Case 执行层。

- 数据入口：`AkshareFundamentalsProvider.fetch(symbol)`
- 当前接口：`ak.stock_individual_info_em(symbol=<六位代码>)`
- 当前规范化字段：`symbol`、`name`、`shares_outstanding`、`shares_float`、`market_cap`、`float_market_cap`、`industry`、`listing_date`
- 上下文结构：

```python
{
    'provider': 'akshare',
    'symbol': '000001',
    'asof': '2026-09-06T12:00:00',
    'metrics': {
        'market_cap': 123456789.0,
        'industry': '银行',
    },
}
```

- `DataContextBuilder` 通过 `fundamentals_provider` 支持依赖注入，后续可替换 TuShare、本地缓存或数据库 Provider。
- 开发环境默认 `FUNDAMENTALS_ENABLED=False`，避免测试和离线策略执行访问外部网络。
- 生产环境默认开启，也可通过 `FUNDAMENTALS_ENABLED=0` 关闭。
- 数据源异常、空响应、非 A 股标的均安全降级为 `metrics={}`，不阻塞 Case 执行。

当前版本定位为“个股基础信息/估值基础字段”接入，不代表完整财务报表能力。后续应增加财务指标、资产负债表/利润表/现金流量表、历史时点缓存和数据有效期校验，并为基本面数据增加独立缓存或持久化表。

#### 执行流程

```
1. Scheduler 唤醒 Plan → 2. 解析 symbol_scope 获取标的列表
   → 3. 为每个 (Plan, Symbol) 创建任务入队
   → 4. Worker 从队列取出任务
   → 5. 创建 SuiteRun 实例（状态: pending）
   → 6. 加载 Suite 编排树（优先发布快照 SuiteVersion，回退实时构建）
   → 7. 注入 INIT 事件到 event_queue
   → 8. 进入事件循环（EventLoop）：
        while event_queue:
            event = event_queue.pop(0)
            定位事件目标节点（根 Suite 或 target_suite_id 指向的子 Suite）
            匹配订阅该事件的 Case（按节点快照内 trigger 过滤）
            for each matched Case:
                执行 Case（parallel 模式线程并发；fail_stop 失败即终止）
                记录 NodeRun（case 节点）
                产出 CASE_COMPLETED 事件
            节点内聚合 Case 结果
            按 CASE_COMPLETED 匹配出边 → 递归执行子 Suite 分支
              （parallel 模式多分支并发；全部分支完成即 join 汇合）
            分支结果按边权重并入父节点聚合，更新 NodeRun（suite 节点）
   → 9. Suite 完成 → 写入 ExecutionLog（final_direction = 根节点完整聚合）
   → 10. 若为 Executor 节点 → 风控校验 → 生成 Order
```

### 模块9：`monitoring`（分时监控）🟡 设计定稿 · 待实施

| 属性 | 说明 |
|------|------|
| **状态** | ✅ 全部完成（2026-09-12：后端 26 个专项测试通过；前端 ECharts 分时监控页 `/monitoring` 已落地，`vue-tsc` + `vite build` 通过；2026-09-14 分时数据源由 akshare 替换为 **gm SDK** 并新增启动完整性回填，测试增至 48 个） |
| **优先级** | P1 |
| **依赖** | `watchlists.Symbol`, `datasources`（快照数据源） |

#### 设计约束

- **分时数据为临时数据**：生命周期「开盘记录 → 收盘清空」，收盘后清空全表，不做长期持久化（持久化意义小，表膨胀无收益）。
- **多市场时区感知**：A 股（Asia/Shanghai）、港股（Asia/Hong_Kong）、美股（America/New_York，含夏令时）时区与交易时段不同，采样、清理、前端渲染均需按各市场本地时间判断。
- **存储于主库**：`IntradayPoint` 为 Django ORM 常规表（主库 `default`），每日收盘清空后表归零，数据量可控（自选池/Plan 标的范围，百级标的 × 240 分钟 = 万级行）。

#### 市场时区与交易时段

```python
# apps/monitoring/market_calendar.py
MARKET_TIMEZONES = {
    'A':  'Asia/Shanghai',
    'HK': 'Asia/Hong_Kong',
    'US': 'America/New_York',   # zoneinfo 自动支持 EDT/EST 夏令时切换
}
TRADING_SESSIONS = {
    'A':  [('09:30', '11:30'), ('13:00', '15:00')],
    'HK': [('09:30', '12:00'), ('13:00', '16:00')],
    'US': [('09:30', '16:00')],   # 连续时段
}
```

- `in_trading_session(market, now_local)` 判断当前市场本地时间是否在交易时段内。
- 采样任务按 `market` 分组标的，只对处于交易时段的市场执行采样。

#### 数据模型

```python
class IntradayPoint(models.Model):
    """分时监控点（临时数据：当日有效，收盘后由 clear_intraday 清空）。"""
    symbol    = models.ForeignKey(Symbol, on_delete=models.CASCADE, related_name='intraday_points')
    ts        = models.DateTimeField(db_index=True)   # UTC 存储（USE_TZ=True）
    price     = models.DecimalField(max_digits=12, decimal_places=4, verbose_name='现价')
    change    = models.DecimalField(max_digits=8,  decimal_places=4, verbose_name='涨跌幅%')
    volume    = models.BigIntegerField(verbose_name='累计成交量')
    amount    = models.DecimalField(max_digits=20, decimal_places=2, verbose_name='累计成交额')
    avg_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, verbose_name='均价')
    high      = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    low       = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    open_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    pre_close = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['symbol', '-ts'])]
        constraints = [
            models.UniqueConstraint(fields=('symbol', 'ts'), name='uniq_intraday_symbol_ts'),
        ]
```

- `(symbol, ts)` 唯一约束保证同一分钟重复采样被拒绝或 `update_or_create` 覆盖。
- `ts` 统一存 UTC；查询/展示层按市场时区转换（API 返回 `local_time` 字段，前端免二次转换）。

#### 数据源更新（内部更新器）

分时数据源更新由 **Django 服务进程内更新器** 自主管理（`apps/monitoring/updater.py`，随 `MonitoringConfig.ready()` 启动），**禁止单独的更新命令**（原 `manage.py sample_intraday` 已移除）：

- 启动时**清空全部分时数据**（2026-09-15：避免上次运行/前一交易日遗留点与新会话混叠；清空后由启动回填重建当日数据），再执行开盘到当前的完整性回填（`backfill_intraday`）；**不完整时每轮重试直到完整**（覆盖启动瞬时故障，如 gm 终端连接未就绪），完整后跳过不再全表比对；
- 每 `MONITORING_UPDATER_INTERVAL` 秒（默认 60，环境变量可覆盖）执行一轮采样；
- **市场开盘时清理历史数据**（2026-09-15）：每市场每本地日一次——该市场处于交易时段的首轮更新中，删除 `ts` 早于当日当地 00:00 的全部记录（往日历史），当日数据保留；非交易时段/周末不触发（UTC 23:00 收盘清理仍作兜底）；
- UTC 23:00 自动触发当日 `clear_intraday`（每自然日最多一次，幂等）；
- `MONITORING_UPDATER_ENABLED=0` 可整体关闭；test/migrate/shell 等管理命令进程不启动；
- `manage.py clear_intraday` 保留为清理兜底（清理非更新，不写入数据）。

#### 清理任务

```bash
manage.py clear_intraday [--before=YYYY-MM-DD]
```

- 删除 `ts < --before`（默认当日 00:00 UTC）的全部记录。
- 内部更新器在 **UTC 23:00** 自动触发（美股收盘后、A 股开盘前，全市场当日数据同时过期）；各市场**开盘时**另有一次「清理历史」动作（删除早于当日当地零点的记录）。
- 也可次日开盘前手动兜底。

#### API 端点

| 方法 | 端点 | 功能 |
|------|------|------|
| GET | `/api/monitoring/intraday/?symbol=000001` | 当日分时序列（时间升序） |
| GET | `/api/monitoring/intraday/realtime/?symbol=000001` | 最新一条 + RealtimeSnapshot 合并 |
| GET | `/api/monitoring/intraday/stream/?symbol=000001` | **SSE 持久化推送**（`snapshot` → 周期 `tick` → `session_status` 变化时 `session`；`interval` 5~60s 缺省 15） |

响应结构（分时序列，注意 `market`/`timezone`/`session_status` 为前端渲染依据）：

```json
{
  "symbol": "000001",
  "market": "A",
  "timezone": "Asia/Shanghai",
  "session_status": "trading",
  "pre_close": "10.20",
  "points": [
    {"ts": "2026-09-10T01:31:00Z", "local_time": "09:31", "price": "10.50",
     "change": "2.94", "avg_price": "10.48", "volume": 1200, "amount": "12600.00"},
    ...
  ]
}
```

- `session_status`：`trading` / `lunch_break` / `closed` / `pre_market`，前端据此控制轮询暂停/恢复与「已收盘」提示。
- 数据量：单标的单日 ~240 个点，一次返回无分页压力（不走分页）。

#### 前端页面

- 路由 `/monitoring`，导航菜单「分时监控」。
- ECharts 分时图：现价折线（蓝）+ 均价黄线 + 底部量能柱 + 涨跌幅着色。
- X 轴按市场本地时间渲染（`local_time` 字段）。
- **SSE 持久化连接**（EventSource 订阅 `/api/monitoring/intraday/stream/`）替代 15s 轮询：`snapshot` 全量 → `tick` 按 ts 增量合并（非全量重绘）→ `session` 状态变化即时更新；连接连续失败且未收到消息时自动降级回 HTTP 轮询。
- 非交易时段显示「已收盘」提示。
- 标的切换 tab 或下拉（自选池 / Plan 标的范围）。

##### 后端实施记录（2026-09-12）

| 交付物 | 说明 |
|--------|------|
| `apps/monitoring` 新 Django 应用 | `models.py`（IntradayPoint）、`market_calendar.py`、`snapshot_provider.py`、`services.py`、`updater.py`、`serializers.py`、`views.py`、`urls.py`、`admin.py`、`tests.py`（65 个专项测试） |
| `IntradayPoint` | 主库 `default` 常规表；(symbol, ts) 唯一约束 + (symbol, -ts) 索引；ts 存 UTC；`sample_intraday` 按分钟对齐 `ts` 并用 `update_or_create` 覆盖（同分钟幂等） |
| `market_calendar.py` | `MARKET_TIMEZONES` / `TRADING_SESSIONS` / `session_status()` / `in_trading_session()`；美股经 zoneinfo 自动处理 EDT/EST |
| `snapshot_provider.py` | `MarketSnapshotProvider` 抽象 + `AkshareSpotProvider`（A `stock_zh_a_spot_em` / HK `stock_hk_spot_em` / US `stock_us_spot_em`）+ `GmSnapshotProvider`（gm SDK tick，A 股 SHSE/SZSE）+ `CompositeSnapshotProvider`（gm 主源 + akshare 回退）；与 `runner/fundamentals.py` 相同的依赖注入模式，测试 mock 不依赖网络 |
| 管理命令 | ~~`manage.py sample_intraday`~~ **已移除（2026-09-14）**：分时数据更新由 Django 服务进程内更新器 `updater.py` 自主管理（启动回填 + 周期采样 + UTC 23:00 清理），禁止单独更新命令；`manage.py clear_intraday` 保留为清理兜底 |
| API | `GET /api/monitoring/intraday/?symbol=`（当日序列，时间升序）· `GET /api/monitoring/intraday/realtime/?symbol=`（最新一条 + RealtimeSnapshot 合并）· `GET /api/monitoring/intraday/stream/?symbol=`（SSE 持久化推送）；**不走分页**；响应含 `market` / `timezone` / `session_status` / `pre_close` / `points[]`（`ts` 为 UTC ISO-8601，`local_time` 为市场本地 HH:MM） |
| 内部更新器 + SSE（2026-09-14，**2026-09-15 增补**） | `updater.py`：`IntradayUpdater` 守护线程随 `MonitoringConfig.ready()` 启动（**启动清空全表 → 启动回填 → 每 60s 采样 → 开盘清理历史 → UTC 23:00 兜底清理**；`MONITORING_UPDATER_ENABLED`/`_INTERVAL` 配置；test/migrate/shell/`run_mcp_server` 进程不启动，runserver 仅 RUN_MAIN 子进程启动）；`views.stream`：`StreamingHttpResponse` SSE（`snapshot`→`tick`→`session`，interval 5~60s 缺省 15）；`sample_intraday` 管理命令已删除 |

设计落地说明：

- 实时快照数据源：设计中的「ashare 快照接口（`stock_zh_a_spot_em` 等）」在 `apps/datasources/ashare.py` 中不存在（该文件仅含 K 线接口）；`stock_zh_a_spot_em` 实为 **akshare** 接口，故实现为 `AkshareSpotProvider` 直接从 akshare 三市场 spot 接口拉取，未改动 `datasources.ashare` 层。
- 分时数据源改用 **gm SDK（掘金量化）**（2026-09-14）：新增 `GmSnapshotProvider`，用 gm `history(frequency='tick')` 取交易日内聚合快照（`price`/`open`/`high`/`low`/`cum_volume`/`cum_amount`），与 `IntradayPoint` 的「现价/开盘价/日内高低/累计成交量/累计成交额」契约一一对应；`pre_close` 由前一日 1d bar 的 `close` 推导，`change=(price-pre_close)/pre_close*100`。gm 仅覆盖国内市场（`SHSE.`/`SZSE.` 前缀，按 `Symbol.exchange` 或代码前缀映射），港股/美股不在覆盖范围，故 `CompositeSnapshotProvider`（gm 主源 + akshare 回退）在 A 股优先用 gm、HK/US 或 gm 失败时回退 akshare，保持多市场能力与韧性。`services.sample_intraday` 把该市场标列表传入 `fetch_market(market, symbols)`（gm 按标的拉取，非全市场批量）。
- 启动完整性回填（2026-09-14）：内部更新器启动时（进入采样循环前）调用 `services.backfill_intraday`，检查交易日内从开盘到当前是否有缺失分钟，不完整则先回填。`market_calendar.trading_minutes_local` 枚举当日已开启的交易分钟（剔除午休/周末/开盘前）；缺失分钟经 provider 可选接口 `fetch_intraday_history(market, symbol, start, end)` 拉逐分钟 bar（`GmSnapshotProvider` 用 `history(frequency='60s')` 实现，`AkshareSpotProvider`/HK/US 不支持返回空→静默跳过），按快照语义（累计成交量/成交额、日内最高/最低、开盘价、change）仅补写缺失点、不覆盖已有点。
- `GmSnapshotProvider._pre_close`（2026-09-14 修正）：昨收不再按 `history_n(1d)[-2]` 位置猜——盘前/刚开盘当日 bar 未生成时会把前天收盘误当昨收；改为取**日期严格早于今日（市场本地）**的最近一根日 bar（`count=5` + `eob/bob/date` 日期解析），无日期可解析时才保守取最后一根。
- `session_status` 判定规则（简化，不含节假日历）：周末一律 `closed`；开盘前 `pre_market`；时段内 `trading`；双时段市场两时段之间 `lunch_break`；其余 `closed`。
- **标的代码字符串处理 · 指数/个股区分（2026-09-14）**：统一规则收敛于 `apps/watchlists/services.py`——`normalize_a_share_code`（剥前后缀、补零到 6 位）、`is_a_share_index`、`resolve_a_share_exchange`。指数专属段（399/880/930/931/932/980/899）纯前缀判定；**000xxx 二义段**（000001 既是平安银行也是上证指数）必须以 `exchange` 显式标注或 `sh` 前缀判为沪指数，缺省保守按深市个股。**原始代码带 `sh/sz/bj` 前缀（如库中 `code='sh000001'`）视为显式市场标记**：交易所解析在剥前缀**之前**依据原始代码判定（`resolve_a_share_exchange`），`gm_symbol_for` 传原始代码——已实测 `sh000001` → `SHSE.000001`（上证指数）而非深市个股。接入点：`monitoring.gm_symbol_for`（SHSE/SZSE/BJSE 前缀，含北交所）、`datasources.ashare._normalize_ashare_code`（sh/sz 前缀保留指数语义）、`watchlists.sync_market_data` 交易所解析、`monitoring.AkshareSpotProvider`（A 市场请求含指数时按需合并 `stock_zh_index_spot_sina` 指数行情，全个股请求不触发指数接口；`_normalize_returned_code` 兼容 sina `sh000300` 前缀）。专项测试：watchlists 3 个 + datasources 4 个 + monitoring 4 个（含 `test_gm_symbol_for_distinguishes_index_and_stock`、指数回退合并/跳过/二义缺省）。
- 数值渲染：价格/涨跌幅以 Decimal 4 位小数字符串输出（与本项目其他模块 DecimalField 序列化一致）；单位随上游数据源原样存储，不做换算。
- 采样标的缺省范围：已发布 Plan 的 `symbol_scope` 并集；无已发布 Plan 时回退全部标的（保证独立可用）。
- 采样触发：由 **Django 服务进程内的 `updater.py` 自主管理**（启动清空 → 启动回填 → 每 `MONITORING_UPDATER_INTERVAL` 秒采样 → 开盘清理 → UTC 23:00 兜底清理），**不存在外部 cron / 计划任务调用路径**（`sample_intraday` 命令已移除）。

##### 前端实施记录（2026-09-12）

| 交付物 | 说明 |
|--------|------|
| `quant-frontend/src/views/Monitoring.vue` | ECharts 分时图：现价折线（蓝）+ 均价虚线（黄）+ 底部量能柱（按涨跌红绿着色）+ dataZoom 缩放；X 轴按市场本地时间（`local_time`）渲染；tooltip 展示现价/均价/涨跌幅/成交量/成交额 |
| `quant-frontend/src/views/Monitoring.vue` | 分时技术指标子图（2026-09-14）：主图下方追加 **MACD(12,26,9)**（DIF/DEA 线 + 红绿柱）、**KDJ(9,3,3)**（K/D/J 三线）、**RSI(14)**（30/70 参考线，Y 轴固定 0-100）三个子图，ECharts 多 grid 布局 + axisPointer 跨图联动 + dataZoom 全图联动；图例区 checkbox 开关（默认 MACD 开、KDJ/RSI 关），开关状态保存在 `localStorage('monitoring.indicators')`；图表容器高度随启用指标数自适应（380 + 150×N px）；指标在**前端逐分钟序列实时计算**（EMA/Wilder 平滑，缺数据分钟为 null 断线），随 SSE tick 增量更新自动重算 |
| 路由与导航 | `/monitoring` 路由 + 侧边菜单「分时监控」 |
| 交互 | 标的切换下拉（自选池 → 回退全量标的）；盘中 15s 轮询**增量追加**（按 `ts` 合并后 `setOption` 增量更新，非全量重绘）；`session_status` 非 `trading` 时暂停轮询并显示「已收盘 / 午休 / 开盘前」提示 |
| X 轴固定刻度（2026-09-14） | X 轴按市场固定为全交易分钟（与后端 `market_calendar` 时段一致：A=240 / HK=330 / US=390），不随已有数据伸缩；序列数据按 `local_time` 对齐固定刻度、缺失为 null；刻度只标注每 30 分钟与收盘点 |
| Y 轴最小振幅（2026-09-14） | 价格轴以昨收为中心：`±max(实际波动, 最小振幅%)×1.05`；默认 2%，图表右上 `el-input-number`（0.1~20，步进 0.1）可改，保存在 `localStorage`（`monitoring.yMinSpanPct`），刷新后保留 |
| SSE 持久化连接（2026-09-14） | `api/monitoring.ts` 新增 `subscribeIntradayStream()`（EventSource 订阅 `/api/monitoring/intraday/stream/`，处理 `snapshot`/`tick`/`session` 事件）；`Monitoring.vue` 用 SSE 替代 15s 轮询（`tick` 按 ts 增量合并），连续 3 次错误且未收到消息自动降级回 HTTP 轮询，状态标签显示「实时推送 (SSE) / 轮询中（降级）/ 推送已暂停」 |
| 验证 | `vue-tsc -b` 0 错误 + `vite build` 通过（Monitoring 产物分块已生成） |


### 模块10：`quick-strategy`（策略快速创建向导）✅ 全部完成

| 属性 | 说明 |
|------|------|
| **状态** | ✅ 全部完成（2026-09-14：前端实施完成——`QuickStrategy.vue` 3 步向导 + `quickStrategy.ts` 模板/校验/一键链路/失败清理，`vue-tsc` + `vite build` 通过；后端零改动） |
| **优先级** | P1 |
| **依赖** | `cases` / `suites` / `plans` 既有 API（前端编排）；**后端零新增接口** |

#### 定位与原则

- 目标：把「想法 → 可运行的 Plan」从 7+ 步（建 Case → 发布 → 建 Suite → 编排拓扑 → 发布 → 建 Plan → 发布 → 启动）压缩为 **3 步向导**，一条路径一键完成。
- 边界：面向**模板化、确定性**策略形态（单/多指标信号、过滤、双方向执行）；复杂多分支/自由拓扑仍走 `/designer` 画布。
- **前端优先**：参数收集、`Case.params` 生成、轻量校验、创建/发布/启动**全链路由前端编排既有 API**；后端目标**零改动**（文末可选增强不阻塞主线）。

#### 交互与入口

- 新页面 `quant-frontend/src/views/QuickStrategy.vue`，路由 `/quick-strategy`，侧边菜单「快速创建」；Dashboard 首页入口卡片。
- 3 步向导（`el-steps`）：① 模板与信号 ② 运行配置 ③ 预览与一键创建。

#### 步骤1：模板与信号

模板（前端常量，`src/utils/quickStrategy.ts`，不落库）：

| 模板 | 生成结构 | 说明 |
|------|----------|------|
| `signal_only` | 1 个 signal Case | 单指标信号，root Suite 聚合 |
| `signal_executor` | signal + filter(可选) + executor 共 2~3 个 Case | 信号 → 过滤 → 下单 |
| `dual_direction` | root（信号/裁决）+ 2 个子 Suite（buy / sell executor） | 双方向分支，root→子边事件条件 `op=eq, field=direction, threshold=1/-1` |

- 信号参数表单按 `INDICATOR_CATALOG` 渲染：indicator 下拉（ma/sma/ema/mean/macd/rsi/kdj/boll/roc/momentum/pct_change/volatility）+ `period`；MACD 增加 `fast/slow/signal`；RSI/KDJ 增加 `threshold_oversold/overbought`（前端校验 0~100 且超卖<超买）；BOLL/volatility 增加 `threshold`；`direction ∈ {-1,0,1}`。
- 过滤器（可选）：`field` + `op(keep/drop)` + `threshold/value`。
- 执行器（可选）：`order {direction: buy|sell, price, volume}` 与 `result.direction` 同步。
- `trigger` 由向导自动生成：根节点 `SUITE_INIT`；子节点由边事件 `CASE_COMPLETED` + 操作符承接（复用现有 `event_condition` 契约，无新字段）。

#### 步骤2：运行配置

- 标的范围：自选池分组多选 → `{type:'groups', group_ids}`；标的搜索多选 → `{type:'symbols', symbol_codes}`；全市场 → `{type:'all'}`。
- 触发方式：
  - 手动：`trigger_type=manual`；
  - 定时：每日 HH:MM 或「星期几+时间」选择器 → 前端生成 5 字段 `cron_expr`（校验与后端 `validate_cron_expression` 一致）；
  - 事件：下拉已注册事件（`GET /api/execution/event-types/list-all/` 缓存）。
- 执行模式：`serial / parallel / fail_stop`；重试策略 `{max_retries, delay_seconds}`。
- 资金（可选）：`account_id` + `allocated_capital`（沿用 Plan 创建资金校验；默认不填）。

#### 步骤3：预览与一键创建

- 只读树形预览（复用 Designer 的节点/边文案）：Case 列表（名称/节点类型/params 摘要）、Suite（聚合方式）、Plan（trigger / symbol_scope / exec_mode）。
- 一键执行 `quickCreateStrategy(payload)`（`src/utils/quickStrategy.ts`，顺序调用；任一步失败提示已创建资源清单并提供「重试 / 保留草稿」）：

```
1. POST /api/cases/ ×n              创建 draft Cases（Promise.all）
2. POST /api/suites/                创建 root Suite（case_ids）
   （dual_direction）创建 2 个子 Suite + POST /api/suites/{root}/topology/
3. POST /api/cases/{id}/publish/ ×n
4. POST /api/suites/{id}/publish/ （要求全部 Case 已发布，前端先发布 Case）
5. POST /api/plans/                 root_suite + trigger + symbol_scope + exec_mode + retry_policy
6. POST /api/plans/{id}/publish/   （要求根 Suite 已发布）
7. （manual 且 suite_start_mode=auto 时可选）POST /api/plans/{id}/start/
```

#### 前端校验前置（避免后端 400 往返）

- `quickStrategy.ts` 内置与后端 `validate_case_schema` 对齐的轻量校验：params 白名单键、indicator/calculation 目录、RSI/KDJ 阈值区间、order/filter/verdict 结构、MACD fast<slow。
- 向导加载时缓存 `event-types/list-all/`，trigger 事件类型即时校验。

#### 后端改动

- **目标：零改动**（全部复用既有端点，P-03 热加载自动感知新发布 Plan）。
- 可选增强（P2，不阻塞主线）：`POST /api/plans/create-with-publish/` 事务化批量「创建+发布」，返回幂等键，仅为消除中途失败残留；若采纳补充专项测试。

#### 验收标准

- 前端：`vue-tsc -b` 0 错误 + `vite build` 通过；三模板一键链路端到端验证（生成 Case.params 与后端校验一致、Plan `symbols` 解析正确）。
- 失败路径：中途接口失败时准确提示已在库资源，支持重试；不产生半发布状态。
- 测试：前端契约用例（params 生成器等值断言 ≤8 个）并入 P1 阶段5；后端不改动（既有 345 口径回归不受影响）。


##### 前端实施记录（2026-09-14）

| 交付物 | 说明 |
|--------|------|
| `quant-frontend/src/utils/quickStrategy.ts` | 模板与蓝图构建：`INDICATOR_CATALOG` 前端目录（与后端因子目录一致）+ 指标专属字段（MACD fast/slow/signal、RSI/KDJ 阈值区间、BOLL/volatility threshold）+ `buildSignalParams` / `buildFilterParams` / `buildExecutorParams`；三模板蓝图 `buildBlueprint`（`signal_only` 1 Case、`signal_executor` 2~3 Case、`dual_direction` root+双子 Suite，子边 `op=eq field=direction threshold=±1` 分流）；`cronForSchedule`（UI 星期语义 0=周一 → 后端 5 字段 cron）；轻量校验 `validateQuickForm`（params 白名单键、指标目录、RSI/KDJ 阈值区间与超卖<超买、order 结构、trigger 事件类型经 `event-types/list-all/` 缓存校验）；一键链路 `quickCreateStrategy`（建 Case → 建 Suite(+拓扑边) → 发布 Case → 发布 Suite（子先根后）→ 建 Plan → 发布 Plan → （manual+auto）可选启动，启动失败仅提示不回滚；任一步失败抛 `QuickCreateError` 含已创建资源清单）；失败清理 `cleanupCreated`（Plan → Suites → Cases 依赖逆序删除） |
| `quant-frontend/src/views/QuickStrategy.vue` | 3 步向导（el-steps）：① 模板选择 + 信号参数表单（指标下拉 + period + 指标专属字段按模板联动显隐）② 运行配置（标的范围 groups/symbols/all + 触发方式 manual/time/event + cron 选择器（每日/每周）+ exec_mode + retry + suite_start_mode + 可选资金）③ 只读预览（Case params 摘要 / Suite 树 / Plan 配置）+ 一键创建（进度步骤展示 + 失败展示已创建资源清单与「重试 / 保留草稿」）|
| `quant-frontend/src/api/strategy.ts` | 新增 `createCase` / `publishCase` / `createSuite` / `updateTopology` / `publishSuite` / `createPlan` / `publishPlan` / `startPlan` 及删除方法、资金占用 API 封装 |
| 路由与导航 | `/quick-strategy` 路由 + 侧边菜单「快速创建」（MagicStick 图标） |
| `quant-frontend/src/views/Home.vue` | 首页改造：入口卡片（快速创建 / 策略设计器 / 分时监控 / 执行监控）+ 关键路径步骤条，替换原计数器占位页 |
| 验证 | `vue-tsc -b` 0 错误 + `vite build` 通过（`QuickStrategy` 分块 27.6 kB JS + 2.4 kB CSS）；后端零改动（既有 345 口径回归不受影响） |

设计落地说明：

- 一键链路实际执行顺序为「建 Case → 建 Suite（含拓扑边）→ 发布 Case → 发布 Suite（子先根后）→ 建 Plan → 发布 Plan → 可选启动」——与设计稿等价：拓扑边随 Suite 创建后立即写入（`POST /api/suites/{id}/topology/`），先于所有发布动作；发布顺序满足「Suite 发布要求全部 Case 已发布」「Plan 发布要求根 Suite 已发布」两条既有校验。
- 双向模板的买入/卖出 executor Case `result.direction` 分别为 `1` / `-1`，与 root→子边 `op=eq field=direction threshold=±1` 事件条件形成闭环；信号 Case 与执行器 Case 均挂在 root Suite 内（root 聚合）。
- 可选启动步骤：仅当 `trigger_type=manual` 且 `suite_start_mode=auto` 时执行 `POST /api/plans/{id}/start/`（`start_plan` 前置仅要求 `run_status=new`，刚发布的 Plan 满足）；**启动失败不回滚**——已发布 Plan 是完整交付物，启动失败仅在步骤清单中提示「可稍后在 Plan 管理页手动启动」。
- 失败清理为**可选动作**（用户选「保留草稿」则不动库）：`cleanupCreated` 按依赖逆序删除（Plan → Suites → Cases），删除失败逐项提示但不中断清理流程；删除接口沿用既有删除保护契约（被引用时 409）。

### 模块11：`mcp_server`（AI 助手接入层 · MCP 服务）✅ 已完成

| 属性 | 说明 |
|------|------|
| **状态** | ✅ 全部完成（2026-09-15：`mcp_server` 包（**SSE/HTTP 为主传输**，`stdio` 保留）+ 14 个工具 + 1 个概览资源 + `/health` 健康检查；`manage.py run_mcp_server` 的 SSE 握手与令牌鉴权已实测。2026-09-17：命令行 `--allow-trigger` 写开关（MCP-18）。**2026-09-21：MCP-19 变量描述——14 个工具逐变量带 `inputSchema` 描述 + CLI/配置变量逐个带说明**；46 个专项测试通过） |
| **优先级** | P1 |
| **依赖** | `watchlists` / `datasources` / `cases` / `suites` / `plans` / `execution` / `monitoring` 既有模型与服务（只读门面，**后端零新增 REST 接口**） |
| **新增依赖** | `mcp[cli]>=2.2.0`；`uvicorn>=0.31.1`、`starlette>=0.27`（SSE 服务与 CORS/响应）；`pydantic>=2.0`（工具变量描述 `Annotated[<类型>, Field(description=...)]`，MCP-19）——三者均已显式登记 `requirements.txt` |

#### 定位与边界

- 目标：把「AI 助手 / 编码助手」接入既有量化系统——标的、K 线、策略元数据、告警、分时监控的可查询视图，并提供**默认关闭**的受控触发入口。
- **接入层而非执行层**：`mcp_server` 是**入站适配器**（与 `runner` 同为叶子包），依赖方向 `mcp_server → apps.*`；`apps.*` 不反向依赖 `mcp_server`，Django 仍不参与运行时调度，Suite 仍由 `runner` 执行。
- **默认只读**：除 `trigger_plan_execution` 外全部只读；`trigger_plan_execution` 仅创建 `pending` `SuiteRun`（交由 `runner` 调度），**MCP 不直接下单**，且需环境变量 `MCP_ALLOW_TRIGGER=1` 显式开启。
- **传输面向 Web 项目**：默认 **SSE（HTTP）** —— `manage.py run_mcp_server` / `python -m mcp_server` 以 ASGI 服务形式常驻监听（默认 `127.0.0.1:8765`），AI 助手与前端按 URL 接入；`--transport stdio` 保留给本机 IDE 类客户端（子进程方式）。
- **进程职责单一**：MCP 服务进程不承担分时更新——`run_mcp_server` 已加入 `MonitoringConfig.ready()` 的跳过命令集，`python -m mcp_server` 由 `bootstrap.setup_django()` 默认置 `MONITORING_UPDATER_ENABLED=0`；分时采样只由 Django 服务进程负责，避免多进程重复外部请求与写库。

#### 工具清单（MCP-01 ~ MCP-12）

| 工具 | 能力 | 复用后端 |
|------|------|----------|
| `search_symbols` | 代码/名称模糊搜索，`market` 过滤，`limit` 收敛 1~200 | `watchlists.Symbol` |
| `resolve_symbol_name` | 代码 → 中文名（优先读库，回退 `watchlists.services.resolve_symbol_name`） | `watchlists` |
| `query_kline` | 分表 K 线（缺省近 90 日，最多 500 根，超出保留最近 N 根） | `datasources.services.query_kline_table` |
| `list_plans` / `get_plan` | Plan 列表（缺省 `published`）/ 详情（可选解析 `symbol_scope` 为标的列表） | `plans` + `resolve_plan_symbols` |
| `list_cases` / `get_case` | Case 列表（`status`/`node_type` 过滤）/ 详情（含 `params`） | `cases.Case` |
| `get_suite_topology` | Suite 递归拓扑快照 | `suites.services.build_topology_snapshot` |
| `list_event_types` | 已注册事件类型（系统内置 + `EventTypeRegistry`） | `execution.registry.EventRegistry.list_all` |
| `list_alerts` / `alert_statistics` | 告警列表（`status`/`severity`）/ 统计（与 `/api/execution/alerts/statistics/` 同口径） | `execution.Alert` |
| `get_intraday_series` | 当日分时序列（市场时区 / `session_status` / `pre_close`），**不走外部网络** | `monitoring` 模型 + 序列化器 + `market_calendar` |
| `list_suite_runs` | 最近运行实例（`plan_id` / `symbol` 过滤） | `execution.SuiteRun` |
| `trigger_plan_execution` | **写操作**：为 Plan + 标的创建 `pending` SuiteRun（需 `MCP_ALLOW_TRIGGER=1`） | `execution.services.trigger_plan` |

资源：`quant://docs/overview`（text/plain，系统概览与安全边界说明，同 `server.instructions`）。

#### 输入输出契约

- 所有输出经 `mcp_server/formatting.to_jsonable` 归一：`Decimal → 字符串`（与项目 DecimalField 序列化一致）、`date/datetime → ISO-8601`、模型实例 `→ pk`、其余 `→ str`，确保 MCP 结构化输出不含自定义类型。
- 参数校验失败抛 `ValueError`（资源不存在 / 空代码 / 反向日期窗口），写开关未开启抛 `PermissionError`，均带可定位的中文提示。
- `limit` 在工具内收敛上限（标的 200 / K 线 500 / 告警 100 / 运行 100），单次调用不会拉全表；分页 `N-01` 契约不适用于 MCP（工具自带 `limit`，非 REST 列表接口）。
- **变量描述（MCP-19）**：每个工具入参都用 `Annotated[<类型>, Field(description=...)]` 声明中文说明，随 `tools/list` 下发为 `inputSchema.properties.<变量>.description`（mcp 2.x 不再解析 docstring 参数段落，故必须显式标注）；`tools_impl` 门面函数逐个变量给出 `Args` / `Returns` / `Raises`；命令行参数与 `MCP_*` 配置项分别由 `argparse` 的 `help` 与 `config.py` 模块文档 + 字段行内注释承载。

#### 装配与运行

```powershell
# 推荐：SSE（HTTP）常驻服务，客户端按 URL 接入
.\.venv\Scripts\python.exe .\manage.py run_mcp_server --port 8765
# 需要对外暴露时必须配令牌（否则拒绝启动）：--host 0.0.0.0 --auth-token <token>

# 等价包入口
.\.venv\Scripts\python.exe -m mcp_server --transport sse

# 本机 IDE 客户端：stdio 子进程
.\.venv\Scripts\python.exe -m mcp_server --transport stdio
```

- `mcp_server/server.py::create_server()` 装配 `MCPServer`（工具/资源/`/health`）；`build_http_app()` 装配 SSE ASGI 应用（DNS rebinding 保护 + Bearer 鉴权 + 可选 CORS）；`run_http_server()` 以 uvicorn 常驻监听；`__main__.py` → `server.main()`（解析 `--transport/--host/--port/--auth-token`）→ `bootstrap.setup_django()`。
- `mcp_server/bootstrap.py` 默认 `DJANGO_SETTINGS_MODULE=quant_engine.settings.dev`（可用环境变量覆盖），并默认关闭分时更新器。
- 传输配置（`MCP_TRANSPORT`/`MCP_HOST`/`MCP_PORT`/`MCP_AUTH_TOKEN`/`MCP_ALLOWED_HOSTS`/`MCP_ALLOWED_ORIGINS`/`MCP_CORS_ORIGINS`）见模块11「传输、鉴权与运维」；MCP 客户端配置示例见 `README.md`「MCP 服务（AI 助手接入）」。

#### 命令行写开关（MCP-18，2026-09-17）

- ✅ 已完成：`--allow-trigger` 支持管理命令与包入口，适用于 SSE 和 stdio；等效于当前进程设置 `MCP_ALLOW_TRIGGER=1`。
- 显式传入开关优先于环境变量（包括 `MCP_ALLOW_TRIGGER=0`）；未传参则沿用环境变量（`1` / `true` / `yes` 开启），未配置时仍为只读。此参数不接受附加值。
- 只开放既有 `trigger_plan_execution` 门禁；仅创建 `pending SuiteRun`，不直接下单，不绕过 Plan/标的校验及非回环地址的令牌要求。
- 无新增 REST API、数据模型、JSON 白名单字段或依赖；MCP 工具保持 14 个。SSE 客户端不能通过连接参数开启写权限；stdio 客户端可在启动 `args` 中追加 `"--allow-trigger"`。配置变更需重启 MCP 服务。

```powershell
& 'c:\Users\PC\Documents\quant_platform\quant_engine\.venv\Scripts\python.exe' 'c:\Users\PC\Documents\quant_platform\quant_engine\manage.py' run_mcp_server --port 8765 --allow-trigger
& 'c:\Users\PC\Documents\quant_platform\quant_engine\.venv\Scripts\python.exe' -m mcp_server --transport stdio --allow-trigger
```

验证（2026-09-17）：MCP 专项 **41 个测试全部通过**（原 36 个 + 本次 5 个）。覆盖配置解析、入口转发、两种传输下的实际工具门禁、环境变量兼容及鉴权不可绕过；新增启动与触发测试使用 mock，不提交实盘订单。两个入口的 `--help` 均已验证。下方 2026-09-15 的 36 个用例记录为历史基线。

完整回归（2026-09-17）：`manage.py test --noinput -v 0` **437 个测试全部通过（OK，Python 子进程退出码 0）**，包含跨模块联动；数量以该次无标签运行输出为准，不对各模块求和。日志中仍有既有动态 K 线模型重复注册警告。

```powershell
& 'c:\Users\PC\Documents\quant_platform\quant_engine\.venv\Scripts\python.exe' 'c:\Users\PC\Documents\quant_platform\quant_engine\manage.py' test mcp_server -v 1 --noinput
```

#### 变量描述（MCP-19，2026-09-21）

- ✅ 已完成：为 MCP 服务的每一类「变量」补齐描述，AI 客户端与运维不必再猜语义（**只补描述，不改契约**）。
  - **工具入参（14 个只读/触发工具 / 29 个变量）**：`mcp_server/server.py` 的 `@server.tool` 函数改用 `Annotated[<类型>, Field(description=...)]`，SDK 转成 `tools/list → inputSchema.properties.<变量>.description`，说明中给出含义、默认值、取值域与收敛规则（如 `limit` 越界收敛、`status` 可选枚举）。
  - **门面实现**：`mcp_server/tools_impl.py` 14 个函数逐个补 `Args` / `Returns` / `Raises`，逐变量说明入参与输出字段（含 `symbol_scope` 解析上限、`bars` 归一方式、异常类型）；`formatting.to_jsonable` 亦说明归一规则。
  - **命令行变量**：`server._build_arg_parser()` 与 `manage.py run_mcp_server` 的 `--transport` / `--host` / `--port` / `--auth-token` / `--allow-trigger` 全部带 `help`（默认值来源 + 取值域 + 安全约束），`--help` 可直接当运维手册。
  - **配置变量**：`mcp_server/config.py` 保留 `MCP_*` 变量表，并为每个 dataclass 字段、`DEFAULT_*` 常量加行内注释（含「非回环绑定必须令牌」「`MCP_ALLOW_TRIGGER` 默认关闭」等边界）。
- 契约测试（`mcp_server/tests.py` 新增 5 个用例）：① 每个入参 `description` 非空且长度 ≥ 6；② `inputSchema` 变量名与必填集合必须与 `tools_impl` 门面签名逐一相等；③ 工具说明非空且门面 docstring 覆盖每个变量（`Args` / `Returns`）；④ 两个 CLI 入口的 MCP 自有选项 `help` 非空且集合一致；⑤ 代码读取的每个 `MCP_*` 变量都必须出现在 `config.py` 模块文档中（防止新增变量漏文档）。
- 边界：工具数量（14）、入参名、默认值、必填项、返回结构与安全门禁（只读默认、`MCP_ALLOW_TRIGGER` / `--allow-trigger`、非回环绑定令牌）均未变；无新增 REST API、数据模型或 JSON 白名单字段。新增直接依赖 `pydantic>=2.0`（`mcp[cli]` 的必需传递依赖，本机已安装，无新增下载），仅用于 `Field` 描述声明。

验证（2026-09-21）：MCP 专项 **46 个测试全部通过**（原 41 个 + MCP-19 5 个）；`tools/list` 实测 14 个工具、29 个入参全部带中文描述，门面签名与 schema 完全一致。全量回归 `manage.py test --noinput -v 0` **442 个测试全部通过（OK，退出码 0）**。

#### 配置写工具（MCP-20，2026-09-21）

- ✅ 已完成：MCP 新增 10 个**受控配置写工具**，覆盖 Case / Suite / Plan 的创建、编辑、删除（Suite 拓扑整体替换单列）：

| 工具 | 能力 | 复用后端（与 REST 同源校验） |
|------|------|------------------------------|
| `create_case` / `update_case` / `delete_case` | draft Case 增改删 | `cases` serializers（`validate_case_schema` 白名单 + EventRegistry）+ 删除保护服务 |
| `create_suite` / `update_suite` | draft Suite 基本字段（聚合方式/父 Suite/挂载 Case/占用资金） | `suites` serializers + services |
| `update_suite_topology` | 整体替换 Case 挂载与出边（事务内 DAG + `event_condition` 白名单校验） | `suites.services.update_topology` |
| `delete_suite` | 删除 Suite | 删除保护（被 Plan 引用拒绝） |
| `create_plan` / `update_plan` / `delete_plan` | draft Plan 增改删 | `plans` serializers（cron / 事件注册 / `symbol_scope` 白名单 / 账户资金）+ 删除保护 |

- **门禁独立于执行开关**：默认禁用（`PermissionError`），需 `MCP_ALLOW_MUTATE=1` 或启动参数 `--allow-mutate`；与 `MCP_ALLOW_TRIGGER` / `--allow-trigger` 互不影响。布尔开关，两个入口（`manage.py run_mcp_server`、`python -m mcp_server`）与两种传输（SSE/stdio）均支持；显式传参覆盖环境变量（含 `=0`），配置变更需重启。
- **边界**：只改 **draft** 配置——不发布、不启动、不下单、不触碰状态机与版本快照；publish / rollback / start / stop 仍走 REST 动作接口。删除冲突抛 `MutationConflictError`（REST 409 语义），校验失败抛可定位 `ValueError`（REST 400 同源信息）。无新增 REST API、数据模型或 JSON 白名单字段；工具数 14 → **24**。
- 验证（2026-09-21）：MCP 专项 **57 个测试全部通过**（46 + MCP-20 11 个：默认 10 工具全拒绝、开关独立性、三资源增改删回环、params/`symbol_scope`/cron/拓扑条件非法值与 REST 同源拒绝、三类删除保护冲突）。全量回归 `manage.py test --noinput -v 0` **453 个测试全部通过（OK，退出码 0）**。两个入口 `--help` 均已验证。

#### 测试（P1 阶段6）

- `mcp_server/tests.py` 57 个用例：
  - 工具门面（MCP-02 ~ MCP-12）：搜索与 `market` 过滤、命名解析库内命中与回退、分表 K 线窗口与尾段截断、Plan 详情与标的解析、Case 过滤与拓扑快照、事件类型、告警列表与统计、SuiteRun 过滤、分时序列字段契约、`to_jsonable` 归一；
  - 错误契约：未入库标的、空代码、反向日期窗口、资源不存在（均抛可定位 `ValueError`）；
  - 写开关（MCP-10）：默认 `PermissionError`；开启后仅创建 `pending` SuiteRun 且 `Order` 计数为 0；空标的列表与未发布 Plan 仍拒绝；
  - 装配层（MCP-01/13）：14 工具 + 1 资源注册、`bootstrap` 默认关闭分时更新器、概览文本声明边界；
  - **SSE 传输与安全（MCP-14 ~ MCP-16）**：配置默认值与覆盖、端口/传输类型校验、非回环绑定必须令牌、`/sse` `/messages` `/health` 路由、健康检查在开/关令牌下的 200/401、DNS rebinding 保护拒绝非法 `Host`；
  - **进程门禁（MCP-17）**：`run_mcp_server` 进程不启动分时更新器，`runserver` 子进程仍启动；
  - **变量描述（MCP-19）**：`McpToolSchemaTest`（29 个入参 `inputSchema` 描述非空且长度 ≥ 6；变量名与必填集合与 `tools_impl` 门面签名逐一相等；工具说明非空且门面 docstring 逐变量给出 `Args`/`Returns`）+ `McpVariablesDocumentationTest`（两个 CLI 入口的 MCP 自有选项 `help` 非空且集合一致；代码读取的每个 `MCP_*` 变量都出现在 `config.py` 模块文档中）。
- 端到端实测（2026-09-15）：`manage.py run_mcp_server --port 8791` 常驻监听；MCP SSE 客户端 `initialize` → `tools/list`（14 个）→ `tools/call`（`search_symbols` 返回非错误）→ `resources/list`（`quant://docs/overview`）全部通过；配置令牌后 `/health` 与 `/sse` 在缺令牌/错令牌时返回 401、正确令牌通过；MCP 进程日志内无分时更新器采样。

#### 已知边界与后续（P2）

- 鉴权仅静态共享令牌（无多用户 / OAuth2、无令牌轮换与吊销）；HTTPS 由部署层（nginx / 反向代理）承担。
- 传输已支持 SSE；`streamable-http` 传输与浏览器直连场景（除 CORS 外）未做专门适配。
- 无写操作审计日志（`trigger_plan_execution` 仅创建 `pending` SuiteRun，实际执行轨迹仍由 `SuiteRun`/`NodeRun`/`ExecutionLog` 记录）。
- 工具返回体未做字段级脱敏白名单（当前未暴露 `auth_info` 等敏感字段，仍建议在扩展工具时逐个白名单化）。




| 模块 | 状态 | 测试用例数 | 完成度 |
|------|------|------------|--------|
| `users` | ✅ 已完成 | 5 通过 | 100% |
| `watchlists` | ✅ 已完成 | 22 通过 | 100% |
| `datasources` | ✅ 已完成 | 30 通过 | 100%（~~D-01 用户自配数据源~~ 已移除（2026-09-15，异构源不可适配）；保留 K 线分表/快照/同步（**含增量优化**）/基本面缓存） |
| `execution` | 🟢 执行闭环完成 | 84 通过 | 90%（生产回报字段验证待完善；NodeRun 已就绪；**日志生命周期清理（N-04）与 PII/日志卫生（N-05 剩余）待做**） |
| `cases` | ✅ P0 能力完成 | 22 通过 | 100%（含 run_status 状态机：new→running→done/failed） |
| `suites` | 🟢 编排核心能力完成 | 30 通过 | 98%（含 run_status 状态机：new→running→done/interrupt；画布前端对接已完成，见 5.1.1） |
| `plans` | ✅ P0 能力完成 | 15 通过 | 100%（含 run_status 状态机：new→running→done/interrupt；suite_start_mode；~~多实例调度治理~~ → 单机部署下非必要，已降为 P4，见 5.1.2） |
| `runner` | ✅ P0 能力完成 | 93 通过 | 100%（P1：真实交易回报、基本面扩展指标与总仓位风控） |
| `monitoring` | ✅ 已完成 | 65 通过 | 100%（后端模型/内部更新器（**启动清空 + 启动回填 + 开盘清理历史 + UTC23 兜底**）/SSE 推送/API + 前端 ECharts 分时监控页均已落地，见模块9） |
| `quick-strategy` | ✅ 已完成 | —（前端） | 100%（3 步向导 + 一键链路 + 失败清理已落地，见模块10 前端实施记录；后端零改动） |
| `mcp_server` | ✅ 已完成 | 57 通过 | 100%（**SSE（HTTP）MCP 服务**：24 工具（14 只读/受控触发 + 10 受控配置写 MCP-20） + 1 概览资源 + `/health` · 令牌鉴权 / DNS rebinding 保护 / 非回环绑定 fail-fast · 默认只读，写操作通过 `MCP_ALLOW_TRIGGER=1` 或启动参数 `--allow-trigger` 开启，且只创建 `pending` SuiteRun，见模块11 MCP-18 · **MCP-19 变量描述：14 工具 / 29 入参逐个带 `inputSchema` 描述，CLI 与 `MCP_*` 配置变量逐个带说明**） |

> 测试用例数按 `manage.py test <模块>` 当前实际输出为准；全项目总数以 `manage.py test`（无标签，含 runner）同一次完整回归的实际输出为准。**最近一次完整回归（2026-09-21）：✔ 453 个测试全部通过（OK，`manage.py test --noinput -v 0` 退出码 0）**（数量演进：2026-09-15 → 402；2026-09-17 MCP-18 +5 → 437；2026-09-21 MCP-19 变量描述 +5 → 442；MCP-20 配置写工具 +11 → **453**）。此前基线：**2026-09-15 ✔ 402 个测试全部通过** = users 5 + watchlists 22 + datasources 30 + execution 84 + cases 22 + suites 30 + plans 15 + runner 93 + monitoring 65 + mcp_server 36；**2026-09-17 ✔ 437 个**（mcp_server 41）。根因定位：早期 20 failures + 1 error 均非业务缺陷——① `arcis.django.ArcisMiddleware` 默认按 IP 限流（100 次/60 秒），测试进程内所有请求共享 127.0.0.1，watchlists/datasources 套件超阈值后返回 429（含 `test_search_symbol` 的 `JsonResponse` 无 `.data`，同源）；② `monitoring` gm 昨收用例为时间炸弹（硬编码日期相对"今日"），已固定 `timezone.now`；③ `datasources` 两处 Decimal 字符串断言依赖 MySQL 精度展示（SQLite 返回 `'10.6'`），已改为 Decimal 数值断言。测试环境隔离：新增 `quant_engine/settings/test.py`（`ARCIS_CONFIG={'rate_limit': False}`），`manage.py` 检测 `test` 子命令自动切换；开发/生产限流保持不变。


## 五、待办事项汇总

### 5.1 开发任务

本节只记录需要实现或继续完善的功能；已完成事项放在状态表中，不再作为待开发任务重复排队。

#### 5.1.1 已完成开发能力

| 功能 | 影响模块 | 状态 | 关联需求 |
|------|----------|------|----------|
| EventRegistry 数据库回退与缓存回填 | `execution` | ✅ 已完成 | EX-02 |
| gm SDK 行情查询、订阅、调度、下单和订单回报适配 | `runner`, `execution` | ✅ 已完成 | R-01、R-07、R-11、EX-18 |
| gm 订单外部 ID 关联与本地状态回写 | `runner`, `execution` | ✅ 基础完成 | EX-18、R-11 |
| 基础行情 Fixture、数量/金额风控、可注入 Case 计算 | `runner`, `cases`, `datasources` | ✅ 已完成 | R-06、R-07、R-08 |
| AkShare 基本面上下文适配器（个股信息、估值基础字段、异常降级） | `runner`, `datasources` | ✅ 基础完成 | R-07 |
| 技术指标因子引擎（MA/EMA/MACD/RSI/KDJ/BOLL/ROC 等）与过滤/裁决 | `runner`, `cases` | ✅ 已完成 | C-09、R-06 |
| 数据上下文构建（DB 分表 K线 + 实时快照 + gm 回退） | `runner`, `datasources` | ✅ 已完成（基本面基础字段已接入） | R-07 |
| 复杂风控（单向持仓/单笔与每日限额/交易时段） | `runner` | ✅ P0 已完成（总仓位上限为 P1） | R-08 |
| 分级资金占用（Plan.account_id/allocated_capital 占用账户资金；Suite 向 Plan 申请、Case 向 Suite 申请，层级总额校验 + 下单原子扣减/回退） | `plans`, `execution`, `runner` | ✅ 已完成（FundAllocation + funds 服务 + `/api/execution/fund-allocations/`；14 个专项测试） | R-08 |
| Plan 热加载注册中心（PlanRegistry，冷启动自愈） | `runner`, `plans` | ✅ 已完成 | R-09 |
| Suite 发布拓扑快照（SuiteVersion，不可变、递归子树） | `suites` | ✅ 已完成 | S-09 |
| 节点级运行实例（NodeRun，父子层级 + 轨迹回放） | `execution` | ✅ 已完成 | EX-15 |
| 跨 Suite 递归编排（子 Suite 执行、树形聚合、parallel 分支 join、fail_stop） | `runner`, `suites`, `execution` | ✅ 已完成 | S-09、S-10、S-11、EX-15 |
| 运行状态机（Case/Suite/Plan 三级 run_status：new→running→done/interrupt/failed；Case 依托 Suite 运行；Suite 全部 case done 自动 done，case failed 自动 interrupt；Plan 全部 suite done 自动 done；手动 stop 强制停止子级；Plan 支持 auto/manual suite_start_mode；Plan 创建校验账户空闲资金，Suite 加入校验 Plan 空闲资金） | `cases`, `suites`, `plans`, `execution` | ✅ 已完成（state_machine 服务 + 25 个专项测试；`/api/plans/{id}/start|stop/`、`/api/suites/{id}/start|stop/`） | C-08、S-01、P-01、P-07 |
| 并发资金原子扣减（Plan 创建对 AccountFundConfig 行加 `select_for_update`，校验 + 占用原子完成；Suite 加入对 Plan 级 FundAllocation 行加 `select_for_update`，校验 + 分配原子完成；下单扣减已有行级锁） | `plans`, `suites`, `execution` | ✅ 已完成（三层均使用行级锁 + 事务，消除 check-then-act race condition） | R-08 |
| 基本面财务数据扩展（Provider 抽象基类 + AkShare 实现，财务指标/资产负债表/利润表/现金流量表 60+ 英文契约字段；子报表独立降级） | `runner`, `datasources` | ✅ 已完成（`FundamentalsProvider` + `AkshareFundamentalsProvider`；21 个专项测试） | R-07 |
| 基本面缓存与历史时点（`FundamentalSnapshot`/`FundamentalCacheMeta` 持久化快照 + `CachedFundamentalsProvider` 装饰器；asof 历史点读、TTL 有效期、回源回填、回源失败降级旧缓存） | `runner`, `datasources` | ✅ 已完成（历史时点不读取未来数据；命中即有效不判 TTL；7 个专项测试） | R-07 |
| Suite 边条件操作符（event_condition 扩展 `op: eq/neq/gt/gte/lt/lte/between` + `field/threshold`，后端校验 + 运行时匹配同一契约，兼容旧键值相等契约） | `suites`, 前端 | ✅ 已完成（`event_condition_matches` + 双重校验；13 个专项测试） | S-09 |
| Suite 拓扑完整性校验（跨树入边、重复边、非法权重、孤立节点、不可达节点；发布前串联 validate_dag + validate_topology） | `suites` | ✅ 已完成（5 个专项测试） | S-09 |
| 告警管理（Alert 模型 + AlertChannel 渠道配置 + `alert_service` 通知服务；应用内 / 邮件多渠道，按最低级别与类型白名单分发；`/api/execution/alerts/`、`/api/execution/alert-channels/` 及操作/统计/重发接口；前端告警管理页 + 告警渠道配置页） | `execution`, `runner`, `plans`, `quant-frontend` | ✅ 已完成（21 个专项测试；EX-20 ~ EX-26） | EX-20、EX-21、EX-22、EX-23、EX-24、EX-25、EX-26 |
| API 统一分页（列表接口统一 `{count, next, previous, page, total_pages, results}` 分页结构；`page` / `page_size` / `limit` 兼容别名；全局 `REST_FRAMEWORK.DEFAULT_PAGINATION_CLASS` + 自定义列表动作分页；前端 axios 拦截器解包 `results` 保持旧字段兼容，列表调用默认 `page_size: 500`） | 全部 API, `quant-frontend` | ✅ 已完成（`quant_engine/pagination.py`，N-01；9 个专项测试：cases+1、suites+1、plans+1、watchlists+2、datasources+1、execution+3，另含既有用例的页内/limit 兼容断言） | N-01 |
| 画布可视化编排前端对接（策略设计器 `/designer`：@vue-flow/core 拖拽节点/连线编排 Case 与子 Suite；编排边条件对话框，`event_condition` 白名单 + 操作符 `eq/neq/gt/gte/lt/lte/between`；拓扑读写与发布；执行轨迹回放：按 SuiteRun 步进/自动播放回放 NodeRun 节点状态与事件顺序；侧边导航新增菜单） | `quant-frontend`, `execution` | ✅ 已完成（2026-09-10；`src/views/Designer.vue` + 路由 `/designer`；后端新增 `GET /api/execution/run/{run_id}/node-runs/`；`vue-tsc` 0 错误、`vite build` 通过；2026-09-12 配套修正 NodeRun 列表测试按 N-01 分页契约解包 `results`） | S-09、EX-15、5.1.4 任务 11 |
| MCP 服务（`mcp_server` 包：**SSE（HTTP）为主传输**（`manage.py run_mcp_server` / `python -m mcp_server`，`--transport stdio` 保留）+ 14 个工具 + 1 个概览资源 + `/health` 健康检查；ASGI 装配（`build_http_app`）+ `BearerAuthMiddleware` 令牌鉴权 + DNS rebinding 保护 + 可选 CORS；`McpTransportConfig` 配置校验（非回环绑定必须令牌，否则 fail-fast）；默认只读，`trigger_plan_execution` 写操作需 `MCP_ALLOW_TRIGGER=1` 且只创建 `pending` SuiteRun；`to_jsonable` 统一 JSON 安全输出；MCP 服务进程不启动分时更新器；MCP-19 变量描述：工具入参逐个带 `inputSchema` 描述，门面 docstring 逐变量给 `Args`/`Returns`，CLI 参数与 `MCP_*` 配置变量逐个带说明；**MCP-20 配置写工具（2026-09-21）：新增 10 个 Case/Suite/Plan 增改删工具（含 `update_suite_topology`），复用 REST 同源校验与删除保护，默认禁用需 `MCP_ALLOW_MUTATE=1` 或 `--allow-mutate`，只改 draft 配置不发布不下单）** | `mcp_server`（入站适配器，复用 apps 模型与服务） | ✅ 已完成（2026-09-15 落地，2026-09-17 MCP-18、2026-09-21 MCP-19/MCP-20，见模块11；**57 个专项测试通过** + SSE 端到端握手与令牌鉴权实测；新增依赖 `mcp[cli]>=2.2.0`、`uvicorn>=0.31.1`、`starlette>=0.27`、`pydantic>=2.0`） | 模块11 设计、MCP-18、MCP-19、MCP-20 |

#### 5.1.2 待开发任务

| 优先级 | 开发任务 | 影响模块 | 依赖/关联需求 |
|--------|----------|----------|--------------|
| P1 | ~~API 分页与敏感配置保护~~ → 已拆分：**API 统一分页 ✅ 已完成（N-01，见 5.1.1）**；**敏感配置保护范围收敛**（2026-09-15）：`DataSource.auth_info` 已随 D-01 模块移除而取消，剩余为 PII 与日志卫生（见下方「敏感配置保护设计」）待办 | `execution` / `users`（PII 与日志面） | N-05（分页对应 N-01 已完成） |
| P4 | 多实例 Scheduler 治理（分布式任务去重、租约/领导者选举、任务幂等键）——**降级原因：当前部署目标为单机**，单一 Scheduler 实例 + 进程内 `_enqueued` 去重已覆盖同分钟同 `(Plan, Symbol)` 只入队一次；多实例治理仅在多机/多实例场景必要，故由 P1 降为 P4（未来多机扩展时再评估） | `plans`, `runner` | N-03 增强 |
| P2 | ~~画布可视化编排前端对接（拖拽节点/连线、执行轨迹回放视图）~~ → ✅ **已完成（2026-09-10，见 5.1.1）**：基于 `@vue-flow/core` 的策略设计器（`/designer`）已落地——拖拽节点/连线编排、编排边条件配置（含操作符）、拓扑读写、发布、NodeRun 执行轨迹回放；后端配套 `GET /api/execution/run/{run_id}/node-runs/` | `quant-frontend`, `execution` | S-09、EX-15（详见 5.1.4 任务 11） |
| P1 | ~~分时监控模块~~ → ✅ **全部完成（2026-09-12，见模块9）**：多市场（A/HK/US）时区感知分时监控；分时数据为**临时数据**（开盘记录 → 收盘清空）；`IntradayPoint` + `sample_intraday`/`clear_intraday` 命令 + `/api/monitoring/intraday/` 与 `/realtime` + 前端 ECharts 分时监控页（`/monitoring`，盘中 15s 轮询增量追加）均已落地；2026-09-14 分时数据源替换为 **gm SDK**（A 股主源 + akshare 回退 HK/US） | `monitoring`, `quant-frontend` | 关联新模块（见模块9 设计文档） |
| P1 | ~~策略快速创建向导~~ → ✅ **全部完成（2026-09-14，见模块10）**：3 步向导 `/quick-strategy`，模板化生成 Case/Suite/Plan 并一键「创建→发布→（可选）启动」；**全链路由前端编排既有 API（后端零新增接口）** | `quant-frontend`（`cases`/`suites`/`plans` API 复用） | 关联新模块（见模块10 设计文档） |
| P2 | 执行日志生命周期管理（30 天自动清理、归档、清理命令、监控） | `execution`, `runner` | N-04 |
| P2 | 性能与容量基线（API/队列/查询/并发基准；非外部调用 API < 500ms） | 全部 runner/API | N-02 |
| P2 | MCP 服务增强（OAuth2 / 多用户与令牌轮换、`streamable-http` 传输、写操作审计日志、工具返回字段级白名单）——**边界**：当前为 SSE + 静态令牌，默认只读且 `trigger_plan_execution` 受 `MCP_ALLOW_TRIGGER=1` 保护，非回环绑定强制令牌，故不阻塞主线 | `mcp_server` | 模块11（已知边界与后续） |

##### 敏感配置保护设计（2026-09-15 定稿，覆盖 N-05）

**1. 敏感数据归属划分**（系统配置层 vs user 数据层，保护手段不同）：

| 归属 | 数据 | 保护手段 | 现状 |
|------|------|----------|------|
| **系统配置层**（不进数据库、不进 git） | `SECRET_KEY`（仅覆盖登录态 Session/CSRF） | production 随机生成 = **预期行为**（刷新仅致登录过期）；需固定时经 `local.py`/环境变量注入 | ✅ 已确认 |
| 同上 | `GM_TOKEN`（gm 交易令牌）、`MCP_AUTH_TOKEN`、数据库凭据 | **仅经环境变量注入**，生产环境不落任何文件 | ✅ production.py 已按此实现（`os.getenv`，SQLite 仅作 `USE_SQLITE=1` 回退） |
| 同上 | 真实密钥落点 | `quant_engine/settings/local.py`（已 gitignore，未跟踪）为唯一本机密钥文件；~~`.env.example`~~ 已删除（项目无 dotenv 加载链，属无效文件） | ✅ 2026-09-15 清理 |
| **user 数据层**（进数据库） | ~~`DataSource.auth_info`~~ **已随 D-01/`DataSource` 模块移除（2026-09-15）**——系统不再支持用户自配第三方数据源，用户层已无密钥录入入口，分用户加密（KEK/DEK）方案**取消实施**；若未来恢复用户凭据录入再按本节设计执行 |
| 同上 | `AccountFundConfig.account_id`、`User.phone/company`、`AlertChannel.email_recipients`、交易明细（Order/ExecutionLog/Alert.message） | N-05 焦点收敛至此：不属密钥但属半敏感/PII——不进日志明文、通知邮件不夹带异常栈细节、API 权限分级 | ⏳ 保留待办 |

**2. 生产环境配置基线（2026-09-15 落地）**：`production.py` 数据库**默认 MariaDB**（主库 `DB_*` 与 K 线库 `KLINE_DB_*` 均经环境变量注入；`USE_SQLITE=1` 仅限本机演练/CI 回退）；`wsgi.py` 默认指向 `production`（原指向空的 `quant_engine.settings` 会 `ImproperlyConfigured`），生产入口为 Gunicorn：`gunicorn quant_engine.wsgi:application --workers 4 --env DJANGO_SETTINGS_MODULE=quant_engine.settings.production`。

**3. user 层分用户加密设计（~~`DataSource.auth_info`~~ 2026-09-15 已随 D-01 移除而**存档取消**，保留设计备未来恢复用户凭据录入时使用）：**

- **密钥层级（KEK/DEK 两级）**：
  - `KEK`（主加密密钥）：系统级，**仅存环境变量 / `local.py`**（`AUTH_ENCRYPTION_KEY`，Fernet key），生产绝不落库落盘；
  - `DEK`（用户数据密钥）：**每用户一把**，首次录入敏感字段时随机生成，以 KEK 加密后存库（`encrypted_dek`）——库中只有密文 DEK，DB 泄露无法解出任何用户密钥。
- **新模型 `UserSecretKey`**（`users` 模块）：`user(OneToOne)` + `encrypted_dek(Text)` + `kek_version(SmallInteger)` + `created_at`。
- **加解密实现**（`apps/datasources/crypto.py` 或 `users/crypto.py`）：`cryptography.fernet`；`auth_info` 整体序列化后以用户 DEK 加密，密文格式带 `kek_version` 前缀；写入用当前 `kek_version`，读取按密文中的版本取对应 KEK（支持轮换：换新 KEK 后旧密文仍可读，懒式重加密升级）。
- **API 契约**：`DataSource` serializer 改为**字段白名单**（弃用 `__all__`）；`auth_info` 写入接受明文（落库即加密），**读取一律脱敏**——返回 `auth_configured: true/false` + 不含任何密钥内容的摘要（如 token 末 4 位可省略）；明文仅在服务层内部使用（数据源同步/连接时，且以当前请求用户身份解密）。删除保护/权限：非录入者与管理员不可见摘要之外的任何信息。
- **失败语义**：KEK 缺失或 DEK 解密失败 → 同步/连接报可定位错误（`ValueError`/数据源测试失败），不阻塞其他功能；密钥丢失不可恢复（用户重新录入），符合“密钥不落库”原则的代价。
- **日志卫生**：解密路径不打日志；`repr`/异常消息不包含明文（`__str__` 排查点写入测试）。
- **测试（P1 阶段6 补充）**：加密往返等值、库内确为密文（明文 token 不出现在 DB 字符串中）、API 返回脱敏（明文不出现在响应）、非 owner 读取受限、KEK 版本轮换后旧密文可读、DEK 缺失时同步降级报错、日志不含明文。

#### 5.1.3 开发顺序

| 顺序 | 开发阶段 | 主要交付物 | 关联需求 | 状态 |
|------|----------|------------|----------|------|
| 1 | P0 基础闭环 | `cases`、`suites`、`plans`、`runner` 核心能力 | C-07、C-09、S-09、S-10、S-11、P-03、P-08、P-09、P-10、R-01、R-06、R-07、R-09 | ✅ 已完成 |
| 2 | P1 生产可靠性 | 真实交易回报、账户级风控、基本面扩展 | R-07、R-08、EX-18 | 🟢 大部分已完成（订单联调、账户级风控、基本面财务数据/缓存/历史时点、边条件操作符、拓扑校验、交易失败告警通道已完成；剩余真实交易环境验证） |
| 3 | P1/P2 产品与运维增强 | Suite 条件操作符、拓扑增强、分页、日志清理、**分时监控** | S-09、N-01、N-03、N-04、N-05 | ⏳ 部分完成（条件操作符、拓扑增强、**API 统一分页已完成**、**分时监控模块9 已完成**（见模块9）；**日志清理（N-04）与 PII/日志卫生（N-05 剩余）待做**；~~`auth_info` 加密~~已随 D-01 移除而取消；多实例治理已随单机部署目标降为 P4） |
| 4 | P1 产品体验增强 | **策略快速创建向导（模块10）**：3 步向导一键生成「Case/Suite/Plan 并发布」 | 模块10 设计 | ✅ 已完成（2026-09-14，见模块10 前端实施记录） |

#### 5.1.4 新一轮开发任务（v2.4）

本轮目标是将 P0 核心闭环推进到可控的模拟交易生产链路。任务按“先降低交易风险，再扩展数据和产品能力”的原则排列；同一优先级内按依赖顺序执行。

##### 第一阶段：交易安全闭环

| 顺序 | 优先级 | 开发任务 | 影响模块 | 主要交付物 | 验收标准 |
|------|--------|----------|----------|------------|----------|
| 1 | P1 | gm 模拟账户订单生命周期联调 | `runner`, `execution` | 订单提交、受理、部分成交、完全成交、拒单、撤单和重复回报适配 | ✅ 真实模拟账户链路已跑通（2026-09-07，账户 efd94fdb-…：提交→受理→完全成交 100 股回报归一化写库）；适配器含部分成交累加、重复回报指纹幂等、`request_cancel`（`order_cancel(wait_cancel_orders)` 真实契约）、状态码映射（1/2/3/5/6/8/10） |
| 2 | P1 | 账户总资金与总仓位风控 | `runner`, `execution` | 账户资产查询、持仓汇总、单 Plan/全账户限额、下单前原子校验 | ✅ 账户/持仓 Provider、资金和总仓位拦截已完成；分级资金占用链（Plan 占用 → Suite 申请 → Case 申请，行级锁原子扣减）已完成（FundAllocation）；并发原子扣减已完成（Plan 创建对 AccountFundConfig 行加 `select_for_update`，Suite 加入对 Plan 级 FundAllocation 行加 `select_for_update`，消除 check-then-act race condition） |
| 3 | P1 | 交易失败补偿与任务可观测性 | `runner`, `execution`, `plans` | 订单提交失败分类、重试上限、失败原因、任务关联 ID、告警日志 | ✅ 失败订单回写、错误码、任务 ID、重试传播已完成；告警通道已接入（`Alert` / `AlertChannel` / `alert_service` + 21 个专项测试） |
| 4 | P1 | 运行状态机（Case/Suite/Plan 三级 run_status） | `cases`, `suites`, `plans`, `execution` | Case/Suite/Plan 三级 run_status（new→running→done/interrupt/failed）；Case 依托 Suite 运行；Suite 全部 case done 自动 done，case failed 自动 interrupt；Plan 全部 suite done 自动 done；手动 stop 强制停止子级；Plan 支持 auto/manual suite_start_mode；Plan 创建校验账户空闲资金，Suite 加入校验 Plan 空闲资金 | ✅ 已完成（state_machine 服务 + 25 个专项测试；`/api/plans/{id}/start|stop/`、`/api/suites/{id}/start|stop/`） |

##### 第二阶段：数据与策略能力增强

| 顺序 | 优先级 | 开发任务 | 影响模块 | 主要交付物 | 验收标准 |
|------|--------|----------|----------|------------|----------|
| 4 | P1 | 基本面财务数据扩展 | `runner`, `datasources` | 财务指标、资产负债表、利润表、现金流量表 Provider | ✅ 已完成（`FundamentalsProvider` 抽象基类 + `AkshareFundamentalsProvider`，财务指标 19 + 资产负债表 11 + 利润表 15 + 现金流量表 6 个英文契约字段；子报表独立降级、依赖注入；21 个专项测试） |
| 5 | P1 | 基本面缓存与历史时点 | `runner`, `datasources` | 持久化/缓存模型、`asof` 查询、有效期和失败重试 | ✅ 已完成（`FundamentalSnapshot`/`FundamentalCacheMeta` 持久化模型 + `CachedFundamentalsProvider` 装饰器；历史时点 asof 点读不读取未来数据、命中即有效不判 TTL；实时路径 TTL 有效期命中缓存/过期回源刷新；回源失败降级旧缓存；7 个专项测试） |
| 6 | P1 | Suite 边条件操作符 | `suites`, `quant-frontend` | `eq`、`neq`、`gt`、`gte`、`lt`、`lte`、`between` 契约和执行器 | ✅ 已完成（后端序列化器与 `services.event_condition_matches` 同一字段契约；`op+field+threshold` 成组校验、between 双元边界、bool 拒绝；兼容旧键值相等契约；13 个专项测试；前端契约同步仍待做） |
| 7 | P1 | Suite 拓扑完整性校验 | `suites` | 孤立节点、跨树入边、重复边、非法权重和不可达节点检查 | ✅ 已完成（`validate_topology` 发布前串联 `validate_dag`；跨树入边/重复边/非法权重(0<w≤1000)/孤立节点/不可达节点均报明确 `SuiteError`；合法递归子 Suite 未被误判；5 个专项测试） |

##### 第三阶段：平台可靠性与使用体验

| 顺序 | 优先级 | 开发任务 | 影响模块 | 主要交付物 | 验收标准 |
|------|--------|----------|----------|------------|----------|
| 8 | P1 | API 分页与敏感配置保护（已拆分为两个子任务） | 全部 API、`datasources` | 统一分页响应、`auth_info` 加密/脱敏、权限检查 | ✅ **API 统一分页已完成**（列表接口统一 `{count, next, previous, page, total_pages, results}`；`page/page_size/limit` 兼容别名；前端拦截器解包 `results`，接口测试通过）；✅ **`auth_info` 加密子任务已随 D-01/`DataSource` 模块移除而取消**（2026-09-15，用户自配第三方数据源删除，`auth_info` 不复存在）；⏳ 剩余 PII 与日志卫生待办（账户 ID/联系方式/交易明细不进日志与通知明文） |
| 9 | P4 | 多实例 Scheduler 治理（原 P1，**单机部署目标下降级**） | `plans`, `runner` | 分布式任务去重、租约/领导者选举、任务幂等键 | ~~多个 Scheduler 实例只产生一个 `(Plan, Symbol, minute)` 任务；实例故障可恢复~~ → 降级为 **P4 储备**：单机单实例下进程内去重已满足；未来多机部署时再恢复本验收标准 |
| 10 | P2 | 执行日志生命周期管理 | `execution`, `runner` | 30 天自动清理、归档策略、清理命令和监控 | 清理不影响未完成运行和订单；清理任务可重复执行且幂等 |
| 11 | P2 | ~~画布编排与执行轨迹回放~~ → ✅ **已完成（2026-09-10，见 5.1.1）** | `quant-frontend`, `execution` | 拖拽节点、连线配置、NodeRun 轨迹和失败节点定位 | ✅ 前端拓扑与后端快照双向一致（`/designer` 拓扑读写/发布走 `GET|POST /api/suites/{id}/topology/`）；可按 SuiteRun 回放节点状态和事件顺序（`GET /api/execution/run/{run_id}/node-runs/` + Designer 页步进/自动播放回放，失败节点标红定位） |
| 12 | P2 | 性能与容量基线 | 全部 runner/API | API、队列、数据查询和并发执行基准 | 建立基准数据；非外部调用 API 达到 500ms 目标；记录并发容量和瓶颈 |

##### 本轮依赖关系

```text
订单生命周期联调
    ↓
账户级风控 → 交易失败补偿与可观测性
    ↓
运行状态机（Case/Suite/Plan 三级 run_status）
    ↓
基本面财务数据 → 基本面缓存与历史时点
    ↓
Suite 边条件操作符 → 拓扑完整性校验
    ↓
分页 ✅（已完成）→ 配置保护 → 日志清理与性能基线
（多实例 Scheduler 治理已降级 P4：单机部署下非必要，见 5.1.2）
    ↓
画布编排与执行轨迹回放 ✅（已完成 2026-09-10，见 5.1.1 / 5.1.4 任务 11）
```

##### 本轮统一验收要求

- 所有交易联调只允许使用已配置的 gm 模拟账户，禁止自动化测试提交实盘订单。
- 涉及 `runner`、`execution`、`plans` 的改动必须同时通过单元测试、跨模块测试和失败场景测试。
- 所有外部数据源必须支持超时、空响应、异常、重复数据和降级路径测试。
- 所有异步任务必须具备幂等键、最大重试次数和可追踪的失败原因。
- 每个任务完成后同步更新本节状态、对应需求编号和实际测试命令结果。

### 5.2 测试任务

本节只记录验证任务，不把测试数量或验收标准混入开发任务清单。

| 阶段 | 测试任务 | 当前结果 | 待补验证 |
|------|----------|----------|----------|
| P0 阶段1 | `execution` 基础功能与 API 测试 | ✅ 18 个通过 | 真实交易回报生产链路联调（模拟回报适配已完成） |
| P0 阶段2 | `cases` CRUD、发布和深层参数校验测试 | ✅ 21 个通过 | 持续维护新增指标的目录兼容性 |
| P0 阶段3 | `suites` CRUD、拓扑、DAG 与发布快照测试 | ✅ 10 个通过 | ✅ 画布前端拓扑测试已落地（Designer 页对接 topology 读写/发布接口并经 `vue-tsc` + `vite build` 验证；边条件操作符测试已并入 P1 阶段2 完成） |
| P0 阶段4 | `plans` CRUD、发布、标的解析、调度与版本管理测试 | ✅ 13 个通过 | ~~多实例调度治理测试~~（已随任务降级 P4：单机部署单实例无需验证，见 5.1.2） |
| P0 阶段5 | `runner`、编排（tests_orchestration）、gm SDK 和 execution 联动测试 | ✅ 71 个通过（含编排、Cron 边界、WorkerPool 重试失败传播、数据上下文、订单生命周期用例） | 生产行情、风控边界、真实交易环境测试 |
| P1 阶段1 | 交易安全闭环单元与跨模块测试 | ✅ 99 个通过（execution + plans + runner 联合回归，含订单生命周期） | 真实模拟账户链路已跑通（2026-09-07）；~~并发资金扣减待补~~ → ✅ 已完成（三层行级锁原子扣减，见 5.1.1） |
| P1 阶段1 | 告警（Alert）专项测试（模型/服务/渠道过滤/API/集成） | ✅ 21 个通过（tests_alerts；含渠道过滤、邮件/应用内通知、确认/解决动作、统计与集成用例） | 生产邮件网关（SMTP）与真实通知链路联调 |
| P1 阶段2 | 运行状态机专项测试 | ✅ 25 个通过（Case/Suite/Plan 三级 run_status 流转、自动完成、中断、手动停止、资金校验） | — |
| 阶段6 | 全项目回归测试 | ⚠️ 历史统计口径不统一；最近一次完整回归：✔ **345 个测试**（2026-09-14，含 monitoring 48 个；同一命令口径：`manage.py test` 无标签，含 runner）。**注意**：当前 develop_backend 基线（a64e53c）完整回归本身即报 16 failures + 2 errors（集中在 watchlists，与本次分时监控 gm 替换/回填无关）；monitoring 模块独立运行 48 个全部通过 | 以同一次完整回归命令的实际输出为准；建议另立任务修复 watchlists 预存失败。**更新（2026-09-15，多次变更后）**：✔ **402 个测试全部通过**（D-01/`DataSource` 移除 → 397；K 线增量同步 +2；分时启动清空/开盘清理 +3）。根因：早期 21 个失败全部为测试环境/用例缺陷（arcis 默认限流 429、gm 昨收用例时间炸弹、Decimal 字符串断言依赖 MySQL 精度展示），非业务缺陷；已新增 `quant_engine/settings/test.py` 隔离限流并修正 3 处用例。**更新（2026-09-17，MCP-18）**：✔ **437 个测试全部通过**。**更新（2026-09-21，MCP-19 变量描述）**：✔ **442 个测试全部通过**（`manage.py test --noinput -v 0` 退出码 0；442 = 437 + MCP-19 5 个用例） |
| P1 阶段2 | 基本面财务数据扩展专项测试 | ✅ 21 个通过（Provider 抽象、三大报表 + 财务指标、子报表独立降级、英文契约） | — |
| P1 阶段2 | 基本面缓存与历史时点专项测试 | ✅ 7 个通过（asof 历史点读、TTL 命中/过期、回源回填、回源失败降级、命中/未命中统计） | 真实外部数据源联调 |
| P1 阶段2 | Suite 边条件操作符专项测试 | ✅ 13 个通过（eq/neq/gt/gte/lt/lte/between 边界值、成组校验、旧契约兼容） | 前端契约同步后补前端耦合测试 |
| P1 阶段2 | Suite 拓扑完整性校验专项测试 | ✅ 5 个通过（跨树入边、重复边、非法权重、孤立节点、合法递归子 Suite） | — |
| P1 阶段3 | API 统一分页专项测试（接口契约：`count/next/previous/page/total_pages/results`；`page/page_size` 翻页与 `limit` 兼容别名；覆盖 cases/suites/plans/watchlists/datasources/execution 各模块列表接口与自定义列表动作） | ✅ 专项断言并入各模块用例（cases `test_list_cases_paginated`；suites `test_suite_list_paginated`；plans `test_plan_list_paginated`；watchlists `test_list_symbols_paginated`/`test_list_groups_paginated`；~~datasources `test_list_datasources_paginated`~~ 已随 D-01 移除；execution `PaginationContractTest` 3 个用例） | 前端分页交互（逐页翻页 UI）待做 |
| P1 阶段4 | 分时监控专项测试 | ✅ 65 个通过（`apps/monitoring/tests.py`：`IntradayPoint` 模型/唯一约束、`session_status` 多市场时段与夏令时、`trading_minutes_local` 交易分钟枚举、spot 规范化与缺失字段降级、`GmSnapshotProvider` tick/逐分钟历史规范化与 SHSE/SZSE 映射、`CompositeSnapshotProvider` 回退与历史转发编排、`sample_intraday` 采样/同分钟覆盖/异常隔离/标列表传递、`backfill_intraday` 启动回填（补缺失/不覆盖/完整性跳过/不支持源跳过/双时段）、`clear_intraday` 清空幂等、API 契约与 realtime 合并、`IntradayUpdater` 内部更新器（run_once 委托/UTC23 清理幂等/单例/启动幂等/**启动清空（全清一次 + 失败重试）**/**开盘清理（交易时段触发、边界为当地零点、同日幂等、收盘不触发）**）、SSE stream 首块快照与 symbol 校验）；前端页面经 `vue-tsc -b` + `vite build` 验证（见模块9 前端实施记录） | gm SDK 真实终端联调已核对（2026-09-14，SZSE.000426）：60s bar 时间在 `bob`/`eob`（ISO 带时区，已兼容）；`history` 按 bar 结束时间过滤 `end_time`（回填 `end` 已加 1 分钟）；60s bar `volume`/`amount` 为分钟值（逐 bar 累加生成累计值）；bar 内 `pre_close=0`（回填用前一日 1d bar close）；tick 为空时快照回退当日 60s bar 聚合。实测回填 120/120 分钟完整。SSE 长连接在 dev runserver 实际推送与断线重连待联调 |
| P1 阶段5 | 策略快速创建向导 | ✅ 前端实施完成（2026-09-14，见模块10 前端实施记录）：`vue-tsc -b` 0 错误 + `vite build` 通过；三模板（signal_only / signal_executor / dual_direction）一键链路 + 失败「重试/保留草稿」+ `cleanupCreated` 逆序清理 | 覆盖：`quickStrategy.ts` params 生成与后端 `validate_case_schema` 等值的**运行时端到端验证**（真实后端一键创建三模板各一例并核对入库结构与 Plan symbols 解析）待做；后端零改动（既有回归口径不受影响） |
| P1 阶段6 | MCP 服务（模块11）专项测试 | ✅ 46 个通过（`mcp_server/tests.py`：工具门面（标的分市场搜索与 limit 收敛、命名解析库内命中与回退、分表 K 线窗口/尾段截断/`to_jsonable` 归一、Plan 详情与标的解析、Case 过滤与拓扑快照、事件类型、告警列表与统计、SuiteRun 过滤、分时序列字段契约）、错误契约（未入库标的/空代码/反向日期窗口/资源不存在）、写开关（默认 `PermissionError`；开启后仅创建 `pending` SuiteRun 且 `Order` 计数为 0；空标的列表与未发布 Plan 仍拒绝）、装配层（14 工具 + 1 资源注册、`bootstrap` 默认关闭分时更新器、概览文本声明边界）、**SSE 传输与安全**（传输配置默认值/覆盖/非法值拒绝、非回环绑定必须令牌、`/sse` `/messages` `/health` 路由、健康检查开/关令牌下的 200/401、DNS rebinding 保护拒绝非法 `Host`）、**进程门禁**（`run_mcp_server` 不启动分时更新器，`runserver` 子进程仍启动））；另实测 `manage.py run_mcp_server` 的 SSE 端到端握手（14 工具 + 资源 + 工具调用）与令牌鉴权（401/200）；**MCP-19 变量描述**（`McpToolSchemaTest` 3 个：29 个入参 `inputSchema` 描述非空且长度 ≥ 6、变量名与必填集合与 `tools_impl` 门面签名逐一相等、工具说明与门面 docstring 逐变量覆盖 `Args`/`Returns`；`McpVariablesDocumentationTest` 2 个：两个 CLI 入口的 MCP 自有选项 `help` 非空且集合一致、代码读取的每个 `MCP_*` 变量都出现在 `config.py` 模块文档中） | OAuth2 / 多用户与令牌轮换、`streamable-http` 传输、写操作审计（P2，见模块11 已知边界） |

#### 测试验收标准

> 测试数量按测试命令口径记录。`runner` 测试包含跨模块联动用例，因此各模块数量不应直接求和；全项目总数须以同一次完整回归命令的实际输出为准。

- 每项开发任务必须有对应的单元测试或集成测试，并标注对应需求编号。
- 涉及 `execution`、`runner` 和 gm SDK 的跨模块改动，必须通过 runner 联动测试及全项目回归测试。
- 真实交易接口测试必须使用 mock 或沙盒账户，禁止在自动化测试中直接提交实盘订单。
- 生产能力阶段的测试重点包括数据为空、SDK 异常、重复回报、订单拒绝、风控拦截和任务重试。

### 5.3 非功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| N-01 | 所有 API 支持分页 | ✅ 已实现；P1；关联开发任务：API 统一分页（见 5.1.1）；关联测试任务：P1 阶段3 |
| N-02 | API 响应时间 < 500ms（不含外部数据源调用） | P2 |
| N-03 | 策略配置变更支持热加载（无需重启服务） | ✅ 已实现；P0；关联开发任务：PlanRegistry/调度配置刷新；关联测试任务：5.2-4 |
| N-04 | 执行日志保留 30 天（自动清理） | P2；关联开发任务：日志清理（5.1.2，待做） |
| N-05 | ~~敏感信息加密存储（数据源 `auth_info`）~~ → **随 D-01/`DataSource` 模块移除而取消**（2026-09-15）：用户自配第三方数据源已删除，`auth_info` 字段不复存在；剩余范围收敛为 PII 与日志卫生（账户 ID / 联系方式 / 交易明细不进日志与通知明文） | P1；关联开发任务：PII/日志卫生（5.1.2，待做） |


## 六、附录

### 6.1 事件类型完整清单

| 事件类型 | 分类 | 触发场景 |
|----------|------|----------|
| `SYSTEM_START` | 系统级 | 系统启动 |
| `SYSTEM_STOP` | 系统级 | 系统停止 |
| `SUITE_INIT` | Suite 生命周期 | Suite 初始化 |
| `SUITE_START` | Suite 生命周期 | Suite 开始执行 |
| `SUITE_COMPLETED` | Suite 生命周期 | Suite 执行完成 |
| `SUITE_FAILED` | Suite 生命周期 | Suite 执行失败 |
| `CASE_START` | Case 生命周期 | Case 开始执行 |
| `CASE_COMPLETED` | Case 生命周期 | Case 执行完成 |
| `CASE_FAILED` | Case 生命周期 | Case 执行失败 |
| `CASE_SKIPPED` | Case 生命周期 | Case 被跳过 |
| `TIMER` | 时间 | 定时触发 |
| `PRICE_SURGE` | 外部事件 | 价格急升 |
| `PRICE_DROP` | 外部事件 | 价格急跌 |
| `VOLUME_SPIKE` | 外部事件 | 成交量放大 |
| `MACRO_CPI` | 外部事件 | CPI 数据公布 |
| `MACRO_INTEREST` | 外部事件 | 利率决议公布 |
| （用户自定义） | 用户自定义 | 通过 `EventTypeRegistry` 注册 |

### 6.2 关键路径时序

```
用户创建 Case → 用户编排 Suite（拖拽连线）→ 用户配置 Plan（定时/事件）
→ 用户发布 Plan → 异步引擎热加载配置 → Scheduler 触发执行
→ Worker 创建 SuiteRun → EventLoop 驱动执行 → 写入日志/生成委托单
```

---

**文档状态**：✅ 需求基线已锁定，可作为后续开发参考依据。


