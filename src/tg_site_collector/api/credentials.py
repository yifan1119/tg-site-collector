"""POST /api/workflow/verify-bot · /api/workflow/verify-tavily · /api/workflow/verify-chat

凭证校验 endpoint · 用户绑定 token / API key 时校验有效性。
后端代调对应外部 API · 返回简要校验结果(不暴露完整 response)。

⚠️ MVP 阶段无 JWT 鉴权(等 A 模块 ready) · 仅靠内存限流 + bot_token 维度防滥用。
   未来必接 a_module.middleware 鉴权 + 校验 chat_id 是 caller 已绑定的 chat。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict, deque

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

_LOG = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workflow", tags=["b-module"])

# ── 简单内存限流(per bot_token) ─────────────────────────────────────
# MVP 阶段防滥用 · 单进程足够 · 多 worker 时改 Redis sliding window。
_RATE_LIMIT_WINDOW_SEC = 60
_RATE_LIMIT_MAX_PER_WINDOW = 10  # 每个 bot_token 每分钟最多 10 次任意 verify-* 调用
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def _hash_key(token: str) -> str:
    """限流用 hash 做 key · 不留原 token 在内存。"""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _check_rate(token: str) -> None:
    key = _hash_key(token)
    now = time.time()
    bucket = _rate_buckets[key]
    # 滑动窗口:扔掉过期记录
    while bucket and now - bucket[0] > _RATE_LIMIT_WINDOW_SEC:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT_MAX_PER_WINDOW:
        raise HTTPException(
            429,
            f"rate limit: {_RATE_LIMIT_MAX_PER_WINDOW} verify calls per "
            f"{_RATE_LIMIT_WINDOW_SEC}s per token",
        )
    bucket.append(now)
    # 顺手清理: 每次调用扫一遍,把已彻底过期(deque 空了)的 key 删掉
    # 防止内存随时间无限增长(每个新 token 一个永久 key)
    if len(_rate_buckets) > 1024:  # 简单上限触发清理
        _gc_empty_buckets(now)


def _gc_empty_buckets(now: float) -> None:
    """删除已无活跃记录的 bucket key · 防内存泄漏。"""
    stale_keys = []
    for k, b in _rate_buckets.items():
        # 把过期的也清掉再判断
        while b and now - b[0] > _RATE_LIMIT_WINDOW_SEC:
            b.popleft()
        if not b:
            stale_keys.append(k)
    for k in stale_keys:
        del _rate_buckets[k]


# ── verify-bot ────────────────────────────────────────────────────


class VerifyBotRequest(BaseModel):
    bot_token: str = Field(min_length=10)


class VerifyBotResponse(BaseModel):
    ok: bool
    bot_id: int | None = None
    bot_username: str | None = None
    bot_name: str | None = None
    error: str | None = None


@router.post(
    "/verify-bot",
    response_model=VerifyBotResponse,
    summary="校验 TG bot token",
)
async def verify_bot(req: VerifyBotRequest) -> VerifyBotResponse:
    _check_rate(req.bot_token)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"https://api.telegram.org/bot{req.bot_token}/getMe"
            )
            data = r.json()
            if not data.get("ok"):
                return VerifyBotResponse(
                    ok=False, error=str(data.get("description", "unknown"))
                )
            result = data["result"]
            return VerifyBotResponse(
                ok=True,
                bot_id=result.get("id"),
                bot_username=result.get("username"),
                bot_name=result.get("first_name"),
            )
    except Exception as exc:
        _LOG.warning("scope=b.verify_bot.fail err=%s", exc)
        return VerifyBotResponse(
            ok=False, error=f"network/parse error: {type(exc).__name__}"
        )


# ── verify-tavily ─────────────────────────────────────────────────


class VerifyTavilyRequest(BaseModel):
    api_key: str = Field(min_length=8)


class VerifyTavilyResponse(BaseModel):
    ok: bool
    plan: str | None = None
    sample_results: int = 0
    error: str | None = None


@router.post(
    "/verify-tavily",
    response_model=VerifyTavilyResponse,
    summary="校验 Tavily API key",
)
async def verify_tavily(req: VerifyTavilyRequest) -> VerifyTavilyResponse:
    _check_rate(req.api_key)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                "https://api.tavily.com/search",
                json={"api_key": req.api_key, "query": "hello", "max_results": 1},
            )
            if r.status_code in (401, 402, 403):
                return VerifyTavilyResponse(
                    ok=False,
                    error=f"HTTP {r.status_code} · 配额/认证失败",
                )
            data = r.json()
            if "error" in data or "detail" in data:
                err = data.get("error") or data.get("detail")
                return VerifyTavilyResponse(ok=False, error=str(err)[:200])
            return VerifyTavilyResponse(
                ok=True,
                sample_results=len(data.get("results", [])),
            )
    except Exception as exc:
        _LOG.warning("scope=b.verify_tavily.fail err=%s", exc)
        return VerifyTavilyResponse(
            ok=False,
            sample_results=0,
            error=f"network/parse error: {type(exc).__name__}",
        )


# ── verify-chat ───────────────────────────────────────────────────


class VerifyChatRequest(BaseModel):
    bot_token: str
    chat_id: int


class VerifyChatResponse(BaseModel):
    ok: bool
    sent_message_id: int | None = None
    error: str | None = None


@router.post(
    "/verify-chat",
    response_model=VerifyChatResponse,
    summary="校验 bot 能给 chat 发消息",
)
async def verify_chat(req: VerifyChatRequest) -> VerifyChatResponse:
    """直接调 sendMessage 推一条"绑定校验通过"消息 · 用户能在 TG 看到就证明 chat OK。

    ⚠️ MVP 安全边界:
    - 限流(每 bot_token 60s/10 次)防 spam
    - bot 只能给"已 /start 过的用户"发消息(TG 自身约束) · 攻击者拿不到别人的 bot_token
      就不能滥用
    - 未来接 A 模块 JWT 后:校验 chat_id 必须是 caller 的 binding 之一
    """
    _check_rate(req.bot_token)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{req.bot_token}/sendMessage",
                data={
                    "chat_id": req.chat_id,
                    "text": "✅ 绑定校验通过 · 站点采集 Bot 已就位",
                },
            )
            data = r.json()
            if not data.get("ok"):
                return VerifyChatResponse(
                    ok=False, error=str(data.get("description", "unknown"))
                )
            return VerifyChatResponse(
                ok=True, sent_message_id=data["result"].get("message_id")
            )
    except Exception as exc:
        _LOG.warning("scope=b.verify_chat.fail err=%s", exc)
        return VerifyChatResponse(ok=False, error=f"network: {type(exc).__name__}")
