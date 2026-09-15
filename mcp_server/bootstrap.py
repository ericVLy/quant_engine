"""Initialize Django before MCP tools touch the ORM."""
import os


def setup_django() -> None:
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'quant_engine.settings.dev')
    # MCP 进程是 AI 助手的接入层，不承担分时数据更新职责：
    # 若沿用默认值，每个客户端进程都会拉起分时内部更新器（周期外部数据源请求 + 库写入）。
    # 需要时可在启动前显式设置 MONITORING_UPDATER_ENABLED=1 覆盖。
    os.environ.setdefault('MONITORING_UPDATER_ENABLED', '0')
    import django

    django.setup()
