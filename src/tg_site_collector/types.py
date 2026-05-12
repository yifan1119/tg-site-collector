"""B 模块共享类型 · Pydantic 模型 · workflow / activity / API 三方共享。

⚠️ 必须 immutable + serializable —— Temporal workflow 在跨 worker 跨进程传递时只能传
JSON-friendly 数据。所以所有字段都是基础类型 / list / dict / 嵌套 BaseModel。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# tenant_id 安全字符集:小写字母/数字/横杠/下划线 · 长度 1-64
# 拒绝 .. / 路径分隔符 / 其他特殊字符 · 防止文件 IO 越界写
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_tenant_id(tenant_id: str) -> str:
    """所有 tenant_id 入参必走这个校验 · 拒绝路径遍历攻击。"""
    if not isinstance(tenant_id, str):
        raise ValueError(f"tenant_id must be str, got {type(tenant_id).__name__}")
    if not _TENANT_ID_RE.match(tenant_id):
        raise ValueError(
            f"invalid tenant_id: {tenant_id!r} · "
            f"must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}"
        )
    return tenant_id


class CollectorMode(StrEnum):
    """触发关键词对应的 workflow 行为。"""

    COLLECT = "collect"  # 「采集」走缓存 + 增量
    SEARCH_NEW = "search_new"  # 「搜新的」消耗 Tavily 配额
    PUSH_ONLY = "push_only"  # 「推送给我」推上次 history JSON
    HELP = "help"  # 「帮助」回固定文案


class WorkflowContext(BaseModel):
    """site-collector workflow 启动入参 · 由 trigger 层装配。"""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    # design note · user_id = workflow.start 触发主体 · A.M2.check 5 闸算法的 PermissionUser.user_id
    # MVP fallback: trigger 装配时若解析不到员工绑定 · 用 tenant_id 自身(personal owner=self 走通)
    user_id: str
    tg_chat_id: int
    bot_token: str  # MVP 直接传，未来从 secret store 动态拉
    tavily_api_key: str | None = None
    mode: CollectorMode
    keywords: list[str] = Field(default_factory=list)  # 仅 search_new 用
    workers: int = 15
    timeout_per_site: int = 12
    keywords_per_batch: int = 20
    sites_per_batch: int = 200
    # 导航站子站展开 · Playwright 渲染 · 默认开
    # 关:出料 ×1 · 用时 ×1
    # 开:出料 ×3-5 · 用时 ×3
    nav_expand: bool = True
    nav_max_candidates: int = 50
    # 增量采集 · 跳过最近 N 小时内已采过的 URL · 默认 0 = 不跳过(全量)
    # 168 = 7 天 · 重复跑节省 70-90% 时间
    skip_if_fresh_hours: int = 0


class BatchProgress(BaseModel):
    """单批进度（@workflow.query 暴露给 Dashboard）。"""

    model_config = ConfigDict(frozen=True)

    batch_index: int
    batch_total: int
    batch_kind: str  # "keyword" | "site"
    site_count: int = 0
    contact_count: int = 0
    elapsed_sec: int = 0


class CollectorState(BaseModel):
    """workflow 自身可观测状态。"""

    phase: str = "pending"  # pending|searching|collecting|pushing|done|failed|cancelled
    keyword_batches_total: int = 0
    keyword_batches_done: int = 0
    site_batches_total: int = 0
    site_batches_done: int = 0
    new_urls_found: int = 0
    sites_collected: int = 0
    contacts_extracted: int = 0
    last_error: str | None = None


class RunSummary(BaseModel):
    """workflow 终态返回。"""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    run_id: str
    sites_collected: int
    contacts_extracted: int
    new_urls_found: int
    duration_sec: int
    tavily_quota_hit: bool = False


class ContactItem(BaseModel):
    """单条联系方式（采集结果元素）。"""

    model_config = ConfigDict(frozen=True)

    type: str  # email | telegram | qq | wechat | phone | twitter | facebook | form
    value: str
    value_safe: str  # 脱敏版（email/phone/qq/wechat 必脱敏）
    source_page: str = ""


class SiteResult(BaseModel):
    """单站点采集结果。"""

    site_url: str
    site_name: str = ""
    status: str  # success | partial | failed
    contacts: list[ContactItem] = Field(default_factory=list)
    failure_reason: str = ""


class BatchResult(BaseModel):
    """一批站点采集完成的结果。"""

    batch_index: int
    site_count: int
    contact_count: int
    sites: list[SiteResult] = Field(default_factory=list)


def chunk(items: list[Any], size: int) -> list[list[Any]]:
    """切片工具 · workflow / activity 共用。"""
    if size <= 0:
        return [items] if items else []
    return [items[i : i + size] for i in range(0, len(items), size)]
