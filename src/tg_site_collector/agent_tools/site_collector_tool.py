"""site-collector workflow 的 Agent Tool 适配层。

PRD § B.5 「Workflow as Tool for Agent」:
- Tool spec(JSON Schema)给 Agent LLM 看 · 描述能力 + 入参约束
- Tool handler 是 Python 函数 · Agent decision loop 调用时进入 → 启 workflow → 返结果

Anthropic Tool Use 格式 + LangGraph @tool 兼容(input_schema 用 JSON Schema draft-07)。

⚠️ 当前 B2 Agent Runtime (PRD § B.5) 尚未实装 · 本文件是预留 · MVP 只验证 schema 合法。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from tg_site_collector.services import keyword_lists as kw_svc
from tg_site_collector.services.temporal_client import b_task_queue, get_client
from tg_site_collector.types import CollectorMode, WorkflowContext
from tg_site_collector.workflows import SITE_COLLECTOR_WORKFLOW_NAME

# ── Agent Tool Specification (JSON Schema) ──────────────────────────

SITE_COLLECTOR_TOOL_SPEC: dict[str, Any] = {
    "name": "site_collector",
    "description": (
        "采集网站联系方式 (email / TG / Twitter / QQ / 微信 / phone / form 等)。"
        "三种 mode: collect (走缓存,不消耗 Tavily 配额) / "
        "search_new (用关键词搜新站再采,消耗 Tavily) / "
        "push_only (重新推上次的 history.json)。"
        "支持自定义关键词列表 (keyword_list_id) 或一次性 inline keywords。"
        "结果通过 TG bot 推回指定 chat_id · 同时落 history.json 留存。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tenant_id": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
                "description": "租户 ID · 数据按此隔离 · 字母数字 _ -, 1-64 字符",
            },
            "tg_chat_id": {
                "type": "integer",
                "description": "TG chat ID · 结果推到这个 chat",
            },
            "bot_token": {
                "type": "string",
                "description": "TG Bot token (123456:AAxx...) · 用于推送",
            },
            "mode": {
                "type": "string",
                "enum": ["collect", "search_new", "push_only"],
                "description": (
                    "collect: 走缓存采集 (默认 / 推荐) · "
                    "search_new: Tavily 搜新关键词再采 · "
                    "push_only: 仅推上次结果"
                ),
            },
            "keyword_list_id": {
                "type": ["string", "null"],
                "description": (
                    "关键词列表 ID · 可选 · 平台模板用 'tpl_' 前缀 "
                    "(tpl_导航 / tpl_小说 / tpl_视频 / tpl_漫画 / tpl_全量) · "
                    "用户自定义用 'list_' 前缀"
                ),
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "一次性 inline 关键词 · 跟 keyword_list_id 二选一 / 可合并 · "
                    "search_new mode 必传其一"
                ),
                "default": [],
            },
            "tavily_api_key": {
                "type": ["string", "null"],
                "description": "Tavily API key · search_new mode 必填 · collect 不需",
            },
            "workers": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 15,
                "description": "并发线程数",
            },
            "timeout_per_site": {
                "type": "integer",
                "minimum": 3,
                "maximum": 60,
                "default": 12,
                "description": "单站抓取超时秒",
            },
            "keywords_per_batch": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 20,
                "description": "搜索关键词分批大小 · 每批跑完推一次 TG 进度",
            },
            "sites_per_batch": {
                "type": "integer",
                "minimum": 10,
                "maximum": 1000,
                "default": 200,
                "description": "站点采集分批大小 · 每批跑完落 JSON + 推 TG 文件",
            },
            "nav_expand": {
                "type": "boolean",
                "default": True,
                "description": (
                    "导航站子站展开(Playwright 渲染) · "
                    "True=深度(URL +20-40% · contacts ×2-3 · 用时 ×2-3) / "
                    "False=快速(只采 surface URL)"
                ),
            },
            "nav_max_candidates": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 50,
                "description": "nav-expand 阶段最多渲染多少个候选导航站",
            },
            "skip_if_fresh_hours": {
                "type": "integer",
                "minimum": 0,
                "maximum": 8760,
                "default": 0,
                "description": (
                    "增量采集 · 跳过最近 N 小时内已采过的 URL · "
                    "0=全量重采 / 168=7天内不重采 · "
                    "高频跑省 70-90% 时间"
                ),
            },
        },
        "required": ["tenant_id", "tg_chat_id", "bot_token", "mode"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "Temporal workflow run ID · 用于查 status / cancel",
            },
            "workflow_name": {"type": "string", "const": "site-info-collector"},
            "task_queue": {"type": "string"},
            "status": {"type": "string", "const": "started"},
        },
    },
    # PRD § B.5 + § A.M4 Resource Registry 字段
    "resource_metadata": {
        "kind": "workflow",
        "module": "b",
        "workflow_name": SITE_COLLECTOR_WORKFLOW_NAME,
        "version": "1.0.0",
        "side_effects": ["network", "tg_send", "file_write"],
        "long_running": True,  # 单次 30-90 分钟 · Agent 不要 await 同步等
        "cancellable": True,
        # 调用前 / 后 Agent Runtime 必走的护栏 phase (S 模块 ready 时启用)
        "guard_phases": ["pre", "post"],
    },
}


# ── Tool Handler · Agent decision loop 调用入口 ────────────────────


async def site_collector_tool_handler(**kwargs: Any) -> dict[str, Any]:
    """Agent 调 site_collector tool 时的 Python 入口。

    职责:
    1. 把 LLM 给的 args 转成 WorkflowContext
    2. 调 Temporal start_workflow
    3. 返 run_id 给 Agent (Agent 后续可以 query status / cancel)

    注意: 这是 fire-and-forget · workflow 异步跑,Agent 不阻塞等结果。
    Agent 想看进度要单独调 get_run_status tool。
    """
    mode_str = kwargs.get("mode", "collect")
    try:
        mode = CollectorMode(mode_str)
    except ValueError as exc:
        raise ValueError(
            f"invalid mode: {mode_str} · valid: collect/search_new/push_only"
        ) from exc

    # 解析 keywords · 跟 trigger.py 行为一致:
    # 1) keyword_list_id → resolve_keywords 拿模板/列表的词
    # 2) inline keywords 合并去重(保序)
    tenant_id = kwargs["tenant_id"]
    resolved_keywords: list[str] = []
    list_id = kwargs.get("keyword_list_id")
    if list_id:
        resolved_keywords.extend(kw_svc.resolve_keywords(tenant_id, list_id))
        if not resolved_keywords:
            raise ValueError(f"keyword_list_id not found: {list_id}")
    seen: set[str] = set(resolved_keywords)
    for kw in kwargs.get("keywords") or []:
        kw = kw.strip()
        if kw and kw not in seen:
            seen.add(kw)
            resolved_keywords.append(kw)

    ctx = WorkflowContext(
        tenant_id=tenant_id,
        # design note · agent tool 调用 fallback = tenant_id (Agent Runtime ready 后从
        # AgentCallContext 拿真 user_id)
        user_id=kwargs.get("user_id") or tenant_id,
        tg_chat_id=kwargs["tg_chat_id"],
        bot_token=kwargs["bot_token"],
        tavily_api_key=kwargs.get("tavily_api_key"),
        mode=mode,
        keywords=resolved_keywords,
        workers=kwargs.get("workers", 15),
        timeout_per_site=kwargs.get("timeout_per_site", 12),
        keywords_per_batch=kwargs.get("keywords_per_batch", 20),
        sites_per_batch=kwargs.get("sites_per_batch", 200),
        nav_expand=kwargs.get("nav_expand", True),
        nav_max_candidates=kwargs.get("nav_max_candidates", 50),
        skip_if_fresh_hours=kwargs.get("skip_if_fresh_hours", 0),
    )

    workflow_id = (
        f"site-collector-{ctx.tenant_id}-{ctx.tg_chat_id}-"
        f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    )

    client = await get_client()
    handle = await client.start_workflow(
        SITE_COLLECTOR_WORKFLOW_NAME,
        ctx,
        id=workflow_id,
        task_queue=b_task_queue(),
    )
    return {
        "run_id": handle.id,
        "workflow_name": SITE_COLLECTOR_WORKFLOW_NAME,
        "task_queue": b_task_queue(),
        "status": "started",
    }
