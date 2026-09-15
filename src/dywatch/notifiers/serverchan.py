"""Server 酱（sctapi.ftqq.com）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import HttpChannel


@dataclass(frozen=True, slots=True)
class ServerChanChannel(HttpChannel):
    name: str = "serverchan"
    sendkey: str = ""

    def request(self, message: Any) -> tuple[str, dict[str, Any]]:
        return (
            f"https://sctapi.ftqq.com/{self.sendkey}.send",
            {"title": message.subject[:32], "desp": message.markdown},
        )
