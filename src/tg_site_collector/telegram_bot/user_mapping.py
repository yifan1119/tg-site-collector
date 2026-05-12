"""TG user_id → employee.user_id 绑定查找。

MVP 实现：从 JSON 文件加载（`config/tg_bindings.json`）· 手工维护。
D5+ 规划：自助绑定流程（TG /bind <dashboard token> · Dashboard 发 token）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

DEFAULT_BINDINGS_PATH: Final = Path("./config/tg_bindings.json")
_ENV_BINDINGS_PATH: Final = "TG_BINDINGS_PATH"


class TgEmployeeBinder:
    """不可变 · 线程安全 · O(1) 查询 tg_user_id → employee.user_id。"""

    def __init__(self, bindings: dict[int, str]) -> None:
        self._bindings: dict[int, str] = dict(bindings)

    def resolve(self, tg_user_id: int) -> str | None:
        return self._bindings.get(tg_user_id)

    def __len__(self) -> int:  # 便于调试日志
        return len(self._bindings)


def load_bindings(path: Path | str | None = None) -> TgEmployeeBinder:
    """读 JSON 文件 · 空文件 / 缺文件 返回空 binder。

    JSON 形态：`{"<tg_user_id>": "<employee_user_id>", ...}`
    以 `_` 开头的 key 视为注释 / 示例 · 会被跳过。
    """
    resolved = _resolve_path(path)
    if not resolved.exists():
        return TgEmployeeBinder({})

    try:
        with resolved.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError:
        # 空文件 / 损坏的 JSON · 视为空绑定而不是崩溃
        return TgEmployeeBinder({})

    if not isinstance(data, dict):
        return TgEmployeeBinder({})

    bindings: dict[int, str] = {}
    for raw_key, raw_value in data.items():
        if not isinstance(raw_key, str) or raw_key.startswith("_"):
            continue
        if not isinstance(raw_value, str):
            continue
        try:
            tg_id = int(raw_key)
        except ValueError:
            continue
        bindings[tg_id] = raw_value
    return TgEmployeeBinder(bindings)


def _resolve_path(path: Path | str | None) -> Path:
    if path is not None:
        return Path(path)
    env_value = os.getenv(_ENV_BINDINGS_PATH)
    if env_value:
        return Path(env_value)
    return DEFAULT_BINDINGS_PATH
