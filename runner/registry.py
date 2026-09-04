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
        'symbol_scope', 'exec_mode', 'retry_policy', 'status', 'version',
    )

    @classmethod
    def refresh(cls, plan, force=False):
        """缓存（或在版本变化时刷新）一个已发布 Plan 及其可执行快照。"""
        if not force:
            existing = cls._plans.get(plan.pk)
            if existing and existing.get('version') == plan.version:
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
        from apps.plans.models import Plan
        for entry in cls._plans.values():
            plan = entry.get('plan')
            if plan is not None and getattr(plan, 'status', None) == 'published':
                yield plan
        # 进程冷启动时回退到数据库，并顺带把缺失实例刷新进缓存（自愈加载）
        refreshed = False
        for plan in Plan.objects.filter(status='published'):
            existing = cls._plans.get(plan.pk)
            if existing is None:
                cls.refresh(plan)
                refreshed = True
                yield plan
            elif plan.version != existing.get('version'):
                cls.refresh(plan)
                yield plan

    @classmethod
    def remove(cls, plan_id):
        cls._plans.pop(plan_id, None)