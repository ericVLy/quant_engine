# MCP Agent 使用流程规范

> 面向**接入本系统的 AI Agent / 智能体**，回答「拿到 MCP 后怎么用、什么不能做、出错怎么办」。
> 部署与实现细节见 `README.md` 的「MCP 服务」小节与 `mcp_server/` 源码。

---

## 0. 一分钟速览

| 项 | 值 |
|---|---|
| 传输 | 默认 **SSE（HTTP）**，端点 `http://127.0.0.1:8765/sse`；本机 IDE 可用 `stdio` |
| 服务名 | `quant-engine` v1.0.0 |
| 工具数 | **24** 个：13 只读 + 11 受控写（以 `tools/list` 为准） |
| 鉴权 | 回环地址默认不鉴权；**非回环绑定必须配 `MCP_AUTH_TOKEN`**（Bearer） |
| 默认姿态 | **写操作全部关闭**（`MCP_ALLOW_TRIGGER` / `MCP_ALLOW_MUTATE` 均为关） |
| 核心隐喻 | `Case`（原子策略）→ `Suite`（DAG 编排）→ `Plan`（调度）→ `SuiteRun` / `Order` |

**给 Agent 的第一条纪律**：默认只用只读工具。任何写操作都要先向用户确认，见 §5。

---

## 1. 接入

### 1.1 SSE（HTTP，推荐）

```json
{
  "mcpServers": {
    "quant-engine": {
      "type": "sse",
      "url": "http://127.0.0.1:8765/sse",
      "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
    }
  }
}
```

配置了令牌时必须带 `Authorization` 头，否则服务端拒绝。

### 1.2 stdio（本机 IDE）

```json
{ "mcpServers": { "quant-engine": { "command": "python", "args": ["-m", "mcp_server", "--transport", "stdio"] } } }
```

### 1.3 连通性自检

访问 `GET /health`（配置令牌后同样需要 `Authorization` 头）：

```json
{ "status": "ok", "server": "quant-engine", "transport": "sse" }
```

拿不到 `ok` 就不要继续调用工具——后续报错会难以区分是"服务没起"还是"参数不对"。

### 1.4 协议资源

服务注册了 `quant://docs/overview`（`text/plain`），内容是系统定位、核心隐喻与写操作边界。
**建议接入后先读一次**，它是最权威的能力边界说明。

---

## 2. 工具清单

### 2.1 只读工具（13 个，默认全部可用）

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `search_symbols` | 按代码/中文名模糊搜标的 | `query`、`market`（A/HK/US）、`limit`≤200 |
| `resolve_symbol_name` | 代码 → 中文名 | `code`、`market` |
| `query_kline` | 查 K 线 | `symbol_code`、`start_date`、`end_date`、`limit`≤500 |
| `list_plans` | 列 Plan（**默认仅 published**） | `status`、`limit` |
| `get_plan` | Plan 详情 | `plan_id`、`include_symbols` |
| `list_cases` | 列 Case | `status`、`node_type`、`limit` |
| `get_case` | Case 详情（含 `params`） | `case_id` |
| `get_suite_topology` | Suite 编排树（DAG） | `suite_id` |
| `list_event_types` | 已注册事件类型 | `include_system` |
| `list_alerts` | 查告警 | `status`、`severity`、`limit` |
| `alert_statistics` | 告警统计 | — |
| `get_intraday_series` | 分时监控序列 | `symbol_code`、`limit` |
| `list_suite_runs` | 执行记录 | `plan_id`、`symbol`、`limit` |

### 2.2 受控写工具（11 个，**默认全部禁用**）

两组开关**相互独立**，开一个不会连带开另一个。

**A 组 · 执行触发**（需 `MCP_ALLOW_TRIGGER=1` 或 `--allow-trigger`）

| 工具 | 作用 |
|---|---|
| `trigger_plan_execution` | 为已发布 Plan + 标的创建 `pending` SuiteRun |

**B 组 · 配置写入**（需 `MCP_ALLOW_MUTATE=1` 或 `--allow-mutate`）

| 工具 | 作用 |
|---|---|
| `create_case` / `update_case` / `delete_case` | Case 增改删（仅 draft） |
| `create_suite` / `update_suite` / `update_suite_topology` / `delete_suite` | Suite 增改删 + 拓扑写入 |
| `create_plan` / `update_plan` / `delete_plan` | Plan 增改删（仅 draft） |

---

## 3. 核心概念与关键约束

### 3.1 标的范围下沉到 Case（最容易搞错的一点）

> Plan **不直接声明标的**。Plan 覆盖的标的 = 其根 Suite 编排树内**各 Case 声明标的的并集**。

`Case.params.symbol_scope` 是该 Case 负责的范围：

```json
{ "type": "all" }                                  // 全部标的
{ "type": "groups", "groups": ["核心池"] }          // 按分组
{ "type": "symbols", "symbols": ["000001","600000"] } // 按标的
```

**树内没有任何已发布 Case 声明 `symbol_scope` 时，Plan 无法发布**（错误信息：
`编排树内没有任何已发布 Case 声明 symbol_scope`）。 用 `get_plan(include_symbols=True)`
可直接读到解析后的并集，比自己遍历树更省事。

### 3.2 事件类型与叠加事件

- 事件类型分三类：`system`（代码内置，禁建）/ `user`（必须叠加在系统事件上）/ `plugin`（可选叠加）；
- 校验叠加事件的基事件是否合法，用 `list_event_types`；
- 边的 `next_event` 属于**路由元数据**（决定下一跳），不是 payload 键——别拿它比对 payload。

### 3.3 编排边不是任意连的

`update_suite_topology` 会做 **DAG 无环校验**与 `event_condition` 白名单校验，
成环或非法条件直接报错（详见 §6）。

### 3.4 发布与启停**不在 MCP 范围内**

MCP 只改 `draft`。**发布（publish）、启动/停止一律走 REST 或前端**。
这是刻意设计：避免 Agent 一步把策略推进到可执行态。
所以你写完 Plan/Suite 后，用户需要在前端或 REST 完成发布，MCP 这边就完成了。

### 3.5 触发 ≠ 下单

`trigger_plan_execution` **只创建 `pending` SuiteRun**，真正的下单由独立的
runner 进程按调度/认领后执行。触发后请用 `list_suite_runs` 观察状态流转，
**不要**因为触发了就认为已成交。

---

## 4. 典型工作流

### 4.1 查数据（最常见）

```
search_symbols(query="浦发") → 拿到 code
  → query_kline(symbol_code="600000", limit=120)
  → get_intraday_series(symbol_code="600000")   # 需要盘中实时时
```

**先搜再查**：`query_kline` 的 `symbol_code` 必须是**已存在于 `watchlists.Symbol` 的代码**，
不存在会直接报错。不要凭猜测构造代码。

### 4.2 读懂一个策略

```
list_plans(status="published")
  → get_plan(plan_id)                 # 拿到根 Suite 与标的并集
  → get_suite_topology(suite_id)      # 展开 DAG
  → get_case(case_id)                 # 逐个看 params 逻辑
```

### 4.3 排查告警

```
alert_statistics()                     # 先看总量与分布
  → list_alerts(status="pending", severity="critical")   # 再拉明细
  → list_suite_runs(plan_id=<相关 Plan>)                 # 关联执行记录
```

### 4.4 创建策略（写操作，需 B 组开关）

顺序是**强制的**，不能跳步：

```
create_case(signal) + create_case(executor)
  → create_suite(case_ids=[...])       # 建 Suite
  → update_suite_topology(suite_id, topology={...})   # 写入编排边
  → create_plan(root_suite=<id>, trigger_type="manual")
  → 【交付用户】发布走 REST/前端
```

拓扑要**整体写入**（不是增量 patch）。建议先建空 Suite 再写拓扑，便于分步确认。

### 4.5 触发执行（需 A 组开关）

```
list_plans(status="published")  → 选已发布 Plan
  → trigger_plan_execution(plan_id, symbols=["000001"])
  → list_suite_runs(plan_id)    # 观察 pending → running → completed/failed
```

---

## 5. 写操作纪律（Agent 必守）

### 5.1 触发前必须确认

调用 `trigger_plan_execution` 会**创建真实待执行任务**，可能导致真实下单。
Agent 在调用前必须：

1. 明确告知用户：将要触发的 Plan、标的、可能的资金占用；
2. 获得用户**显式确认**；
3. 确认 Plan 状态是 `published`（`list_plans(status="published")`），且 `allocated_capital` 不超过账户可用额度。

### 5.2 配置写入前先读现状

`update_*` 是 partial 更新，但 `params`（Case）是**整体替换**。
改 Case 前先 `get_case` 取回原值并合并，否则会把没传的字段清空。

### 5.3 不要绕过设计边界

- 不要为了"一步到位"去尝试调 publish——工具不存在，也**不应该**存在；
- 不要在未确认的情况下批量删除（有引用/执行记录会被拒绝，见 §6）；
- 不要把只读工具的返回当成实时权威——资金/持仓以同步快照为准。

---

## 6. 错误契约

| 错误 | 含义 | 处理 |
|---|---|---|
| `PermissionError` | 写开关未开 | **不要重试**。告知用户需要 `MCP_ALLOW_TRIGGER=1` / `MCP_ALLOW_MUTATE=1` 或启动参数 |
| `MutationConflictError` | 被引用 / 有执行记录（REST 语义 409） | 先读引用关系（`get_suite_topology` / `list_plans`），再决定换引用方还是放弃 |
| `ValueError` | 字段校验失败，**消息含可定位字段名** | 按提示修字段后重试 |
| `ExecutionError` | 生命周期校验失败（如 Plan 未发布） | 用 `get_plan` 确认状态；`trigger_plan_execution` 只接受 `published` |
| 标的不存在 | `query_kline` / 事件的标的未纳管 | 先 `search_symbols` 确认 |
| 找不到 Plan/Case/Suite | ID 不存在（`DoesNotExist`） | 回 `list_*` 重新取 ID |

**关键**：写开关未开是**配置问题**，不是参数问题，重试只会一直失败。

---

## 7. 符号代码规范

- A 股：`600000`、`000001`、`002716`；
- 带市场前缀：`sh000001`（上证指数）、`sz399001`——**前缀有语义，别剥**；
- gm SDK 侧用 `SHSE.600000` / `SZSE.000426` 形式（交易所前缀）；
- MCP 工具用的是**系统侧代码**，与 gm 持仓返回的形式不同。

排查持仓/标的匹配问题时，注意两侧代码形态要归一后再比。

---

## 8. 常见错误与排查

| 症状 | 原因 | 处理 |
|---|---|---|
| 写工具一律 `PermissionError` | 写开关默认关闭 | 配环境变量或启动参数；**不要**反复重试 |
| `query_kline` 报标的不存在 | 代码未纳管或形态错 | `search_symbols` 先确认 |
| Plan 发布失败 | 树内无 Case 声明标的 | `get_plan(include_symbols=True)` 查并集 |
| `update_suite_topology` 报错 | DAG 成环 / 非法 `event_condition` | 检查边方向与条件白名单 |
| 连不上 / `/health`不通 | 服务未起或令牌不对 | 核对 `MCP_PORT`、`Authorization` 头 |
| 触发后无成交 | 只创建了 `pending`，需 runner 消费 | `list_suite_runs` 看状态；确认 runner 在跑 |

---

## 9. 自检清单

接入后建议按序验证：

- [ ] `GET /health` 返回 `ok`
- [ ] 读一次 `quant://docs/overview`
- [ ] `search_symbols` 能返回标的
- [ ] `query_kline` 能取到 K 线
- [ ] `list_plans(status="published")` 能列出 Plan
- [ ] 调一次写工具，确认返回 `PermissionError`（**验证只读姿态生效**）
- [ ] 需要写时，确认已拿到用户授权并开启对应开关

---

## 10. 相关文件

| 文件 | 用途 |
|---|---|
| `mcp_server/server.py` | 工具注册与参数注解（**工具契约的第一来源**） |
| `mcp_server/tools_impl.py` | 只读工具实现 |
| `mcp_server/mutations.py` | 受控写门面与错误契约 |
| `mcp_server/config.py` | 传输配置、鉴权与开关校验 |
| `manage.py mcp_smoke_test` | 端到端冒烟（真实 MCP 协议跑通全链路） |
| `README.md` §MCP 服务 | 启动、部署与安全边界 |
