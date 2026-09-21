"""MCP 服务传输配置（SSE 为主，stdio 可选）。

Web 项目接入方式：MCP 以 HTTP/SSE 形式提供服务，客户端通过 URL 连接；
stdio 仅保留给本机 IDE 类客户端以子进程方式接入。

环境变量（均可被命令行参数覆盖）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MCP_TRANSPORT` | `sse` | `sse`（HTTP）或 `stdio`（子进程） |
| `MCP_HOST` | `127.0.0.1` | 监听地址；非回环地址必须配 `MCP_AUTH_TOKEN` |
| `MCP_PORT` | `8765` | 监听端口 |
| `MCP_SSE_PATH` | `/sse` | SSE 建连端点 |
| `MCP_MESSAGE_PATH` | `/messages/` | 客户端消息回传端点 |
| `MCP_AUTH_TOKEN` | 空（不鉴权） | Bearer 令牌；非回环绑定时必填 |
| `MCP_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*,[::1]:*` | DNS rebinding 保护：允许的 Host |
| `MCP_ALLOWED_ORIGINS` | `http://127.0.0.1:*,http://localhost:*` | DNS rebinding 保护：允许的 Origin |
| `MCP_CORS_ORIGINS` | 空（不加 CORS 头） | 浏览器直连时允许的来源（逗号分隔） |
| `MCP_ALLOW_TRIGGER` | 空（写操作关闭） | 置 `1`/`true`/`yes` 才允许 `trigger_plan_execution`；**命令行 `--allow-trigger` 等效** |
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace

DEFAULT_TRANSPORT = 'sse'   # MCP_TRANSPORT：默认 HTTP/SSE（Web 接入）
DEFAULT_HOST = '127.0.0.1'  # MCP_HOST：默认仅本机可访问
DEFAULT_PORT = 8765         # MCP_PORT：默认监听端口
DEFAULT_SSE_PATH = '/sse'   # MCP_SSE_PATH：默认 SSE 建连端点
DEFAULT_MESSAGE_PATH = '/messages/'  # MCP_MESSAGE_PATH：默认消息回传端点
DEFAULT_ALLOWED_HOSTS = ('127.0.0.1:*', 'localhost:*', '[::1]:*')       # MCP_ALLOWED_HOSTS
DEFAULT_ALLOWED_ORIGINS = ('http://127.0.0.1:*', 'http://localhost:*')  # MCP_ALLOWED_ORIGINS
DEFAULT_HEALTH_PATH = '/health'  # 健康检查端点（固定，不可配置）
LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1', '[::1]'})  # 视为回环的 host 取值
VALID_TRANSPORTS = ('sse', 'stdio')  # 合法传输方式
_TRUTHY = frozenset({'1', 'true', 'yes'})  # 布尔环境变量的真值写法


def _parse_bool(raw: str | None) -> bool:
    """把环境变量文本解析为布尔值（仅 ``1`` / ``true`` / ``yes`` 为真）。

    Args:
        raw: 环境变量原始值，可为 ``None``。

    Returns:
        bool: 是否视为开启。
    """
    return str(raw or '').strip().lower() in _TRUTHY


class McpConfigError(ValueError):
    """MCP 服务配置非法（端口、传输类型、鉴权缺失等）。"""


def _split_csv(raw: str | None) -> list[str]:
    """把逗号分隔的环境变量拆成去空白的字符串列表。

    Args:
        raw: 形如 ``a,b , c`` 的原始文本，可为 ``None``。

    Returns:
        list[str]: 去掉空项后的列表。
    """
    return [item.strip() for item in str(raw or '').split(',') if item.strip()]


def _normalize_path(raw: str, env_name: str) -> str:
    """规范化端点路径：保证以 ``/`` 开头。

    Args:
        raw: 环境变量原始值。
        env_name: 环境变量名，用于生成可定位的错误信息。

    Returns:
        str: 以 ``/`` 开头的路径。

    Raises:
        McpConfigError: 值为空。
    """
    value = str(raw or '').strip()
    if not value:
        raise McpConfigError(f'{env_name} 不能为空')
    return value if value.startswith('/') else f'/{value}'


@dataclass(frozen=True)
class McpTransportConfig:
    """MCP 服务运行参数（含安全边界）。

    每个字段对应一个 ``MCP_*`` 环境变量（见模块文档表格），并可被同名命令行参数覆盖；
    字段行内注释即该变量的说明，取默认值的地方标注在 ``DEFAULT_*`` 常量上。
    """

    # MCP_TRANSPORT：sse=HTTP/SSE 常驻服务（默认，Web/前端按 URL 接入）；stdio=子进程（本机 IDE）
    transport: str = DEFAULT_TRANSPORT
    # MCP_HOST：监听地址；非回环地址（如 0.0.0.0）必须同时配置 MCP_AUTH_TOKEN
    host: str = DEFAULT_HOST
    # MCP_PORT：监听端口，取值范围 1~65535
    port: int = DEFAULT_PORT
    # MCP_SSE_PATH：SSE 建连端点（需以 / 开头）
    sse_path: str = DEFAULT_SSE_PATH
    # MCP_MESSAGE_PATH：客户端消息回传端点（需以 / 开头）
    message_path: str = DEFAULT_MESSAGE_PATH
    # 健康检查端点，当前固定 /health（不作为环境变量暴露）
    health_path: str = DEFAULT_HEALTH_PATH
    # MCP_AUTH_TOKEN：Bearer 令牌；空=不鉴权（此时仅允许绑定回环地址）
    auth_token: str = ''
    # MCP_ALLOWED_HOSTS：DNS rebinding 保护的 Host 白名单（逗号分隔）
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    # MCP_ALLOWED_ORIGINS：DNS rebinding 保护的 Origin 白名单（逗号分隔）
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    # MCP_CORS_ORIGINS：浏览器直连时允许的来源；空=不添加 CORS 头
    cors_origins: tuple[str, ...] = ()
    # MCP_ALLOW_TRIGGER / --allow-trigger：trigger_plan_execution 写操作门禁，默认关闭（只读）
    allow_trigger: bool = False

    @property
    def is_loopback(self) -> bool:
        """监听地址是否为回环地址（决定是否强制鉴权）。"""
        return self.host in LOOPBACK_HOSTS

    @property
    def auth_enabled(self) -> bool:
        """是否启用 Bearer 令牌鉴权（``auth_token`` 非空）。"""
        return bool(self.auth_token)

    @property
    def bind_addr(self) -> str:
        """监听地址的 ``host:port`` 展示串（用于日志）。"""
        return f'{self.host}:{self.port}'

    @property
    def sse_url(self) -> str:
        """SSE 端点的完整 URL（供客户端配置复制）。"""
        return f'http://{self.host}:{self.port}{self.sse_path}'

    def validate(self) -> 'McpTransportConfig':
        """校验配置；错误信息可直接指导运维修复。

        Returns:
            McpTransportConfig: ``self``，便于链式调用。

        Raises:
            McpConfigError: 传输类型非法、端口越界、监听地址非法、
                非回环绑定缺少令牌，或非回环绑定使用了通配 Host。
        """
        if self.transport not in VALID_TRANSPORTS:
            raise McpConfigError(
                f'MCP_TRANSPORT 仅支持 {"/".join(VALID_TRANSPORTS)}，当前为 {self.transport!r}',
            )
        if not 1 <= self.port <= 65535:
            raise McpConfigError(f'MCP_PORT 必须在 1~65535，当前为 {self.port}')
        if self.host != self.host.strip() or not self.host:
            raise McpConfigError('MCP_HOST 不能为空或含首尾空格')
        if not self.is_loopback and not self.auth_enabled:
            raise McpConfigError(
                f'绑定非回环地址 {self.host!r} 时必须设置 MCP_AUTH_TOKEN，'
                '否则同网段任何客户端都可读取策略与告警数据。',
            )
        if not self.is_loopback and '*' in self.allowed_hosts:
            raise McpConfigError('非回环绑定时 MCP_ALLOWED_HOSTS 不允许使用通配 Host')
        return self

    def with_overrides(self, **overrides: object) -> 'McpTransportConfig':
        """应用命令行覆盖项并重新校验（``None`` / 空串表示不覆盖）。

        Args:
            **overrides: ``transport`` / ``host`` / ``port`` / ``auth_token`` /
                ``allow_trigger`` 等同名字段覆盖值。

        Returns:
            McpTransportConfig: 覆盖后的新配置（已通过 :meth:`validate`）。

        Raises:
            McpConfigError: 覆盖后配置非法。
        """
        cleaned = {
            key: value
            for key, value in overrides.items()
            if value is not None and value != ''
        }
        return replace(self, **cleaned).validate() if cleaned else self.validate()


def load_transport_config(env: dict[str, str] | None = None) -> McpTransportConfig:
    """从环境变量装载并校验传输配置。

    Args:
        env: 环境变量映射；``None`` 时读取 ``os.environ``（便于测试注入）。

    Returns:
        McpTransportConfig: 已校验的配置对象。

    Raises:
        McpConfigError: ``MCP_PORT`` 非整数、路径为空，或
            :meth:`McpTransportConfig.validate` 中的任一校验失败。
    """
    source = os.environ if env is None else env
    raw_port = str(source.get('MCP_PORT') or DEFAULT_PORT).strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise McpConfigError(f'MCP_PORT 必须是整数，当前为 {raw_port!r}') from exc

    config = McpTransportConfig(
        transport=str(source.get('MCP_TRANSPORT') or DEFAULT_TRANSPORT).strip().lower(),
        host=str(source.get('MCP_HOST') or DEFAULT_HOST).strip(),
        port=port,
        sse_path=_normalize_path(
            source.get('MCP_SSE_PATH') or DEFAULT_SSE_PATH, 'MCP_SSE_PATH',
        ),
        message_path=_normalize_path(
            source.get('MCP_MESSAGE_PATH') or DEFAULT_MESSAGE_PATH, 'MCP_MESSAGE_PATH',
        ),
        auth_token=str(source.get('MCP_AUTH_TOKEN') or '').strip(),
        allowed_hosts=tuple(
            _split_csv(source.get('MCP_ALLOWED_HOSTS')) or DEFAULT_ALLOWED_HOSTS
        ),
        allowed_origins=tuple(
            _split_csv(source.get('MCP_ALLOWED_ORIGINS')) or DEFAULT_ALLOWED_ORIGINS
        ),
        cors_origins=tuple(_split_csv(source.get('MCP_CORS_ORIGINS'))),
        allow_trigger=_parse_bool(source.get('MCP_ALLOW_TRIGGER')),
    )
    return config.validate()