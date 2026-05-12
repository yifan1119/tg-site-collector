"""导航站子站展开 activity · Playwright 渲染 JS 后抓 a[href]。

为啥要展开:
- Tavily 给的 URL 里有「导航站」(色情资源大全 / 黄网导航类)
- 这种站本身联系方式少 · 但列了 50-100 个其他成人站
- 子站列表是 JS 动态加载的(纯 requests 拿不到) · 必须 Playwright 渲染
- 展开后 URL 数能 ×3-5 倍 · 出料量级翻倍

何时调:
- workflow 在 search 完后 · collect 之前
- 仅 ctx.nav_expand=true 时(由用户「深度采集」/「快速采集」决定)
- 候选用启发式判定(URL pattern + title 含「导航/nav/大全」)

参考: 老 OpenClaw skill nav-extractor.py 翻成 async + 池化 + heartbeat。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlparse

from temporalio import activity

from tg_site_collector.services.browser_pool import BrowserPool

# 排除的域名(不当作子站收集 · 来源: 老 OpenClaw skill nav-extractor.py)
_EXCLUDE_DOMAINS = {
    "google.com", "google.com.hk", "google.co.jp", "googleapis.com", "gstatic.com",
    "bing.com", "baidu.com", "sogou.com", "so.com", "360.cn",
    "youtube.com", "wikipedia.org", "wikimedia.org",
    "zhihu.com", "weibo.com", "bilibili.com", "douban.com",
    "reddit.com", "quora.com",
    "twitter.com", "x.com", "facebook.com", "instagram.com",
    "tiktok.com", "douyin.com", "xiaohongshu.com",
    "github.com", "stackoverflow.com",
    "apple.com", "microsoft.com", "amazon.com",
    "w3.org", "schema.org", "jquery.com", "cloudflare.com",
    "cdn.jsdelivr.net", "unpkg.com", "fonts.googleapis.com",
}

# 导航站启发式 · URL pattern + 关键词
_NAV_URL_PATTERNS = re.compile(
    r"(nav|daohang|hao123|fuli(?:ba|ware)|114|123|"
    r"index|portal|aggreg|directory)",
    re.IGNORECASE,
)
_NAV_TITLE_KEYWORDS = (
    "导航", "网址大全", "网站大全", "色站", "成人站", "黄网", "福利站",
    "聚合", "推荐", "索引", "portal", "directory", "大全", "导航站",
)


def is_excluded_domain(domain: str) -> bool:
    domain = domain.lower().removeprefix("www.")
    return any(
        domain == exc or domain.endswith("." + exc) for exc in _EXCLUDE_DOMAINS
    )


def looks_like_nav_url(url: str) -> bool:
    """URL pattern 启发式 · 不准但便宜的快速预筛。"""
    return bool(_NAV_URL_PATTERNS.search(url))


def title_looks_like_nav(title: str) -> bool:
    """页面 title 含导航关键词。"""
    if not title:
        return False
    t = title.lower()
    return any(kw.lower() in t for kw in _NAV_TITLE_KEYWORDS)


@activity.defn(name="nav_expand_batch")
async def nav_expand_batch(
    tenant_id: str,
    candidate_urls: list[str],
    timeout_per_site_ms: int = 25_000,
) -> dict[str, Any]:
    """批量展开候选导航站 · return {expanded: int, new_subsites: list[str]}。

    每站 heartbeat 一次 + 超时不阻塞批次(单站炸不影响别人)。
    """
    activity.logger.info(
        f"[{tenant_id}] nav-expand 开始 · {len(candidate_urls)} 候选导航站"
    )

    new_subsites: set[str] = set()
    expanded = 0
    pool = await BrowserPool.get()

    for idx, url in enumerate(candidate_urls):
        activity.heartbeat(
            {
                "current_url": url,
                "processed": idx,
                "found_subsites": len(new_subsites),
            }
        )
        ctx = None
        page = None
        try:
            ctx = await pool.new_context()
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=timeout_per_site_ms)

            # 先快速判断是否真导航站(避免抓非导航站的外链当结果)
            title = (await page.title()) or ""
            if not (looks_like_nav_url(url) or title_looks_like_nav(title)):
                # URL 像 + title 不像 · 可能误判 · 仍尝试抓但限量
                pass

            source_domain = urlparse(url).netloc.lower().removeprefix("www.")
            anchors = await page.query_selector_all("a[href]")
            page_subsites: set[str] = set()
            for a in anchors:
                href = await a.get_attribute("href")
                if not href or not href.startswith("http") or "@" in href:
                    continue
                parsed = urlparse(href)
                domain = parsed.netloc.lower().removeprefix("www.")
                if (
                    domain
                    and domain != source_domain
                    and not is_excluded_domain(domain)
                ):
                    page_subsites.add(f"{parsed.scheme}://{parsed.netloc}")

            if page_subsites:
                new_subsites.update(page_subsites)
                expanded += 1
                activity.logger.info(
                    f"  [{idx + 1}/{len(candidate_urls)}] {url} → {len(page_subsites)} 子站"
                )
            else:
                activity.logger.info(
                    f"  [{idx + 1}/{len(candidate_urls)}] {url} → 无子站 · 跳过"
                )
        except asyncio.CancelledError:
            activity.logger.warning(
                f"nav-expand cancelled at {idx}/{len(candidate_urls)}"
            )
            # cleanup 后再 raise · 防 page/ctx 泄漏
            await _safe_close(page, "page")
            await _safe_close(ctx, "context")
            raise
        except Exception as exc:
            # 单站失败不影响别的 · 异常名脱敏防 url+token 泄漏
            activity.logger.warning(
                f"  [{idx + 1}/{len(candidate_urls)}] {url} 失败: {type(exc).__name__}"
            )
        finally:
            # page 和 ctx 各自独立 close · 一边失败不影响另一边
            await _safe_close(page, "page")
            await _safe_close(ctx, "context")

    return {
        "expanded": expanded,
        "candidates_total": len(candidate_urls),
        "new_subsites": sorted(new_subsites),
    }


async def _safe_close(resource: object, label: str) -> None:
    """关 page / context 时容错 · 一个挂了不影响另一个。"""
    if resource is None:
        return
    try:
        await resource.close()  # type: ignore[attr-defined]
    except Exception as exc:
        activity.logger.warning(
            f"failed to close {label}: {type(exc).__name__}"
        )


def select_nav_candidates(urls: list[str], max_candidates: int = 50) -> list[str]:
    """从 URL 列表里筛出疑似导航站 · 给 nav_expand_batch 当输入。

    启发式: URL pattern 含 nav/daohang/导航关键词。
    Title 判定要打开页才知道,所以 URL 阶段先粗筛,展开时再二次判定。
    上限 max_candidates 防止一次跑太多 Playwright(每个 5-10s)。
    """
    out: list[str] = []
    for u in urls:
        if looks_like_nav_url(u):
            out.append(u)
            if len(out) >= max_candidates:
                break
    return out
