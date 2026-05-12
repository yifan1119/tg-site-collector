"""TG 终态推送 activity · 启动 / 完成 / 取消 三类消息。"""

from __future__ import annotations

from typing import Any

from temporalio import activity

from tg_site_collector.services.tg_client import TGClient


@activity.defn(name="tg_send_progress")
async def tg_send_progress(bot_token: str, tg_chat_id: int, text: str) -> dict[str, Any]:
    tg = TGClient(bot_token)
    return await tg.send_message(chat_id=tg_chat_id, text=text)


@activity.defn(name="tg_send_summary")
async def tg_send_summary(
    bot_token: str,
    tg_chat_id: int,
    summary: dict[str, Any],
    history_path: str | None = None,
) -> dict[str, Any]:
    tg = TGClient(bot_token)
    quota_hit = summary.get("tavily_quota_hit", False)
    quota_line = "\n⚠️ Tavily 配额已耗尽,本次只搜了部分关键词" if quota_hit else ""
    text = (
        "✅ 站点采集完成\n"
        "━━━━━━━━━━\n"
        f"🔍 关键词批次: {summary.get('keyword_batches_done', 0)}/{summary.get('keyword_batches_total', 0)}\n"
        f"🆕 新发现 URL: {summary.get('new_urls_found', 0)}\n"
        f"📦 站点采集: {summary.get('sites_collected', 0)} 个 (批次 {summary.get('site_batches_done', 0)}/{summary.get('site_batches_total', 0)})\n"
        f"📇 联系方式: {summary.get('contacts_extracted', 0)} 条\n"
        f"⏱ 耗时: {summary.get('duration_sec', 0)} 秒"
        f"{quota_line}"
    )
    out = await tg.send_message(chat_id=tg_chat_id, text=text)

    if history_path:
        try:
            await tg.send_document(
                chat_id=tg_chat_id,
                document_path=history_path,
                caption="📎 完整 history.json (累计所有采集)",
            )
        except Exception as exc:
            activity.logger.warning(f"send history doc fail: {exc}")

    return out


@activity.defn(name="tg_notify_cancelled")
async def tg_notify_cancelled(bot_token: str, tg_chat_id: int, run_id: str) -> dict[str, Any]:
    tg = TGClient(bot_token)
    return await tg.send_message(
        chat_id=tg_chat_id,
        text=(
            f"⚠️ 采集已中止 (run={run_id[:12]})\n"
            f"已落盘的 batch JSON 都保留 · history.json 不丢"
        ),
    )
