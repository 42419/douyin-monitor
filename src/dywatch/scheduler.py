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

import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .dtk import MonitorError


@dataclass(slots=True)
class GlobalGate:
    """A one-way latch with a deadline, shared by every author in a round."""

    default_seconds: int = 60
    backoff_after: int = 2
    backoff_max: int = 600
    #: **上游** `retry_after` 的封顶（与 `backoff_max` 是两件事：那个是我们自己退避的封顶）。
    #: 默认 1 小时：上游说"等一小时"就真的等一小时，而不是每十分钟放出一轮请求去撞墙。
    retry_after_max: int = 3600
    #: 可选的结构化日志器（`runtime.StructuredLogger` 那一套）。没给就不记——
    #: 这样单测可以直接构造闸门，不必为了日志多搭一层。
    logger: Any = None

    #: monotonic deadline; 0 means open
    _until: float = 0.0
    _reason: str = ""
    _closes: int = 0
    _consecutive: int = 0
    #: 上游 `retry_after` 被封顶的次数（>0 就说明"我们没完全听上游的"）
    _capped: int = 0
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
        """Shut the gate for `seconds` (already-resolved, never negative).

        截止时间取更长的那个（`max`），**理由只跟着那个更长的截止时间走**。
        早先 `_reason` 是无条件覆盖的：一个 600 秒的熔断之后又来一个 60 秒的限流，
        倒计时显示 600 秒、横幅与日志里写的却是"限流"——看的人会以为再等一分钟就好。
        """
        seconds = max(1, int(seconds))
        deadline = time.monotonic() + seconds
        if deadline > self._until:
            self._until = deadline
            self._reason = reason
        self._closes += 1
        self._history.append((time.monotonic(), reason, seconds))
        del self._history[:-20]
        return seconds

    @staticmethod
    def _parse_retry_after(error: MonitorError) -> int | None:
        """上游给的 `retry_after`（秒）；**解析不了或不合理就当没给**。纯解析：不封顶、无副作用。

        `retry_after` 是原样透传的 JSON 值，类型没人保证：`"30.5"`、`NaN`、负数都可能出现。
        以前闸门根本不关，这些值只是被写进日志；现在它们真的决定"停多久"，所以：

        * 转不成数字 / 非正数 → `None`，退回我们自己的退避（而不是在 `except` 分支里抛
          `ValueError`，让一个账号的异常处理把整轮带崩）。

        封顶放在 `_cap()`，**不放在这里**：解析会在"只是比较一下要不要延长"时被反复调用，
        留痕（日志 + 计数）如果跟着解析走，一次事故被 N 个并发请求撞上就会记 N 次。
        """
        raw = error.retry_after
        if raw is None or isinstance(raw, bool):
            return None
        try:
            seconds = math.ceil(float(raw))
        except (TypeError, ValueError, OverflowError):
            return None
        if seconds <= 0:
            return None
        return seconds

    def _cap(self, seconds: int, error: MonitorError) -> int:
        """封顶 `retry_after_max`（默认 1 小时），**被封顶时留痕**；没封顶就原样返回。

        上游一个异常大的数值（或一次手滑）不该把监控停摆一天。留痕（debug 日志 +
        `retry_after_capped` 计数）是因为"上游说等一小时、我们只等了十分钟"如果完全静默，
        运维只能靠读代码才知道闸门为什么每十分钟开一次。

        **只在这个值真的被用来决定闸门时长的时候调用**（`backoff_for`、以及 `trip` 里真的要
        延长的那一支）：一次事故只记一次，而不是"被几个请求撞上就记几次"。
        """
        if seconds <= self.retry_after_max:
            return seconds
        self._capped += 1
        self._log(
            "gate.retry_after_capped",
            raw=seconds,
            capped=self.retry_after_max,
            code=error.code,
            count=self._capped,
        )
        return self.retry_after_max

    def _log(self, event: str, **fields: Any) -> None:
        """可选日志：没给 logger 时什么都不做（单测里构造闸门不必搭日志）。"""
        if self.logger is not None:
            self.logger.debug(event, **fields)

    def backoff_for(self, error: MonitorError) -> int:
        """How long to stay closed for this error.

        Uses the upstream's own `retry_after` when it gives one — it knows better than
        we do — and otherwise doubles with consecutive failures up to the ceiling.
        """
        self._consecutive += 1
        parsed = self._parse_retry_after(error)
        if parsed is not None:
            return self._cap(parsed, error)
        if self._consecutive <= self.backoff_after:
            return self.default_seconds
        doublings = min(self._consecutive - self.backoff_after, 10)
        return min(self.default_seconds * (2**doublings), self.backoff_max)

    def trip(self, error: MonitorError) -> int:
        """一个"闸门类"错误到了：关闸（或确认它已经关着），返回现在还要停多少秒。

        一轮里有 `MAX_CONCURRENT` 个请求同时在途，**一次**上游故障会让它们几乎同时失败。
        如果每一个都走 `backoff_for()`，`_consecutive` 一次事故就被加了 N 次——它本该数的
        是"连续几次故障"，不是"这次故障被几个请求撞上"：5 并发、默认值下，第 3 个起就
        翻倍成 120 秒，第 4 个 240 秒，实际停多久取决于有几个请求恰好在途。

        所以闸门**已经关着**时来的失败是同一次事故的后到者：不再推进连续计数、不再记一次
        关闸；唯一的例外是它带着**更长**的 `retry_after`（上游明说要等更久），那就延长。

        "更长"按**整秒**比：和 `remaining` 的向上取整比，而不是和浮点余量比。5 个请求都带
        `retry_after=30` 时，第一个把闸门关到 30 秒，后到的那个一比，余量已经是 29.99——
        浮点比较会把它当成"更长"，于是每个后到者都"延长"一次、`times_closed` 记成 5。
        封顶留痕也只在真的要延长时才发生，所以同一次事故同样只记一次。
        """
        if self.is_open():
            return self.close(self.backoff_for(error), reason=error.code)
        parsed = self._parse_retry_after(error)
        if parsed is not None and min(parsed, self.retry_after_max) > math.ceil(
            self.remaining()
        ):
            return self.close(self._cap(parsed, error), reason=error.code)
        return max(1, math.ceil(self.remaining()))

    def note_success(self) -> None:
        """A request that got through resets the doubling."""
        self._consecutive = 0

    def snapshot(self) -> dict[str, object]:
        return {
            "open": self.is_open(),
            "reason": self.reason,
            "remaining_seconds": round(self.remaining(), 1),
            "times_closed": self._closes,
            # 上游 `retry_after` 被封顶的次数：>0 就说明"我们没完全听上游的"，
            # 面板 / `/api/state` / `/metrics` 都能看到（见 DESIGN 修正 #32）
            "retry_after_capped": self._capped,
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
