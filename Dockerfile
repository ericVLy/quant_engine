# Quant Engine 生产镜像（模块化构建）
# - 阶段1：Node 构建前端（quant-frontend → vite 构建产物输出到 ../static/）
# - 阶段2：Python 运行时（python:3.12-slim Debian；gm/akshare 依赖 manylinux 轮子，
#   不能用 Alpine/musl）+ nginx 反向代理（nginx/quant_engine_nginx.conf → Gunicorn :8000）
#   + MCP SSE 服务（manage.py run_mcp_server，默认 :8765，仅容器内；写开关默认关闭）
#
# 运行：docker run -p 8080:8080 quant-engine
# MCP 需从容器外接入时：docker run -p 8080:8080 -p 8765:8765 \
#   -e MCP_HOST=0.0.0.0 -e MCP_AUTH_TOKEN=<token> quant-engine
# 默认 USE_SQLITE=1（SQLite 演练形态）；生产请覆盖 DB_*/KLINE_DB_*/GM_TOKEN 等环境变量
# 并置 USE_SQLITE=0，见 quant_engine/settings/production.py。

# ---------------------------------------------------------------- 阶段1：前端
FROM node:22-slim AS frontend-builder

WORKDIR /build/quant-frontend

# 先拷贝依赖清单，利用层缓存
COPY quant-frontend/package.json quant-frontend/package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com && npm install

COPY quant-frontend/ ./
# npm run build = vue-tsc -b && vite build；vite outDir=../static（见 vite.config.ts）
RUN npm run build

# ------------------------------------------------------------- 阶段2：运行时
FROM python:3.12-slim

# 系统依赖：nginx 反代；libpq 不需要（psycopg[binary] 轮子自带）
RUN apt-get update && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行用户
RUN groupadd -g 1000 quant && useradd -m -u 1000 -g quant -s /bin/sh quant

# 端口约定：nginx 对外 8080，反代 Gunicorn 8000
EXPOSE 8080

ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DJANGO_SETTINGS_MODULE=quant_engine.settings.production \
    # 单容器演练默认 SQLite（production.py 的回退开关）；生产挂真实 DB_* 时置 0
    USE_SQLITE=1 \
    # ---- MCP 服务（模块11，随容器常驻，SSE :8765）----
    # 绑定回环地址仅容器内可访问；需从容器外接入时：
    #   MCP_HOST=0.0.0.0 且必须同时提供 MCP_AUTH_TOKEN（非回环绑定强制令牌，缺失 fail-fast）
    MCP_HOST=127.0.0.1 \
    MCP_PORT=8765 \
    MCP_AUTH_TOKEN= \
    # 写开关（默认关闭，只读）：trigger=触发 Plan 执行（仅创建 pending SuiteRun）；
    # mutate=创建/编辑/删除 Case/Suite/Plan（仅 draft 配置）。两者相互独立。
    MCP_ALLOW_TRIGGER=0 \
    MCP_ALLOW_MUTATE=0

# Python 依赖（requirements.txt 为 UTF-16 BOM 编码，pip 可识别；gunicorn 不在清单内，单独安装）
COPY requirements.txt /tmp/requirements.txt
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ \
    && pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.txt \
    && pip install --no-cache-dir --root-user-action=ignore "gunicorn"

WORKDIR /home/quant/quant

# 先放前端构建产物（nginx /static/ alias 指向这里；.dockerignore 已排除仓库内 static/）
COPY --from=frontend-builder --chown=quant:quant /build/static/ ./static/

# 源码
COPY --chown=quant:quant . .

# nginx 运行目录准备：
# - nginx.conf 内 pid/log 为绝对路径 /usr/local/nginx/logs/...，需预建并赋权
# - 相对 include（mime.types / quant_engine_nginx.conf）经 -p 前缀解析到项目 nginx/ 目录
RUN mkdir -p /usr/local/nginx/logs /var/log/nginx \
    && chown -R quant:quant /usr/local/nginx /var/log/nginx /home/quant/quant /var/lib/nginx

USER quant

# 收集 admin/DRF 静态文件到 STATIC_ROOT（staticfiles/），供 nginx /assets/ 服务
RUN python manage.py collectstatic --noinput

# MCP 服务对外端口（SSE，MCP_PORT，默认 8765）
EXPOSE 8765

# 校验 nginx 配置（-p 前缀使相对 include 解析到 ./nginx/）
RUN nginx -t -p /home/quant/quant/nginx/ -c /home/quant/quant/nginx/nginx.conf

# 启动：nginx(:8080) → Gunicorn(:8000, quant_engine.wsgi)；MCP SSE 服务(:8765)后台常驻
# 仅自动执行 migrate（K 线分表按项目设计由运行时 ensure_kline_table 动态创建）；
# makemigrations 不应在运行时执行。
# MCP 配置经环境变量传入（MCP_HOST/MCP_PORT/MCP_AUTH_TOKEN/MCP_ALLOW_TRIGGER/MCP_ALLOW_MUTATE），
# run_mcp_server 进程不启动分时更新器（模块11 进程门禁），分时采样由本容器的 Django 服务承担。
CMD set -xe; \
    nginx -p /home/quant/quant/nginx/ -c /home/quant/quant/nginx/nginx.conf; \
    python manage.py migrate --noinput; \
    python manage.py run_mcp_server & \
    gunicorn quant_engine.wsgi:application --bind 0.0.0.0:8000 --workers "${GUNICORN_WORKERS:-4}"

