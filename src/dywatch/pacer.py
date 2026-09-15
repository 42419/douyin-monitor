"""全局请求节奏器。

它是旧项目 `douyin-monitor-enhance` 里唯一一个"并发相关"的组件，而且思路是对的：
**并发的意义是"一个慢账号不拖累其它账号"，不是"把请求更快地打出去"。**

所以这里只做一件事：任何请求在真正发出之前先来报到，报到通过的条件是
"距上一次报到通过至少隔了 `random(min, max)` 秒"——与并发数无关。
报到之后实际的 HTTP 请求在锁外进行，于是一个卡住的请求不会连带卡住排队者。

实测的这套节奏是 **3~8 秒**，配合轮末 15~40 秒的随机等待，整体约 11 次/分钟。
它比任何"聪明"的自适应算法都更值得保留：它是被几个月的真实运行验证过的。
"""

from __future__ import annotations

import asyncio
import random
import time


class RequestPacer:
    """Serialize request *starts* at a human-ish, randomized interval."""

    __slots__ = ("_min", "_max", "_lock", "_next_allowed", "_rng")

    def __init__(
        self,
        min_interval: float,
        max_interval: float,
        *,
        rng: random.Random | None = None,
    ) -> None:
        if min_interval <= 0:
            raise ValueError("min_interval 必须大于 0")
        if min_interval > max_interval:
            raise ValueError("min_interval 不能大于 max_interval")
        self._min = float(min_interval)
        self._max = float(max_interval)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0
        self._rng = rng or random.Random()

    @property
    def average_interval(self) -> float:
        return (self._min + self._max) / 2

    def per_minute_ceiling(self) -> float:
        """How many request *starts* an hour of this pace can produce, per minute."""
        return 60.0 / self.average_interval if self.average_interval else float("inf")

    async def wait_for_turn(self) -> float:
        """Block until this caller's turn, then return how long it waited."""
        async with self._lock:
            now = time.monotonic()
            start_of_slot = max(now, self._next_allowed)
            wait_for = start_of_slot - now
            self._next_allowed = start_of_slot + self._rng.uniform(self._min, self._max)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        return wait_for

    def reset(self) -> None:
        """Forget the schedule. Used when the global gate closes and time jumps."""
        self._next_allowed = 0.0


class RoundWaiter:
    """The other half of the pace: a random pause between rounds.

    Split from :class:`RequestPacer` because the two are independent — one spaces the
    requests *inside* a round, the other spaces the rounds themselves. Conflating them
    was how a previous version ended up unable to explain its own request rate.
    """

    __slots__ = ("_min", "_max", "_rng")

    def __init__(self, min_seconds: int, max_seconds: int, *, rng: random.Random | None = None) -> None:
        self._min = int(min_seconds)
        self._max = int(max_seconds)
        self._rng = rng or random.Random()

    def next_wait(self) -> int:
        if self._max <= self._min:
            return self._min
        return self._rng.randint(self._min, self._max)

    async def sleep(self) -> int:
        seconds = self.next_wait()
        await asyncio.sleep(seconds)
        return seconds


__all__ = ["RequestPacer", "RoundWaiter"]
