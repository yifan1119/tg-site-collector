"""Telegram Bot 用户回复文案（常量集中 · 便于 i18n / 调整语气）。"""

from typing import Final

MSG_UNBOUND: Final = "尚未绑定员工身份 · 请先在 Dashboard 完成 TG 绑定后再来问我。"
MSG_EMPLOYEE_INACTIVE: Final = "你的员工身份已失效（离职 / 停用）· 请联系 HR。"
MSG_EMPTY_QUESTION: Final = "请在 /ask 后面写你的问题 · 例如：/ask 入职第一天做什么"
MSG_API_FALLBACK: Final = "⚠️ Opus 暂不可用 · 请稍后重试或改问更简单的问题。"
