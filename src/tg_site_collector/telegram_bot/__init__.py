"""Telegram Bot 接入层 · 员工 TG user_id → Query Agent。"""

from tg_site_collector.telegram_bot.bot import build_application
from tg_site_collector.telegram_bot.user_mapping import TgEmployeeBinder, load_bindings

__all__ = ["TgEmployeeBinder", "build_application", "load_bindings"]
