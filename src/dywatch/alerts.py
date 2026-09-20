"""运维类告警的抑制窗口。

**去重不是为了省消息，而是为了让告警可信。** 一个持续失败的上游如果每轮都推一次，
几分钟之内人就会把通知关掉——而那时他失去的不只是这条噪音，还有所有真正重要的告警。
所以每一类"会重复为真"的状态都有作用域和窗口：同一个账号 5 分钟内只提醒一次，
上游级问题按错误码分桶、按小时计。

作品级事件（新作品 / 作品消失）**不在这里**：它们天然只发生一次，
由 `diff` 的 known/tombstone 集合保证，不需要窗口——给它们加窗口只会吞掉真实通知。

窗口用单调钟而不是墙钟：批量推进时间做测试要能推进它，恢复备份之后也不该因为
"系统时间跳了"而把窗口算错。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Final, Mapping

from .models import Event, EventKind

MINUTE: Final[int] = 60
HOUR: Final[int] = 3600


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    severity: str
    window_seconds: int
    #: global = 全实例一个桶；author = 每账号一个桶；none = 不抑制
    scope: str = "global"


TRIGGERS: Final[Mapping[EventKind, TriggerSpec]] = {
    # 上游整体问题：按错误码分桶，1 小时一次
    EventKind.UPSTREAM_DEGRADED: TriggerSpec("error", HOUR, "global"),
    # 账号级：窗口与 FAIL_COOLDOWN 保持一致（settings 里那一项就是给它用的）
    EventKind.ACCOUNT_FAILED: TriggerSpec("error", 5 * MINUTE, "author"),
    EventKind.ACCOUNT_RECOVERED: TriggerSpec("info", 0, "none"),
    EventKind.NEVER_SEEN: TriggerSpec("warning", 6 * HOUR, "author"),
    EventKind.GAP_DETECTED: TriggerSpec("warning", 12 * HOUR, "author"),
    # 一次性提醒由作者状态里的 stale_alerted 保证，这里不需要窗口
    EventKind.STALE_NO_UPDATE: TriggerSpec("info", 0, "none"),
    # 自身降级：报出来就得让人知道，但别每分钟说一遍
    EventKind.SELF_DEGRADED: TriggerSpec("warning", 6 * HOUR, "global"),
    # 标题变更会连着来：作者发完作品再补话题标签是常态，1 小时内同一账号只报一次
    EventKind.TITLE_CHANGED: TriggerSpec("info", HOUR, "author"),
    # 回归本身罕见，但窗口挪动可以让同一条作品反复"出去又回来"，给个宽窗口兜住这种抖动
    EventKind.REVIVED: TriggerSpec("info", 6 * HOUR, "author"),
    # `hidden_from_guest` 刻意**不在**这张表里，见下面 `NO_WINDOW_EVENTS` 里的理由
}

#: 没有抑制窗口的事件——它们**每条都携带一次性的新信息**，抑制等于丢通知。
#:
#: `HIDDEN_FROM_GUEST` 曾按"6 小时/账号"给过窗口（2026-09-20 改掉）：那条思路是错的，
#: 而且和它自己的机制相反——一条作品被核验标记之后就**再也不会进入核验**（标记存在的
#: 目的就是让它不再被判成消失），所以同一条作品根本不可能重复产生这个事件。窗口在这里
#: 唯一能压掉的，只会是"6 小时内又发现了**另一条**被藏的作品"这种真正的新信息，而那条
#: 通知永远不会补发（标记已落库、不会再次核验）→ 永久丢失。
#: 发现速率本身由作者的发布节奏决定，且同一次核验里发现的多条会**聚合成一条**，
#: 不需要窗口兜重复。
NO_WINDOW_EVENTS: Final[frozenset[EventKind]] = frozenset(
    {
        EventKind.NEW_POST,
        EventKind.POST_REMOVED,
        EventKind.ALL_GONE,
        EventKind.HIDDEN_FROM_GUEST,
    }
)

#: **投递顺序**，数字越小越先发。
#:
#: `new_post` 必须第一个送出去，理由是它和其他事件不同质：它是这个工具存在的理由，
#: 而且**错过就补不回来**（用户不会知道曾经有过这条作品）。而一轮里的事件共用一条
#: 投递通道（渠道间隔 + 每渠道超时重试），排在它前面的每一条都可能把它推到几十秒之后，
#: 甚至因为渠道限流而让它变成"发送失败"那一条。所以顺序不按 `diff` 的输出，按这张表。
#:
#: 作品类排在运维类之前：运维类基本都被 `TRIGGERS` 的窗口压过一轮，
#: 而且它们的"过期成本"远低于漏掉一条作品的成本。
NOTIFY_PRIORITY: Final[tuple[EventKind, ...]] = (
    EventKind.NEW_POST,
    # 与 new_post 同理：它只在核验那一刻被发现一次，错过就补不回来。而且它同时
    # 承载"作者发了新东西"这层信息（只是访客看不到），排在作品类里最靠前的位置
    EventKind.HIDDEN_FROM_GUEST,
    EventKind.ALL_GONE,
    EventKind.POST_REMOVED,
    EventKind.REVIVED,
    EventKind.TITLE_CHANGED,
    EventKind.ACCOUNT_FAILED,
    EventKind.ACCOUNT_RECOVERED,
    EventKind.NEVER_SEEN,
    EventKind.GAP_DETECTED,
    EventKind.STALE_NO_UPDATE,
    EventKind.UPSTREAM_DEGRADED,
    EventKind.SELF_DEGRADED,
)


def priority_of(kind: EventKind) -> int:
    """投递优先级；不在表里的排最后（稳定排序保证同优先级维持 `diff` 的原顺序）。"""
    try:
        return NOTIFY_PRIORITY.index(kind)
    except ValueError:  # pragma: no cover - 枚举与表目前一一对应
        return len(NOTIFY_PRIORITY)


def dedup_key(event: Event) -> str:
    """Redis 风格的可读键，方便在日志里一眼看出是哪类问题在吵。"""
    spec = TRIGGERS.get(event.kind)
    if spec is None:
        return ""
    if spec.scope == "author":
        return f"{event.kind.value}:{event.sec_user_id}"
    return event.kind.value


class Deduplicator:
    """Suppression windows, in memory, driven by an injectable clock.

    In memory is a deliberate choice: a restart forgetting the windows costs one
    extra alert, while persisting them costs a table and a class of bugs about
    which clock the stored stamp was written with.
    """

    __slots__ = ("_clock", "_marks")

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._marks: dict[str, float] = {}

    def allow(self, key: str, window_seconds: int) -> bool:
        if not key:
            return True
        if window_seconds <= 0:
            return True
        now = self._clock()
        last = self._marks.get(key)
        if last is not None and 0.0 <= now - last < window_seconds:
            return False
        self._marks[key] = now
        return True

    def release(self, key: str) -> None:
        """Give the window back after a delivery failed.

        A window exists to stop a storm of *delivered* alerts. Holding it after a
        message reached nobody turns one refused connection into an hour of silence,
        which is the opposite of what it is for.
        """
        self._marks.pop(key, None)

    def reset(self) -> None:
        self._marks.clear()


def should_send(event: Event, dedup: Deduplicator) -> tuple[bool, str]:
    """Whether this event should be delivered now, and the key it claimed."""
    if event.kind in NO_WINDOW_EVENTS:
        return True, ""
    spec = TRIGGERS.get(event.kind)
    if spec is None:
        return True, ""
    key = dedup_key(event)
    if spec.scope == "global" and event.kind is EventKind.UPSTREAM_DEGRADED:
        # 上游问题按错误码分桶，"池子空了"和"接口熔断"不该互相抑制
        key = f"{key}:{event.payload.get('code', 'unknown')}"
    return dedup.allow(key, spec.window_seconds), key


__all__ = [
    "Deduplicator",
    "NOTIFY_PRIORITY",
    "NO_WINDOW_EVENTS",
    "TRIGGERS",
    "TriggerSpec",
    "priority_of",
    "dedup_key",
    "should_send",
]
