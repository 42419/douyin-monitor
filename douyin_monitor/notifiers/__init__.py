"""多推送渠道支持。

通过 .env 中的 NOTIFY_CHANNELS 配置启用一个或多个推送渠道（逗号分隔），
例如 NOTIFY_CHANNELS=dingtalk,bark。未配置时默认仅启用 dingtalk，
与旧版本行为保持一致。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List

from .bark import BarkNotifier
from .base import BaseNotifier
from .composite import CompositeNotifier
from .dingtalk import DingTalkNotifier
from .serverchan import ServerChanNotifier
from .telegram import TelegramNotifier
from .wecom import WeComNotifier

if TYPE_CHECKING:
    from ..config import Config

__all__ = [
    "BaseNotifier",
    "CompositeNotifier",
    "DingTalkNotifier",
    "BarkNotifier",
    "WeComNotifier",
    "ServerChanNotifier",
    "TelegramNotifier",
    "build_notifier",
]


def build_notifier(cfg: "Config") -> BaseNotifier:
    """根据 Config 中启用的渠道列表，构建对应的通知器（单个渠道时直接返回，
    多个渠道时返回 CompositeNotifier 广播给所有渠道）。

    缺少必要配置的渠道会被跳过并记录警告；如果启用列表中一个渠道都没有
    成功构建，直接抛出 ValueError（调用方应据此终止启动，避免监控跑起来
    却完全无法通知）。
    """
    built: List[BaseNotifier] = []

    for channel in cfg.notify_channels:
        if channel == "dingtalk":
            if cfg.dingtalk_token and cfg.dingtalk_secret:
                built.append(
                    DingTalkNotifier(cfg.dingtalk_token, cfg.dingtalk_secret, cfg.at_mobiles)
                )
            else:
                logging.warning("已启用 dingtalk 渠道，但缺少 DINGTALK_TOKEN/DINGTALK_SECRET，已跳过")
        elif channel == "bark":
            if cfg.bark_device_key:
                built.append(BarkNotifier(cfg.bark_server, cfg.bark_device_key))
            else:
                logging.warning("已启用 bark 渠道，但缺少 BARK_DEVICE_KEY，已跳过")
        elif channel == "wecom":
            if cfg.wecom_webhook_key:
                built.append(WeComNotifier(cfg.wecom_webhook_key))
            else:
                logging.warning("已启用 wecom 渠道，但缺少 WECOM_WEBHOOK_KEY，已跳过")
        elif channel == "serverchan":
            if cfg.serverchan_sendkey:
                built.append(ServerChanNotifier(cfg.serverchan_sendkey))
            else:
                logging.warning("已启用 serverchan 渠道，但缺少 SERVERCHAN_SENDKEY，已跳过")
        elif channel == "telegram":
            if cfg.telegram_bot_token and cfg.telegram_chat_id:
                built.append(TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id))
            else:
                logging.warning("已启用 telegram 渠道，但缺少 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID，已跳过")
        else:
            logging.warning(f"未知的通知渠道 {channel!r}，已忽略")

    if not built:
        raise ValueError(
            "没有任何通知渠道被成功启用，请检查 .env 中 NOTIFY_CHANNELS 及对应渠道的凭证配置"
        )

    if len(built) == 1:
        return built[0]

    logging.info(f"已启用 {len(built)} 个通知渠道: {', '.join(n.name for n in built)}")
    return CompositeNotifier(built)
