"""B 模块 REST API · /api/workflow/* + /api/keyword-lists/* endpoint。"""

from tg_site_collector.api.credentials import router as credentials_router
from tg_site_collector.api.keyword_lists import router as keyword_lists_router
from tg_site_collector.api.runs import router as runs_router
from tg_site_collector.api.trigger import router as trigger_router

__all__ = [
    "credentials_router",
    "keyword_lists_router",
    "runs_router",
    "trigger_router",
]
