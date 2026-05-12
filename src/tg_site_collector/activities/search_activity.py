"""关键词搜索 activity · Tavily API。

每批 N 个关键词,搜完直接推 TG 增量进度,return URL 列表给 workflow 累加。
配额耗尽时 raise TavilyQuotaExhausted (non-retryable),workflow 跳过后续批次。
另外:配额接近上限(80%)时主动推 TG 预警一次。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from temporalio import activity

from tg_site_collector.services.tg_client import TGClient, redact_tg_url
from tg_site_collector.types import validate_tenant_id

# Tavily 免费 tier 1000/month · 平台 tier 4000/month
# 通过 env 覆盖:TAVILY_QUOTA_TOTAL / TAVILY_QUOTA_WARN_AT
_QUOTA_TOTAL = int(os.getenv("TAVILY_QUOTA_TOTAL", "1000"))
_QUOTA_WARN_AT = int(os.getenv("TAVILY_QUOTA_WARN_AT", "800"))  # 80% 提醒
_USAGE_DIR = Path(os.getenv("B_DATA_DIR", "/tmp/tg-site-collector/b-data")).resolve()


def _usage_file(tenant_id: str) -> Path:
    validate_tenant_id(tenant_id)
    p = (_USAGE_DIR / tenant_id / "tavily-usage.json").resolve()
    parent = p.parent
    if _USAGE_DIR not in parent.parents and parent != _USAGE_DIR:
        raise ValueError(f"usage path escapes data root: {p}")
    parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_usage(tenant_id: str) -> dict[str, Any]:
    p = _usage_file(tenant_id)
    if not p.exists():
        return {"month": "", "count": 0, "warned": False, "exhausted_warned": False}
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        # 老格式补默认字段
        data.setdefault("exhausted_warned", False)
        return data
    except json.JSONDecodeError:
        return {"month": "", "count": 0, "warned": False, "exhausted_warned": False}


def _write_usage(tenant_id: str, data: dict[str, Any]) -> None:
    _usage_file(tenant_id).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _bump_usage(tenant_id: str, n: int = 1) -> dict[str, Any]:
    """每次 Tavily 成功调用 +n · 跨月份自动重置。返当前 usage state。"""
    cur_month = datetime.utcnow().strftime("%Y-%m")
    state = _read_usage(tenant_id)
    if state.get("month") != cur_month:
        state = {
            "month": cur_month,
            "count": 0,
            "warned": False,
            "exhausted_warned": False,
        }
    state["count"] = state.get("count", 0) + n
    _write_usage(tenant_id, state)
    return state


class TavilyQuotaExhausted(Exception):
    """Tavily 402 / "exceeds usage limit" · workflow 用 non_retryable_error_types 捕获。"""


async def _push_exhausted_once(
    tenant_id: str,
    bot_token: str,
    tg_chat_id: int,
    batch_idx: int,
    batch_total: int,
) -> None:
    """配额耗尽时推一次 TG · 每月只推一次(用 state.exhausted_warned 去重)。"""
    state = _read_usage(tenant_id)
    if state.get("exhausted_warned"):
        return
    remaining_batches = max(0, batch_total - batch_idx - 1)
    try:
        tg = TGClient(bot_token)
        await tg.send_message(
            chat_id=tg_chat_id,
            text=(
                f"❌ Tavily 配额耗尽 · 本月已用 {state.get('count', 0)}/{_QUOTA_TOTAL}\n"
                f"剩 {remaining_batches} 批关键词跳过 · "
                f"充值后跑 search_new 续"
            ),
        )
    except Exception:
        pass
    state["exhausted_warned"] = True
    _write_usage(tenant_id, state)


_EXCLUDE_DOMAIN_PREFIXES = (
    "google.",
    "bing.",
    "baidu.",
    "wikipedia.",
    "youtube.",
    "twitter.",
    "facebook.",
    "github.",
)


def _looks_excluded(url: str) -> bool:
    lower = url.lower()
    return any(
        f"//{pfx}" in lower or f".{pfx}" in lower
        for pfx in _EXCLUDE_DOMAIN_PREFIXES
    )


@activity.defn(name="search_keywords_batch")
async def search_keywords_batch(
    tenant_id: str,
    tg_chat_id: int,
    bot_token: str,
    tavily_key: str | None,
    keywords: list[str],
    batch_idx: int,
    batch_total: int,
    max_per_keyword: int = 10,
) -> list[str]:
    """搜一批 N 个关键词 · 推 TG 进度 · return 去重后的 URL list。"""
    activity.logger.info(
        f"[{tenant_id}] kw-batch {batch_idx + 1}/{batch_total} ({len(keywords)} 个词)"
    )

    if not tavily_key:
        activity.logger.warning(f"[{tenant_id}] no tavily key · skip")
        return []

    found: set[str] = set()
    async with httpx.AsyncClient(timeout=30) as client:
        for kw in keywords:
            activity.heartbeat({"current_keyword": kw, "found": len(found)})
            try:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": tavily_key,
                        "query": kw,
                        "max_results": max_per_keyword,
                    },
                )
                if resp.status_code in (401, 402, 403, 429):
                    await _push_exhausted_once(
                        tenant_id, bot_token, tg_chat_id, batch_idx, batch_total
                    )
                    raise TavilyQuotaExhausted(
                        f"HTTP {resp.status_code} · word={kw}"
                    )
                data = resp.json()
                if "error" in data or "detail" in data:
                    err = str(data.get("error") or data.get("detail"))
                    if any(k in err.lower() for k in ("usage limit", "quota", "exceeds")):
                        await _push_exhausted_once(
                            tenant_id, bot_token, tg_chat_id, batch_idx, batch_total
                        )
                        raise TavilyQuotaExhausted(err)
                    activity.logger.warning(f"tavily err [{kw}]: {err}")
                    continue
                # 成功一次计数 +1
                state = _bump_usage(tenant_id, 1)
                # 接近上限时推一次预警(每月只推一次)
                if (
                    not state.get("warned")
                    and state.get("count", 0) >= _QUOTA_WARN_AT
                ):
                    try:
                        tg = TGClient(bot_token)
                        await tg.send_message(
                            chat_id=tg_chat_id,
                            text=(
                                f"⚠️ Tavily 配额已用 {state['count']}/{_QUOTA_TOTAL}"
                                f"({int(state['count'] * 100 / _QUOTA_TOTAL)}%)"
                                f" · 剩 {max(0, _QUOTA_TOTAL - state['count'])} 次\n"
                                f"建议:\n"
                                f"• 充值或换 key(tavily.com/dashboard)\n"
                                f"• 暂时只用「采集」走缓存,别再发「搜新的」"
                            ),
                        )
                    except Exception:
                        pass
                    state["warned"] = True
                    _write_usage(tenant_id, state)

                for r in data.get("results", []):
                    url = r.get("url", "")
                    if url.startswith("http") and not _looks_excluded(url):
                        found.add(url)
            except TavilyQuotaExhausted:
                raise
            except asyncio.CancelledError:
                activity.logger.warning(f"cancel during kw [{kw}]")
                raise
            except Exception as exc:
                activity.logger.warning(f"tavily fail [{kw}]: {exc}")
                continue

    urls = sorted(found)

    # 推 TG 增量进度
    try:
        tg = TGClient(bot_token)
        await tg.send_message(
            chat_id=tg_chat_id,
            text=(
                f"🔍 关键词进度 {batch_idx + 1}/{batch_total} · "
                f"本组 {len(keywords)} 词 → 发现 {len(urls)} 个 URL"
            ),
        )
    except Exception as exc:
        activity.logger.warning(
            f"tg push fail (non-fatal): {redact_tg_url(str(exc))}"
        )

    return urls
