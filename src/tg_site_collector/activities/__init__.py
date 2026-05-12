"""B 模块 activities · Temporal Activity 集合。

每个 activity 必须能独立 retry / heartbeat / cancel。所有副作用（IO / requests / Playwright）
只能在 activity 里发生 · workflow 自己保持确定性。
"""

from tg_site_collector.activities.audit_activity import o_log_event
from tg_site_collector.activities.auth_activity import a_m2_check
from tg_site_collector.activities.collect_activity import collect_sites_batch
from tg_site_collector.activities.data_io_activity import (
    io_get_recent_urls,
    io_persist_history,
    io_persist_partial,
    io_read_url_cache,
    io_write_url_cache,
)
from tg_site_collector.activities.guard_activity import s_evaluate
from tg_site_collector.activities.nav_extract_activity import nav_expand_batch
from tg_site_collector.activities.search_activity import (
    TavilyQuotaExhausted,
    search_keywords_batch,
)
from tg_site_collector.activities.tg_activity import (
    tg_notify_cancelled,
    tg_send_progress,
    tg_send_summary,
)

__all__ = [
    "TavilyQuotaExhausted",
    "a_m2_check",
    "collect_sites_batch",
    "io_get_recent_urls",
    "io_persist_history",
    "io_persist_partial",
    "io_read_url_cache",
    "io_write_url_cache",
    "nav_expand_batch",
    "o_log_event",
    "s_evaluate",
    "search_keywords_batch",
    "tg_notify_cancelled",
    "tg_send_progress",
    "tg_send_summary",
]
