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
}

#: 作品级事件没有窗口——它们只发生一次，抑制它们等于丢通知
NO_WINDOW_EVENTS: Final[frozenset[EventKind]] = frozenset(
    {EventKind.NEW_POST, EventKind.POST_REMOVED, EventKind.ALL_GONE}
)


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
    "NO_WINDOW_EVENTS",
    "TRIGGERS",
    "TriggerSpec",
    "dedup_key",
    "should_send",
]
