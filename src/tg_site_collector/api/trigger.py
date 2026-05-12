"""POST /api/workflow/trigger · 启动 site-collector workflow。

JWT 鉴权(design note · v0.3):
- endpoint 走 `Depends(get_current_user_ctx)` · 401 兜底
- user_id 后端从 JWT 派 · **忽略** request body 里的 user_id 字段(防 spoof)
- tenant_id MVP 仍由 body 传(向后兼容前端) · 未来 v0.4 改后端从 user_ctx 派 personal owner
- 返 run_id
- B.start_workflow contract 见 PRD § B.7.5
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from tg_site_collector.common.auth import get_current_user_ctx
from tg_site_collector.services import keyword_lists as kw_svc
from tg_site_collector.services.temporal_client import (
    b_task_queue,
    get_client,
)
from tg_site_collector.types import CollectorMode, WorkflowContext
from tg_site_collector.workflows import SITE_COLLECTOR_WORKFLOW_NAME
from tg_site_collector.common.auth import UserContext

_LOG = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workflow", tags=["b-module"])


class TriggerRequest(BaseModel):
    tenant_id: str
    # design note · user_id 字段保留(向后兼容)但**后端忽略** · 实际 user_id 从 JWT 派
    # 不抛 422 防破前端老调用 · log warning 提示
    user_id: str | None = None
    tg_chat_id: int
    bot_token: str
    tavily_api_key: str | None = None
    mode: CollectorMode = CollectorMode.COLLECT
    # 三选一传 keywords:
    # 1) keyword_list_id="tpl_导航"  ← 平台模板
    # 2) keyword_list_id="list_xxx"  ← 租户自定义
    # 3) keywords=["..."]            ← 一次性 inline
    keyword_list_id: str | None = None
    keywords: list[str] = []
    workers: int = 15
    timeout_per_site: int = 12
    keywords_per_batch: int = 20
    sites_per_batch: int = 200
    # 导航站子站展开 · True = 深度(出料多 · 慢)/ False = 快速(出料少 · 快)
    nav_expand: bool = True
    nav_max_candidates: int = 50
    # 增量采集 · 跳过最近 N 小时内已采过的 URL · 默认 0 = 全量重采
    # 168 = 7 天 · 高频跑省 70-90% 时间
    skip_if_fresh_hours: int = 0


class TriggerResponse(BaseModel):
    run_id: str
    workflow_name: str
    task_queue: str
    status: str = "started"


@router.post("/trigger", response_model=TriggerResponse)
async def trigger_workflow(
    req: TriggerRequest,
    user_ctx: Annotated[UserContext, Depends(get_current_user_ctx)],
) -> TriggerResponse:
    """启动 site-collector workflow · JWT 鉴权 · user_id 从 JWT 派。"""
    if req.mode == CollectorMode.HELP:
        raise HTTPException(400, "mode=help is handled by TG bot directly, not workflow")

    # design note · 后端 user_id 一律从 JWT 派 · 前端 body 传的忽略(防 spoof)
    if req.user_id and req.user_id != user_ctx.user_id:
        _LOG.warning(
            "scope=b.trigger.user_id_spoof_ignored body=%s jwt=%s · "
            "前端不应再传 user_id · 已用 JWT 派的覆盖",
            req.user_id,
            user_ctx.user_id,
        )
    user_id = user_ctx.user_id

    # tenant_id 校验(防 path traversal)
    from tg_site_collector.types import validate_tenant_id

    try:
        validate_tenant_id(req.tenant_id)
    except ValueError as exc:
        raise HTTPException(400, f"invalid tenant_id: {exc}") from exc

    # 解析 keywords:list_id 优先(允许 list_id + extra inline keywords 合并)
    resolved_keywords: list[str] = []
    if req.keyword_list_id:
        resolved_keywords.extend(
            kw_svc.resolve_keywords(req.tenant_id, req.keyword_list_id)
        )
        if not resolved_keywords:
            raise HTTPException(
                404, f"keyword_list_id not found: {req.keyword_list_id}"
            )
    # 用户传的 inline keywords 也加进来(去重保序)
    seen: set[str] = set(resolved_keywords)
    for kw in req.keywords:
        kw = kw.strip()
        if kw and kw not in seen:
            seen.add(kw)
            resolved_keywords.append(kw)

    if req.mode == CollectorMode.SEARCH_NEW and not resolved_keywords:
        raise HTTPException(
            400,
            "mode=search_new requires keywords (via keyword_list_id or inline)",
        )

    ctx = WorkflowContext(
        tenant_id=req.tenant_id,
        user_id=user_id,  # design note · 从 JWT 派 · 不信前端
        tg_chat_id=req.tg_chat_id,
        bot_token=req.bot_token,
        tavily_api_key=req.tavily_api_key,
        mode=req.mode,
        keywords=resolved_keywords,
        workers=req.workers,
        timeout_per_site=req.timeout_per_site,
        keywords_per_batch=req.keywords_per_batch,
        sites_per_batch=req.sites_per_batch,
        nav_expand=req.nav_expand,
        nav_max_candidates=req.nav_max_candidates,
        skip_if_fresh_hours=req.skip_if_fresh_hours,
    )

    workflow_id = (
        f"site-collector-{req.tenant_id}-{req.tg_chat_id}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    )

    try:
        client = await get_client()
        handle = await client.start_workflow(
            SITE_COLLECTOR_WORKFLOW_NAME,
            ctx,
            id=workflow_id,
            task_queue=b_task_queue(),
        )
    except Exception as exc:
        _LOG.exception("scope=b.trigger.fail err=%s", exc)
        raise HTTPException(503, f"temporal unavailable: {exc}") from exc

    _LOG.info(
        "scope=b.trigger.ok tenant=%s user=%s mode=%s run_id=%s",
        req.tenant_id,
        user_id,
        req.mode.value,
        handle.id,
    )
    return TriggerResponse(
        run_id=handle.id,
        workflow_name=SITE_COLLECTOR_WORKFLOW_NAME,
        task_queue=b_task_queue(),
    )


class HealthResponse(BaseModel):
    ok: bool
    temporal_host: str
    workflow: str = SITE_COLLECTOR_WORKFLOW_NAME


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """探活 · 验证能连 Temporal · 不需要 auth(供 LB / monitor)。"""
    from tg_site_collector.services.temporal_client import temporal_host

    try:
        client = await get_client()
        # 跑一个轻 query 验证连接
        _ = client.namespace
        return HealthResponse(ok=True, temporal_host=temporal_host())
    except Exception as exc:
        _LOG.warning("scope=b.health.fail err=%s", exc)
        return HealthResponse(ok=False, temporal_host=temporal_host())
