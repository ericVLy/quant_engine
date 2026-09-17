"""PII 脱敏与日志卫生工具（N-05）。

目标（见 documents.md 5.1.2「敏感配置保护设计」）：
- 账户 ID / 联系方式 / 交易明细**不进日志与通知明文**；
- 通知邮件不夹带异常栈细节；
- 作为「防御性」兜底，日志处理器统一过一遍脱敏。

脱敏策略（保留可人工核对的少量特征位）：

| 类型 | 示例输入 | 输出 |
|------|----------|------|
| 邮箱 | `trader@example.com` | `tr***@example.com` |
| 手机号 | `13800138000` | `138****8000` |
| 账户/外部订单 ID（UUID 形态） | `efd94fdb-1234-...-5678` | `efd9****5678` |
| 长十六进制密钥 | `90a06e71d167d48c5471c9d56e781a69` | `90a0****（已脱敏）` |

> 说明：脱敏是「不回显明文」而非「不可逆加密」；系统配置层的密钥仍只经环境变量注入。
"""
import logging
import re

MASK = '****'

EMAIL_RE = re.compile(r'([A-Za-z0-9._%+-]{1,2})[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')
PHONE_RE = re.compile(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)')
UUID_RE = re.compile(
    r'\b([0-9a-fA-F]{4})[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{8}([0-9a-fA-F]{4})\b',
)
HEX_SECRET_RE = re.compile(r'\b([0-9a-fA-F]{4})[0-9a-fA-F]{20,}\b')
TRACEBACK_RE = re.compile(r'Traceback \(most recent call last\):.*', re.DOTALL)


def mask_email(value: str) -> str:
    """`trader@example.com` → `tr***@example.com`（保留域名便于定位来源）。"""
    return EMAIL_RE.sub(lambda m: f'{m.group(1)}{MASK}@{m.group(2)}', str(value or ''))


def mask_phone(value: str) -> str:
    """`13800138000` → `138****8000`。"""
    return PHONE_RE.sub(lambda m: f'{m.group(1)}{MASK}{m.group(2)}', str(value or ''))


def mask_account_id(value: str) -> str:
    """账户 ID / 外部订单 ID：UUID 形态保留前 4 后 4，其余形态保留前 4 后 2。"""
    text = str(value or '')
    if not text:
        return ''
    masked = UUID_RE.sub(lambda m: f'{m.group(1)}{MASK}{m.group(2)}', text)
    if masked != text:
        return masked
    if len(text) <= 6:
        return MASK
    return f'{text[:4]}{MASK}{text[-2:]}'


def strip_traceback(value: str) -> str:
    """折叠异常栈（通知邮件不夹带完整栈，避免泄露路径/配置细节）。"""
    text = str(value or '')
    if 'Traceback (most recent call last)' not in text:
        return text
    return TRACEBACK_RE.sub('[异常栈已折叠]', text)


def redact_text(value: str) -> str:
    """通用文本脱敏：邮箱 → 手机号 → UUID/长十六进制密钥 → 折叠异常栈。"""
    text = str(value or '')
    text = mask_email(text)
    text = mask_phone(text)
    text = UUID_RE.sub(lambda m: f'{m.group(1)}{MASK}{m.group(2)}', text)
    text = HEX_SECRET_RE.sub(lambda m: f'{m.group(1)}{MASK}（已脱敏）', text)
    return strip_traceback(text)


class RedactionLogFilter(logging.Filter):
    """logging 过滤器：对每条日志的最终消息统一过一遍脱敏（防御性兜底）。

    用法（settings.LOGGING）::

        "filters": {"redaction": {"()": "apps.execution.redaction.RedactionLogFilter"}},
        "handlers": {"file": {"filters": ["redaction"], ...}}

    设计要点：
    - 先把 %-style args 折叠进 message，再整体脱敏（args 里也可能带 PII）；
    - 脱敏过程异常时静默放行原始消息——过滤器失败不能阻塞日志输出。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.args:
                record.msg = record.getMessage()
                record.args = None
            record.msg = redact_text(record.msg)
        except Exception:
            pass
        return True