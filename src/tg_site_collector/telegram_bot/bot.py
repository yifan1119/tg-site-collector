"""Telegram Bot 应用装配 · 简化独立版本 · 只挂站点采集 handler · 不耦合知识库。

跟 monorepo 版区别:
- 删 LLM client / DB session / wiki_root(知识库 ``/ask`` 命令已删)
- 删 ``handlers.on_text_message``(知识库回答 handler)· 改成直接调
  ``b_module_router.try_route`` 路由站点采集关键词
- 保留 ``/start`` ``/help`` ``install_bot_commands``(纯文案 · 干净)
- 保留 ``user_mapping``(TG user_id ↔ employee_id 绑定 · JSON 持久化)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from tg_site_collector.telegram_bot import b_module_router
from tg_site_collector.telegram_bot.user_mapping import TgEmployeeBinder, load_bindings
from tg_site_collector.telegram_bot.welcome import (
    help_command,
    install_bot_commands,
    start_command,
)

_LOGGER = logging.getLogger(__name__)

_ENV_TG_BOT_TOKEN = "TG_BOT_TOKEN"

BOT_DATA_BINDER = "binder"


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """私聊文本 → 站点采集 router 路由 · 不匹配关键词时给提示。"""
    routed = await b_module_router.try_route(update, context)
    if not routed and update.message and update.message.text:
        await update.message.reply_text(
            "输入 /help 看支持的关键词 · 直接发关键词触发站点采集"
        )


def build_application(
    *,
    token: str,
    binder: TgEmployeeBinder,
) -> Application[Any, Any, Any, Any, Any, Any]:
    """工厂 · 组装 Application · handler 通过 bot_data 拿共享依赖。"""
    application: Application[Any, Any, Any, Any, Any, Any] = (
        Application.builder().token(token).build()
    )
    application.bot_data[BOT_DATA_BINDER] = binder

    # 只响应 1:1 私聊 · 防群里关键词误触发
    private_only = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", start_command, filters=private_only))
    application.add_handler(CommandHandler("help", help_command, filters=private_only))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & private_only, on_text_message)
    )

    async def _post_init(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        await install_bot_commands(app)

    application.post_init = _post_init
    return application


def main() -> None:  # pragma: no cover - entry point · 真跑需 TG token
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    token = os.getenv(_ENV_TG_BOT_TOKEN)
    if not token:
        raise RuntimeError(f"{_ENV_TG_BOT_TOKEN} is required")

    binder = load_bindings()
    _LOGGER.info("scope=tg.bot.start bindings=%d", len(binder))

    application = build_application(token=token, binder=binder)
    application.run_polling()


if __name__ == "__main__":
    main()
