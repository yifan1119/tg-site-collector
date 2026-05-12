"""数据 IO activity · 读写每租户独立目录的 url-cache / history。

MVP 路径：./data/b-data/{tenant_id}/{url-cache.txt,history.json}
可通过 env B_DATA_DIR 覆盖（本地测试用 /tmp/...）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from temporalio import activity

from tg_site_collector.types import validate_tenant_id

# 同租户并发写锁(防 history.json 后写覆盖前写)
# MVP 单 worker 内存锁 · 多 worker 才需要分布式锁
_tenant_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# URL 追踪参数前缀(query 里出现这些前缀的 key 全删)
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "msclkid", "ref", "_ga")


def _normalize_url(url: str) -> str:
    """删追踪参数 + rstrip / · 用于 history 去重 / skip_if_fresh 比对。

    例:
      https://x.com/?utm_source=fb&p=1 → https://x.com?p=1
      https://x.com/?utm_source=fb     → https://x.com
      https://x.com/                    → https://x.com
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return url.rstrip("/")
    # 过滤 query 里的追踪参数
    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(k.lower().startswith(pfx) for pfx in _TRACKING_PREFIXES)
    ]
    new_query = urlencode(kept)
    rebuilt = urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
    )
    return rebuilt.rstrip("/")


def _now_aware() -> datetime:
    """tz-aware UTC now · 跨 fromisoformat 比较时不会触发 naive vs aware TypeError。"""
    return datetime.now(UTC)


def _now_iso() -> str:
    """ISO timestamp · 显式带 +00:00 后缀 · fromisoformat 解析后是 aware datetime。"""
    return _now_aware().isoformat()


def _parse_iso_aware(s: str) -> datetime | None:
    """解析 history 里的 last_fetched_at · 兼容老格式(带 Z 后缀的 naive)。

    Python 3.11+ fromisoformat 支持 Z · 但 3.10 不支持 → 兜底替换。
    返 None 表示解析失败 · 调用者算"未采过"。
    """
    if not s:
        return None
    s = str(s).strip()
    # Z → +00:00 让所有版本都能解析
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # 老格式(无时区)假定 UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt

_DATA_ROOT = Path(os.getenv("B_DATA_DIR", "/tmp/tg-site-collector/b-data")).resolve()


def _tenant_dir(tenant_id: str) -> Path:
    validate_tenant_id(tenant_id)
    p = (_DATA_ROOT / tenant_id).resolve()
    # 防御式二次校验:就算 validate 漏了也不能越出 _DATA_ROOT
    if _DATA_ROOT not in p.parents and p != _DATA_ROOT:
        raise ValueError(f"tenant path escapes data root: {p}")
    p.mkdir(parents=True, exist_ok=True)
    return p


@activity.defn(name="io_read_url_cache")
async def io_read_url_cache(tenant_id: str) -> list[str]:
    """读 url-cache.txt。不存在返 []。"""
    p = _tenant_dir(tenant_id) / "url-cache.txt"
    if not p.exists():
        return []
    urls = [
        line.strip()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("http")
    ]
    activity.logger.info(f"[{tenant_id}] cache loaded: {len(urls)} urls")
    return urls


@activity.defn(name="io_write_url_cache")
async def io_write_url_cache(tenant_id: str, new_urls: list[str]) -> int:
    """追加 URL 到 cache（去重）· 返追加数。"""
    async with _tenant_locks[tenant_id]:
        p = _tenant_dir(tenant_id) / "url-cache.txt"
        existing = set()
        if p.exists():
            existing = {
                line.strip()
                for line in p.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        added = [u for u in new_urls if u not in existing]
        if added:
            with p.open("a", encoding="utf-8") as f:
                for u in added:
                    f.write(u + "\n")
        return len(added)


@activity.defn(name="io_persist_history")
async def io_persist_history(tenant_id: str, batch_results: list[dict[str, Any]]) -> int:
    """合并新批次到 history.json · 按 normalize 后 site_url 去重 · 返累计 site 数。

    history.json 格式: List[SiteResult dict]
    并发安全:同租户加锁(防 workflow 同时跑 N 次后写覆盖前写)。
    URL normalize:删 utm_/fbclid/gclid/msclkid/ref/_ga 追踪参数(防同站重复)。
    """
    async with _tenant_locks[tenant_id]:
        p = _tenant_dir(tenant_id) / "history.json"
        history: list[dict[str, Any]] = []
        if p.exists():
            try:
                history = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                activity.logger.warning(f"[{tenant_id}] history.json corrupt · 重建")
                history = []

        by_url = {_normalize_url(s.get("site_url", "")): s for s in history}
        fetched_ts = _now_iso()
        for site in batch_results:
            url = _normalize_url(site.get("site_url", ""))
            if not url:
                continue
            if url in by_url:
                existing_keys = {
                    (c["type"], c["value"].lower())
                    for c in by_url[url].get("contacts", [])
                }
                for c in site.get("contacts", []):
                    if (c["type"], c["value"].lower()) not in existing_keys:
                        by_url[url].setdefault("contacts", []).append(c)
                if site.get("status") == "success":
                    by_url[url]["status"] = "success"
                by_url[url]["last_fetched_at"] = fetched_ts
            else:
                site["last_fetched_at"] = fetched_ts
                by_url[url] = site

        p.write_text(
            json.dumps(list(by_url.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return len(by_url)


@activity.defn(name="io_get_recent_urls")
async def io_get_recent_urls(tenant_id: str, hours: int) -> list[str]:
    """读 history.json · 返最近 N 小时内 last_fetched_at 之后的 site_url 列表(已 normalize)。

    hours <= 0 时返 [] (= 不跳过任何 URL · 全量采)。
    损坏 / 缺字段 / 时间格式不对的条目算 "未采过" · 不进列表 · 走重抓路径(更安全)。
    返回 URL 已经过 normalize(去 utm_ 等追踪参数) · 调用者也要 normalize 后比对。
    """
    if hours <= 0:
        return []
    p = _tenant_dir(tenant_id) / "history.json"
    if not p.exists():
        return []
    try:
        history = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    cutoff = _now_aware() - timedelta(hours=hours)
    recent: list[str] = []
    for site in history:
        last_dt = _parse_iso_aware(site.get("last_fetched_at"))
        if last_dt is None:
            continue
        if last_dt > cutoff:
            url = _normalize_url(site.get("site_url", ""))
            if url:
                recent.append(url)
    return recent


def _cleanup_old_runs(tenant_id: str, max_age_days: int = 7) -> int:
    """清 runs/ 下 N 天前的子目录 · 防 partial.json 累积撑爆磁盘。

    Piggyback 在 io_persist_partial 后调一次 · 不另起 cron。
    返删的目录数(给 log 用)。
    """
    runs_dir = _tenant_dir(tenant_id) / "runs"
    if not runs_dir.exists():
        return 0
    cutoff_ts = time.time() - max_age_days * 86400
    removed = 0
    try:
        for sub in runs_dir.iterdir():
            if not sub.is_dir():
                continue
            try:
                mtime = sub.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff_ts:
                shutil.rmtree(sub, ignore_errors=True)
                removed += 1
    except OSError:
        return removed
    if removed:
        activity.logger.info(
            f"[{tenant_id}] cleaned {removed} run dirs older than {max_age_days}d"
        )
    return removed


@activity.defn(name="io_persist_partial")
async def io_persist_partial(
    tenant_id: str, run_id: str, state: dict[str, Any]
) -> str:
    """workflow cancel 时 · 写 partial 状态用于事后追溯。

    并发安全:同租户加锁。Piggyback 清 7 天前 runs/(防累积)。
    """
    async with _tenant_locks[tenant_id]:
        p = _tenant_dir(tenant_id) / "runs" / run_id / "partial.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        _cleanup_old_runs(tenant_id)
        return str(p)
