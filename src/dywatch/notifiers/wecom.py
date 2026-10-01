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
    #: Key 写错时是 `200 {"errcode":93000}`——只看状态码会把"根本没发出去"记成已送达
    error_field: str | None = "errcode"

    def request(self, message: Any) -> tuple[str, dict[str, Any]]:
        return (
            f"{self.webhook}?key={self.key}",
            {"msgtype": "markdown", "markdown": {"content": message.markdown}},
        )
