"""跨模块的协作者契约（`typing.Protocol`）。

`pipeline` 与 `loop` 里的协作者原本写成 `Any`：能跑，但 IDE 补全、类型检查、
以及"这个函数到底需要什么"一起消失了。用 `Protocol` 而不是直接 import 具体类，有两个原因：

* `pipeline` 不该 import `notifiers`——`notifiers.base` 已经 import 了 `render`，
  反向再加一条边会让依赖图成环。`Protocol` 是纯结构描述，不产生运行时依赖。
* 测试里的假对象（`RoundClient`、`Logger`）只要形状对就能用，不必继承任何基类，
  也不必为了通过类型检查而多写一层适配。

**这些协议只描述"被调用到的方法"，不是 `DtkClient` / `Notifier` 的完整镜像。**
加一个新的调用点时顺手把方法加进来——那份清单本身就是"这个模块依赖了什么"。
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import ArchiveItem, Page
from .render import Message

__all__ = [
    "DeliveryLike",
    "DtkClientLike",
    "DtkProbe",
    "DtkReader",
    "LoggerLike",
    "NotifierLike",
]


class DtkReader(Protocol):
    """`pipeline` 与归档旁路需要的那四个方法。"""

    async def author_posts(
        self, sec_user_id: str, count: int, *, include_raw: bool = False
    ) -> Page: ...

    async def archive_of_author(
        self, sec_user_id: str, *, limit: int = 50
    ) -> dict[str, ArchiveItem]: ...

    async def start_download(
        self, content_id: str, *, skip_existing: bool = True
    ) -> dict[str, Any]: ...

    async def pin_download(self, download_id: str, pinned: bool) -> dict[str, Any]: ...


class DtkProbe(Protocol):
    """`loop` 只需要读一次上游健康快照。"""

    async def system_status(self) -> dict[str, Any]: ...


class DtkClientLike(DtkReader, DtkProbe, Protocol):
    """`loop` 既要读列表、也要读健康快照，所以两个面都要。

    只写 `DtkReader` 会让 `loop` 里那次 `system_status()` 变成类型错误；
    只写 `DtkProbe` 又会让它转手传给 `run_author` 时不匹配。合成一个即可。
    """


class DeliveryLike(Protocol):
    """`Notifier.send` 的返回值：`pipeline` 只读这两个字段与 `as_dict()`。"""

    sent: list[str]
    failed: dict[str, str]

    def as_dict(self) -> dict[str, Any]: ...


class NotifierLike(Protocol):
    async def send(self, message: Message) -> DeliveryLike: ...


class LoggerLike(Protocol):
    """结构化日志的四档。`info` 是兜底（`_log` 用它做 `getattr` 的默认值）。"""

    def debug(self, event: str, **fields: Any) -> None: ...

    def info(self, event: str, **fields: Any) -> None: ...

    def warning(self, event: str, **fields: Any) -> None: ...

    def error(self, event: str, **fields: Any) -> None: ...
