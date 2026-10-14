# Windows 单机生产部署

本机（Windows）以 **production 模式**运行量化引擎，并在**用户登录时自动启动**。

## 架构与进程

| 进程 | 启动方式 | 职责 |
|---|---|---|
| **MariaDB 12.3** | Windows 服务（Automatic） | 主库 + K 线库 |
| **Web** | 计划任务 `QuantEngine-Web` → `start_web.cmd` → **waitress** | HTTP/WSGI；同时是分时采样子进程的**父进程** |
| **Scheduler** | 计划任务 `QuantEngine-Scheduler` → `start_scheduler.cmd` | 到点触发已发布 Plan → SuiteRun → 下单 |
| 分时采样 | 由 Web 进程自动拉起（`run_intraday_updater` 子进程） | 每 60s 采集分时数据 |

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
| `-Trigger OnStart` | 改为**系统启动**时以 SYSTEM 身份运行（未登录也起）。**注意**：gm 终端是用户会话里的桌面应用，SYSTEM 身份通常访问不到 |
| `-Trigger OnLogon` | **默认**。用户登录即启动，gm 终端可正常访问 |
| `-Port 8000` | Web 端口 |
| `-Host_ 0.0.0.0` | 监听所有网卡（局域网访问）；`ALLOWED_HOSTS` 同步放宽 |
| `-SkipMigrate` | 只重装服务，跳过数据库迁移 |

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

## 注意事项

- **调度器只能有一个实例**：多开会重复触发 Plan（单机部署约束）。
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
