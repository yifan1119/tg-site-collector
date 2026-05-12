"""S.evaluate 调用 · MVP 阶段 stub 返 pass（S 模块未实装）。

⚠️ NOT PRODUCTION-READY ⚠️
design decision:MVP 阶段所有 phase 都直接 pass;S 模块 ready 后改这里。**生产部署前
必须接 S 模块**(护栏直接绕过 = 无 PII / 违禁内容 / 越权检查)。
"""

from __future__ import annotations

import logging
from typing import Any

from temporalio import activity

_LOG = logging.getLogger(__name__)


@activity.defn(name="s_evaluate")
async def s_evaluate(tenant_id: str, phase: str, payload: dict[str, Any]) -> dict[str, Any]:
    """MVP stub · phase ∈ {pre, mid, post} · 直接 pass。"""
    _LOG.info(
        "scope=b.s_evaluate.stub tenant=%s phase=%s decision=pass",
        tenant_id,
        phase,
    )
    return {"decision": "pass", "phase": phase, "reason": "MVP stub · S not wired yet"}
