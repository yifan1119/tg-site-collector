"""B 模块关键词路由 · 在裸文本进 C 模块 query 之前先看是不是采集相关指令。

设计:
- 用户在 TG 直接发「采集」「搜新的」「推送给我」「帮助」等关键词
- 路由器识别 → 直接启 B 模块 workflow → 回 ack 给用户
- 不匹配关键词 → 返 False 让上游走 C 模块 query

接入点: telegram_bot/handlers.py 在 on_text_message 里:
    if await b_module_router.try_route(update, ctx, text):
        return
    await _handle_question(...)  # 原有 C 路径
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from telegram import Update
from telegram.ext import ContextTypes

from tg_site_collector.services import keyword_lists as kw_svc
from tg_site_collector.services.temporal_client import (
    b_task_queue,
    get_client,
    temporal_host,
)
from tg_site_collector.types import CollectorMode, WorkflowContext
from tg_site_collector.workflows import SITE_COLLECTOR_WORKFLOW_NAME
from tg_site_collector.telegram_bot.welcome import HELP_TEXT as _HELP_TEXT  # 复用 · 避免文案漂移

# 「搜新的」TG 默认 keyword_list_id · 用平台「全量」模板
# 用户没显式选列表时 · 自动套全量(239 词)
_DEFAULT_KEYWORD_LIST_ID = "tpl_全量"

_LOGGER = logging.getLogger(__name__)

# 关键词 → (mode, nav_expand, skip_if_fresh_hours) 映射
# nav_expand=True (深度) · 出料多但慢
# nav_expand=False (快速) · 出料少但快
# skip_if_fresh_hours > 0 → 增量采集(跳过 N 小时内已采过的 · 高频跑省时间)
_DEFAULT_FRESH_HOURS = 168  # 7 天 · 增量模式默认窗口
_KEYWORD_ROUTE_MAP: dict[str, tuple[CollectorMode, bool, int]] = {
    # 默认: 全量深度采集(每次重抓所有 URL)
    "采集": (CollectorMode.COLLECT, True, 0),
    "开始采集": (CollectorMode.COLLECT, True, 0),
    "跑一遍": (CollectorMode.COLLECT, True, 0),
    "深度采集": (CollectorMode.COLLECT, True, 0),
    "完整采集": (CollectorMode.COLLECT, True, 0),
    "全量采集": (CollectorMode.COLLECT, True, 0),
    # 增量: 跳过 7 天内已采的 URL · 高频跑省时间
    "增量采集": (CollectorMode.COLLECT, True, _DEFAULT_FRESH_HOURS),
    "增量": (CollectorMode.COLLECT, True, _DEFAULT_FRESH_HOURS),
    # 快速: 关 nav-expand · 不展开导航站
    "快速采集": (CollectorMode.COLLECT, False, 0),
    "速采": (CollectorMode.COLLECT, False, 0),
    # 搜新的
    "搜新的": (CollectorMode.SEARCH_NEW, True, 0),
    "重新搜索": (CollectorMode.SEARCH_NEW, True, 0),
    "全量搜索": (CollectorMode.SEARCH_NEW, True, 0),
    "深度搜新": (CollectorMode.SEARCH_NEW, True, 0),
    "快速搜新": (CollectorMode.SEARCH_NEW, False, 0),
    # 推送 / 帮助
    "推送给我": (CollectorMode.PUSH_ONLY, False, 0),
    "推一次": (CollectorMode.PUSH_ONLY, False, 0),
    "帮助": (CollectorMode.HELP, False, 0),
    "help": (CollectorMode.HELP, False, 0),
}

def _match_route(text: str) -> tuple[CollectorMode, bool, int] | None:
    """关键词匹配 · text 包含任一关键词即触发 (mode, nav_expand, skip_if_fresh_hours)。

    匹配按字典插入顺序 · 长 keyword 在前优先匹配(防「快速采集」被「采集」截胡)。
    """
    cleaned = text.strip().lower()
    # 长 keyword 优先(增量采集 / 快速采集 / 深度采集 比 采集 长 · 先匹配)
    for kw, route in sorted(
        _KEYWORD_ROUTE_MAP.items(), key=lambda kv: -len(kv[0])
    ):
        if kw.lower() in cleaned:
            return route
    return None


# 向后兼容: 旧测试用 _match_mode · 包一层
def _match_mode(text: str) -> CollectorMode | None:
    route = _match_route(text)
    return route[0] if route else None


def _resolve_bot_token() -> str | None:
    """从 env 读取本 bot 的 token · workflow 推回结果时用。"""
    return os.getenv("TG_BOT_TOKEN")


def _resolve_tavily_key() -> str | None:
    """搜新的模式需要 · 没配则 search_new 会跑 0 结果(activity 已处理)。"""
    return os.getenv("TAVILY_API_KEY")


def _resolve_tenant_id(tg_user_id: int, employee_user_id: str | None) -> str:
    """tenant_id 解析策略:

    优先级:
    1. binder 映射出的 employee_user_id (员工绑定后的真实租户)
    2. fallback: tg-{tg_user_id} (未绑定时用临时 tenant)

    MVP 阶段 path traversal 防御 (validate_tenant_id) 会校验 · 这里产出符合
    [A-Za-z0-9][A-Za-z0-9_-]{0,63} 的字符串。
    """
    if employee_user_id and employee_user_id.replace("-", "").replace("_", "").isalnum():
        return employee_user_id[:64]
    return f"tg-{tg_user_id}"


async def try_route(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    employee_user_id: str | None = None,
) -> bool:
    """匹配 → 启 workflow + ack 返 True;不匹配返 False。

    Args:
        update: TG Update 对象
        context: TG context (含 application bot_data)
        text: 用户消息原文
        employee_user_id: 已绑定员工 ID (上游 binder.resolve 出来的) · 没绑就传 None
    """
    route = _match_route(text)
    if route is None:
        return False
    mode, nav_expand, skip_if_fresh_hours = route

    message = update.message
    tg_user = update.effective_user
    chat = update.effective_chat
    if message is None or tg_user is None or chat is None:
        return False

    # help 不启 workflow · 直接回文案
    if mode == CollectorMode.HELP:
        await message.reply_text(_HELP_TEXT)
        return True

    bot_token = _resolve_bot_token()
    if not bot_token:
        await message.reply_text(
            "❌ TG_BOT_TOKEN 未配置 · 联系管理员"
        )
        _LOGGER.warning("scope=b.tg_router.no_token")
        return True

    tavily_key = _resolve_tavily_key()
    if mode == CollectorMode.SEARCH_NEW and not tavily_key:
        await message.reply_text(
            "❌ TAVILY_API_KEY 未配置 · 「搜新的」模式不可用 · 试试「采集」走缓存"
        )
        return True

    tenant_id = _resolve_tenant_id(tg_user.id, employee_user_id)

    # search_new 必须有 keywords · TG 用户没办法 inline 传词 → 自动套全量模板
    keywords: list[str] = []
    if mode == CollectorMode.SEARCH_NEW:
        keywords = kw_svc.resolve_keywords(tenant_id, _DEFAULT_KEYWORD_LIST_ID)
        if not keywords:
            await message.reply_text(
                "❌ 平台「全量」关键词模板缺失 · 联系管理员重 seed templates"
            )
            return True
    workflow_id = (
        f"site-collector-{tenant_id}-{chat.id}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    )

    try:
        client = await get_client()
        # collect / push_only 不需要 keywords · search_new 上面已解析默认全量
        # design note · user_id 优先 employee_user_id(已绑定真员工) · fallback tenant_id
        # (未绑 TG 用户 = 临时 tenant · personal owner=self 走通 5 闸)
        ctx = WorkflowContext(
            tenant_id=tenant_id,
            user_id=employee_user_id or tenant_id,
            tg_chat_id=chat.id,
            bot_token=bot_token,
            tavily_api_key=tavily_key,
            mode=mode,
            keywords=keywords,
            nav_expand=nav_expand,
            skip_if_fresh_hours=skip_if_fresh_hours,
        )
        handle = await client.start_workflow(
            SITE_COLLECTOR_WORKFLOW_NAME,
            ctx,
            id=workflow_id,
            task_queue=b_task_queue(),
        )
        _LOGGER.info(
            "scope=b.tg_router.start tenant=%s chat=%s mode=%s run_id=%s",
            tenant_id,
            chat.id,
            mode.value,
            handle.id,
        )
        depth = "深度(含 nav 展开)" if nav_expand else "快速(无 nav 展开)"
        increment = (
            f" · 增量({skip_if_fresh_hours}h 内已采的跳过)"
            if skip_if_fresh_hours > 0
            else ""
        )
        await message.reply_text(
            f"🚀 采集已启动 · 完成会自动推送到当前 chat\n"
            f"模式: {mode.value} · {depth}{increment}\n"
            f"run_id: {handle.id[:32]}…"
        )
    except Exception as exc:
        _LOGGER.exception(
            "scope=b.tg_router.fail tenant=%s mode=%s host=%s err=%s",
            tenant_id,
            mode.value,
            temporal_host(),
            exc,
        )
        await message.reply_text(
            f"❌ 启动失败 · workflow 引擎可能离线\n详情联系管理员 ({type(exc).__name__})"
        )
    return True
