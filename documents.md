# 量化交易系统 · 全模块需求文档

> 版本：v2.5  
> 日期：2026-09-09  
> 状态：实现基线已稳定 · 以代码为准，文档已同步校正


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
| **状态** | ✅ 已完成（18 个测试全部通过） |
| **优先级** | P0 |
| **依赖** | `watchlists.Symbol` |

#### 功能需求

| 编号 | 需求描述 | 实现文件 |
|------|----------|----------|
| D-01 | 数据源配置 CRUD（AkShare/TuShare/TDX/YFinance） | `models.py`, `views.py` |
| D-02 | 实时快照存储（仅保留最新值，`OneToOneField`） | `models.py` (RealtimeSnapshot) |
| D-03 | K线抽象基类（定义公共字段，不建表） | `models.py` (AbstractKLine) |
| D-04 | A股 K线表：按标的编码创建独立分表，运行时建表 | `models.py`, `services.py` |
| D-05 | 港股 K线表：按标的编码创建独立分表，运行时建表 | `models.py`, `services.py` |
| D-06 | 美股 K线表：按标的编码创建独立分表，运行时建表 | `models.py`, `services.py` |
| D-07 | K线增量同步：按 symbol 生成表名并去重插入 | `services.py` (sync_kline_for_symbol) |
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
| GET/POST | `/api/datasources/sources/` | 数据源配置 CRUD |
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
| **状态** | 🟢 已完成 CRUD、拓扑读写、DAG 校验、发布校验、发布拓扑快照（SuiteVersion）、子 Suite 递归执行、树形运行时聚合、并行分支 join；画布前端对接与边条件操作符扩展待完善 |
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
- 当前能力覆盖自动 Cron 触发、配置热刷新、重试策略校验和历史版本回滚；跨进程分布式去重和多实例领导者选举仍属于后续增强范围。


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


## 四、模块完成进度总览

| 模块 | 状态 | 测试用例数 | 完成度 |
|------|------|------------|--------|
| `users` | ✅ 已完成 | 5 通过 | 100% |
| `watchlists` | ✅ 已完成 | 15 通过 | 100% |
| `datasources` | ✅ 已完成 | 18 通过 | 100% |
| `execution` | 🟢 执行闭环完成 | 14 通过 | 90%（生产回报字段验证待完善；NodeRun 已就绪） |
| `cases` | ✅ P0 能力完成 | 21 通过 | 100%（含 run_status 状态机：new→running→done/failed） |
| `suites` | 🟢 编排核心能力完成 | 10 通过 | 95%（含 run_status 状态机：new→running→done/interrupt；画布前端对接、边条件操作符扩展待完善） |
| `plans` | ✅ P0 能力完成 | 13 通过 | 100%（含 run_status 状态机：new→running→done/interrupt；suite_start_mode；多实例调度治理待完善） |
| `runner` | ✅ P0 能力完成 | 58 通过 | 100%（P1：真实交易回报、基本面扩展指标与总仓位风控） |


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

#### 5.1.2 待开发任务

| 优先级 | 开发任务 | 影响模块 | 依赖/关联需求 |
|--------|----------|----------|--------------|
| P1 | API 分页与敏感配置保护（列表统一分页、`auth_info` 加密/脱敏、权限检查） | 全部 API, `datasources` | N-01, N-05 |
| P1 | 多实例 Scheduler 治理（分布式任务去重、租约/领导者选举、任务幂等键） | `plans`, `runner` | N-03 增强 |
| P2 | 画布可视化编排前端对接（拖拽节点/连线、执行轨迹回放视图） | `quant-frontend` | S-09、EX-15 |
| P2 | 执行日志生命周期管理（30 天自动清理、归档、清理命令、监控） | `execution`, `runner` | N-04 |
| P2 | 性能与容量基线（API/队列/查询/并发基准；非外部调用 API < 500ms） | 全部 runner/API | N-02 |

#### 5.1.3 开发顺序

| 顺序 | 开发阶段 | 主要交付物 | 关联需求 | 状态 |
|------|----------|------------|----------|------|
| 1 | P0 基础闭环 | `cases`、`suites`、`plans`、`runner` 核心能力 | C-07、C-09、S-09、S-10、S-11、P-03、P-08、P-09、P-10、R-01、R-06、R-07、R-09 | ✅ 已完成 |
| 2 | P1 生产可靠性 | 真实交易回报、账户级风控、基本面扩展 | R-07、R-08、EX-18 | 🟢 大部分已完成（订单联调、账户级风控、基本面财务数据/缓存/历史时点、边条件操作符、拓扑校验、交易失败告警通道已完成；剩余真实交易环境验证） |
| 3 | P1/P2 产品与运维增强 | Suite 条件操作符、拓扑增强、分页、加密、日志清理、多实例治理 | S-09、N-01、N-03、N-04、N-05 | ⏳ 部分完成（条件操作符、拓扑增强已完成；分页、日志清理、多实例治理待做） |

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
| 8 | P1 | API 分页与敏感配置保护 | 全部 API、`datasources` | 统一分页响应、`auth_info` 加密/脱敏、权限检查 | 列表接口统一分页；API 和日志不泄露密钥；旧客户端字段兼容 |
| 9 | P1 | 多实例 Scheduler 治理 | `plans`, `runner` | 分布式任务去重、租约/领导者选举、任务幂等键 | 多个 Scheduler 实例只产生一个 `(Plan, Symbol, minute)` 任务；实例故障可恢复 |
| 10 | P2 | 执行日志生命周期管理 | `execution`, `runner` | 30 天自动清理、归档策略、清理命令和监控 | 清理不影响未完成运行和订单；清理任务可重复执行且幂等 |
| 11 | P2 | 画布编排与执行轨迹回放 | `quant-frontend`, `execution` | 拖拽节点、连线配置、NodeRun 轨迹和失败节点定位 | 前端拓扑与后端快照双向一致；可按 SuiteRun 回放节点状态和事件顺序 |
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
分页/配置保护 → 多实例 Scheduler → 日志清理与性能基线
    ↓
画布编排与执行轨迹回放
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
| P0 阶段3 | `suites` CRUD、拓扑、DAG 与发布快照测试 | ✅ 10 个通过 | 画布前端拓扑测试（边条件操作符测试已并入 P1 阶段2 完成） |
| P0 阶段4 | `plans` CRUD、发布、标的解析、调度与版本管理测试 | ✅ 13 个通过 | 多实例调度治理测试 |
| P0 阶段5 | `runner`、编排（tests_orchestration）、gm SDK 和 execution 联动测试 | ✅ 71 个通过（含编排、Cron 边界、WorkerPool 重试失败传播、数据上下文、订单生命周期用例） | 生产行情、风控边界、真实交易环境测试 |
| P1 阶段1 | 交易安全闭环单元与跨模块测试 | ✅ 99 个通过（execution + plans + runner 联合回归，含订单生命周期） | 真实模拟账户链路已跑通（2026-09-07）；并发资金扣减待补 |
| P1 阶段1 | 告警（Alert）专项测试（模型/服务/渠道过滤/API/集成） | ✅ 21 个通过（tests_alerts；含渠道过滤、邮件/应用内通知、确认/解决动作、统计与集成用例） | 生产邮件网关（SMTP）与真实通知链路联调 |
| P1 阶段2 | 运行状态机专项测试 | ✅ 25 个通过（Case/Suite/Plan 三级 run_status 流转、自动完成、中断、手动停止、资金校验） | — |
| 阶段6 | 全项目回归测试 | ⚠️ 历史统计口径不统一；最新一次完整回归：✔ 260 个测试全部通过（同一命令口径） | 以同一次完整回归命令的实际输出为准 |
| P1 阶段2 | 基本面财务数据扩展专项测试 | ✅ 21 个通过（Provider 抽象、三大报表 + 财务指标、子报表独立降级、英文契约） | — |
| P1 阶段2 | 基本面缓存与历史时点专项测试 | ✅ 7 个通过（asof 历史点读、TTL 命中/过期、回源回填、回源失败降级、命中/未命中统计） | 真实外部数据源联调 |
| P1 阶段2 | Suite 边条件操作符专项测试 | ✅ 13 个通过（eq/neq/gt/gte/lt/lte/between 边界值、成组校验、旧契约兼容） | 前端契约同步后补前端耦合测试 |
| P1 阶段2 | Suite 拓扑完整性校验专项测试 | ✅ 5 个通过（跨树入边、重复边、非法权重、孤立节点、合法递归子 Suite） | — |

#### 测试验收标准

> 测试数量按测试命令口径记录。`runner` 测试包含跨模块联动用例，因此各模块数量不应直接求和；全项目总数须以同一次完整回归命令的实际输出为准。

- 每项开发任务必须有对应的单元测试或集成测试，并标注对应需求编号。
- 涉及 `execution`、`runner` 和 gm SDK 的跨模块改动，必须通过 runner 联动测试及全项目回归测试。
- 真实交易接口测试必须使用 mock 或沙盒账户，禁止在自动化测试中直接提交实盘订单。
- 生产能力阶段的测试重点包括数据为空、SDK 异常、重复回报、订单拒绝、风控拦截和任务重试。

### 5.3 非功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| N-01 | 所有 API 支持分页 | P1 |
| N-02 | API 响应时间 < 500ms（不含外部数据源调用） | P2 |
| N-03 | 策略配置变更支持热加载（无需重启服务） | ✅ 已实现；P0；关联开发任务：PlanRegistry/调度配置刷新；关联测试任务：5.2-4 |
| N-04 | 执行日志保留 30 天（自动清理） | P2 |
| N-05 | 敏感信息加密存储（数据源 `auth_info`） | P1 |


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


