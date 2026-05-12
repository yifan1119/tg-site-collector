"""站点采集 activity · 200 站一批 · 完成立即推 TG batch JSON。

并发抓站点用 ThreadPoolExecutor（requests 是 sync）· 每 5 站 heartbeat。
取消时落 partial JSON 兜底。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from temporalio import activity

from tg_site_collector.services.collector_core import collect_one_site
from tg_site_collector.services.tg_client import TGClient, redact_tg_url
from tg_site_collector.types import validate_tenant_id

_DATA_ROOT = Path(os.getenv("B_DATA_DIR", "/tmp/tg-site-collector/b-data")).resolve()
# run_id 由 Temporal 自己生成 (UUID),没注入风险。但仍 normalize 一下。
_SAFE_RUN_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_\-]{1,128}$")


def _batch_path(tenant_id: str, run_id: str | None, batch_idx: int, *, partial: bool = False) -> Path:
    validate_tenant_id(tenant_id)
    if run_id is None or not _SAFE_RUN_ID_RE.match(run_id):
        raise ValueError(f"unsafe run_id: {run_id!r}")
    suffix = "-partial" if partial else ""
    p = (
        _DATA_ROOT
        / tenant_id
        / "runs"
        / run_id
        / f"站点采集-{batch_idx + 1:03d}{suffix}.json"
    ).resolve()
    if _DATA_ROOT not in p.parents:
        raise ValueError(f"batch path escapes data root: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _flush(tenant_id: str, run_id: str | None, batch_idx: int, results: list[dict[str, Any]], *, partial: bool = False) -> Path:
    p = _batch_path(tenant_id, run_id, batch_idx, partial=partial)
    p.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


@activity.defn(name="collect_sites_batch")
async def collect_sites_batch(
    tenant_id: str,
    tg_chat_id: int,
    bot_token: str,
    urls: list[str],
    batch_idx: int,
    batch_total: int,
    workers: int = 15,
    timeout_per_site: int = 12,
) -> dict[str, Any]:
    """采 N 站 · return {"site_count","contact_count","sites","batch_path"}。"""
    info = activity.info()
    activity.logger.info(
        f"[{tenant_id}] site-batch {batch_idx + 1}/{batch_total} ({len(urls)} 站) "
        f"workers={workers} timeout={timeout_per_site}s"
    )

    results: list[dict[str, Any]] = []
    contact_count = 0

    loop = asyncio.get_event_loop()

    # 共享进度计数器(thread-safe through GIL · int dict 原子读)
    progress = {"done": 0, "contacts": 0}

    def _run_pool() -> tuple[list[dict[str, Any]], int]:
        local_results: list[dict[str, Any]] = []
        local_contacts = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(collect_one_site, url, timeout_per_site): url for url in urls
            }
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    site = fut.result()
                except Exception as exc:
                    site = {
                        "site_url": url,
                        "site_name": "",
                        "status": "failed",
                        "contacts": [],
                        "failure_reason": f"thread err: {type(exc).__name__}",
                    }
                # 更新共享进度(主 async 线程定期读并 heartbeat)
                progress["done"] = progress["done"] + 1
                if site["contacts"]:
                    local_results.append(site)
                    local_contacts += len(site["contacts"])
                    progress["contacts"] = progress["contacts"] + len(site["contacts"])
        return local_results, local_contacts

    # run_in_executor 返 asyncio.Future · 直接用,不要再 wrap create_task
    pool_future = loop.run_in_executor(None, _run_pool)

    # 主 async 线程边等 pool 边发 heartbeat · 防 heartbeat_timeout 触发重试
    async def _heartbeat_loop() -> None:
        while not pool_future.done():
            activity.heartbeat(
                {
                    "processed": progress["done"],
                    "total": len(urls),
                    "found_contacts": progress["contacts"],
                }
            )
            try:
                await asyncio.sleep(15)  # 每 15s 心跳 · 远低于 heartbeat_timeout
            except asyncio.CancelledError:
                break

    hb_task = asyncio.create_task(_heartbeat_loop())

    try:
        results, contact_count = await pool_future
    except asyncio.CancelledError:
        pool_future.cancel()
        activity.logger.warning(f"cancel batch {batch_idx + 1}, flush partial")
        _flush(tenant_id, info.workflow_run_id, batch_idx, results, partial=True)
        raise
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task

    # 最终 heartbeat(让外部知道这批结束)
    activity.heartbeat(
        {
            "processed": progress["done"],
            "total": len(urls),
            "found_contacts": contact_count,
        }
    )

    # 写 batch JSON
    batch_path = _flush(tenant_id, info.workflow_run_id, batch_idx, results)

    # 推 TG (失败不阻塞 workflow · 异常脱敏 url 防 token 泄漏)
    try:
        tg = TGClient(bot_token)
        await tg.send_document(
            chat_id=tg_chat_id,
            document_path=batch_path,
            caption=(
                f"📦 批次 {batch_idx + 1}/{batch_total} 完成\n"
                f"站点: {len(urls)} · 出料: {len(results)} · 联系方式: {contact_count} 条"
            ),
        )
    except Exception as exc:
        activity.logger.warning(
            f"tg push fail (non-fatal): {redact_tg_url(str(exc))}"
        )

    return {
        "batch_index": batch_idx,
        "site_count": len(results),
        "contact_count": contact_count,
        "sites": results,
        "batch_path": str(batch_path),
    }
