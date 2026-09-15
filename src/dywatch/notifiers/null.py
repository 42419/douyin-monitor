"""静默模式的空通知器：什么都不发，但仍然返回"已处理"。

它存在的意义是让 `SILENT_MODE` 不必在调用处写成 `if`。监控、判定、状态、面板全部照常，
只有最后一跳被替换掉——于是"静默跑一周看看数据对不对"和"正式上线"之间只差一个开关。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..render import Message
from .base import Delivery


class NullNotifier:
    """Accepts everything, delivers nothing."""

    name = "null"

    async def send(self, message: Message) -> Delivery:
        del message
        return Delivery()

    async def send_test(self) -> Delivery:
        return Delivery()

    async def aclose(self) -> None:
        return None

    @property
    def names(self) -> list[str]:
        return []

    @property
    def channels(self) -> Sequence[Any]:
        return ()


__all__ = ["NullNotifier"]
