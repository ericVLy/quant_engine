"""受控写操作门面（MCP-20）：Plan / Suite / Case 的创建、编辑、删除。

默认关闭：需环境变量 ``MCP_ALLOW_MUTATE=1``（或启动时 ``--allow-mutate``）才放行；
与执行开关 ``MCP_ALLOW_TRIGGER``（`trigger_plan_execution`）相互独立，
开启一个不会连带开启另一个。

设计约束（只做门面，不新增 REST 接口、不复制业务逻辑）：

- **创建/编辑**复用 ``apps.*.serializers``：``Case.params`` 白名单与深层语义校验、
  ``Plan.symbol_scope`` / ``retry_policy`` / cron / 账户资金占用校验、
  ``Suite`` 的 ``case_ids`` 与 ``allocated_capital`` 都在同一处执行；
- **删除**复用 ``apps.*.services`` 的删除保护（被引用 / 有执行记录 → 409 语义冲突）；
- **编排边**复用 ``apps.suites.services.update_topology``（含 ``event_condition`` 白名单、
  DAG 无环校验、事务内整体替换）；
- 不提供 publish / start / stop：发布与运行仍由 REST 或前端完成（避免 MCP 一步进入可执行态）。

错误契约：校验失败抛 ``ValueError``（可定位字段名）、
被引用/有执行记录抛 :class:`MutationConflictError`（REST 语义 409）、
写开关未开启抛 ``PermissionError``。
"""
from __future__ import annotations

import os
from typing import Any

from mcp_server.formatting import to_jsonable

MUTATE_FLAG_ENV = 'MCP_ALLOW_MUTATE'
_TRUTHY = frozenset({'1', 'true', 'yes'})

__all__ = [
    'MutationConflictError',
    'mutate_enabled',
    'ensure_mutate_enabled',
    'create_case',
    'update_case',
    'delete_case',
    'create_suite',
    'update_suite',
    'update_suite_topology',
    'delete_suite',
    'create_plan',
    'update_plan',
    'delete_plan',
]


class MutationConflictError(RuntimeError):
    """资源被引用或存在执行记录，变更/删除被拒绝（REST 语义 409 Conflict）。"""


def mutate_enabled() -> bool:
    """当前进程是否允许 MCP 写操作（读环境变量 ``MCP_ALLOW_MUTATE``）。

    Returns:
        bool: ``1`` / ``true`` / ``yes`` 视为开启，其余（含未设置）为关闭。
    """
    return os.getenv(MUTATE_FLAG_ENV, '').strip().lower() in _TRUTHY


def ensure_mutate_enabled() -> None:
    """确认写开关已开启，否则拒绝本次调用。

    Raises:
        PermissionError: 写开关未开启（默认状态）。
    """
    if not mutate_enabled():
        raise PermissionError(
            'MCP 写操作（创建/编辑/删除 Case/Suite/Plan）已禁用。'
            '设置环境变量 MCP_ALLOW_MUTATE=1，或启动 MCP 服务时加 --allow-mutate 后重试。'
        )


def _flatten_errors(detail: Any, prefix: str = '') -> str:
    """把 DRF 的嵌套错误结构压成 ``字段: 说明；...`` 的单行文本。

    Args:
        detail: ``serializer.errors`` 或其中任意一层节点。
        prefix: 已累积的字段路径（如 ``symbol_scope.type``）。

    Returns:
        str: 可直接展示给客户端的可定位错误说明。
    """
    if isinstance(detail, dict):
        parts = [
            _flatten_errors(value, f'{prefix}.{key}' if prefix else str(key))
            for key, value in detail.items()
        ]
        return '；'.join(part for part in parts if part)
    if isinstance(detail, (list, tuple)):
        parts = [_flatten_errors(item, prefix) for item in detail]
        return '；'.join(part for part in parts if part)
    return f'{prefix}: {detail}' if prefix else str(detail)


def _save(serializer: Any) -> Any:
    """校验并保存 serializer。

    Args:
        serializer: 已绑定 ``data``（或 ``instance + data``）的 DRF serializer。

    Returns:
        Any: ``serializer.save()`` 的结果（模型实例）。

    Raises:
        ValueError: 任一字段/对象级校验失败，消息含字段路径。
    """
    if not serializer.is_valid():
        raise ValueError(_flatten_errors(serializer.errors))
    return serializer.save()


def _get_or_missing(model: Any, pk: int, label: str) -> Any:
    """按主键取资源，不存在时抛可定位 ``ValueError``。

    Args:
        model: Django 模型类（``Case`` / ``Suite`` / ``Plan``）。
        pk: 资源主键。
        label: 资源中文名（用于错误消息，如 ``Case``）。

    Returns:
        Any: 模型实例。

    Raises:
        ValueError: 资源不存在。
    """
    try:
        return model.objects.get(pk=pk)
    except model.DoesNotExist:
        raise ValueError(f'{label} {pk} 不存在') from None


def _conflict_guard(exc_types: tuple[type[Exception], ...], func, *args, **kwargs) -> Any:
    """把共享删除保护的领域异常统一包装为 :class:`MutationConflictError`（409 语义）。

    Args:
        exc_types: 需要映射为 409 的异常类型（``CaseError`` / ``SuiteError`` / ``PlanError``）。
        func: 实际执行删除的服务函数。
        *args / **kwargs: 原样透传给 ``func``。

    Returns:
        Any: ``func`` 的返回值。

    Raises:
        MutationConflictError: 资源被引用或存在执行记录，删除被拒绝。
    """
    try:
        return func(*args, **kwargs)
    except exc_types as exc:
        raise MutationConflictError(str(exc)) from exc


def _present(**kwargs: dict) -> dict:
    """过滤出调用方显式提供的字段（``None`` 视为未提供，不进入 partial 更新）。

    Args:
        kwargs: 门面函数收到的可选字段。

    Returns:
        dict: 仅含显式提供的字段的载荷，交给 serializer ``partial=True`` 更新。
    """
    return {key: value for key, value in kwargs.items() if value is not None}


# ---------------------------------------------------------------------------
# Case
# ---------------------------------------------------------------------------

def create_case(name: str, node_type: str, params: dict | None = None) -> dict:
    """创建 Case（draft 状态，复用 REST 同款 :class:`CaseSerializer` 校验）。

    Args:
        name: Case 名称，长度 1~100。
        node_type: 节点类型：signal（信号）/ filter（过滤）/ verdict（裁决）/ executor（执行器）。
        params: 参数对象，须满足 ``Case.params`` 白名单（trigger/period/order/result 等）；
            trigger.event_type 必须已注册；留空创建空参数草稿。

    Returns:
        dict: 新建 Case 的完整字段（id、name、node_type、params、version=1、status=draft 等）。

    Raises:
        PermissionError: 写开关未开启。
        ValueError: 名称/节点类型/params 校验失败（消息含字段路径）。
    """
    from apps.cases.models import Case
    from apps.cases.serializers import CaseSerializer

    ensure_mutate_enabled()
    serializer = CaseSerializer(
        data={'name': name, 'node_type': node_type, 'params': params or {}}
    )
    instance = _save(serializer)
    return to_jsonable(serializer.data)


def update_case(
    case_id: int,
    name: str | None = None,
    node_type: str | None = None,
    params: dict | None = None,
) -> dict:
    """编辑 Case（仅 draft 语义的普通字段；发布走 REST ``/publish/``）。

    Args:
        case_id: 目标 Case 主键。
        name: 新名称；未提供保持不变。
        node_type: 新节点类型（signal/filter/verdict/executor）；未提供保持不变。
        params: 新参数对象（整体替换，须满足白名单）；未提供保持不变。

    Returns:
        dict: 更新后 Case 的完整字段。

    Raises:
        PermissionError: 写开关未开启。
        ValueError: Case 不存在或字段校验失败。
    """
    from apps.cases.models import Case
    from apps.cases.serializers import CaseSerializer

    ensure_mutate_enabled()
    case = _get_or_missing(Case, case_id, 'Case')
    payload = _present(name=name, node_type=node_type, params=params)
    serializer = CaseSerializer(instance=case, data=payload, partial=True)
    _save(serializer)
    return to_jsonable(serializer.data)


def delete_case(case_id: int) -> dict:
    """删除 Case；被 Suite 引用时拒绝（409 语义）。

    Args:
        case_id: 目标 Case 主键。

    Returns:
        dict: ``{'deleted': 'case', 'id': <case_id>}``。

    Raises:
        PermissionError: 写开关未开启。
        ValueError: Case 不存在。
        MutationConflictError: Case 已被 Suite 引用。
    """
    from apps.cases.models import Case
    from apps.cases.services import CaseError, delete_case as _delete_case

    ensure_mutate_enabled()
    case = _get_or_missing(Case, case_id, 'Case')
    _conflict_guard((CaseError,), _delete_case, case)
    return {'deleted': 'case', 'id': case_id}


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

def create_suite(
    name: str,
    aggregate_method: str = 'weighted_sum',
    parent_id: int | None = None,
    case_ids: list[int] | None = None,
    allocated_capital: float | str | None = None,
) -> dict:
    """创建 Suite（draft 状态；复用 REST 同款 :class:`SuiteSerializer`）。

    Args:
        name: Suite 名称，长度 1~100。
        aggregate_method: 节点内 Case 聚合方式：weighted_sum（加权求和，默认）/
            vote（投票）/ and（逻辑与）/ or（逻辑或）。
        parent_id: 父 Suite 主键（构成子 Suite 树）；留空=根 Suite。
        case_ids: 挂载的 Case 主键数组；留空=先建空 Suite（拓扑后续经 update_suite_topology）。
        allocated_capital: 占用资金（大于 0 的数值，字符串数字亦可）；留空=不占用。

    Returns:
        dict: 新建 Suite 的完整字段（id、cases、version=1、status=draft 等）。

    Raises:
        PermissionError: 写开关未开启。
        ValueError: 名称/聚合方式/parent/case_ids/资金校验失败。
    """
    from apps.suites.models import Suite
    from apps.suites.serializers import SuiteSerializer

    ensure_mutate_enabled()
    payload: dict = {'name': name, 'aggregate_method': aggregate_method}
    if parent_id is not None:
        payload['parent'] = parent_id
    if case_ids is not None:
        payload['case_ids'] = case_ids
    if allocated_capital is not None:
        payload['allocated_capital'] = allocated_capital
    serializer = SuiteSerializer(data=payload)
    _save(serializer)
    return to_jsonable(serializer.data)


def update_suite(
    suite_id: int,
    name: str | None = None,
    aggregate_method: str | None = None,
    parent_id: int | None = None,
    case_ids: list[int] | None = None,
    allocated_capital: float | str | None = None,
) -> dict:
    """编辑 Suite 基本字段（拓扑整体替换请用 update_suite_topology）。

    Args:
        suite_id: 目标 Suite 主键。
        name: 新名称；未提供保持不变。
        aggregate_method: 新聚合方式（weighted_sum/vote/and/or）；未提供保持不变。
        parent_id: 新父 Suite 主键；未提供保持不变。
        case_ids: 新 Case 主键数组（整体替换挂载关系）；未提供保持不变。
        allocated_capital: 新占用资金；未提供保持不变。

    Returns:
        dict: 更新后 Suite 的完整字段。

    Raises:
        PermissionError: 写开关未开启。
        ValueError: Suite 不存在或字段校验失败。
    """
    from apps.suites.models import Suite
    from apps.suites.serializers import SuiteSerializer

    ensure_mutate_enabled()
    suite = _get_or_missing(Suite, suite_id, 'Suite')
    payload = _present(
        name=name,
        aggregate_method=aggregate_method,
        parent=parent_id,
        case_ids=case_ids,
        allocated_capital=allocated_capital,
    )
    serializer = SuiteSerializer(instance=suite, data=payload, partial=True)
    _save(serializer)
    return to_jsonable(serializer.data)


def update_suite_topology(
    suite_id: int,
    case_ids: list[int],
    edges: list[dict],
) -> dict:
    """整体替换 Suite 的 Case 挂载与出边（事务内，含 DAG 与事件条件校验）。

    Args:
        suite_id: 目标 Suite 主键（只能是该 Suite 的出边，目标不能指向自身）。
        case_ids: 全量 Case 主键数组（整体替换；空数组=清空挂载）。
        edges: 全量出边数组，每项 ``{from_suite, to_suite, condition, event_condition, weight}``；
            ``event_condition`` 须满足白名单（event_type 必填 + 可选 op/field/threshold 操作符契约）；
            空数组=清空出边。

    Returns:
        dict: ``{'topology_updated': <suite_id>}``（节点详情可再调 get_suite_topology 校验）。

    Raises:
        PermissionError: 写开关未开启。
        ValueError: Suite 不存在、case_ids/edges 非数组或含非法值。
        MutationConflictError: DAG 校验失败 / 引用不存在的 Suite / 非法 event_condition
            （由 ``SuiteError`` 映射，消息可定位）。
    """
    from apps.suites.models import Suite
    from apps.suites.services import SuiteError, update_topology

    ensure_mutate_enabled()
    if not isinstance(case_ids, list) or not isinstance(edges, list):
        raise ValueError('case_ids 与 edges 必须是数组')
    suite = _get_or_missing(Suite, suite_id, 'Suite')
    _conflict_guard((SuiteError,), update_topology, suite, case_ids, edges)
    return {'topology_updated': suite_id}


def delete_suite(suite_id: int) -> dict:
    """删除 Suite；被 Plan 引用时拒绝（409 语义）。

    Args:
        suite_id: 目标 Suite 主键。

    Returns:
        dict: ``{'deleted': 'suite', 'id': <suite_id>}``。

    Raises:
        PermissionError: 写开关未开启。
        ValueError: Suite 不存在。
        MutationConflictError: Suite 已被 Plan 引用。
    """
    from apps.suites.models import Suite
    from apps.suites.services import SuiteError, delete_suite as _delete_suite

    ensure_mutate_enabled()
    suite = _get_or_missing(Suite, suite_id, 'Suite')
    _conflict_guard((SuiteError,), _delete_suite, suite)
    return {'deleted': 'suite', 'id': suite_id}


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def create_plan(
    name: str,
    root_suite_id: int,
    trigger_type: str = 'manual',
    cron_expr: str | None = None,
    event_type: str | None = None,
    symbol_scope: dict | None = None,
    exec_mode: str = 'serial',
    retry_policy: dict | None = None,
    account_id: str = '',
    allocated_capital: float | str | None = None,
    suite_start_mode: str = 'manual',
) -> dict:
    """创建 Plan（draft 状态；复用 REST 同款 :class:`PlanSerializer` 全部校验）。

    Args:
        name: Plan 名称，长度 1~100。
        root_suite_id: 根 Suite 主键（创建阶段不要求已发布；发布时才校验）。
        trigger_type: 触发方式：time（时间驱动，需 cron_expr）/ event（事件驱动，
            需已注册 event_type）/ manual（手动触发，默认）。
        cron_expr: 5 字段 cron 表达式（如 ``0 9 30 * *``）；time 触发时必填。
        event_type: 触发事件类型；event 触发时必填且必须在 EventRegistry 注册。
        symbol_scope: 标的范围，白名单 ``{type, group_ids, symbol_codes}``；
            type=all 只含 type；groups 需 group_ids 整数数组；symbols 需 symbol_codes 字符串数组；
            缺省 ``{'type': 'all'}``。
        exec_mode: 执行模式：serial（串行，默认）/ parallel（并行）/ fail_stop（失败停止）。
        retry_policy: 重试策略 ``{max_retries: >=0 整数, delay_seconds: >=0 数值}``。
        account_id: 交易账户 ID（64 字符内）；提供 allocated_capital 时建议同时提供。
        allocated_capital: 占用资金（大于 0）；与 account_id 一起触发账户空闲资金校验。
        suite_start_mode: Suite 启动模式：manual（默认）/ auto。

    Returns:
        dict: 新建 Plan 的完整字段（id、version=1、status=draft、available_capital 等）。

    Raises:
        PermissionError: 写开关未开启。
        ValueError: 任一字段/对象级校验失败（含 cron、事件注册、symbol_scope 白名单、资金校验）。
    """
    from apps.plans.models import Plan
    from apps.plans.serializers import PlanSerializer

    ensure_mutate_enabled()
    payload: dict = {
        'name': name,
        'root_suite': root_suite_id,
        'trigger_type': trigger_type,
        'exec_mode': exec_mode,
        'suite_start_mode': suite_start_mode,
        'symbol_scope': symbol_scope if symbol_scope is not None else {'type': 'all'},
        'account_id': account_id or '',
    }
    if cron_expr is not None:
        payload['cron_expr'] = cron_expr
    if event_type is not None:
        payload['event_type'] = event_type
    if retry_policy is not None:
        payload['retry_policy'] = retry_policy
    if allocated_capital is not None:
        payload['allocated_capital'] = allocated_capital
    serializer = PlanSerializer(data=payload)
    _save(serializer)
    return to_jsonable(serializer.data)


def update_plan(
    plan_id: int,
    name: str | None = None,
    root_suite_id: int | None = None,
    trigger_type: str | None = None,
    cron_expr: str | None = None,
    event_type: str | None = None,
    symbol_scope: dict | None = None,
    exec_mode: str | None = None,
    retry_policy: dict | None = None,
    account_id: str | None = None,
    allocated_capital: float | str | None = None,
    suite_start_mode: str | None = None,
) -> dict:
    """编辑 Plan 配置字段（发布/回滚/启停走 REST 动作接口，不在 MCP 范围）。

    Args:
        plan_id: 目标 Plan 主键。
        name: 新名称（长度 1~100）；未提供保持不变。
        root_suite_id: 新根 Suite 主键；未提供保持不变。
        trigger_type: 新触发方式（time/event/manual）；未提供保持不变。
        cron_expr: 新 5 字段 cron 表达式；未提供保持不变。
        event_type: 新触发事件类型（须已注册）；未提供保持不变。
        symbol_scope: 新标的范围（白名单同 create_plan）；未提供保持不变。
        exec_mode: 新执行模式（serial/parallel/fail_stop）；未提供保持不变。
        retry_policy: 新重试策略 {max_retries, delay_seconds}；未提供保持不变。
        account_id: 新交易账户 ID；未提供保持不变。
        allocated_capital: 新占用资金（大于 0）；未提供保持不变。
        suite_start_mode: 新 Suite 启动模式（manual/auto）；未提供保持不变。

    Returns:
        dict: 更新后 Plan 的完整字段。

    Raises:
        PermissionError: 写开关未开启。
        ValueError: Plan 不存在或字段校验失败（与 REST 400 同源）。
    """
    from apps.plans.models import Plan
    from apps.plans.serializers import PlanSerializer

    ensure_mutate_enabled()
    plan = _get_or_missing(Plan, plan_id, 'Plan')
    payload = _present(
        name=name,
        root_suite=root_suite_id,
        trigger_type=trigger_type,
        cron_expr=cron_expr,
        event_type=event_type,
        symbol_scope=symbol_scope,
        exec_mode=exec_mode,
        retry_policy=retry_policy,
        account_id=account_id,
        allocated_capital=allocated_capital,
        suite_start_mode=suite_start_mode,
    )
    serializer = PlanSerializer(instance=plan, data=payload, partial=True)
    _save(serializer)
    return to_jsonable(serializer.data)


def delete_plan(plan_id: int) -> dict:
    """删除 Plan；已有执行记录时拒绝（409 语义）。

    Args:
        plan_id: 目标 Plan 主键。

    Returns:
        dict: ``{'deleted': 'plan', 'id': <plan_id>}``。

    Raises:
        PermissionError: 写开关未开启。
        ValueError: Plan 不存在。
        MutationConflictError: Plan 已有执行记录（SuiteRun）。
    """
    from apps.plans.models import Plan
    from apps.plans.services import PlanError, delete_plan as _delete_plan

    ensure_mutate_enabled()
    plan = _get_or_missing(Plan, plan_id, 'Plan')
    _conflict_guard((PlanError,), _delete_plan, plan)
    return {'deleted': 'plan', 'id': plan_id}
