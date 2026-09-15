"""Bark（iOS 推送）。

`level` 由严重级别决定：error 用 `timeSensitive`（穿透专注模式），
warning 用 `active`，info 用 `passive`（不点亮屏幕）。这是把"严重程度"翻译成
接收端唯一真正在意的那件事——会不会响。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import BARK_LEVELS, HttpChannel


@dataclass(frozen=True, slots=True)
class BarkChannel(HttpChannel):
    name: str = "bark"
    server: str = "https://api.day.app"
    device_key: str = ""
    group: str = "dywatch"

    def request(self, message: Any) -> tuple[str, dict[str, Any]]:
        return (
            f"{self.server.rstrip('/')}/{self.device_key}",
            {
                "title": message.subject,
                "body": message.text,
                "level": BARK_LEVELS.get(message.severity, "active"),
                "group": self.group,
            },
        )
