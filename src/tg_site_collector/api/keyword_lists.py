"""GET/POST/PUT/DELETE /api/keyword-lists/* · 用户管理自定义关键词列表 + 看模板。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tg_site_collector.services import keyword_lists as kw_svc

_LOG = logging.getLogger(__name__)
router = APIRouter(prefix="/api/keyword-lists", tags=["b-module"])


# ── pydantic 入参 ─────────────────────────────────────


class _CreateBody(BaseModel):
    tenant_id: str
    name: str = Field(min_length=1, max_length=120)
    keywords: list[str] = Field(min_length=1)
    description: str = ""


class _UpdateBody(BaseModel):
    tenant_id: str
    name: str | None = Field(default=None, min_length=1, max_length=120)
    keywords: list[str] | None = None
    description: str | None = None


class _CloneBody(BaseModel):
    tenant_id: str
    new_name: str | None = None


# ── endpoints ────────────────────────────────────────


@router.get("/templates", summary="列平台预设模板（4 类 + 全量）")
async def list_templates() -> dict[str, Any]:
    tpls = kw_svc.load_templates()
    return {
        "count": len(tpls),
        "templates": [
            {
                "id": t["id"],
                "name": t["name"],
                "description": t["description"],
                "category": t.get("category"),
                "keyword_count": len(t["keywords"]),
            }
            for t in tpls
        ],
    }


@router.get("", summary="列我的列表 + 平台模板（默认全部）")
async def list_all(tenant_id: str, include_templates: bool = True) -> dict[str, Any]:
    own = kw_svc.list_tenant_lists(tenant_id)
    tpls = kw_svc.load_templates() if include_templates else []
    return {
        "tenant_id": tenant_id,
        "templates": [
            {
                "id": t["id"],
                "name": t["name"],
                "description": t["description"],
                "category": t.get("category"),
                "keyword_count": len(t["keywords"]),
                "is_template": True,
            }
            for t in tpls
        ],
        "my_lists": [
            {
                "id": lst["id"],
                "name": lst["name"],
                "description": lst["description"],
                "keyword_count": len(lst["keywords"]),
                "is_template": False,
                "source_template": lst.get("source_template"),
                "created_at": lst.get("created_at"),
                "updated_at": lst.get("updated_at"),
            }
            for lst in own
        ],
    }


@router.get("/{list_id}", summary="看单个列表详情（含完整 keywords）")
async def get_one(list_id: str, tenant_id: str) -> dict[str, Any]:
    lst = kw_svc.get_list(tenant_id, list_id)
    if not lst:
        raise HTTPException(404, f"keyword list not found: {list_id}")
    return lst


@router.post("", summary="创建租户自定义列表")
async def create(body: _CreateBody) -> dict[str, Any]:
    try:
        return kw_svc.create_list(
            tenant_id=body.tenant_id,
            name=body.name,
            keywords=body.keywords,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/{list_id}", summary="改租户列表（模板只读）")
async def update(list_id: str, body: _UpdateBody) -> dict[str, Any]:
    try:
        out = kw_svc.update_list(
            body.tenant_id,
            list_id,
            name=body.name,
            keywords=body.keywords,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not out:
        raise HTTPException(404, f"keyword list not found: {list_id}")
    return out


@router.delete("/{list_id}", summary="删除租户列表（模板不可删）")
async def delete(list_id: str, tenant_id: str) -> dict[str, Any]:
    try:
        ok = kw_svc.delete_list(tenant_id, list_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not ok:
        raise HTTPException(404, f"keyword list not found: {list_id}")
    return {"deleted": True, "id": list_id}


@router.post("/{list_id}/clone", summary="克隆模板/列表 → 改我的")
async def clone(list_id: str, body: _CloneBody) -> dict[str, Any]:
    out = kw_svc.clone_list(body.tenant_id, list_id, new_name=body.new_name)
    if not out:
        raise HTTPException(404, f"source list not found: {list_id}")
    return out
