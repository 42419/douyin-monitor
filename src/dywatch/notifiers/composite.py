"""由配置装配通知器：一个有渠道的 `Notifier`，或者一个静默的 `NullNotifier`。

装配在这里而不是在 `Notifier` 里，是为了让 `Notifier` 只认识"渠道列表"这一个概念——
它不需要知道配置键长什么样，也就不需要为了一个新增的渠道改两次。
"""

from __future__ import annotations

from typing import Any, Union

from .bark import BarkChannel
from .base import HttpChannel, Notifier
from .dingtalk import DingTalkChannel
from .null import NullNotifier
from .serverchan import ServerChanChannel
from .telegram import TelegramChannel
from .webhook import WebhookChannel
from .wecom import WeComChannel

AnyNotifier = Union[Notifier, NullNotifier]


def build_channels(settings: Any) -> list[HttpChannel]:
    """One channel per name in `NOTIFY_CHANNELS`, skipping the malformed ones.

    一个渠道配置写错不能连坐其它渠道：告警的失效方向必须是"少一条"，
    而不是"全部静默"。
    """
    built: list[HttpChannel] = []
    at_mobiles = tuple(settings["AT_MOBILES"] or ())

    for name in settings["NOTIFY_CHANNELS"]:
        if name == "dingtalk":
            if not settings["DINGTALK_TOKEN"]:
                continue
            built.append(
                DingTalkChannel(
                    token=settings["DINGTALK_TOKEN"],
                    secret=settings["DINGTALK_SECRET"],
                    at_mobiles=at_mobiles,
                )
            )
        elif name == "wecom":
            if settings["WECOM_WEBHOOK_KEY"]:
                built.append(WeComChannel(key=settings["WECOM_WEBHOOK_KEY"]))
        elif name == "bark":
            if settings["BARK_DEVICE_KEY"]:
                built.append(
                    BarkChannel(
                        server=settings["BARK_SERVER"],
                        device_key=settings["BARK_DEVICE_KEY"],
                    )
                )
        elif name == "serverchan":
            if settings["SERVERCHAN_SENDKEY"]:
                built.append(ServerChanChannel(sendkey=settings["SERVERCHAN_SENDKEY"]))
        elif name == "telegram":
            if settings["TELEGRAM_BOT_TOKEN"] and settings["TELEGRAM_CHAT_ID"]:
                built.append(
                    TelegramChannel(
                        bot_token=settings["TELEGRAM_BOT_TOKEN"],
                        chat_id=settings["TELEGRAM_CHAT_ID"],
                    )
                )
        elif name == "webhook":
            if settings["WEBHOOK_URL"]:
                built.append(WebhookChannel(url=settings["WEBHOOK_URL"]))
    return built


def build_notifier(settings: Any, *, client: Any = None) -> AnyNotifier:
    if settings["SILENT_MODE"]:
        return NullNotifier()
    return Notifier(
        build_channels(settings),
        gap_seconds=float(settings["NOTIFY_GAP"]),
        client=client,
    )


__all__ = ["AnyNotifier", "build_channels", "build_notifier"]
