"""SSE 传输的静态令牌鉴权（纯 ASGI 中间件，无额外依赖）。

MCP 工具会读取策略元数据、K 线与告警，`trigger_plan_execution` 还会写库，
因此 HTTP 暴露时必须先过鉴权；本中间件只做「共享令牌」这一层，
OAuth2 / 多用户鉴权属 P2（见 documents.md 模块11）。
"""
from __future__ import annotations

import hmac
from typing import Any, Awaitable, Callable

SCOPE = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

UNAUTHORIZED_BODY = (
    '{"detail":"MCP 服务需要 Bearer 令牌：请在 Authorization 头携带 MCP_AUTH_TOKEN"}'
).encode('utf-8')


class BearerAuthMiddleware:
    """校验 `Authorization: Bearer <MCP_AUTH_TOKEN>`。

    - 仅在配置了 `MCP_AUTH_TOKEN` 时装配；
    - `OPTIONS` 预检请求放行，交由 CORS 中间件处理（浏览器预检不带凭据）；
    - 令牌比较使用 `hmac.compare_digest`，避免时序侧信道。
    """

    def __init__(self, app: Callable[..., Awaitable[None]], token: str) -> None:
        self.app = app
        self._expected = f'Bearer {token}'.encode('utf-8')

    async def __call__(self, scope: SCOPE, receive: Receive, send: Send) -> None:
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        if str(scope.get('method', '')).upper() == 'OPTIONS':
            await self.app(scope, receive, send)
            return
        if self._is_authorized(scope):
            await self.app(scope, receive, send)
            return
        await self._reject(send)

    def _is_authorized(self, scope: SCOPE) -> bool:
        for name, value in scope.get('headers') or []:
            if name.lower() == b'authorization':
                return hmac.compare_digest(bytes(value), self._expected)
        return False

    async def _reject(self, send: Send) -> None:
        await send({
            'type': 'http.response.start',
            'status': 401,
            'headers': [
                (b'content-type', b'application/json; charset=utf-8'),
                (b'content-length', str(len(UNAUTHORIZED_BODY)).encode('ascii')),
                (b'www-authenticate', b'Bearer'),
            ],
        })
        await send({'type': 'http.response.body', 'body': UNAUTHORIZED_BODY})