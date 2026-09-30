# pylint: disable=import-outside-toplevel  # 延迟导入以规避循环依赖/加载期副作用
class PlanRegistry:
    """In-process published Plan cache used by long-running workers.

    设计目标（R-09 热加载）：

    - ``refresh`` 由 ``apps.plans.services.publish_plan`` 在提交后调用，实现
      Plan 发布/版本变更后内存配置自动刷新；
    - ``snapshot`` 固化 Plan 的可执行配置（root_suite、exec_mode、retry 等），
      Scheduler / Worker 优先读取快照，避免每次查询数据库；
    - Scheduler 每次轮询把命中 Cron 的已发布 Plan 同步回注册中心，保证
      进程冷启动后也能「自愈」地加载最新公开发布配置。
    """

    _plans = {}

    _executable_keys = (
        'root_suite_id', 'trigger_type', 'cron_expr', 'event_type',
        'exec_mode', 'retry_policy', 'status', 'version',
        # Plan 级风控限额：直接影响下单前拦截，必须随注册中心一起热加载，
        # 否则改了限额仍按旧值执行（内存缓存会长期滞留）
        'risk_position_mode', 'risk_max_order_volume', 'risk_max_order_value',
        'risk_max_daily_value', 'risk_max_account_value',
        'risk_max_position_value', 'risk_max_position_volume', 'risk_allowed_sessions',
    )

    @classmethod
    def _needs_refresh(cls, plan, existing):
        """缓存是否过期：首次、版本变化，或任一可执行字段被改动。

        仅比较 ``version`` 是不够的：已发布 Plan 可以不经发布直接编辑
        （REST / MCP 的 update_plan 不改 version），若只按版本判断，
        改动后的 ``cron_expr`` 会被长期缓存，
        调度器就会按旧配置触发（队列真正被执行后这是实害）。
        """
        if existing is None:
            return True
        if existing.get('version') != plan.version:
            return True
        snapshot = {key: getattr(plan, key, None) for key in cls._executable_keys}
        return existing.get('snapshot') != snapshot

    @classmethod
    def refresh(cls, plan, force=False):
        """缓存（或在版本/可执行配置变化时刷新）一个已发布 Plan 及其可执行快照。"""
        existing = cls._plans.get(plan.pk)
        if not force and not cls._needs_refresh(plan, existing):
            return plan
        snapshot = {key: getattr(plan, key, None) for key in cls._executable_keys}
        cls._plans[plan.pk] = {'version': plan.version, 'plan': plan, 'snapshot': snapshot}
        return plan

    @classmethod
    def get(cls, plan_id):
        return cls._plans.get(plan_id)

    @classmethod
    def get_snapshot(cls, plan_id):
        entry = cls._plans.get(plan_id)
        return (entry or {}).get('snapshot')

    @classmethod
    def published_plans(cls):
        """返回按注册中心缓存的已发布 Plan 实例（模拟 session）。"""
        cls.sync_from_database()
        for entry in cls._plans.values():
            plan = entry.get('plan')
            if plan is not None:
                yield plan

    @classmethod
    def sync_from_database(cls):
        """同步已发布 Plan，清理已下线配置并刷新版本变化。"""
        from apps.plans.models import Plan

        published = list(Plan.objects.filter(status='published'))
        published_ids = {plan.pk for plan in published}
        for plan_id in set(cls._plans) - published_ids:
            cls.remove(plan_id)
        for plan in published:
            if cls._needs_refresh(plan, cls._plans.get(plan.pk)):
                cls.refresh(plan, force=True)
        return len(published)

    @classmethod
    def remove(cls, plan_id):
        cls._plans.pop(plan_id, None)
