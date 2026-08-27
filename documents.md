# 量化交易系统 · 全模块需求文档

> 版本：v2.0  
> 日期：2026-08-25  
> 状态：需求设计阶段 · 部分模块已实现


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
| D-04 | A股 K线表（含复权因子/涨跌停价/换手率） | `models.py` (AStockKLine) |
| D-05 | 港股 K线表（含前收盘价/货币单位） | `models.py` (HKStockKLine) |
| D-06 | 美股 K线表（含拆分因子/盘前盘后价） | `models.py` (USStockKLine) |
| D-07 | K线增量同步（`update_or_create`，避免重复） | `services.py` (sync_kline_for_symbol) |
| D-08 | K线同步日志（记录每次拉取状态） | `models.py` (KLineSyncLog) |
| D-09 | K线查询接口（按标的 + 日期范围） | `views.py` (query_kline) |
| D-10 | K线同步触发接口（单标的 / 全部） | `views.py` (sync_kline) |

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

class AStockKLine(AbstractKLine):
    adj_factor = models.DecimalField(max_digits=12, decimal_places=6, default=1.0)
    limit_up, limit_down = models.DecimalField(...)
    turnover_rate = models.DecimalField(...)
    class Meta: db_table = 'kline_a_stock'
    # 同样模式：HKStockKLine, USStockKLine
```


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
| EX-14 | **事件循环基础处理** | 🟡 部分完成 | `services.py`；关联待办：5.2-4 |
| EX-15 | **事件匹配逻辑（Event → Edge 路由）** | 🟡 基础完成 | `services.py`；关联待办：5.2-2、5.2-4 |
| EX-16 | **Plan 触发接口（创建 SuiteRun）** | ✅ 完成 | `views.py`, `services.py` |
| EX-17 | **SuiteRun 状态流转逻辑** | ✅ 完成 | `services.py` |
| EX-18 | **委托单状态回写（对接交易接口）** | 🟡 已接入 gm 适配器，生产回报验证待完善 | `runner/gm_adapter.py`；关联待办：5.1-2 |

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
| **状态** | 🟡 已完成 CRUD、触发校验、发布和删除保护；参数 Schema 与版本快照待完善 |
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
| C-07 | Case 版本历史记录（发布快照） | P1；关联待办：5.2-1 |
| C-08 | 节点类型定义（signal / filter / verdict / executor） | ✅ 完成 |
| C-09 | 参数 JSON Schema 校验（根据节点类型校验参数完整性） | P1；关联待办：5.2-1 |

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
| S-09 | 条件路由支持（`Edge.event_condition` 匹配事件类型） | 🟡 基础完成；关联待办：5.2-2 |
| S-10 | 聚合方式支持（加权求和 / 投票 / 逻辑与 / 逻辑或） | 🟡 字段已支持，运行时聚合待实现；关联待办：5.2-2 |
| S-11 | 并行节点执行支持 | ❌ 待实现；关联待办：5.2-2 |

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
| P-03 | Plan 发布（校验根 Suite 已发布 + 创建版本快照 + 通知异步引擎热加载） | 🟡 基础完成；关联待办：5.2-3、5.4-N-03 |
| P-04 | 标的范围解析（all / 分组 / 指定列表 → 调用 `watchlists.services.resolve_symbol_scope`） | ✅ 完成 |
| P-05 | Cron 表达式校验 | 🟡 基础完成（5 字段和字符校验）；关联待办：5.2-3 |
| P-06 | Plan 删除保护（已有执行记录时返回 409 Conflict） | ✅ 完成 |
| P-07 | 触发方式支持（时间驱动 / 事件驱动 / 手动触发） | ✅ 完成 |
| P-08 | 执行模式支持（串行 / 并行 / 失败停止） | P1；关联待办：5.2-3 |
| P-09 | 重试策略配置（重试次数 + 延迟秒数） | P2；关联待办：5.2-3 |
| P-10 | Plan 历史版本回滚 | P2；关联待办：5.2-3 |

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
| R-06 | **CaseExecutor**：执行单个 Case 的运算逻辑（因子计算/过滤/裁决） | 🟡 声明式结果完成，计算引擎待接入；关联待办：5.1-1、5.2-1 |
| R-07 | **数据夹具（Fixture）**：为 Case 执行提供数据上下文（K线/基本面/实时快照） | 🟡 gm 基础行情 Fixture 已接入，生产数据上下文待扩展；关联待办：5.1-1、5.2-4 |
| R-08 | **风控拦截器**：在 Executor 节点输出前校验仓位/资金限制 | 🟡 基础数量/金额限制已接入；复杂风控规则待扩展；关联待办：5.1-1、5.2-4 |
| R-09 | **热加载**：Plan 发布后自动刷新内存中的 DAG 配置 | P1；关联待办：5.2-3、5.4-N-03 |
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
| `execution` | ⚠️ 基础闭环完成 | 12 通过 | 70%（服务层完成，runner/Case 执行待开发） |
| `cases` | 🟡 P0 基础能力完成 | 8 通过 | 70%（Schema、版本历史和执行器待开发） |
| `suites` | 🟡 P0 基础能力完成 | 9 通过 | 70%（运行时聚合、并行执行和完整画布拓扑待完善） |
| `plans` | 🟡 P0 基础能力完成 | 10 通过 | 70%（完整 Cron、调度器、版本快照和热加载待开发） |
| `runner` | 🟡 基础能力完成 | 3 通过 | 60%（核心执行链路完成，生产能力待完善） |


## 五、待办事项汇总

### 5.1 当前 execution 状态

| 序号 | 问题描述 | 影响模块 | 紧急程度 | 关联需求 |
|------|----------|----------|----------|----------|
| 1 | gm 行情 Fixture、基础风控拦截和可注入 Case 计算已接入；复杂因子与生产数据上下文仍待扩展 | `runner`, `cases`, `datasources` | 🟡 P1 | 关联：R-06、R-07、R-08 |
| 2 | gm 订单状态回调已按外部订单 ID 关联并回写；真实交易环境回报字段适配仍需持续验证 | `runner`, `execution` | 🟡 P1 | 关联：EX-18、R-11 |
| 3 | `EventRegistry.validate` 已支持数据库回退并回填缓存 | `execution` | ✅ 已解决 | 关联：EX-02 |
| 4 | gm SDK 适配器已接入行情查询、订阅、调度、下单和订单回报接口 | `runner`, `execution` | ✅ 已完成 | 关联：R-01、R-07、R-11 |

### 5.2 后续模块开发（建议顺序）

| 顺序 | 模块 | 预估工作量 | 关键依赖 |
|------|------|------------|----------|
| 1 | `cases` 剩余能力 | 中 | 参数 Schema、版本历史、真实 CaseExecutor；关联：C-07、C-09、R-06 |
| 2 | `suites` 剩余能力 | 中 | 运行时聚合、并行节点执行；关联：S-09、S-10、S-11、EX-15 |
| 3 | `plans` 剩余能力 | 中 | 完整 Cron、版本快照、热加载；关联：P-03、P-05、P-08、P-09、P-10、R-09 |
| 4 | `runner` 生产能力 | 大 | 复杂因子、生产数据上下文、交易环境验证；关联：R-06、R-07、R-08、EX-18 |

### 5.3 测试验证计划

| 阶段 | 内容 | 验收标准 |
|------|------|----------|
| 阶段1 | `execution` 模块测试全部通过 | 12 个测试全部 OK |
| 阶段2 | `cases` 模块基础功能测试全部通过 | 8 个测试全部 OK；Schema 和版本历史测试待补 |
| 阶段3 | `suites` 模块基础功能测试全部通过 | 9 个测试全部 OK；运行时聚合和并行执行测试待补 |
| 阶段4 | `plans` 模块基础功能测试全部通过 | 10 个测试全部 OK；调度器和版本快照测试待补 |
| 阶段5 | `runner` 基础集成测试全部通过 | 8 个 runner/SDK 测试 OK；复杂因子和真实交易环境测试待补 |
| 阶段6 | 全项目回归测试 | 85 个测试全部 OK |

### 5.4 非功能需求

| 编号 | 需求描述 | 优先级 |
|------|----------|--------|
| N-01 | 所有 API 支持分页 | P1 |
| N-02 | API 响应时间 < 500ms（不含外部数据源调用） | P2 |
| N-03 | 策略配置变更支持热加载（无需重启服务） | P0；关联待办：5.2-3、R-09 |
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