"""钉钉群机器人（markdown + 加签）。

机器人的安全设置若是「加签」，URL 必须带 `timestamp` 与 `sign`；若是「自定义关键词」，
则消息里必须出现那个词——标题带着它，所以这里不需要额外处理。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .base import HttpChannel, sign_dingtalk_url


@dataclass(frozen=True, slots=True)
class DingTalkChannel(HttpChannel):
    name: str = "dingtalk"
    webhook: str = "https://oapi.dingtalk.com/robot/send"
    token: str = ""
    secret: str = ""
    at_mobiles: tuple[str, ...] = ()

    def request(self, message: Any) -> tuple[str, dict[str, Any]]:
        url = f"{self.webhook}?access_token={self.token}"
        if self.secret:
            url = sign_dingtalk_url(url, self.secret, time.time())
        body: dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {"title": message.subject, "text": message.markdown},
        }
        if self.at_mobiles:
            body["at"] = {"atMobiles": list(self.at_mobiles), "isAtAll": False}
        return url, body
