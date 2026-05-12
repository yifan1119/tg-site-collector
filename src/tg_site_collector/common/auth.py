"""Auth stub · 统一 IAM / JWT / UserContext 抽象。

从 monorepo 拆出来的独立版本 · 所有原本散落的 IAM 调用点都集中在这一个文件 ·
方便同事接他自己的鉴权系统。

⚠️ NOT PRODUCTION-READY ⚠️
默认实现:
- ``ENV_JWT_SECRET = "JWT_SECRET"`` · 从环境变量读 secret
- ``JWTMiddleware`` · 验 ``Authorization: Bearer <token>`` · 失败放行(stub)
- ``get_current_user_ctx`` · 从 request.state 读 user_ctx · 没有则返默认 demo user
- ``UserContext`` · 简化版 dataclass(user_id / tenant_id / department / hierarchy_role)

接你的 IAM 三种方式:
1. 改 ``JWTMiddleware`` 真验 JWT(用 PyJWT / jose)
2. 改 ``get_current_user_ctx`` 调你的 IAM endpoint
3. 完全 fork 这个文件 · 按你的 contract 重写
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Final

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

LOGGER = logging.getLogger(__name__)

ENV_JWT_SECRET: Final[str] = "JWT_SECRET"
ENV_AUTH_REQUIRED: Final[str] = "AUTH_REQUIRED"  # "1" = 强制鉴权 · 默认 stub allow


@dataclass(frozen=True)
class UserContext:
    """简化版用户上下文。

    跟 monorepo 原 ``c_module.query.types.UserContext`` 字段对齐 · 让 workflow /
    activity / api 不用改。生产时按你 IAM 返回结构扩展。
    """

    user_id: str
    tenant_id: str
    department: str = "default"
    hierarchy_role: str = "member"
    function_roles: list[str] = field(default_factory=list)


_DEFAULT_DEMO_USER = UserContext(
    user_id="demo_user",
    tenant_id="demo",
    department="default",
    hierarchy_role="platform_owner",
    function_roles=["platform_admin"],
)


async def get_current_user_ctx(request: Request) -> UserContext:
    """从 request.state 拿 user_ctx · 没有则 stub 返 demo user。

    生产前改:
    - 真验 JWT(用 PyJWT 解 request.headers["Authorization"])
    - 或调你 IAM /me endpoint
    - 或 cookie session lookup
    """
    if os.getenv(ENV_AUTH_REQUIRED) == "1":
        # 强制鉴权 · 但本 stub 没真验 · 同事接 IAM 前会一直 401 · 安全默认
        ctx = getattr(request.state, "user_ctx", None)
        if ctx is None:
            raise HTTPException(
                status_code=401,
                detail="auth required · IAM not wired · see common/auth.py",
            )
        return ctx  # type: ignore[no-any-return]

    # 默认 stub allow · 返 demo user · 开发用
    ctx = getattr(request.state, "user_ctx", None)
    if ctx is None:
        LOGGER.debug("stub auth · returning demo user")
        return _DEFAULT_DEMO_USER
    return ctx  # type: ignore[no-any-return]


class JWTMiddleware(BaseHTTPMiddleware):
    """JWT 中间件 stub · 默认放行 · 不真验。

    生产前改:用 ``jose.jwt.decode(...)`` 或 ``PyJWT`` 真解 token ·
    成功 → 把解出来的 user_ctx 挂 ``request.state.user_ctx`` ·
    失败 → 401。
    """

    def __init__(self, app: ASGIApp, *, secret: str | None = None) -> None:
        super().__init__(app)
        self._secret = secret or os.getenv(ENV_JWT_SECRET, "")
        if not self._secret:
            LOGGER.warning(
                "JWT_SECRET not set · JWTMiddleware is stub allow · NOT for production"
            )

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        # stub:不验 JWT · 直接放行 · request.state.user_ctx 由 get_current_user_ctx 兜底
        response = await call_next(request)
        return response
