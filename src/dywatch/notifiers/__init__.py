"""通知渠道。

每个渠道只做一件事：把一条渲染好的 `Message` 变成对方要的 JSON。
它不知道监控、判定、状态的存在，也不知道别的渠道存在——
所以新增一个渠道是加一个文件，不是改三处逻辑。
"""

from .bark import BarkChannel
from .base import Delivery, HttpChannel, Notifier, RetryableDelivery, sign_dingtalk_url
from .composite import AnyNotifier, build_channels, build_notifier
from .dingtalk import DingTalkChannel
from .null import NullNotifier
from .serverchan import ServerChanChannel
from .telegram import TelegramChannel
from .webhook import WebhookChannel
from .wecom import WeComChannel

__all__ = [
    "AnyNotifier",
    "BarkChannel",
    "Delivery",
    "DingTalkChannel",
    "HttpChannel",
    "Notifier",
    "NullNotifier",
    "RetryableDelivery",
    "ServerChanChannel",
    "TelegramChannel",
    "WeComChannel",
    "WebhookChannel",
    "build_channels",
    "build_notifier",
    "sign_dingtalk_url",
]
