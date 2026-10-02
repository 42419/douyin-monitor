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
    """Build one channel object per configured target.

    两条路径：

    - `NOTIFY_TARGETS` 配了（新写法）→ 按目标逐个建，**同一个类型可以出现多次**（两个钉钉群、
      两个 Telegram 会话都行），实例名来自配置（`name=` 或按类型自动编号），保证唯一
    - 否则走旧写法：`NOTIFY_CHANNELS`（类型名列表）+ 各类型的**单值**凭据键，一个类型一个实例

    一个渠道配置写错不能连坐其它渠道：告警的失效方向必须是"少一条"，而不是"全部静默"。
    """
    at_mobiles = tuple(settings["AT_MOBILES"] or ())
    targets = settings["NOTIFY_TARGETS"]
    if targets.configured:
        return [_build_from_target(target, at_mobiles) for target in targets.targets]
    return _build_legacy(settings, at_mobiles)


def _build_from_target(target: Any, at_mobiles: tuple[str, ...]) -> HttpChannel:
    """按 `NOTIFY_TARGETS` 的一个条目建渠道。必填字段在解析阶段已经校验过。"""
    fields = target.fields
    name = target.name
    if target.kind == "dingtalk":
        return DingTalkChannel(
            name=name,
            token=fields["token"],
            secret=fields.get("secret", ""),
            at_mobiles=at_mobiles,
        )
    if target.kind == "wecom":
        return WeComChannel(name=name, key=fields["key"])
    if target.kind == "bark":
        return BarkChannel(
            name=name,
            server=fields.get("server") or "https://api.day.app",
            device_key=fields["device_key"],
        )
    if target.kind == "serverchan":
        return ServerChanChannel(name=name, sendkey=fields["sendkey"])
    if target.kind == "telegram":
        return TelegramChannel(
            name=name, bot_token=fields["bot_token"], chat_id=fields["chat_id"]
        )
    if target.kind == "webhook":
        return WebhookChannel(name=name, url=fields["url"])
    # 解析阶段只放行 SPECS 里的类型，这里兜一下（将来加了类型忘了写分支时能立刻看出来）
    raise NotImplementedError(
        f"没有为渠道类型 {target.kind!r} 写装配分支"
    )  # pragma: no cover


def _build_legacy(settings: Any, at_mobiles: tuple[str, ...]) -> list[HttpChannel]:
    """旧写法：`NOTIFY_CHANNELS` + 单值凭据键。**保留是为了不破坏已有部署的 .env。**

    旧写法一个类型只有**一份**凭据，但 `NOTIFY_CHANNELS=dingtalk,dingtalk` 这种写法是**能**
    被写出来的：那会建出两个同名渠道（同一份凭据，同一条消息会往同一个群发两次），而
    `Delivery.sent` / `failed` 是名字的列表与字典，两个同名实例的结果会互相覆盖——其中一次
    失败了，投递记录里也看不出来。所以第二个开始加 `-2` / `-3` 后缀，与 target 写法的编号规则一致。
    """
    built: list[HttpChannel] = []
    counted: dict[str, int] = {}

    def unique(name: str) -> str:
        counted[name] = counted.get(name, 0) + 1
        return name if counted[name] == 1 else f"{name}-{counted[name]}"

    for name in settings["NOTIFY_CHANNELS"]:
        if name == "dingtalk":
            if not settings["DINGTALK_TOKEN"]:
                continue
            built.append(
                DingTalkChannel(
                    name=unique("dingtalk"),
                    token=settings["DINGTALK_TOKEN"],
                    secret=settings["DINGTALK_SECRET"],
                    at_mobiles=at_mobiles,
                )
            )
        elif name == "wecom":
            if settings["WECOM_WEBHOOK_KEY"]:
                built.append(
                    WeComChannel(
                        name=unique("wecom"), key=settings["WECOM_WEBHOOK_KEY"]
                    )
                )
        elif name == "bark":
            if settings["BARK_DEVICE_KEY"]:
                built.append(
                    BarkChannel(
                        name=unique("bark"),
                        server=settings["BARK_SERVER"],
                        device_key=settings["BARK_DEVICE_KEY"],
                    )
                )
        elif name == "serverchan":
            if settings["SERVERCHAN_SENDKEY"]:
                built.append(
                    ServerChanChannel(
                        name=unique("serverchan"),
                        sendkey=settings["SERVERCHAN_SENDKEY"],
                    )
                )
        elif name == "telegram":
            if settings["TELEGRAM_BOT_TOKEN"] and settings["TELEGRAM_CHAT_ID"]:
                built.append(
                    TelegramChannel(
                        name=unique("telegram"),
                        bot_token=settings["TELEGRAM_BOT_TOKEN"],
                        chat_id=settings["TELEGRAM_CHAT_ID"],
                    )
                )
        elif name == "webhook":
            if settings["WEBHOOK_URL"]:
                built.append(
                    WebhookChannel(name=unique("webhook"), url=settings["WEBHOOK_URL"])
                )
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
