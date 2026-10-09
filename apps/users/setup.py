"""部署初始化（首次部署引导）服务。

职责边界：判断「是否仍需引导」与「原子地完成引导并创建首个超级管理员」。

安全设计（该端点无需认证即可创建超级管理员，必须严密）
------------------------------------------------------
1. **一次性开关**：是否已初始化由 ``SetupState.completed`` 决定，而非「用户数是否为 0」。
   否则管理员清空账号后引导会重新打开，等于留下一个可被任意触达者抢占的超级管理员后门。
2. **默认关闭风险最高的一环**：``SETUP_ENABLED=0`` 可强制关闭引导端点（生产加固用）。
3. **并发安全**：两个请求同时通过时，用 ``select_for_update`` 锁住单行状态 + 事务，
   后到者会看到已完成并被拒绝，不会产生两个超级管理员。
4. **不在日志/响应中回显密码**：仅记录完成时间与用户名。
"""
import logging

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone

from .models import SetupState, User

logger = logging.getLogger(__name__)

ADMIN_ROLE = 'admin'


def setup_enabled() -> bool:
    """环境级总开关：``SETUP_ENABLED=0`` 可彻底关闭引导端点。"""
    return bool(getattr(settings, 'SETUP_ENABLED', True))


def is_setup_required() -> bool:
    """是否仍需要引导。

    判定为「需要」需同时满足：总开关打开、状态行未标记完成、且**当前没有任何用户**
    （已有用户的库不应再暴露引导，即使状态行缺失也一律视为已初始化）。
    """
    if not setup_enabled():
        return False
    state = SetupState.objects.filter(pk=SetupState.SINGLETON_PK).first()
    if state and state.completed:
        return False
    # 已有任何账号 → 视为已初始化，绝不因状态行缺失而重新开放（后门防护）
    return not User.objects.exists()


def _lock_state():
    """取单行状态并加行锁（必须在事务内调用）。

    ``select_for_update`` 在 SQLite 上退化为库级写锁，但配合
    ``transaction_mode=IMMEDIATE``（见 ``db_tuning``）仍能保证同一时刻只有一个写入者。
    迁移已预建 pk=1 的行，故正常路径只SELECT+UPDATE，不与并发请求争抢INSERT；
    ``get_or_create`` 仅为兜底（例如手工删库重建后）。
    """
    SetupState.objects.get_or_create(pk=SetupState.SINGLETON_PK)
    return SetupState.objects.select_for_update().get(pk=SetupState.SINGLETON_PK)


class SetupAlreadyCompleted(Exception):
    """引导已完成（或已存在用户），拒绝再次初始化。"""


@transaction.atomic
def complete_setup(*, username: str, password: str, email: str = '', phone: str = '',
                   company: str = '', first_name: str = '', last_name: str = ''):
    """创建首个超级管理员并永久关闭引导。

    参数为keyword-only：调用方只能按名传参，避免误把密码传到 email 之类的位置。
    抛出 :class:`SetupAlreadyCompleted` 表示引导已关闭（调用方转 403）。
    整个过程在一个事务内：要么「用户创建 + 状态标记完成」同时生效，要么都不生效。
    """
    if not setup_enabled():
        raise SetupAlreadyCompleted()

    state = _lock_state()
    # 双保险：事务内重新判定，挡住并发第二个请求
    if state.completed or User.objects.exists():
        raise SetupAlreadyCompleted()

    user = User.objects.create_superuser(
        username=username,
        password=password,
        email=email or '',
        phone=phone or '',
        company=company or '',
        first_name=first_name or '',
        last_name=last_name or '',
    )
    # 既有权限判定同时看 groups('admin') 与 is_staff（见 UserRoleView），两者都给全
    admin_group, _ = Group.objects.get_or_create(name=ADMIN_ROLE)
    user.groups.add(admin_group)
    user.is_staff = True
    user.save(update_fields=['is_staff'])

    state.completed = True
    state.completed_at = timezone.now()
    state.completed_by = username
    state.initialized_marker = 'bootstrap'
    state.save(update_fields=['completed', 'completed_at', 'completed_by', 'initialized_marker'])

    logger.info('部署初始化完成，已创建超级管理员并永久关闭引导：%s', username)
    return user
