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
    #: Telegram 的成败判据是**响应体里的 `ok`**：官方文档只说"请求不成功时 `ok` 为 False，
    #: 并给出 `error_code`"，**通篇没有规定 HTTP 状态码**。实测一次假 token 确实回 401
    #: （会被 `status_code >= 400` 拦住），但那是观察结果，不是 API 的保证——
    #: 把"我们没见到 200"当成"不会回 200"，正好是这次审查反复抓到的那个模式。
    #: 读官方定义的那个字段是零成本的，所以这里也读。
    error_field: str | None = "ok"
    error_ok: Any = True

    def request(self, message: Any) -> tuple[str, dict[str, Any]]:
        return (
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            {
                "chat_id": self.chat_id,
                "text": message.text,
                "disable_web_page_preview": True,
            },
        )
