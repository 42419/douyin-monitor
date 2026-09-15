"""Telegram Bot。bot token 在 URL 路径里，所以投递失败的日志必须屏蔽 URL。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import HttpChannel


@dataclass(frozen=True, slots=True)
class TelegramChannel(HttpChannel):
    name: str = "telegram"
    bot_token: str = ""
    chat_id: str = ""

    def request(self, message: Any) -> tuple[str, dict[str, Any]]:
        return (
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            {
                "chat_id": self.chat_id,
                "text": message.text,
                "disable_web_page_preview": True,
            },
        )
