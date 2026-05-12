"""站点采集核心逻辑（纯 requests + 正则 · 不走 AI）。

直接抓 HTML 提取联系方式（email / TG / Twitter / 微信 / QQ / 电话 / 表单链接）。
跟 Playwright 路径分开 —— Playwright 只在 nav-extractor 子站展开时用。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ── 联系方式正则 ──
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_TG_RE = re.compile(r"(?:t\.me/|telegram\.me/|@)([a-zA-Z][\w_]{4,31})")
_TWITTER_RE = re.compile(r"(?:twitter\.com/|x\.com/)([a-zA-Z][\w_]{1,15})")
_FB_RE = re.compile(r"facebook\.com/([a-zA-Z][\w.\-]{4,})")
_IG_RE = re.compile(r"instagram\.com/([a-zA-Z][\w.\-]{1,})")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s\-]?)?\(?\d{3,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{4}")
_QQ_RE = re.compile(r"\b(?:QQ|qq)[:\s\-]+(\d{5,11})\b")
_WECHAT_RE = re.compile(r"(?:微信|wechat|WeChat)[:\s\-]+([a-zA-Z][\w_-]{2,})")

_SCAN_PATHS = ["/", "/contact", "/contact-us", "/about", "/about-us", "/help"]
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _mask(contact_type: str, value: str) -> str:
    """脱敏：email/phone/qq/wechat 必脱敏，其他公开信息不脱敏。"""
    if contact_type == "email":
        if "@" not in value:
            return value
        local, domain = value.split("@", 1)
        if len(local) <= 2:
            return local[0] + "***@" + domain
        return f"{local[0]}***{local[-1]}@{domain}"
    if contact_type == "phone":
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 7:
            return digits[:3] + "****" + digits[-4:]
        return "****"
    if contact_type in ("qq", "wechat"):
        if len(value) <= 4:
            return value[:1] + "***"
        return value[:2] + "***" + value[-2:]
    return value


def _fetch_page(url: str, timeout: int = 12) -> tuple[str | None, str | int]:
    """返 (html, status)。状态可以是 int (HTTP code) 或 string (timeout/connection_error)。"""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=timeout,
            allow_redirects=True,
        )
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text, resp.status_code
    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.ConnectionError:
        return None, "connection_error"
    except Exception as exc:
        return None, str(exc)


def _extract_site_name(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            return soup.title.string.strip()[:120]
    except Exception:
        pass
    return ""


def _extract_contacts(html: str, source_page: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add(t: str, v: str) -> None:
        v = v.strip()
        key = (t, v.lower())
        if key in seen or not v:
            return
        seen.add(key)
        out.append(
            {
                "type": t,
                "value": v,
                "value_safe": _mask(t, v),
                "source_page": source_page,
            }
        )

    for m in _EMAIL_RE.finditer(html):
        _add("email", m.group(0).lower())
    for m in _TG_RE.finditer(html):
        h = m.group(1)
        if h.lower() not in {"share", "joinchat", "addstickers"}:
            _add("telegram", f"@{h}")
    for m in _TWITTER_RE.finditer(html):
        _add("twitter", f"@{m.group(1)}")
    for m in _FB_RE.finditer(html):
        _add("facebook", m.group(0))
    for m in _IG_RE.finditer(html):
        _add("instagram", f"@{m.group(1)}")
    for m in _QQ_RE.finditer(html):
        _add("qq", m.group(1))
    for m in _WECHAT_RE.finditer(html):
        _add("wechat", m.group(1))

    return out


def collect_one_site(site_url: str, timeout: int = 12) -> dict[str, Any]:
    """采单站 · 返 dict (符合 SiteResult schema)。同步函数 · activity 用 to_thread 调。"""
    contacts: list[dict[str, Any]] = []
    site_name = ""
    any_success = False
    last_error = ""

    for path in _SCAN_PATHS:
        page_url = site_url.rstrip("/") + path
        html, status = _fetch_page(page_url, timeout=timeout)
        if html is None:
            last_error = str(status)
            continue
        if isinstance(status, int) and status >= 400:
            last_error = f"HTTP {status}"
            continue
        any_success = True
        if path == "/" and not site_name:
            site_name = _extract_site_name(html)
        for c in _extract_contacts(html, page_url):
            if (c["type"], c["value"].lower()) not in {
                (x["type"], x["value"].lower()) for x in contacts
            }:
                contacts.append(c)

    if not any_success:
        return {
            "site_url": site_url,
            "site_name": "",
            "status": "failed",
            "contacts": [],
            "failure_reason": f"all paths unreachable: {last_error}",
        }
    if not contacts:
        return {
            "site_url": site_url,
            "site_name": site_name,
            "status": "partial",
            "contacts": [],
            "failure_reason": "page reachable but no contacts extracted",
        }
    return {
        "site_url": site_url,
        "site_name": site_name,
        "status": "success",
        "contacts": contacts,
        "failure_reason": "",
    }


def base_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""
