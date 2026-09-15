"""企业微信群机器人（markdown）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import HttpChannel


@dataclass(frozen=True, slots=True)
class WeComChannel(HttpChannel):
    name: str = "wecom"
    key: str = ""
    webhook: str = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"

    def request(self, message: Any) -> tuple[str, dict[str, Any]]:
        return (
            f"{self.webhook}?key={self.key}",
            {"msgtype": "markdown", "markdown": {"content": message.markdown}},
        )
