"""Playwright browser 单例 · per-worker 进程级共享。

不在每个 activity 里 launch chromium(每次 1.5s overhead),用 pool 一次起、复用。
worker shutdown 时 close。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_LOG = logging.getLogger(__name__)


class BrowserPool:
    """单例 · 进程内共享 chromium 实例。"""

    _instance: BrowserPool | None = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None

    @classmethod
    async def get(cls) -> BrowserPool:
        async with cls._lock:
            if cls._instance is None:
                cls._instance = BrowserPool()
                await cls._instance._start()
            return cls._instance

    async def _start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        _LOG.info("scope=b.browser_pool.start chromium ready")

    async def new_context(self, **kwargs: Any) -> Any:
        """每次 activity 取独立 BrowserContext (cookie/storage 隔离 · 防站间污染)。"""
        if self._browser is None:
            await self._start()
        ctx = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            **kwargs,
        )
        return ctx

    async def shutdown(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        _LOG.info("scope=b.browser_pool.shutdown")
