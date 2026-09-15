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
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace

DEFAULT_TRANSPORT = 'sse'
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8765
DEFAULT_SSE_PATH = '/sse'
DEFAULT_MESSAGE_PATH = '/messages/'
DEFAULT_ALLOWED_HOSTS = ('127.0.0.1:*', 'localhost:*', '[::1]:*')
DEFAULT_ALLOWED_ORIGINS = ('http://127.0.0.1:*', 'http://localhost:*')
DEFAULT_HEALTH_PATH = '/health'
LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1', '[::1]'})
VALID_TRANSPORTS = ('sse', 'stdio')


class McpConfigError(ValueError):
    """MCP 服务配置非法（端口、传输类型、鉴权缺失等）。"""


def _split_csv(raw: str | None) -> list[str]:
    return [item.strip() for item in str(raw or '').split(',') if item.strip()]


def _normalize_path(raw: str, env_name: str) -> str:
    value = str(raw or '').strip()
    if not value:
        raise McpConfigError(f'{env_name} 不能为空')
    return value if value.startswith('/') else f'/{value}'


@dataclass(frozen=True)
class McpTransportConfig:
    """MCP 服务运行参数（含安全边界）。"""

    transport: str = DEFAULT_TRANSPORT
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    sse_path: str = DEFAULT_SSE_PATH
    message_path: str = DEFAULT_MESSAGE_PATH
    health_path: str = DEFAULT_HEALTH_PATH
    auth_token: str = ''
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    cors_origins: tuple[str, ...] = ()

    @property
    def is_loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_token)

    @property
    def bind_addr(self) -> str:
        return f'{self.host}:{self.port}'

    @property
    def sse_url(self) -> str:
        return f'http://{self.host}:{self.port}{self.sse_path}'

    def validate(self) -> 'McpTransportConfig':
        """校验配置；错误信息可直接指导运维修复。"""
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
        """应用命令行覆盖项并重新校验（None 表示不覆盖）。"""
        cleaned = {
            key: value
            for key, value in overrides.items()
            if value is not None and value != ''
        }
        return replace(self, **cleaned).validate() if cleaned else self.validate()


def load_transport_config(env: dict[str, str] | None = None) -> McpTransportConfig:
    """从环境变量装载并校验传输配置。"""
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
    )
    return config.validate()