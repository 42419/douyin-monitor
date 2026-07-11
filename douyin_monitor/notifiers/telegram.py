"""Telegram Bot 推送。文档：https://core.telegram.org/bots/api#sendmessage"""

from __future__ import annotations

from typing import List, Optional

import requests

from ..config import HTTP_TIMEOUT
from .base import BaseNotifier


class TelegramNotifier(BaseNotifier):
    """通过 Telegram Bot API 推送消息到指定 chat。"""

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str):
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    def send_text(self, title: str, content: str, at_mobiles: Optional[List[str]] = None) -> bool:
        text = f"*{_escape_markdown(title)}*\n\n{content}"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        }
        try:
            resp = requests.post(self._url, json=payload, timeout=HTTP_TIMEOUT)
            data = resp.json() if resp.content else {}
            ok = resp.status_code == 200 and data.get("ok") is True
            return self._log_result(ok, str(data) if not ok else "")
        except requests.exceptions.RequestException as e:
            return self._log_result(False, str(e))


def _escape_markdown(text: str) -> str:
    for ch in "_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text
