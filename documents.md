# 量化交易系统 · 全模块需求文档

> 版本：v2.1  
> 日期：2026-09-02  
> 状态：需求设计阶段 · 核心模块已实现并已通过回归验证


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
   │                           └── execution（执行日志/委托单/事件）
   │                                  │
   │                                  └── runner（独立异步引擎）
   │
   └── （所有模块均依赖 users）
```


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
| W-05 | 全市场 A 股标的同步（AkShare `stock_info_a_code_name`） | `services.py` (sync_market_data) |
| W-06 | 分组 CRUD（名称唯一） | `models.py`, `views.py` |
| W-07 | 分组内批量添加/移除标的 | `views.py` (add_symbols, remove_symbols) |
| W-08 | 用户自选池（绑定分组列表，每个用户仅一个） | `models.py`, `views.py` |
| W-09 | 解析 `symbol_scope` 配置（供 Plan 调用） | `services.py` (resolve_symbol_scope) |

#### API 端点

| 方法 | 端点 | 功能 |
|------|------|------|
| GET/POST | `/api/watchlists/symbols/` | 标的列表/创建 |
| GET/PUT/DELETE | `/api/watchlists/symbols/{id}/` | 标的详情/更新/删除 |
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

当前 K 线存储已从“按市场共表”调整为“按标的编码分表”方案，并进一步拆成独立 K 线数据库：

- 默认主库：`default`，保留 Django 业务数据（用户、策略、案例等）
- 独立 K 线库：`kline`，默认使用 SQLite 本地实现；在生产环境可切换为 MariaDB/MySQL
- 表名规则：`kline_{market}_{symbol_code}`，例如 `kline_a_000001`
- 运行时创建：当首次同步或查询某个 symbol 时，自动检查并创建该 symbol 对应的表
- 查询时按 `symbol_id + date range` 定位目标表
- 兼容策略：旧的市场级 legacy 表仍保留，查询/同步时优先命中新分表，若新表未落数据则回退到 legacy 表
- 开关方式：设置 `USE_KLINE_MARIADB=true`，并提供 `KLINE_DB_HOST`、`KLINE_DB_PORT`、`KLINE_DB_NAME` 等环境变量即可切换到 MariaDB

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


### 模块4：`execution`（事件与执行基础设施）⚠️ 基础闭环已完成

| 属性 | 说明 |
|------|------|
| **状态** | ⚠️ 数据层和基础执行闭环已完成，独立 runner 与 Case 执行待开发 |
| **优先级** | P0 |
| **依赖** | `plans.Plan`, `suites.Suite`（外键允许空） |

#### 功能需求

| 编号 | 需求描述 | 实现状态 | 实现文件 |
|------|----------|----------|----------|
| EX-01 | 系统内置事件类型常量（EventType） | ✅ 完成 | `events.py` |
| EX-02 | 事件类型注册中心（缓存 + 校验 + 列表） | ✅ 完成 | `registry.py` |
| EX-03 | 自定义事件类型注册表模型（EventTypeRegistry） | ✅ 完成 | `models.py` |
| EX-04 | 执行实例模型（SuiteRun） | ✅ 完成 | `models.py` |
| EX-05 | 事件模型（Event） | ✅ 完成 | `models.py` |
| EX-06 | 执行日志模型（ExecutionLog） | ✅ 完成 | `models.py` |
| EX-07 | 委托单模型（Order） | ✅ 完成 | `models.py` |
| EX-08 | 事件类型管理 API（CRUD + list-all） | ✅ 完成 | `views.py`, `serializers.py` |
| EX-09 | SuiteRun 只读 API | ✅ 完成 | `views.py` |
| EX-10 | Event 只读 API | ✅ 完成 | `views.py` |
| EX-11 | ExecutionLog 只读 API | ✅ 完成 | `views.py` |
| EX-12 | Order CRUD API | ✅ 完成 | `views.py` |
| EX-13 | Admin 后台注册所有模型 | ✅ 完成 | `admin.py` |
| EX-14 | **事件循环基础处理** | 🟡 部分完成 | `services.py`；关联开发任务：5.1.3-2、5.1.3-4 |
| EX-15 | **事件匹配逻辑（Event → Edge 路由）** | 🟡 基础完成 | `services.py`；关联开发任务：5.1.3-2 |
| EX-16 | **Plan 触发接口（创建 SuiteRun）** | ✅ 完成 | `views.py`, `services.py` |
| EX-17 | **SuiteRun 状态流转逻辑** | ✅ 完成 | `services.py` |
| EX-18 | **委托单状态回写（对接交易接口）** | 🟡 已接入 gm 适配器，生产回报验证待完善 | `runner/gm_adapter.py`；关联开发任务：5.1.2-4 |

#### API 端点

| 方法 | 端点 | 功能 |
|------|------|------|
| GET/POST | `/api/execution/event-types/` | 事件类型注册表 CRUD（管理员） |
| GET | `/api/execution/event-types/list-all/` | 列出所有事件类型（含内置） |
| GET | `/api/execution/runs/` | SuiteRun 列表 |
| GET | `/api/execution/events/` | Event 列表 |
| GET | `/api/execution/logs/` | ExecutionLog 列表 |
| GET/POST/PUT/DELETE | `/api/execution/orders/` | Order CRUD |
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
```

#### 当前执行服务

`apps/execution/services.py` 已提供以下基础能力：

- 校验已发布 Plan 并创建 SuiteRun，同时注入 `SUITE_INIT` 事件。
- 启动、停止和完成 SuiteRun，并记录开始/结束时间。
- 校验事件类型、持久化 Event，并维护 `event_queue` 中的事件 ID。
- 消费队列事件，按 `Edge.event_condition` 做简单键值匹配并路由后续事件。
- 事件处理异常时，将 Event 和 SuiteRun 标记为失败。

该实现是执行层的同步基础服务，不等同于独立进程中的完整 EventLoop；Case 匹配和 CaseExecutor 仍需在 `runner` 与 `cases` 模块完成后接入。


### 模块5：`cases`（原子策略节点）🟡 P0 基础能力已完成

| 属性 | 说明 |
|------|------|
| **状态** | 🟡 已完成 CRUD、触发校验、发布快照和删除保护；复杂 Schema 规则待完善 |
| **优先级** | P0 |
| **依赖** | `execution.EventRegistry`（校验触发事件类型） |

#### 功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| C-01 | Case 模型（名称、节点类型、参数 JSON、版本、状态） | ✅ 完成 |
| C-02 | Case CRUD API（列表、详情、创建、更新、删除） | ✅ 完成 |
| C-03 | Case 发布（版本号 +1，状态改为 published） | ✅ 完成 |
| C-04 | `params` 中的 `trigger` 配置校验（调用 `EventRegistry.validate`） | ✅ 完成 |
| C-05 | 删除保护（被 Suite 引用时返回 409 Conflict） | ✅ 完成 |
| C-06 | Case 搜索/过滤（按名称、节点类型、状态） | ✅ 完成 |
| C-07 | Case 版本历史记录（发布快照） | ✅ 基础完成 |
| C-08 | 节点类型定义（signal / filter / verdict / executor） | ✅ 完成 |
| C-09 | 参数 JSON Schema 校验（根据节点类型校验参数完整性） | ✅ 基础完成 |

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


### 模块6：`suites`（工作流编排）🟡 P0 基础能力已完成

| 属性 | 说明 |
|------|------|
| **状态** | 🟡 已完成 CRUD、拓扑读写、DAG 校验、发布校验和 Plan 引用删除保护；聚合执行与并行调度待完善 |
| **优先级** | P0 |
| **依赖** | `cases.Case` |

#### 功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| S-01 | Suite 模型（名称、聚合方式、父 Suite、状态、版本） | ✅ 完成 |
| S-02 | Edge 模型（源 Suite → 目标 Suite、条件、事件条件、权重） | ✅ 完成 |
| S-03 | Suite CRUD API | ✅ 完成 |
| S-04 | DAG 无环校验（发布前检查） | ✅ 完成 |
| S-05 | 拓扑获取接口（完整树形结构，供前端画布渲染） | ✅ 完成 |
| S-06 | 拓扑更新接口（批量增删节点和边） | ✅ 完成 |
| S-07 | Suite 发布（递归校验所有引用 Case/Suite 已发布） | ✅ 完成 |
| S-08 | 删除保护（被 Plan 引用时返回 409 Conflict） | ✅ 完成 |
| S-09 | 条件路由支持（`Edge.event_condition` 匹配事件类型） | 🟡 基础完成；关联开发任务：5.1.2-6 |
| S-10 | 聚合方式支持（加权求和 / 投票 / 逻辑与 / 逻辑或） | 🟡 字段已支持，运行时聚合待实现；关联开发任务：5.1.2-6 |
| S-11 | 并行节点执行支持 | ❌ 待实现；关联开发任务：5.1.2-6 |

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

当前实现文件：`models.py`、`serializers.py`、`services.py`、`views.py`、`urls.py`。拓扑更新在事务中替换 Case 关联和当前 Suite 出边，并在提交前执行 DAG 校验。


### 模块7：`plans`（调度管理）🟡 P0 基础能力已完成

| 属性 | 说明 |
|------|------|
| **状态** | 🟡 已完成 CRUD、触发校验、标的解析、发布和删除保护；Cron 完整校验与调度器待完善 |
| **优先级** | P0 |
| **依赖** | `suites.Suite`, `watchlists`（解析 symbol_scope） |

#### 功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| P-01 | Plan 模型（名称、根 Suite、触发方式、Cron 表达式、标的范围、执行模式、重试策略、状态、版本） | ✅ 完成 |
| P-02 | Plan CRUD API | ✅ 完成 |
| P-03 | Plan 发布（校验根 Suite 已发布 + 创建版本快照 + 通知异步引擎热加载） | 🟡 基础完成；关联开发任务：5.1.2-7、5.3-N-03 |
| P-04 | 标的范围解析（all / 分组 / 指定列表 → 调用 `watchlists.services.resolve_symbol_scope`） | ✅ 完成 |
| P-05 | Cron 表达式校验 | 🟡 基础完成（5 字段和字符校验）；关联开发任务：5.1.2-7 |
| P-06 | Plan 删除保护（已有执行记录时返回 409 Conflict） | ✅ 完成 |
| P-07 | 触发方式支持（时间驱动 / 事件驱动 / 手动触发） | ✅ 完成 |
| P-08 | 执行模式支持（串行 / 并行 / 失败停止） | P1；关联开发任务：5.1.2-7 |
| P-09 | 重试策略配置（重试次数 + 延迟秒数） | P2；关联开发任务：5.1.2-7 |
| P-10 | Plan 历史版本回滚 | P2；关联开发任务：5.1.2-7 |

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

当前实现文件：`models.py`、`serializers.py`、`services.py`、`views.py`、`urls.py`。Plan 只能在发布时要求根 Suite 已发布，创建和编辑阶段允许保存草稿配置。


### 模块8：`runner`（独立异步引擎）🟡 基础能力已完成

| 属性 | 说明 |
|------|------|
| **状态** | 🟡 EventLoop、SuiteRunner、Scheduler、TaskQueue、WorkerPool 已完成；数据夹具、风控和外部交易接口待完善 |
| **优先级** | P0 |
| **依赖** | `execution.SuiteRun`, `execution.Event`, `cases.Case`, `suites.Suite`, `plans.Plan` |

#### 功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| R-01 | **Scheduler（调度器）**：定时扫描 Plan，按 Cron 表达式触发执行 | P0 |
| R-02 | **Task Queue**：任务入队（每个 `(Plan, Symbol)` 为一个独立任务） | ✅ 基础完成 |
| R-03 | **Worker Pool**：固定数量协程并发执行任务 | ✅ 基础完成 |
| R-04 | **SuiteRunner**：加载 Suite 拓扑，创建 SuiteRun 实例 | ✅ 基础完成 |
| R-05 | **EventLoop**：消费 SuiteRun.event_queue，匹配事件 → 执行 Case → 产出新事件 | ✅ 基础完成 |
| R-06 | **CaseExecutor**：执行单个 Case 的运算逻辑（因子计算/过滤/裁决） | 🟡 声明式结果完成，计算引擎待接入；关联开发任务：5.1.2-2 |
| R-07 | **数据夹具（Fixture）**：为 Case 执行提供数据上下文（K线/基本面/实时快照） | 🟡 gm 基础行情 Fixture 已接入，生产数据上下文待扩展；关联开发任务：5.1.2-1 |
| R-08 | **风控拦截器**：在 Executor 节点输出前校验仓位/资金限制 | 🟡 基础数量/金额限制已接入；复杂风控规则待扩展；关联开发任务：5.1.2-3 |
| R-09 | **热加载**：Plan 发布后自动刷新内存中的 DAG 配置 | P1；关联开发任务：5.1.2-7、5.3-N-03 |
| R-10 | **执行日志写入**：将执行结果写入 ExecutionLog 表 | ✅ 基础完成 |
| R-11 | **委托单生成**：将 Executor 节点的输出转换为 Order 记录 | ✅ 基础完成 |

当前实现文件：`runner/executor.py`、`runner/engine.py`、`runner/queue.py`、`runner/scheduler.py`、`runner/gm_adapter.py`。Case 可通过 `params.result` 声明 direction、payload 和 order；`GmBrokerAdapter` 已封装 gm SDK 的 `set_token`、`subscribe`、`history`、`history_n`、`schedule`、`order_volume`、`get_orders` 及订单状态回调。真实因子、行情 Fixture、风控和交易回报的生产策略仍可在该适配边界上继续扩展。

#### 执行流程

```
1. Scheduler 唤醒 Plan → 2. 解析 symbol_scope 获取标的列表
   → 3. 为每个 (Plan, Symbol) 创建任务入队
   → 4. Worker 从队列取出任务
   → 5. 创建 SuiteRun 实例（状态: pending）
   → 6. 加载 Suite 拓扑（从数据库读取）
   → 7. 注入 INIT 事件到 event_queue
   → 8. 进入事件循环（EventLoop）：
        while event_queue:
            event = event_queue.pop(0)
            匹配订阅该事件的 Case
            for each matched Case:
                执行 Case
                产出新事件 (CASE_COMPLETED / CASE_FAILED)
                追加到 event_queue
   → 9. Suite 完成 → 写入 ExecutionLog
   → 10. 若为 Executor 节点 → 生成 Order
```


## 四、模块完成进度总览

| 模块 | 状态 | 测试用例数 | 完成度 |
|------|------|------------|--------|
| `users` | ✅ 已完成 | 5 通过 | 100% |
| `watchlists` | ✅ 已完成 | 15 通过 | 100% |
| `datasources` | ✅ 已完成 | 18 通过 | 100% |
| `execution` | ⚠️ 基础闭环完成 | 12 通过 | 75%（生产回报和完整编排增强待完善） |
| `cases` | 🟡 P0 基础能力完成 | 9 通过 | 85%（复杂 Schema 规则和真实因子引擎待完善） |
| `suites` | 🟡 P0 基础能力完成 | 9 通过 | 90%（完整画布拓扑待完善） |
| `plans` | 🟡 P0 基础能力完成 | 10 通过 | 90%（持久化调度进程和版本回滚待完善） |
| `runner` | 🟡 基础能力完成 | 6 通过 | 75%（生产数据、风控和交易能力待完善） |


## 五、待办事项汇总

### 5.1 开发任务

本节只记录需要实现或继续完善的功能；已完成事项放在状态表中，不再作为待开发任务重复排队。

#### 5.1.1 已完成开发能力

| 功能 | 影响模块 | 状态 | 关联需求 |
|------|----------|------|----------|
| EventRegistry 数据库回退与缓存回填 | `execution` | ✅ 已完成 | EX-02 |
| gm SDK 行情查询、订阅、调度、下单和订单回报适配 | `runner`, `execution` | ✅ 已完成 | R-01、R-07、R-11、EX-18 |
| gm 订单外部 ID 关联与本地状态回写 | `runner`, `execution` | ✅ 基础完成 | EX-18、R-11 |
| 基础行情 Fixture、数量/金额风控、可注入 Case 计算 | `runner`, `cases`, `datasources` | ✅ 基础完成 | R-06、R-07、R-08 |

#### 5.1.2 待开发任务

| 优先级 | 开发任务 | 影响模块 | 依赖/关联需求 |
|--------|----------|----------|--------------|
| P0（阻塞） | 扩展真实行情、基本面和实时快照数据上下文 | `runner`, `datasources` | R-07、D-02、D-09；阻塞真实 Case 执行 |
| P0（阻塞） | 扩展真实因子计算、过滤、裁决逻辑（基础声明式计算已完成） | `cases`, `runner` | C-09、R-06；阻塞有效策略运行 |
| P1 | 完善复杂仓位、资金和交易时段风控 | `runner`, `execution` | R-08 |
| P1 | 完善真实交易环境回报字段和订单生命周期适配 | `runner`, `execution` | EX-18 |
| P0（阻塞） | 扩展 Case 参数 JSON Schema 规则和真实 CaseExecutor（基础校验、版本快照已完成） | `cases`, `runner` | C-07、C-09、R-06；阻塞稳定的 Suite/Runner 配置 |
| P0（阻塞） | 完善跨 Suite 条件路由和运行时编排（基础聚合、并行执行已完成） | `suites`, `execution` | S-09、S-10、S-11、EX-15；阻塞完整 Runner 编排 |
| P0（阻塞） | 完善持久化 Cron 调度和配置刷新（基础 Cron、快照、执行模式、热加载已完成） | `plans`, `runner` | P-03、P-05、P-08、P-09、P-10、R-09；阻塞自动调度 |

#### 5.1.3 开发顺序

| 顺序 | 开发阶段 | 主要交付物 | 关联需求 |
|------|----------|------------|----------|
| 1 | P0 阻塞：`cases` 剩余能力 | 参数 Schema、版本历史、真实 CaseExecutor | C-07、C-09、R-06 |
| 2 | P0 阻塞：`suites` 剩余能力 | 条件路由、运行时聚合、并行节点执行 | S-09、S-10、S-11、EX-15 |
| 3 | P0 阻塞：`plans` 剩余能力 | 完整 Cron、版本快照、执行模式、热加载 | P-03、P-05、P-08、P-09、P-10、R-09 |
| 4 | P0 阻塞：`runner` 数据能力 | 真实数据上下文、真实 Case 计算 | R-06、R-07、D-02、D-09 |
| 5 | P1 生产增强：`runner` 交易能力 | 复杂风控、真实交易回报适配 | R-08、EX-18 |

### 5.2 测试任务

本节只记录验证任务，不把测试数量或验收标准混入开发任务清单。

| 阶段 | 测试任务 | 当前结果 | 待补验证 |
|------|----------|----------|----------|
| P0 阶段1 | `execution` 基础功能与 API 测试 | ✅ 12 个通过 | Order 真实交易回报集成测试 |
| P0 阶段2 | `cases` CRUD、发布和触发校验测试 | ✅ 9 个通过 | 复杂 Schema、真实计算测试 |
| P0 阶段3 | `suites` CRUD、拓扑和 DAG 测试 | ✅ 9 个通过 | 完整画布拓扑测试 |
| P0 阶段4 | `plans` CRUD、发布和标的解析测试 | ✅ 10 个通过 | 持久化调度、版本回滚测试 |
| P0 阶段5 | `runner`、gm SDK 和 execution 联动测试 | ✅ 11 个通过 | 生产行情、风控边界、真实交易环境测试 |
| 阶段6 | 全项目回归测试 | ✅ 85 个通过 | 持续回归新增功能 |

#### 测试验收标准

- 每项开发任务必须有对应的单元测试或集成测试，并标注对应需求编号。
- 涉及 `execution`、`runner` 和 gm SDK 的跨模块改动，必须通过 runner 联动测试及全项目回归测试。
- 真实交易接口测试必须使用 mock 或沙盒账户，禁止在自动化测试中直接提交实盘订单。
- 生产能力阶段的测试重点包括数据为空、SDK 异常、重复回报、订单拒绝、风控拦截和任务重试。

### 5.3 非功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| N-01 | 所有 API 支持分页 | P1 |
| N-02 | API 响应时间 < 500ms（不含外部数据源调用） | P2 |
| N-03 | 策略配置变更支持热加载（无需重启服务） | P0；关联开发任务：5.1.2-7；关联测试任务：5.2-4 |
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