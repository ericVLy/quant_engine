"""
告警服务模块：提供应用内通知和邮件通知功能
"""
import logging
from typing import Optional, Dict, Any, List
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from .models import Alert, AlertChannel

logger = logging.getLogger(__name__)


class AlertSeverity:
    """告警严重程度常量"""
    LOW = 'low'
    MEDIUM = 'medium'
    HIGH = 'high'
    CRITICAL = 'critical'


class AlertType:
    """告警类型常量"""
    ORDER_FAILED = 'order_failed'
    SUITE_FAILED = 'suite_failed'
    PLAN_FAILED = 'plan_failed'
    RISK_VIOLATION = 'risk_violation'
    SYSTEM_ERROR = 'system_error'


class AlertService:
    """告警服务类：统一管理告警创建和发送"""
    
    def __init__(self):
        self.channels_cache = None
        self._load_channels()
    
    def _load_channels(self):
        """加载启用的告警渠道配置"""
        try:
            self.channels_cache = AlertChannel.objects.filter(is_enabled=True)
        except Exception as e:
            logger.error(f"加载告警渠道配置失败: {e}")
            self.channels_cache = []
    
    def reload_channels(self):
        """重新加载告警渠道配置"""
        self._load_channels()
    
    def create_alert(
        self,
        alert_type: str,
        title: str,
        message: str,
        severity: str = AlertSeverity.MEDIUM,
        plan=None,
        suite_run=None,
        order=None,
        error_code: Optional[str] = None,
        send_notifications: bool = True
    ) -> Alert:
        """
        创建告警记录
        
        Args:
            alert_type: 告警类型
            title: 告警标题
            message: 告警详细消息
            severity: 严重程度 (low/medium/high/critical)
            plan: 关联的Plan实例
            suite_run: 关联的SuiteRun实例
            order: 关联的Order实例
            error_code: 错误代码
            send_notifications: 是否立即发送通知
            
        Returns:
            Alert: 创建的告警实例
        """
        try:
            alert = Alert.objects.create(
                alert_type=alert_type,
                severity=severity,
                title=title,
                message=message,
                error_code=error_code,
                plan=plan,
                suite_run=suite_run,
                order=order
            )
            
            logger.info(f"创建告警成功: [{severity.upper()}] {title}")
            
            # 立即发送通知
            if send_notifications:
                self.send_alert_notifications(alert)
            
            return alert
            
        except Exception as e:
            logger.error(f"创建告警失败: {e}")
            raise
    
    def send_alert_notifications(self, alert: Alert):
        """
        发送告警通知到所有启用的渠道
        
        Args:
            alert: 告警实例
        """
        if not self.channels_cache:
            logger.warning("没有启用的告警渠道")
            return
        
        for channel in self.channels_cache:
            try:
                if channel.should_send_alert(alert):
                    if channel.channel_type == 'in_app':
                        self._send_in_app_notification(alert, channel)
                    elif channel.channel_type == 'email':
                        print("!!!!!!发送邮件通知!!!!!!!!!!!")
                        self._send_email_notification(alert, channel)
            except Exception as e:
                logger.error(f"发送告警通知失败 (渠道: {channel.channel_type}): {e}")
                # 记录通知错误
                alert.notification_error += f"{channel.channel_type}: {str(e)}\n"
                alert.save(update_fields=['notification_error'])
    
    def _send_in_app_notification(self, alert: Alert, channel: AlertChannel):
        """
        发送应用内通知
        
        Args:
            alert: 告警实例
            channel: 告警渠道配置
        """
        try:
            # 标记应用内通知已发送
            alert.in_app_notified = True
            alert.save(update_fields=['in_app_notified'])
            
            # 这里可以扩展为 WebSocket 推送或存储到应用内通知表
            # 当前版本主要通过数据库查询实现应用内通知
            logger.info(f"应用内通知已发送: {alert.title}")
            
        except Exception as e:
            logger.error(f"发送应用内通知失败: {e}")
            raise
    
    def _send_email_notification(self, alert: Alert, channel: AlertChannel):
        """
        发送邮件通知
        
        Args:
            alert: 告警实例
            channel: 告警渠道配置
        """
        try:
            # 检查是否有邮件收件人
            recipients = channel.email_recipients
            if not recipients:
                logger.warning("邮件渠道未配置收件人，跳过发送")
                return
            
            # 检查邮件配置
            if not hasattr(settings, 'EMAIL_HOST') or not settings.EMAIL_HOST:
                logger.warning("邮件服务器未配置，跳过发送邮件通知")
                print("!!!!!!邮件服务器未配置，跳过发送邮件通知!!!!!!!!!!!")
                return
            
            # 构建邮件主题
            subject = f"{channel.email_subject_prefix} [{alert.severity.upper()}] {alert.title}"
            logger.info(f"准备发送邮件: 主题={subject}, 收件人={recipients}")
            
            # 构建邮件内容
            context = {
                'alert': alert,
                'severity_display': alert.get_severity_display(),
                'alert_type_display': alert.get_alert_type_display(),
                'created_at': alert.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'base_url': getattr(settings, 'BASE_URL', 'http://localhost:8000'),
            }
            
            # 尝试渲染HTML邮件模板
            try:
                html_message = render_to_string('emails/alert_notification.html', context)
                logger.info("HTML邮件模板渲染成功")
            except Exception as e:
                logger.warning(f"渲染HTML邮件模板失败，使用纯文本格式: {e}")
                html_message = None
            
            # 构建纯文本内容（备用）
            text_message = f"""
告警类型: {alert.get_alert_type_display()}
严重程度: {alert.get_severity_display()}
标题: {alert.title}
时间: {alert.created_at.strftime('%Y-%m-%d %H:%M:%S')}
错误代码: {alert.error_code or '无'}
详细消息:
{alert.message}
"""
            # 关联信息
            if alert.plan:
                text_message += f"\n关联计划: {alert.plan.name}"
            if alert.suite_run:
                text_message += f"\n关联执行: {alert.suite_run}"
            if alert.order:
                text_message += f"\n关联订单: {alert.order}"
            
            text_message += f"\n\n请登录系统查看详情: {context['base_url']}/admin/execution/alert/{alert.id}/"
            
            # 发送邮件
            logger.info("开始发送邮件...")
            # print("!!!!!!开始发送邮件!!!!!!!!!!!")
            send_mail(
                subject=subject,
                message=text_message,
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@quantplatform.com'),
                recipient_list=recipients,
                html_message=html_message,
                fail_silently=False
            )
            logger.info("邮件发送成功")
            # print("!!!!!!邮件发送成功!!!!!!!!!!!")
            # 标记邮件通知已发送
            alert.email_notified = True
            alert.save(update_fields=['email_notified'])
            logger.info(f"邮件通知标记已设置: email_notified={alert.email_notified}")
            
            logger.info(f"邮件通知已发送: {alert.title} -> {recipients}")
            
        except Exception as e:
            logger.error(f"发送邮件通知失败: {e}")
            raise
    
    def create_order_failed_alert(
        self,
        order,
        error_message: str,
        error_code: Optional[str] = None,
        severity: str = AlertSeverity.HIGH
    ) -> Alert:
        """
        创建订单失败告警
        
        Args:
            order: 失败的订单实例
            error_message: 错误消息
            error_code: 错误代码
            severity: 严重程度
            
        Returns:
            Alert: 创建的告警实例
        """
        title = f"订单执行失败: {order.symbol} {order.direction} {order.volume}股"
        message = f"""
订单信息:
- 标的代码: {order.symbol}
- 交易方向: {order.get_direction_display()}
- 委托数量: {order.volume}
- 委托价格: {order.price}
- 当前状态: {order.get_status_display()}

错误详情:
{error_message}

订单ID: {order.id}
创建时间: {order.created_at.strftime('%Y-%m-%d %H:%M:%S')}
"""
        return self.create_alert(
            alert_type=AlertType.ORDER_FAILED,
            title=title,
            message=message,
            severity=severity,
            plan=order.log.plan if order.log else None,
            order=order,
            error_code=error_code
        )
    
    def create_suite_failed_alert(
        self,
        suite_run,
        error_message: str,
        error_code: Optional[str] = None,
        severity: str = AlertSeverity.HIGH
    ) -> Alert:
        """
        创建策略执行失败告警
        
        Args:
            suite_run: 失败的策略执行实例
            error_message: 错误消息
            error_code: 错误代码
            severity: 严重程度
            
        Returns:
            Alert: 创建的告警实例
        """
        title = f"策略执行失败: {suite_run.suite.name if suite_run.suite else '未知策略'} @ {suite_run.symbol}"
        message = f"""
策略执行信息:
- 策略名称: {suite_run.suite.name if suite_run.suite else '未知'}
- 标的代码: {suite_run.symbol}
- 执行状态: {suite_run.get_status_display()}
- 开始时间: {suite_run.started_at.strftime('%Y-%m-%d %H:%M:%S') if suite_run.started_at else '未开始'}
- 结束时间: {suite_run.ended_at.strftime('%Y-%m-%d %H:%M:%S') if suite_run.ended_at else '未结束'}

错误详情:
{error_message}

执行ID: {suite_run.id}
创建时间: {suite_run.created_at.strftime('%Y-%m-%d %H:%M:%S')}
"""
        return self.create_alert(
            alert_type=AlertType.SUITE_FAILED,
            title=title,
            message=message,
            severity=severity,
            plan=suite_run.plan,
            suite_run=suite_run,
            error_code=error_code
        )
    
    def create_risk_violation_alert(
        self,
        plan,
        violation_message: str,
        violation_type: str = "风险控制",
        severity: str = AlertSeverity.CRITICAL
    ) -> Alert:
        """
        创建风控违规告警
        
        Args:
            plan: 违规的计划实例
            violation_message: 违规消息
            violation_type: 违规类型
            severity: 严重程度
            
        Returns:
            Alert: 创建的告警实例
        """
        title = f"风控违规: {plan.name}"
        message = f"""
计划信息:
- 计划名称: {plan.name}
- 标的范围: {plan.symbol_scope}
- 执行模式: {plan.get_exec_mode_display()}

违规详情:
- 违规类型: {violation_type}
- 违规消息: {violation_message}

计划ID: {plan.id}
创建时间: {plan.created_at.strftime('%Y-%m-%d %H:%M:%S')}
"""
        return self.create_alert(
            alert_type=AlertType.RISK_VIOLATION,
            title=title,
            message=message,
            severity=severity,
            plan=plan
        )
    
    def create_system_error_alert(
        self,
        error_message: str,
        error_code: Optional[str] = None,
        severity: str = AlertSeverity.MEDIUM
    ) -> Alert:
        """
        创建系统错误告警
        
        Args:
            error_message: 错误消息
            error_code: 错误代码
            severity: 严重程度
            
        Returns:
            Alert: 创建的告警实例
        """
        title = f"系统错误: {error_code or '未知错误'}"
        message = f"""
错误详情:
{error_message}

错误代码: {error_code or '无'}
发生时间: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        return self.create_alert(
            alert_type=AlertType.SYSTEM_ERROR,
            title=title,
            message=message,
            severity=severity,
            error_code=error_code
        )


# 全局告警服务实例
alert_service = AlertService()