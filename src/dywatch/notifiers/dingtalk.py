"""钉钉群机器人（markdown / ActionCard + 加签）。

"新作品"用 ActionCard（最早 bash 版的样子：标题、用户、时间、封面图 + 整块"点击观看"）；
其它事件仍是 markdown。

**@ 只对告警生效**（`ALERT_SEVERITIES`：warning / error，如作品消失、监控异常、上游异常）；
新作品、已恢复、长期无更新这类普通通知（info）不 @。以前每条消息都带 `at`，与
`AT_MOBILES`"告警时 @"的定义不符，新作品也会把人 @ 一遍。

机器人的安全设置若是「加签」，URL 必须带 `timestamp` 与 `sign`；若是「自定义关键词」，
则消息里必须出现那个词——标题带着它，所以这里不需要额外处理。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .. import messages as msg
from .base import HttpChannel, sign_dingtalk_url


#: 哪些严重度的消息才 @ 人（见 `render.SEVERITY`）
ALERT_SEVERITIES = frozenset({"warning", "error"})


@dataclass(frozen=True, slots=True)
class DingTalkChannel(HttpChannel):
    name: str = "dingtalk"
    webhook: str = "https://oapi.dingtalk.com/robot/send"
    token: str = ""
    secret: str = ""
    at_mobiles: tuple[str, ...] = ()
    #: 业务失败也回 200（关键词不匹配 `310000`、被限流 `130101` 等），必须读这个字段
    error_field: str | None = "errcode"

    def request(self, message: Any) -> tuple[str, dict[str, Any]]:
        url = f"{self.webhook}?access_token={self.token}"
        if self.secret:
            url = sign_dingtalk_url(url, self.secret, time.time())
        if message.card_markdown and message.card_url:
            return url, {
                "msgtype": "actionCard",
                "actionCard": {
                    "title": message.subject,
                    "text": message.card_markdown,
                    "singleTitle": msg.CARD_BUTTON,
                    "singleURL": message.card_url,
                },
            }
        body: dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {"title": message.subject, "text": message.markdown},
        }
        if self.at_mobiles and message.severity in ALERT_SEVERITIES:
            body["at"] = {"atMobiles": list(self.at_mobiles), "isAtAll": False}
        return url, body
