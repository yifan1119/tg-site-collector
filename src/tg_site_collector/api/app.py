"""B 模块 FastAPI app 入口 · 独立版。

简化重点(跟 monorepo 比):
- 不需要 DB · session_factory 都用 stub auth 替代(JWT 不真验 jti 黑名单)
- 不需要 JWT secret 必填 · stub 模式默认 allow
- 保留 4 个 router:trigger / runs / keyword_lists / credentials
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI

from tg_site_collector.api.credentials import router as credentials_router
from tg_site_collector.api.keyword_lists import router as keyword_lists_router
from tg_site_collector.api.runs import router as runs_router
from tg_site_collector.api.trigger import router as trigger_router
from tg_site_collector.common.auth import JWTMiddleware
from tg_site_collector.services import keyword_lists as kw_svc

_LOG = logging.getLogger(__name__)


def create_app() -> FastAPI:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # httpx 默认 log URL · TG endpoint URL 含 bot token · 抬到 WARNING 防泄漏
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # 启动时确保平台模板落盘 · 幂等
    try:
        seeded = kw_svc.seed_templates()
        _LOG.info("scope=api.startup templates_seeded=%d", len(seeded))
    except Exception as e:  # noqa: BLE001
        _LOG.warning("template seeding failed (ok if first run): %s", e)

    app = FastAPI(
        title="tg-site-collector · API",
        version="0.1.0",
        description="Multi-tenant site contact scraper · workflow trigger / runs / keyword lists",
    )

    # JWT middleware stub · 默认放行 · 接你自己的 IAM 时改 common/auth.py
    app.add_middleware(JWTMiddleware)

    app.include_router(trigger_router)
    app.include_router(runs_router)
    app.include_router(keyword_lists_router)
    app.include_router(credentials_router)

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "service": "tg-site-collector",
            "version": "0.1.0",
            "endpoints": {
                "workflow": [
                    "POST /api/workflow/trigger",
                    "GET  /api/workflow/runs/{run_id}",
                    "GET  /api/workflow/health",
                ],
                "keyword_lists": [
                    "GET  /api/keyword-lists/templates",
                    "GET  /api/keyword-lists?tenant_id=...",
                    "GET  /api/keyword-lists/{list_id}?tenant_id=...",
                    "POST /api/keyword-lists",
                    "PUT  /api/keyword-lists/{list_id}",
                    "DELETE /api/keyword-lists/{list_id}?tenant_id=...",
                    "POST /api/keyword-lists/{list_id}/clone",
                ],
                "credentials": [
                    "POST /api/credentials/verify-bot",
                    "POST /api/credentials/verify-tavily",
                ],
            },
        }

    return app


app = create_app()
