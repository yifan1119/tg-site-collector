"""Temporal client 单例 · API 层 + worker 都从这里拿。"""

from __future__ import annotations

import os
from functools import lru_cache

from temporalio.client import Client


def temporal_host() -> str:
    return os.getenv("TEMPORAL_HOST", "localhost:7233")


def temporal_namespace() -> str:
    return os.getenv("TEMPORAL_NAMESPACE", "default")


def b_task_queue() -> str:
    return os.getenv("B_TASK_QUEUE", "site-collector")


@lru_cache(maxsize=1)
def _connection_args() -> tuple[str, str]:
    return temporal_host(), temporal_namespace()


async def get_client() -> Client:
    """每次连一个新 Client (FastAPI lifespan 会保留 instance · 这里是 fallback)。"""
    host, ns = _connection_args()
    return await Client.connect(host, namespace=ns)
