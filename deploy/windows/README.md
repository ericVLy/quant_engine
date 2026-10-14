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

默认装到 `%ProgramFiles%\nginx`，监听 **8080**（避开 80 这个需管理员的低端口），
反代到 `127.0.0.1:8000`。脚本会下载官方 Windows 版 → 渲染配置 → `nginx -t` 校验 →
注册计划任务 `QuantEngine-Nginx`。与 `install.ps1` **相互独立**，可单独执行。

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
| `-SyncDeps` | 按 `requirements.txt` **全量同步**依赖（默认只补装 waitress） |
| `-NoUv` | 强制用 pip，不用 uv（见下节） |

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

```powershell
# 立即启动（不必等下次登录）
Start-ScheduledTask -TaskName 'QuantEngine-Web'
Start-ScheduledTask -TaskName 'QuantEngine-Scheduler'

# 查看状态
Get-ScheduledTask -TaskName 'QuantEngine*' | Get-ScheduledTaskInfo

# 访问
Start-Process http://127.0.0.1:8000/
```

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
  - `.ps1` 用 **UTF-8 + BOM** —— PowerShell 5.1 没有 BOM 会按 ANSI(GBK) 读，
    中文注释变乱码并直接导致**语法解析失败**；
  - `env.local.cmd` 由 `install.ps1` 以系统 ANSI（GBK）写出。
