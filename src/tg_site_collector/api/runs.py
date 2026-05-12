"""GET /api/workflow/runs · 查 workflow 运行状态。

JWT 鉴权(design note · v0.3):
- endpoint 走 `Depends(get_current_user_ctx)` · 401 兜底
- run_id 校验:run_id 必须以 `site-collector-{tenant_id}-` 起头 · 检查 caller 是该 tenant
  对应 user(防别人查别人的 run · 简单字符串前缀匹配 · 不依赖 DB lookup)

MVP 实现 B.get_run_status / B.list_runs (PRD § B.7.5)。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from tg_site_collector.common.auth import get_current_user_ctx
from tg_site_collector.services.temporal_client import get_client
from tg_site_collector.common.auth import UserContext

_LOG = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workflow", tags=["b-module"])


class RunStatusResponse(BaseModel):
    run_id: str
    status: str
    progress: dict[str, Any] | None = None
    history_length: int = 0


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: str,
    user_ctx: Annotated[UserContext, Depends(get_current_user_ctx)],
) -> RunStatusResponse:
    """查单 run 状态 + workflow query 'get_progress' 的实时 state。

    权限:run_id 形如 `site-collector-{tenant_id}-{chat_id}-{ts}-{uuid8}`(trigger.py 里
    生成) · 检查 caller 的 user_id == tenant_id(personal owner=self · MVP 假设 user_id
    == tenant_id) · 否则 403。

    未来 v0.4:tenant_id 跟 user_id 解耦后改 DB lookup workflow_id → owner_id 比对。
    """
    # design note · 简单的 ownership 检查 · MVP 假设 tenant_id == user_id
    # 解析格式 `site-collector-{tenant_id}-{chat_id}-...`
    expected_prefix = f"site-collector-{user_ctx.user_id}-"
    if not run_id.startswith(expected_prefix):
        _LOG.warning(
            "scope=b.runs.cross_user_block run_id=%s caller=%s",
            run_id,
            user_ctx.user_id,
        )
        raise HTTPException(404, "run not found")

    try:
        client = await get_client()
        handle = client.get_workflow_handle(run_id)
        desc = await handle.describe()
        status_name = desc.status.name if desc.status else "UNKNOWN"

        progress: dict[str, Any] | None = None
        if status_name == "RUNNING":
            try:
                state = await handle.query("get_progress")
                # state 是 CollectorState (pydantic) · 转 dict
                progress = state.model_dump() if hasattr(state, "model_dump") else dict(state)
            except Exception as exc:
                _LOG.warning("query get_progress fail: %s", exc)

        return RunStatusResponse(
            run_id=run_id,
            status=status_name,
            progress=progress,
            history_length=desc.history_length,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _LOG.exception("get_run_status fail")
        raise HTTPException(404, f"run not found or temporal err: {exc}") from exc
