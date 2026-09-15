"""全局闸门：什么时候**整体**停下来。

这是 v5 的错误码才给得起的一层能力。旧项目面对上游异常只能一个个账号地失败，
既不知道原因也不知道该等多久；v5 把"现在别发请求了"说得很清楚：

    RATE_LIMITED              429，`error.retry_after` 就是答案
    IDENTITY_POOL_EXHAUSTED   503，身份池空了，等它自己补
    ENDPOINT_CIRCUIT_OPEN     503，这个接口被熔断了，等熔断恢复
    QUEUE_FULL                503，队列满了

这四类**不是某个账号的问题**，所以它们既不该算在这个账号头上，也不该按账号刷告警。
闸门关上的时候整轮跳过，只记日志；闸门恢复是自动的，不需要人介入。

闸门之外还有两个小判断：告警冷却（同一个账号的同类告警不要每轮都发）和
退避时长（连续失败越多等得越久，上限封顶）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from .dtk import MonitorError


@dataclass(slots=True)
class GlobalGate:
    """A one-way latch with a deadline, shared by every author in a round."""

    default_seconds: int = 60
    backoff_after: int = 2
    backoff_max: int = 600

    #: monotonic deadline; 0 means open
    _until: float = 0.0
    _reason: str = ""
    _closes: int = 0
    _consecutive: int = 0
    _history: list[tuple[float, str, int]] = field(default_factory=list)

    def is_open(self) -> bool:
        return time.monotonic() >= self._until

    def remaining(self) -> float:
        return max(0.0, self._until - time.monotonic())

    @property
    def reason(self) -> str:
        return self._reason if not self.is_open() else ""

    @property
    def times_closed(self) -> int:
        return self._closes

    def close(self, seconds: int, *, reason: str) -> int:
        """Shut the gate for `seconds` (already-resolved, never negative)."""
        seconds = max(1, int(seconds))
        self._until = max(self._until, time.monotonic() + seconds)
        self._reason = reason
        self._closes += 1
        self._history.append((time.monotonic(), reason, seconds))
        del self._history[:-20]
        return seconds

    def backoff_for(self, error: MonitorError) -> int:
        """How long to stay closed for this error.

        Uses the upstream's own `retry_after` when it gives one — it knows better than
        we do — and otherwise doubles with consecutive failures up to the ceiling.
        """
        if error.retry_after:
            self._consecutive += 1
            return int(error.retry_after)
        self._consecutive += 1
        if self._consecutive <= self.backoff_after:
            return self.default_seconds
        doublings = min(self._consecutive - self.backoff_after, 10)
        return min(self.default_seconds * (2**doublings), self.backoff_max)

    def note_success(self) -> None:
        """A request that got through resets the doubling."""
        self._consecutive = 0

    def snapshot(self) -> dict[str, object]:
        return {
            "open": self.is_open(),
            "reason": self.reason,
            "remaining_seconds": round(self.remaining(), 1),
            "times_closed": self._closes,
        }


def should_alert_failure(
    *,
    fails: int,
    threshold: int,
    last_alert: datetime | None,
    now: datetime,
    cooldown_seconds: int,
) -> bool:
    """Whether a failing account deserves another notification yet."""
    if fails < threshold:
        return False
    if last_alert is None:
        return True
    return (now - last_alert).total_seconds() >= cooldown_seconds


__all__ = ["GlobalGate", "should_alert_failure"]
