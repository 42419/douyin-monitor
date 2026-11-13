"""钉钉群机器人（markdown / ActionCard + 加签）。

"新作品"用**独立跳转 ActionCard**（`btns` 数组 + `btnOrientation: "0"`，见钉钉
「消息发送与接收类型」的 ActionCard 一节）：卡片正文是字段列表 + 封面，底部一个
「点击观看」按钮跳到作品页。不用 `singleTitle`/`singleURL` 那种整体跳转——
整块可点的卡片看不出哪里能点，按钮才是明确的动作位。

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
            # 独立跳转 ActionCard：一个按钮。`btnOrientation="0"` 是竖排（文档定义的取值），
            # 与 sampleActionCard2 那类"卡片正文 + 竖向按钮"的形态一致。
            #
            # 这个分支在下面的 `at` 之前返回。今天只有 `new_post`（info）会填卡片，所以
            # `AT_MOBILES` 不受影响；**将来若有 warning/error 事件也用卡片，必须把 `at`
            # 并进卡片体**，否则它会静默失效（告警不 @ 人，没有报错、没有日志）。
            return url, {
                "msgtype": "actionCard",
                "actionCard": {
                    "title": message.subject,
                    "text": message.card_markdown,
                    "btnOrientation": "0",
                    "btns": [{"title": msg.CARD_BUTTON, "actionURL": message.card_url}],
                },
            }
        body: dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {"title": message.subject, "text": message.markdown},
        }
        if self.at_mobiles and message.severity in ALERT_SEVERITIES:
            body["at"] = {"atMobiles": list(self.at_mobiles), "isAtAll": False}
        return url, body
