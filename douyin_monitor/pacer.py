"""请求节奏控制器：保证整体请求频率不因并发而提高。"""

from __future__ import annotations

import random
import threading
import time


class RequestPacer:
    """控制"发起新请求"的整体节奏，与并发线程数无关。

    每次要发起新请求前先向这里"排队报到"，本类保证相邻两次"报到通过"
    之间至少间隔 [min_interval, max_interval) 秒（随机）。报到通过之后，
    实际的请求执行在报到锁之外进行，不会出现"一个请求卡住，后面排队的
    线程也跟着卡住"的情况。
    """

    def __init__(self, min_interval: float, max_interval: float, stop_event: threading.Event):
        self._min_interval = min_interval
        self._max_interval = max_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._stop_event = stop_event

    def wait_for_turn(self) -> None:
        with self._lock:
            now = time.monotonic()
            start_of_slot = max(now, self._next_allowed)
            wait_for = start_of_slot - now
            self._next_allowed = start_of_slot + random.uniform(self._min_interval, self._max_interval)
        if wait_for > 0:
            self._stop_event.wait(wait_for)
