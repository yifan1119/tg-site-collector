"""B 模块 worker 进程入口 · 跑 SiteCollectorWorkflow + 全部 activities。

启动方式:
    uv run python -m tg_site_collector.workers.site_collector_worker
或:
    uv run python src/agent_center/b_module/workers/site_collector_worker.py

环境变量:
    TEMPORAL_HOST          (default: localhost:7233)
    TEMPORAL_NAMESPACE     (default: default)
    B_TASK_QUEUE           (default: site-collector)
    B_DATA_DIR             (default: /tmp/tg-site-collector/b-data)
    B_AUDIT_DIR            (default: /tmp/tg-site-collector/b-audit)
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from tg_site_collector.activities import (
    a_m2_check,
    collect_sites_batch,
    io_get_recent_urls,
    io_persist_history,
    io_persist_partial,
    io_read_url_cache,
    io_write_url_cache,
    nav_expand_batch,
    o_log_event,
    s_evaluate,
    search_keywords_batch,
    tg_notify_cancelled,
    tg_send_progress,
    tg_send_summary,
)
from tg_site_collector.workflows import SiteCollectorWorkflow

_LOG = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # httpx INFO 默认 log 完整 URL · 我们调 TG api 时 URL 含 bot token
    # 把 httpx 抬到 WARNING 防 token 被记录
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    host = os.getenv("TEMPORAL_HOST", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    task_queue = os.getenv("B_TASK_QUEUE", "site-collector")

    _LOG.info("scope=b.worker.start host=%s ns=%s tq=%s", host, namespace, task_queue)

    client = await Client.connect(host, namespace=namespace)

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[SiteCollectorWorkflow],
        activities=[
            a_m2_check,
            s_evaluate,
            o_log_event,
            io_read_url_cache,
            io_write_url_cache,
            io_persist_history,
            io_persist_partial,
            io_get_recent_urls,
            search_keywords_batch,
            nav_expand_batch,
            collect_sites_batch,
            tg_send_progress,
            tg_send_summary,
            tg_notify_cancelled,
        ],
        max_concurrent_activities=20,
        max_concurrent_workflow_tasks=10,
    )
    _LOG.info("scope=b.worker.ready waiting tasks...")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
