"""Telegram Bot HTTP API 简易封装 · 走 sendMessage / sendDocument。

故意不复用 telegram_bot/ 那套 python-telegram-bot Application —— 那个是 long-polling
模式，跟 workflow 内推送场景不匹配。这里直接 raw HTTP，无依赖。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import httpx

_LOG = logging.getLogger(__name__)

# bot token 正则 · log 时把 /bot{token}/ 替换成 /bot***/
# 避免 httpx 异常 / log 把 token 写到容器日志里
_TG_TOKEN_RE = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+/")


def redact_tg_url(text: str) -> str:
    """脱敏 url 里的 bot token · 给 log / exception 用。"""
    return _TG_TOKEN_RE.sub("/bot***/", text)


class TGClientError(Exception):
    """TG API 调用失败 · 不携带原 url(防 token 泄漏)。"""


class TGClient:
    def __init__(self, bot_token: str, *, timeout: float = 30.0) -> None:
        self._token = bot_token
        self._timeout = timeout

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    async def send_message(self, *, chat_id: int, text: str) -> dict[str, Any]:
        # TG sendMessage 文本上限 4096 · 截断
        if len(text) > 4000:
            text = text[:4000] + "\n…(已截断)"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                self._url("sendMessage"),
                data={"chat_id": chat_id, "text": text},
            )
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError:
                # 抹掉 url 里的 token 再 raise
                raise TGClientError(
                    f"sendMessage failed: HTTP {r.status_code} "
                    f"body={redact_tg_url(r.text[:200])}"
                ) from None
            data: dict[str, Any] = r.json()
            if not data.get("ok"):
                _LOG.warning("scope=b.tg.sendMessage.fail body=%s", data)
            return data

    async def send_document(
        self, *, chat_id: int, document_path: str | Path, caption: str = ""
    ) -> dict[str, Any]:
        path = Path(document_path)
        if not path.exists():
            raise FileNotFoundError(f"document not found: {path}")
        # Path.open 是同步上下文 · 单独 with 不嵌进 async with
        with path.open("rb") as f:
            content = f.read()
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                self._url("sendDocument"),
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": (path.name, content)},
            )
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError:
                raise TGClientError(
                    f"sendDocument failed: HTTP {r.status_code} "
                    f"body={redact_tg_url(r.text[:200])}"
                ) from None
            data: dict[str, Any] = r.json()
            if not data.get("ok"):
                _LOG.warning("scope=b.tg.sendDocument.fail body=%s", data)
            return data
