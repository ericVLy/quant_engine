class PlanRegistry:
    """In-process published Plan cache used by long-running workers."""

    _plans = {}

    @classmethod
    def refresh(cls, plan):
        cls._plans[plan.pk] = {
            'version': plan.version,
            'plan': plan,
        }
        return plan

    @classmethod
    def get(cls, plan_id):
        return cls._plans.get(plan_id)

    @classmethod
    def remove(cls, plan_id):
        cls._plans.pop(plan_id, None)