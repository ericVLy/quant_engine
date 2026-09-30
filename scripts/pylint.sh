#!/usr/bin/env bash
# 静态检查入口。
#
# 用法：
#   scripts/pylint.sh                      # 检查全部 apps/runner/mcp_server/quant_engine
#   scripts/pylint.sh runner/engine.py     # 检查指定文件
#
# 为什么需要这个脚本：
#   * pylint-django 是必需插件，缺失会让全仓 Model.objects 误报 E1101；
#   * jobs 固定为 1（astroid 多进程会偶发 F0002 崩溃），并清理 astroid 缓存。
#
# 规则豁免策略：只对部分文件不合适的规则，一律写在该文件顶部的
# `# pylint: disable=...` 注释里并注明理由；pylint.conf 的 disable 只放全局不合适的。
set -euo pipefail

# astroid 4.x 的已知缺陷：复用 ~/.cache/pylint 的旧 astroid 缓存时可能抛
# F0002（AstroidBuildingError: dictionary changed size during iteration）。
# 该错误与代码无关，清缓存后复跑即可。--jobs=1 只能规避多进程崩溃，
# 缓存问题需显式清理。
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/pylint" 2>/dev/null || true

cd "$(dirname "$0")/.."
PY=.venv/bin/python
export DJANGO_SETTINGS_MODULE=quant_engine.settings.test
# 静态检查会加载 Django AppConfig；不关掉分时更新器会真的发起外部请求
# 并写入数据库（pylint 输出里会混入 [monitoring-updater] 日志）。
export MONITORING_UPDATER_ENABLED=0
export FUNDAMENTALS_ENABLED=0

mode="${1:-all}"
case "$mode" in
  all)
    exec $PY -m pylint --rcfile=pylint.conf apps runner mcp_server quant_engine
    ;;
  *)
    exec $PY -m pylint --rcfile=pylint.conf "$@"
    ;;
esac
