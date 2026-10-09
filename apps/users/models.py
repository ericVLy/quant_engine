from django.db import models
from django.contrib.auth.models import AbstractUser

class User(AbstractUser):
    phone = models.CharField(max_length=20, blank=True)
    company = models.CharField(max_length=100, blank=True)

    class Meta:
        db_table = 'auth_user'
        verbose_name = '用户'
        verbose_name_plural = '用户'

    def __str__(self):
        return self.username


class SetupState(models.Model):
    """部署初始化状态（全局单行，pk 恒为 1）。

    **为什么不以「用户数是否为 0」判断是否需要引导**：那会让引导在管理员误删全部账号后
    重新打开，任何能触达该端点的人都能重新抢占超级管理员。本表提供**持久化的一次性开关**：
    引导成功后写入 ``completed=True``，此后永久关闭，即使清空所有用户也不重开。
    """

    SINGLETON_PK = 1

    # 主键固定为 1（非自增）：全局单行，语义稳定；迁移已预建该行
    id = models.PositiveSmallIntegerField(primary_key=True, default=SINGLETON_PK,
                                          verbose_name='ID')
    completed = models.BooleanField(default=False, verbose_name='已完成初始化')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    completed_by = models.CharField(max_length=150, blank=True, verbose_name='初始化管理员')
    # 环境/部署标识，仅用于审计排查，不参与判定逻辑
    initialized_marker = models.CharField(max_length=64, blank=True, verbose_name='初始化标记')

    class Meta:
        db_table = 'users_setup_state'
        verbose_name = '部署初始化状态'
        verbose_name_plural = '部署初始化状态'

    def __str__(self):
        return '已完成' if self.completed else '未初始化'
