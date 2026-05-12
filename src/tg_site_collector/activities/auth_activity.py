"""A.M2.check stub · standalone 版默认全 allow。

⚠️ NOT PRODUCTION-READY ⚠️
本仓库是从 monorepo 拆出的独立版本 · 默认 auth allow 所有 tenant + user。

**生产部署前必须接你自己的 IAM 系统**(任选一个):
- HTTP 调你自己的 IAM endpoint(取消注释下面的 httpx 代码 + 改 env)
- 集成 OAuth / Keycloak / Auth0
- 直接 DB 查 employees 表

对接方式见 README · 「自定义鉴权」章节。
"""

from __future__ import annotations

import logging
import os

from temporalio import activity

_LOG = logging.getLogger(__name__)

_ENV_AUTH_MODE = "AUTH_MODE"  # stub(默认) / http / deny
_ENV_AUTH_BASE = "AUTH_BASE"  # http 模式 · 你的 IAM endpoint


@activity.defn(name="a_m2_check")
async def a_m2_check(
    tenant_id: str, user_id: str, resource_id: str, action: str
) -> dict[str, object]:
    """检查 user_id 对 resource_id 的 action 权限。

    返回 ``{"decision": "allow" | "deny", "reason": "..."}``。
    deny 时 workflow 应 raise PermissionError 走 fail-closed。

    默认 stub allow · 通过环境变量 ``AUTH_MODE`` 切换实现。
    """
    mode = os.getenv(_ENV_AUTH_MODE, "stub")

    if mode == "deny":
        # 测试用 · 强制 deny
        _LOG.warning(
            "scope=auth.deny tenant=%s user=%s resource=%s action=%s · AUTH_MODE=deny",
            tenant_id,
            user_id,
            resource_id,
            action,
        )
        raise PermissionError(f"auth deny · resource={resource_id} action={action}")

    if mode == "http":
        # 真调你自己的 IAM endpoint · 参考实现 · 按你的 contract 改
        # import httpx
        # base = os.getenv(_ENV_AUTH_BASE, "http://localhost:8000")
        # async with httpx.AsyncClient(timeout=5.0) as client:
        #     r = await client.post(
        #         f"{base}/api/iam/check",
        #         json={"user_id": user_id, "resource_id": resource_id, "action": action},
        #     )
        #     r.raise_for_status()
        #     data = r.json()
        # if data.get("decision") != "allow":
        #     raise PermissionError(f"auth deny · {data.get('reason')}")
        # return data
        raise NotImplementedError(
            "AUTH_MODE=http 模式需要你自己实现 IAM endpoint 对接 · "
            "见 auth_activity.py 注释 + README"
        )

    # 默认 stub allow
    _LOG.info(
        "scope=auth.stub.allow tenant=%s user=%s resource=%s action=%s",
        tenant_id,
        user_id,
        resource_id,
        action,
    )
    return {"decision": "allow", "reason": "stub mode · NOT for production"}
