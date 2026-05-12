"""O.log 调用 · MVP 阶段写本地 jsonl 文件（O 模块未实装）。

⚠️ NOT PRODUCTION-READY ⚠️
design decision:MVP 阶段每条事件 append 到 ./data/audit/audit.jsonl;
O 模块上线后改成调 O.log endpoint。**生产部署前必须接 O 模块**(否则审计本地写绕开
PRD § B.7.3 "B 不得维护自己审计表"约束 + 不满足 SOC2 / GDPR 留存策略)。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from temporalio import activity

_AUDIT_DIR = Path(os.getenv("B_AUDIT_DIR", "/tmp/tg-site-collector/b-audit"))
_AUDIT_FILE = _AUDIT_DIR / "audit.jsonl"


@activity.defn(name="o_log_event")
async def o_log_event(
    tenant_id: str, event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """MVP stub · 写本地 JSONL · O 模块 ready 后改 endpoint。"""
    _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "tenant_id": tenant_id,
        "event_type": event_type,
        "payload": payload,
    }
    with _AUDIT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True, "event_id": record["ts"]}
