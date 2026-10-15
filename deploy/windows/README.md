# Windows 单机生产部署

本机（Windows）以 **production 模式**运行量化引擎，并在**用户登录时自动启动**。

## 架构与进程

| 进程 | 启动方式 | 职责 |
|---|---|---|
| **MariaDB 12.3** | Windows 服务（Automatic） | 主库 + K 线库 |
| **Web** | 计划任务 `QuantEngine-Web` → `start_web.cmd` → **waitress** | HTTP/WSGI；同时是分时采样子进程的**父进程** |
| **Scheduler** | 计划任务 `QuantEngine-Scheduler` → `start_scheduler.cmd` | 到点触发已发布 Plan → SuiteRun → 下单 |
| 分时采样 | 由 Web 进程自动拉起（`run_intraday_updater` 子进程） | 每 60s 采集分时数据 |
| **nginx**（可选） | 计划任务 `QuantEngine-Nginx` | 反向代理：静态资源直出 + `/api` 回源 + SSE 缓冲策略 |

## 把开发环境的标的同步到部署环境

开发机用 SQLite（`db.sqlite3`），部署机用 MariaDB，两边 `Symbol` 表需要人工对齐。
部署环境里标的为空时，Web 界面无可选标的，功能无法使用。

```powershell
# 先预演，看看会新增/更新/删除什么（只读，不停服）
.venv\Scripts\python.exe manage.py sync_symbols `
    --source "C:\Users\<你>\Documents\quant_platform\quant_engine\db.sqlite3" --dry-run

# 正式同步（自动停服 → 写入 → 恢复服务）
.venv\Scripts\python.exe manage.py sync_symbols `
    --source "C:\Users\<你>\Documents\quant_platform\quant_engine\db.sqlite3"
```

> 请在**部署环境**执行，命令以只读方式打开开发库的 SQLite 文件。
> 反过来（开发机直连部署的 MariaDB）需要额外注入部署库口令，风险高。

| 参数 | 说明 |
|---|---|
| `--source` | 开发环境 SQLite 路径（必填） |
| `--dry-run` | 只打印变更计划，不写库不停服 |
| `--prune` | 删除目标库中源库没有的标的（**会级联删快照**，慎用） |
| `--no-stop-services` | 同步期间不停服（自行保证无并发读写） |
| `--keep-backup` | 不在 `.backup/` 留存同步前快照 |

### 为什么要停服

`datasources.SymbolSnapshot` 以 `Symbol` 为主键且 `on_delete=CASCADE`，
分时采样与调度器也会并发读写标的。同步期间停服可避免读到中间状态。

命令通过 `services.ps1` 停/启服务，并且**只恢复同步前本来就在跑的服务**
——不会把用户故意停掉的服务重新拉起来。若无任何变更，命令会提前返回、
不停服（不该为no-op 打扰线上）。

若**探测失效**（脚本缺失、调用失败、或输出无法识别），就无法区分
"本来都没跑"和"没探测到"。此时取**保服务**的一侧：拉起全部并明确告警——
让线上停摆的后果远重于多启动一个服务。

> 探测只能按 **ASCII** 片段（如 `Running`）匹配，不能匹配中文：
> `services.ps1` 的输出是系统 ANSI（中文 Windows = GBK），而命令用
> `encoding='utf-8'` 解码，中文会变乱码。按中文 `任务=Running` 匹配**永远匹配不到**，
> 曾导致一次同步把服务停了却没拉起来。

### 安全边界

- 源库以 `mode=ro` 打开，**绝不会写开发库**（有测试守着）；
- 写库前把目标库现有标的导出到 `.backup/symbols_before_sync_<时间戳>.json`；
- 写入在 `transaction.atomic()` 内，出错回滚，目标库保持原样；
- 默认**只增改不删**，删除必须显式 `--prune`。

## 服务管理（启动 / 停止 / 重启 / 状态）

```powershell
# 重启全部（改了配置或重新部署后最常用的一条命令）
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\services.ps1 -Action restart

# 只重启某一项
... -Action restart -Service Web        # 也可以 Scheduler / Nginx
... -Action start  / -Action stop
... -Action status                      # 看任务状态、进程数、端口
```

| 参数 | 说明 |
|---|---|
| `-Action` | `start` / `stop` / `restart` / `status`，默认 `status` |
| `-Service` | `All`（默认）/ `Web` / `Scheduler` / `Nginx`；多项用逗号分隔 |
| `-WaitSeconds` | 启动后等待端口就绪的秒数，默认 30 |

不需要管理员权限。

> `-Service` 接收单个字符串、内部自行按逗号拆分。**不要**用
> `[string[]]` + `ValidateSet`：PowerShell 的 `-File` 调用方式不解析逗号，
> `powershell -File services.ps1 -Service Web,Scheduler` 会把整个
> `"Web,Scheduler"` 当成单个值而被 ValidateSet 拒绝
> （从 PowerShell 交互式执行却正常，极难察觉）。
> `sync_symbols` 正是通过 `-File` 调本脚本，这个坑导致过一次
> 「脚本报已恢复服务，实际服务没起来」。

### ⚠️ 重启为什么必须清理孤儿进程

`run_hidden.vbs` 用 `WshShell.Run` 启动子进程，而该方式创建的进程
**不在计划任务的作业对象（Job Object）内**——所以 `Stop-ScheduledTask`
只终止 `wscript.exe` 本身，真正的服务进程会活下来变成孤儿。

更麻烦的是**分时采样子进程 `run_intraday_updater`**：它由 Web 拉起，
继承了 waitress 的 stdout/stderr 重定向，也就是**继承了 `logs/web.log`
的文件句柄**。Web 被杀后它仍存活并继续持有该文件，于是下次
`start_web.cmd` 执行 `>> logs\web.log` 时报：

```
The process cannot access the file because it is being used by another process.
```

waitress 随即**秒退**，表现为「任务状态 Ready、端口未监听」，而
`start` 却报告成功——**重启看起来成功、实际服务没起来**。

因此 `services.ps1` 在停止与重启时，都会按命令行特征清理
`waitress` / `run_intraday_updater` / `run_scheduler` / `nginx`
的残留进程，并在重启前确认端口已真正释放。

> 顺带一提：曾想在 `start_web.cmd` 里探测日志是否被锁以自动降级，
> 但**实测 cmd 的 `>>` 重定向失败不会设置 `errorlevel`**，
> `if errorlevel 1` 检测不出文件锁（被独占锁定时仍判为可写）。
> 故放弃该方案，改为由 `services.ps1` 处理孤儿进程，
> 并在就绪失败时打印上面这段诊断。

## 隐藏自启窗口

登录自启时**不会弹出黑色控制台窗口**。做法是给三个计划任务统一套一层
`run_hidden.vbs`（WScript 壳）：

```
计划任务  ->wscript.exe run_hidden.vbs start_web.cmd
                └─ WshShell.Run "cmd.exe /c <脚本>", 0, True
                                                 ↑   ↑
                                          0=隐藏窗口  阻塞等待
```

**为什么必须阻塞等待（`True`）**：若用异步启动，WScript 立刻退出，
计划任务会认为「任务已完成」，状态变成 Ready 而非 Running，
任务设置里的**「失败后重启 3 次」就完全不会生效**。
阻塞等待才能让任务在整个服务运行期间保持「正在运行」。

相关文件：

| 文件 | 作用 |
|---|---|
| `run_hidden.vbs` | 隐藏窗口启动器（GBK + CRLF，VBScript 按 ANSI 读取） |
| `start_nginx.cmd` | nginx 启动脚本，由 `install_nginx.ps1` 生成（内容含本机 NginxDir，故不入库） |

脚本会**自检** `run_hidden.vbs` 是否存在：缺失时给出警告并回退为直接调
`cmd.exe`（会弹窗，但不至于注册出坏任务）。

### 调试提示

因为窗口是隐藏的，出问题时**看不到界面**。排查方式：

- 看日志：`logs\web.log`、`logs\scheduler.log`；
- 手工前台运行：`deploy\windows\start_web.cmd`（会显示控制台）；
- 查任务状态：
  ```powershell
  Get-ScheduledTask -TaskName 'QuantEngine*' | Get-ScheduledTaskInfo
  ```
  `LastTaskResult` 为 `0x41301`（267009）表示**正在运行**，属正常；`0` 表示已退出。

## 访问链路：两种模式

nginx 是**可选**的。生产构建的 `VITE_API_BASE_URL = '/api'` 是相对路径，
因此**不装 nginx 时 waitress 单端口也能同时供前端与 API**（实测 `/` 与
`/api/monitoring/intraday/realtime/` 同端口正常）。

```
模式 A（默认，无 nginx）        模式 B（装了 nginx）
浏览器 ──:8000/ ──> waitress    浏览器 ──:8080/ ──> nginx
                                       ├─ /assets/ 直出（不回源）
                                       └─ 其余 ──> waitress:8000
```

装 nginx 的收益：静态资源不经 Django、便于对外只暴露一个端口、
以及 **SSE 有独立的缓冲策略**（见下）。

### 安装 nginx（可选）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install_nginx.ps1
```

默认装到 `%LOCALAPPDATA%\nginx`，监听 **8080**（避开 80 这个需管理员的低端口），
反代到 `127.0.0.1:8000`。脚本会下载官方 Windows 版 → 渲染配置 → `nginx -t` 校验 →
注册计划任务 `QuantEngine-Nginx`。与 `install.ps1` **相互独立**，可单独执行。

> **为什么不用 `C:\Program Files`**：该目录需要管理员权限，普通账户会在
> 解压搬运时报 `Move-Item : Access to the path 'nginx' is denied`。
> 若你偏好在 `%Program Files%`，用管理员 PowerShell 跑，或显式指定：
> `-NginxDir "C:\Program Files\nginx"`。
> 其他可选位置：项目内 `deploy\windows\nginx`、任意用户可写目录。

### ⚠️ SSE 必须关闭代理缓冲

`/api/monitoring/intraday/stream/` 是 SSE 长连接。nginx 默认会缓冲上游响应，
把推送帧攒在缓冲区里，直到缓冲满或连接结束才吐出来——前端表现为
「推送已暂停」并自动降级为轮询，实时性丢失且**不报错**。

模板里已为该路径单开一个 `location`：

```nginx
location = /api/monitoring/intraday/stream/ {
    proxy_buffering  off;          # ← 关键
    proxy_cache      off;
    chunked_transfer_encoding on;
    proxy_read_timeout 3600s;
}
```

实测（真实 SSE 端点，首字节耗时）：**直连 812ms / 经 nginx 13ms**，
`Content-Type: text/event-stream` 且 `event: snapshot` 帧立即到达。

> 仓库内 `nginx/quant_engine_nginx.conf`（Docker/Linux 路径）**缺这段 SSE 配置**，
> 若将来用 Docker 部署分时监控，需要补上同样的 `proxy_buffering off`。

### 局域网访问要额外配两项

从域名或局域网 IP 访问时，浏览器发出的 `Origin` 与后端不同源，
POST 会被 CSRF 判为跨站而拒绝。改 `env.local.cmd`：

```bat
set ALLOWED_HOSTS=127.0.0.1,localhost,192.168.1.20
set CSRF_TRUSTED_ORIGINS=http://192.168.1.20:8080
```

`win_production.py` 还会信任 nginx 的 `X-Forwarded-*` 头
（`USE_X_FORWARDED_HOST` / `SECURE_PROXY_SSL_HEADER`），
使 Django 在反代后仍识别真实域名与协议。默认开启，可用 `TRUST_PROXY_HEADERS=0` 关闭。

> **为什么用子进程跑采样**：gm SDK 在终端"可连接但不响应"时会**持有 GIL 一直阻塞**，
> 同进程内的线程与 asyncio 循环全部停摆，`join(timeout)` / `wait_for` 的超时逻辑
> 根本执行不到 → 整个 Web 进程冻结（整站打不开）。详见 `documents.md` 模块9。

> **为什么调度器是独立进程**：`run_scheduler` 在 `MonitoringConfig.ready` 的跳过名单里，
> 不会重复拉起采样子进程；两个进程职责分离，任一崩溃不影响另一个。

## 为什么用 MariaDB 而不是 SQLite

`dev` 环境用 SQLite（见 `quant_engine/settings/local.py`），但 SQLite 是**库级单写者**：
`transaction_mode=IMMEDIATE` 下事务入口即抢占写锁，一次长事务会让**全站写请求阻塞到超时**。
生产改用 MariaDB 的 InnoDB 行锁，从根上消除这类问题。

本部署用 `quant_engine.settings.win_production`（新建），**只覆盖数据库等生产项**，
不改 `local.py` —— 因为 `dev.py` 和 `production.py` 都 `from .local import *`，
改 `local.py` 会把开发环境也一起切成 MariaDB。

## 安装

在仓库根目录执行（PowerShell）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install.ps1
```

脚本会依次：

1. 自检虚拟环境与 MariaDB 服务（并在需要时启动它）；
2. **交互式询问数据库口令**，生成 `deploy\windows\env.local.cmd`
   （含口令，已加入 `.gitignore`；口令不写入任何脚本或文档）；
3. `pip install waitress`（Gunicorn 不支持 Windows）；
4. 建库（若不存在）+ `migrate` + `collectstatic`；
5. 注册开机自启计划任务。

可选参数：

| 参数 | 说明 |
|---|---|
| `-Trigger OnStart` | 改为**系统启动**时以 SYSTEM 身份运行（未登录也起）。**需要管理员权限**；且 gm 终端是用户会话里的桌面应用，SYSTEM 身份通常访问不到 |
| `-Trigger OnLogon` | **默认**。用户登录即启动，gm 终端可正常访问 |
| `-RunLevel Limited` | **默认**。普通账户即可注册，**无需管理员** |
| `-RunLevel Highest` | 以最高权限运行，**需管理员账户**（本项目服务本身不需要） |
| `-Port 8000` | Web 端口 |
| `-Host_ 0.0.0.0` | 监听所有网卡（局域网访问）；`ALLOWED_HOSTS` 同步放宽 |
| `-SkipMigrate` | 只重装服务，跳过数据库迁移 |
| `-SkipStatic` | 跳过 `collectstatic`（默认会执行，见下） |
| `-SkipFrontend` | 跳过前端构建（默认会执行，见下） |
| `-SyncDeps` | 按 `requirements.txt` **全量同步**依赖（默认只补装 waitress） |
| `-NoUv` | 强制用 pip，不用 uv（见下节） |

### ⚠️ 部署前必须先构建前端（`static/` 不在版本控制里）

`static/` 与 `staticfiles/` **都被 `.gitignore` 排除**（都是构建产物）。
全新克隆的仓库里**没有任何前端文件**，直接启动会白屏。

所以 `install.ps1` 的第 5 步会：

1. 检查 `npm` 是否可用；
2. `node_modules` 缺失时先 `npm ci`（失败回退 `npm install`）；
3. `npx vite build`（只跑 vite，跳过 `vue-tsc` 类型检查——部署不应因类型报错中断）；
4. **校验产物用的是绝对路径**。

### ⚠️ 前端资源必须是绝对路径（history 路由的硬约束）

本项目前端路由是 **history 模式**（`createWebHistory`），地址形如 `/plans/123`。
若 `vite.config.ts` 的 `base` 是相对路径 `'./'`，`index.html` 里会写成：

```html
<script src="./assets/index-xxx.js">
```

浏览器按**当前 URL** 解析，在 `/plans/123` 页面上会解析成
`/plans/assets/index-xxx.js`。该路径落进后端的 catch-all 路由
（`re_path(r'^(?!api/|admin/|assets/).*$', serve_frontend_index)`），
返回的是 **index.html**：

| 请求 | Content-Type | 实际内容 |
|---|---|---|
| `/assets/index-xxx.js` | `text/javascript` | 真 JS ✓ |
| `/plans/assets/index-xxx.js` | **`text/html`** | index.html ✗ |

`<script>` 拿到 HTML 解析失败 → **白屏**，而且**不报 404**（状态码是 200），
排查起来极其困难。

正确做法：`base: '/'` → 引用变为 `/assets/index-xxx.js`，任意深度都解析正确，
且与 `STATIC_URL`（`'assets/'`，Django 会自动补前导斜杠）及
collectstatic 产物（落在 `staticfiles/` 根）完全一致。

`install.ps1` 会在构建后做这项校验，发现 `./assets/` 直接**中止安装**并提示。

> 排查这类问题的小技巧：直接看响应头。
> 请求 JS 却拿到 `Content-Type: text/html`，就是被 SPA catch-all 吞了。

### ⚠️ 原生命令的 stderr 不是错误

PowerShell 会把子进程的 stderr 行包装成 `NativeCommandError`，而
`nginx` / `vite` / `npm` / `uv` 这类工具**把正常的进度与警告也写到 stderr**。
脚本顶部设了 `$ErrorActionPreference = 'Stop'`，于是"成功消息"被当成致命错误
**直接终止脚本**——即使命令本身成功。

实测踩过三次：

| 命令 | 走 stderr 的内容 | 实际结果 |
|---|---|---|
| `nginx -t` | `syntax is ok` | 成功却被中断 |
| `vite build` | `(!) Some chunks are larger than 500 kB` | **rc=0 成功却被中断** |
| `uv pip install` | 进度信息 | 同上风险 |

因此所有原生命令都经 `Invoke-Native` 调用：临时把 `ErrorActionPreference`
放宽为 `Continue`，**按 `$LASTEXITCODE` 判断成败**，finally 中恢复。

> 若你看到 `node.exe : [plugin builtin:vite-reporter]` 或
> `nginx.exe : nginx: ... syntax is ok` 后脚本直接停住，
> 就是这个原因——不是构建/校验失败。

### collectstatic 是独立步骤，且失败即中止

`collectstatic` 把前端构建产物（`static/assets`）与 Django admin 样式
收集到 `staticfiles/`（即 `STATIC_ROOT`），nginx 的 `/assets/` 直接从这里直出。

两条设计取舍：

1. **独立于 migrate**：修计划任务时用 `-SkipMigrate` 重跑时，
   不应连带把静态资源也跳过（前端与 admin 样式都依赖它）。
   故单独提供 `-SkipStatic`；
2. **失败即中止，不只是警告**：静态资源缺失会让前端白屏、admin 无样式。
   安装脚本会打印实际收集到的文件数，便于确认：

```
=== 5/7 收集静态资源（collectstatic） ===
  [OK] collectstatic 完成（staticfiles 下共 213 个文件）
```

> 校验方法：`staticfiles` 下只有 1 个文件就说明 collectstatic 没生效
> （正常应有两三百个）。手工执行：
> `.venv\Scripts\python.exe manage.py collectstatic --noinput`

### 权限说明（重要）

注册计划任务时若报 **`Access is denied`（HRESULT 0x80070005）**，原因是：

- `-RunLevel Highest`，或
- `-Trigger OnStart`（以 SYSTEM 运行）

两者**都需要管理员权限**。本项目服务（读自己的代码目录、连本机 MariaDB、写自己的日志）
**不需要任何管理员权限**，所以默认是 `-RunLevel Limited -Trigger OnLogon`，普通账户即可完成安装。

若确实要用最高权限或开机（未登录）启动，请**以管理员身份重开 PowerShell** 后重跑：

```
# 管理员 PowerShell
.\deploy\windows\install.ps1 -Trigger OnStart -RunLevel Highest
```

脚本会在注册前做提权自检：权限不足时直接给出可执行的提示并退出，而不是抛一个天书般的 CIM 异常。

> 实测：普通账户下 `RunLevel=Limited` + `OnLogon` 注册**成功**，
> 且 `Start-ScheduledTask` 能正常拉起 waitress（`LastTaskResult 0x41301`
> 表示 SCHED_S_TASK_RUNNING，长驻任务的正常状态）。


## uv 兼容

安装脚本**优先使用 [uv](https://docs.astral.sh/uv/)**，找不到时自动回退 pip。

本项目的 `.venv` 本身就是 uv 创建的（`.venv/pyvenv.cfg` 含 `uv = <版本>`），
所以 uv 是"原生"工具链。已实测：

- `uv pip compile requirements.txt` 能正确解析 **`requirements.txt`** ——
  该文件是 **UTF-16LE + BOM** 编码（pip 可识别，**uv 同样可识别**，实测解析出 262 行依赖）；
- `uv pip install --python .venv\Scripts\python.exe <包>` 可精确装进本项目虚拟环境；
- `uv` 会自动发现仓库根的 `.venv`（`uv python find` 指向 `.venv\Scripts\python.exe`）。

安装 uv（若尚未安装）：

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

常用等价命令：

| 目的 | uv | pip |
|---|---|---|
| 装单个包 | `uv pip install waitress --python .venv\Scripts\python.exe` | `.venv\Scripts\python.exe -m pip install waitress` |
| 按 requirements 同步 | `uv pip install -r requirements.txt --python .venv\Scripts\python.exe` | `.venv\Scripts\python.exe -m pip install -r requirements.txt` |
| 看已装包 | `uv pip list --python .venv\Scripts\python.exe` | `.venv\Scripts\python.exe -m pip list` |
| 解析依赖（不安装） | `uv pip compile requirements.txt` | — |
| 找解释器 | `uv python find` | — |

> 本项目**没有 `pyproject.toml` / `uv.lock`**，依赖仍以 `requirements.txt` 为唯一来源，
> 因此用的是 `uv pip`（pip 兼容层）而非 `uv sync`（项目模式）。
> 若将来要引入 `uv lock` 锁定传递依赖版本，需要先补 `pyproject.toml`，
> 属于独立的一次变更。


## 启动与状态

日常操作建议用 `services.ps1`（见文首「服务管理」）。下面是等价的底层命令：

```powershell
# 立即启动（不必等下次登录）
Start-ScheduledTask -TaskName 'QuantEngine-Web'
Start-ScheduledTask -TaskName 'QuantEngine-Scheduler'

# 查看状态
Get-ScheduledTask -TaskName 'QuantEngine*' | Get-ScheduledTaskInfo

# 访问
Start-Process http://127.0.0.1:8000/
```

> 直接 `Stop-ScheduledTask` 是**不完整**的——它只杀 `wscript.exe`，
> 服务进程与分时采样子进程会残留并继续持有 `logs\web.log`。
> 需要停止时请用 `services.ps1 -Action stop`。

首次访问会自动跳转 `/setup` 引导页，创建超级管理员。
完成后 `env.local.cmd` 里 `SETUP_ENABLED=0` 保持不变，即使清空用户表也不会重开引导。

日志落在仓库的 `logs/` 目录：`web.log`、`scheduler.log`（固定文件名，追加写入）。

## 卸载

```powershell
# 只移除自启任务（不动数据库、不删配置）
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1

# 连带删除含口令的 env.local.cmd
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1 -PurgeEnv
```

## 手工调整配置

编辑 `deploy\windows\env.local.cmd` 后**需重启对应服务**才生效：

| 变量 | 说明 |
|---|---|
| `QUANT_PORT` / `QUANT_HOST` | Web 监听地址与端口 |
| `QUANT_THREADS` | waitress 线程数（默认 8） |
| `QUANT_SCHED_INTERVAL` / `QUANT_SCHED_WORKERS` | 调度器轮询间隔与 worker 数 |
| `MONITORING_UPDATER_ENABLED` / `_INTERVAL` | 分时采样开关与间隔（秒） |
| `GM_SERV_ADDR` | 远程 gm 终端地址，如 `192.168.1.10:7001`（留空=本机终端） |
| `SETUP_ENABLED` | 初始化引导开关 |

## 服务启动与 uv 的关系

启动脚本一律直接调用 **`.venv\Scripts\python.exe`**（waitress / `manage.py`），
**不经过 uv**。原因：

- 计划任务需要调起一个**长驻前台进程**，而 `uv run` 会额外做一次依赖解析与
  环境准备，对常驻服务是纯粹的启动开销；
- 直接用 venv 里的解释器，行为与 `python manage.py ...` 完全一致，排障时最直观；
- uv 只在**安装/同步依赖**环节发挥作用（安装脚本会自动选择 uv 或 pip）。

若想手工前台调试，同样建议直接用 venv 解释器：

```
.venv\Scripts\python.exe manage.py runserver
.venv\Scripts\python.exe -m waitress --listen=127.0.0.1:8000 quant_engine.wsgi:application
```

## 注意事项

- **调度器只能有一个实例**：多开会重复触发 Plan（单机部署约束）。
- **默认不需要管理员权限**：`-RunLevel Limited -Trigger OnLogon` 普通账户即可注册自启。
  只有 `-RunLevel Highest` 或 `-Trigger OnStart` 才需要管理员。
- **不要**手工再跑 `manage.py run_intraday_updater`——采样已由 Web 进程自动拉起，重复启动会重复采样。
- **Gunicorn 多 worker 去重**：`waitress` 是单进程，没有多 worker 问题；
  若将来改用 Gunicorn `--workers N`，每个 worker 都会拉一个采样子进程（已登记为 P2 后续任务）。
- `SECRET_KEY` 固定在 `quant_engine/settings/local.py`（该文件已 gitignore）。
  **不要**删掉它，否则每次重启都要重新登录。
- **编码约定（均为实测踩坑，修改时请保持）**：
  - `.cmd` 用 **GBK + CRLF** —— cmd 的多行续行 `^` 依赖 CRLF，纯 LF 会把
    `--listen=127.0.0.1:8000` 拆成两条命令而报"不是内部或外部命令"；
  - `env.local.cmd` 由 `install.ps1` 以 **GBK + CRLF** 写出 —— 两者缺一不可：
    here-string 内部换行是 LF，直接 `Set-Content` 会写出 LF-only 的 `.cmd`，
    而 cmd 对 LF-only 批处理解析不可靠（`start_web.cmd` 就栽在这里）；
  - **nginx 配置用 UTF-8 无 BOM** —— 这是**反方向**的坑：PowerShell 5.1 的
    `Set-Content -Encoding UTF8` 会**写入 BOM**，nginx 解析到会报
    `unknown directive "锘?"`（同一个 BOM 在不同控制台代码页下显示不同）。
    故 `install_nginx.ps1` 改用
    `[System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))`。
