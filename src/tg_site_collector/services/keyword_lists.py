"""关键词列表 CRUD service · JSON 文件持久化。

布局:
    ./data/b-data/_templates.json     # 平台模板（所有租户共享只读）
    ./data/b-data/{tenant}/keyword-lists.json   # 租户私有

模板由 seed 函数在启动时写一次（幂等）。租户列表用户 CRUD。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from tg_site_collector.services.keyword_library import build_template_lists
from tg_site_collector.types import validate_tenant_id

_DATA_ROOT = Path(os.getenv("B_DATA_DIR", "/tmp/tg-site-collector/b-data")).resolve()
_TEMPLATES_FILE = _DATA_ROOT / "_templates.json"

# 同租户并发写锁(fastapi sync 函数走 threadpool · 用 threading.Lock)
# _meta_lock 保证 dict 自身的 get-or-create 也线程安全
_tenant_threading_locks: dict[str, threading.Lock] = {}
_meta_lock = threading.Lock()


def _get_lock(tenant_id: str) -> threading.Lock:
    with _meta_lock:
        lock = _tenant_threading_locks.get(tenant_id)
        if lock is None:
            lock = threading.Lock()
            _tenant_threading_locks[tenant_id] = lock
        return lock


def _ensure_root() -> Path:
    _DATA_ROOT.mkdir(parents=True, exist_ok=True)
    return _DATA_ROOT


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _new_id() -> str:
    return f"list_{secrets.token_hex(6)}"


# ── 平台模板 ─────────────────────────────────────────────────


def seed_templates(force: bool = False) -> list[dict[str, Any]]:
    """启动时调一次 · 把 4 类 + 全量平台模板写入 _templates.json。

    幂等:已存在不覆盖,除非 force=True。
    """
    _ensure_root()
    if _TEMPLATES_FILE.exists() and not force:
        return load_templates()
    templates = build_template_lists()
    payload = {"updated_at": _now(), "lists": templates}
    _TEMPLATES_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return templates


def load_templates() -> list[dict[str, Any]]:
    """读平台模板 · 没文件时调 seed_templates 自动落盘。"""
    if not _TEMPLATES_FILE.exists():
        return seed_templates()
    try:
        data = json.loads(_TEMPLATES_FILE.read_text(encoding="utf-8"))
        return list(data.get("lists", []))
    except json.JSONDecodeError:
        # 损坏 → 重 seed
        return seed_templates(force=True)


# ── 租户列表 ─────────────────────────────────────────────────


def _tenant_file(tenant_id: str) -> Path:
    validate_tenant_id(tenant_id)
    p = (_DATA_ROOT / tenant_id / "keyword-lists.json").resolve()
    # 防御:resolved 路径必须在 data root 下
    parent = p.parent
    if _DATA_ROOT not in parent.parents and parent != _DATA_ROOT:
        raise ValueError(f"tenant path escapes data root: {p}")
    parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_tenant(tenant_id: str) -> dict[str, Any]:
    p = _tenant_file(tenant_id)
    if not p.exists():
        return {"updated_at": _now(), "lists": []}
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data
    except json.JSONDecodeError:
        return {"updated_at": _now(), "lists": []}


def _save_tenant(tenant_id: str, payload: dict[str, Any]) -> None:
    """Atomic write: 写 .tmp + os.replace 避免 crash 半写。"""
    payload["updated_at"] = _now()
    p = _tenant_file(tenant_id)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, p)


def list_tenant_lists(tenant_id: str) -> list[dict[str, Any]]:
    return list(_load_tenant(tenant_id).get("lists", []))


def list_all(tenant_id: str) -> list[dict[str, Any]]:
    """返 templates + tenant 自定义合并清单。"""
    return load_templates() + list_tenant_lists(tenant_id)


def get_list(tenant_id: str, list_id: str) -> dict[str, Any] | None:
    """按 id 找列表 · 模板和私有都查。"""
    if list_id.startswith("tpl_"):
        for tpl in load_templates():
            if tpl["id"] == list_id:
                return tpl
        return None
    for lst in list_tenant_lists(tenant_id):
        if lst["id"] == list_id:
            return lst
    return None


def create_list(
    tenant_id: str,
    name: str,
    keywords: list[str],
    description: str = "",
    source_template: str | None = None,
) -> dict[str, Any]:
    if not name.strip():
        raise ValueError("name required")
    if not keywords:
        raise ValueError("keywords cannot be empty")
    # 去重 + 保序
    seen: set[str] = set()
    deduped: list[str] = []
    for kw in keywords:
        kw = kw.strip()
        if kw and kw not in seen:
            seen.add(kw)
            deduped.append(kw)

    with _get_lock(tenant_id):
        payload = _load_tenant(tenant_id)
        new = {
            "id": _new_id(),
            "name": name.strip()[:120],
            "description": description.strip()[:500],
            "keywords": deduped,
            "is_template": False,
            "source_template": source_template,
            "created_at": _now(),
            "updated_at": _now(),
        }
        payload.setdefault("lists", []).append(new)
        _save_tenant(tenant_id, payload)
        return new


def update_list(
    tenant_id: str,
    list_id: str,
    *,
    name: str | None = None,
    keywords: list[str] | None = None,
    description: str | None = None,
) -> dict[str, Any] | None:
    if list_id.startswith("tpl_"):
        raise ValueError("templates are read-only")
    with _get_lock(tenant_id):
        payload = _load_tenant(tenant_id)
        for lst in payload.get("lists", []):
            if lst["id"] == list_id:
                if name is not None:
                    lst["name"] = name.strip()[:120]
                if keywords is not None:
                    seen: set[str] = set()
                    deduped: list[str] = []
                    for kw in keywords:
                        kw = kw.strip()
                        if kw and kw not in seen:
                            seen.add(kw)
                            deduped.append(kw)
                    lst["keywords"] = deduped
                if description is not None:
                    lst["description"] = description.strip()[:500]
                lst["updated_at"] = _now()
                _save_tenant(tenant_id, payload)
                return cast(dict[str, Any], lst)
        return None


def delete_list(tenant_id: str, list_id: str) -> bool:
    if list_id.startswith("tpl_"):
        raise ValueError("templates cannot be deleted")
    with _get_lock(tenant_id):
        payload = _load_tenant(tenant_id)
        before = len(payload.get("lists", []))
        payload["lists"] = [
            lst for lst in payload.get("lists", []) if lst["id"] != list_id
        ]
        after = len(payload["lists"])
        if before == after:
            return False
        _save_tenant(tenant_id, payload)
        return True


def clone_list(
    tenant_id: str, source_id: str, *, new_name: str | None = None
) -> dict[str, Any] | None:
    """从模板或自己的列表克隆一份成自有。"""
    src = get_list(tenant_id, source_id)
    if not src:
        return None
    # create_list 自己已加锁 · 不要再嵌套(threading.Lock 不可重入)
    return create_list(
        tenant_id,
        name=(new_name or f"{src['name']} (副本)"),
        keywords=list(src.get("keywords", [])),
        description=src.get("description", ""),
        source_template=source_id,
    )


def resolve_keywords(tenant_id: str, list_id: str | None) -> list[str]:
    """workflow trigger 用 · list_id → 实际 keywords list。"""
    if not list_id:
        return []
    lst = get_list(tenant_id, list_id)
    if not lst:
        return []
    return list(lst.get("keywords", []))
