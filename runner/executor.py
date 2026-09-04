from dataclasses import dataclass, field


@dataclass
class CaseResult:
    direction: int = 0
    payload: dict = field(default_factory=dict)
    order: dict | None = None
    status: str = 'success'


class CaseExecutionError(Exception):
    """Raised when a Case result cannot be interpreted."""


class CaseExecutor:
    """Execute the declarative result of a published Case."""

    def __init__(self, calculator=None):
        self.calculator = calculator

    def execute(self, case, context):
        params = case.params or {}
        if params.get('skip'):
            return CaseResult(status='skipped')
        if self.calculator:
            result = self.calculator(case, context)
        elif params.get('calculation') or params.get('indicator') or params.get('verdict'):
            from .factors import calculate
            result = calculate(params, context)
        else:
            result = params.get('result', {})
        if not isinstance(result, dict):
            raise CaseExecutionError('Case result 必须是 JSON 对象')
        direction = result.get('direction', params.get('direction', context.get('direction', 0)))
        try:
            direction = int(direction)
        except (TypeError, ValueError) as exc:
            raise CaseExecutionError('direction 必须是 -1、0 或 1') from exc
        if direction not in (-1, 0, 1):
            raise CaseExecutionError('direction 必须是 -1、0 或 1')
        payload = dict(result.get('payload', {}))
        payload.update({'direction': direction, 'case_id': case.pk})
        order = result.get('order', params.get('order'))
        if order is not None and not isinstance(order, dict):
            raise CaseExecutionError('order 必须是 JSON 对象')
        return CaseResult(direction=direction, payload=payload, order=order)