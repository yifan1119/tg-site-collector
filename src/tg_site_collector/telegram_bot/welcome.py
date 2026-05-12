"""TG Bot 欢迎 + 帮助文案 + 命令菜单注册。

设计:
- /start 命令 → 欢迎首屏 (新用户上来第一时间看到能干啥)
- /help 命令 → 完整指令清单
- set_my_commands → 注册到 TG · 输入框点 / 出下拉
- 关键词「帮助」/「help」 → 也走 /help 同一份文案 (b_module_router 处理)
"""

from __future__ import annotations

from telegram import BotCommand, Update
from telegram.ext import ContextTypes

WELCOME_TEXT = (
    "🤖 站点采集 Bot · 你好!\n"
    "━━━━━━━━━━━━━━━━━\n"
    "我能帮你自动采集成人站点的联系方式 (email / TG / QQ / 微信 / Twitter 等)\n"
    "全程跑在中台 workflow 里,完成自动推送结果 JSON 到这里。\n"
    "\n"
    "🚀 最常用 3 个指令:\n"
    "• 「采集」     · 走缓存全量深度采集 · 推荐\n"
    "• 「搜新的」   · Tavily 搜新关键词 + 展开 + 采集\n"
    "• 「增量采集」 · 跳过 7 天内已采过的 · 高频跑省 70-90% 时间\n"
    "\n"
    "想看完整指令: 发 /help 或「帮助」"
)

# 完整菜单 · /help 和「帮助」共用
HELP_TEXT = (
    "🤖 站点采集 Bot · 完整指令\n"
    "━━━━━━━━━━━━━━━━━\n"
    "🔥 深度全量(默认 · Playwright 展开导航站子站):\n"
    "• 采集 / 跑一遍 / 深度采集 / 全量采集\n"
    "    走缓存全采 · 出料 ×3-5\n"
    "• 搜新的 / 重新搜索 / 全量搜索 / 深度搜新\n"
    "    Tavily 搜新关键词 + 展开 + 采集\n"
    "\n"
    "💨 增量(跳过 7 天内已采过的 · 省时):\n"
    "• 增量采集 / 增量\n"
    "    高频跑省 70-90% 时间\n"
    "\n"
    "⚡ 快速(关 nav 展开 · 用时 ×1/3 · 出料 ×1):\n"
    "• 快速采集 / 速采\n"
    "• 快速搜新\n"
    "\n"
    "📤 其他:\n"
    "• 推送给我 / 推一次 · 把上次结果重推\n"
    "• /help 或「帮助」 · 显示本说明\n"
    "• /start · 显示欢迎页"
)

# TG 注册到 BotFather 的命令清单 · 输入框 / 按键能看到下拉
BOT_COMMANDS: list[BotCommand] = [
    BotCommand("start", "显示欢迎页"),
    BotCommand("help", "完整指令说明"),
    BotCommand("ask", "向知识库提问 (C 模块)"),
]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/start` · 新用户欢迎首屏。"""
    if update.message:
        await update.message.reply_text(WELCOME_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/help` · 完整指令清单 · 跟 b_module_router 的「帮助」文案一致。"""
    if update.message:
        await update.message.reply_text(HELP_TEXT)


async def install_bot_commands(application: object) -> None:
    """启动后调一次 · 把 BOT_COMMANDS 注册到 TG 后台 · 用户输入框 / 出下拉。

    幂等 · 重复跑不报错(setMyCommands 直接覆盖)。
    """
    bot = application.bot  # type: ignore[attr-defined]
    await bot.set_my_commands(BOT_COMMANDS)
