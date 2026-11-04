"""错误模型与错误码分类 —— `dtk.py` 与 `diff.py` 共用的唯一定义。

为什么单独一个模块：`diff.py` 要判断 `error.is_gate` / `error.is_config`，而
DESIGN §4.1 的接缝要求 **`diff.py` 不 import `dtk.py`**（dtk 是唯一知道 HTTP 的
模块）。把错误类型放到这里之后，`dtk.py` 与 `diff.py` 都只依赖这个不含任何 I/O、
不 import 本包其它模块的小文件，接缝得以成立。

错误码是不可变契约：只增不改。
"""

from __future__ import annotations

from typing import Any, Final, Mapping

#: 上游错误码里，值得整体闸门暂停的那些 —— 都是"现在别发请求"的意思。
GATE_CODES: Final[frozenset[str]] = frozenset(
    {
        "RATE_LIMITED",
        "IDENTITY_POOL_EXHAUSTED",
        "ENDPOINT_CIRCUIT_OPEN",
        "QUEUE_FULL",
    }
)

#: 配置性问题，重试没有意义，应当直接告诉人，并且**连坐所有账号**。
#:
#: `IDENTITY_LOGIN_REQUIRED` 自 DTK 5.1.4 起新增（commit d2cf32b）：它表示"这条
#: 读请求需要一个已登录的身份，而当前没有"。在 dywatch 的单身份模式下，这等价于
#: `DTK_IDENTITY` 那个 cookie 没登录 / 已过期，是**全局**的配置问题，不是某个账号的
#: 偶发失败。不并进来会让每个账号各自累计失败、各自发一条告警（N 条噪音），而真正该
#: 看到的是一条"登录身份失效"。
#:
#: **`INVALID_PARAM` 刻意不在这里**：它的作用域通常只有一个账号（users.conf 里某个
#: sec_user_id 写错，DTK 回 400），连坐所有账号是错的。原因见 dtk.py 的历史注记。
CONFIG_CODES: Final[frozenset[str]] = frozenset(
    {
        "UNAUTHENTICATED",
        "FORBIDDEN_SCOPE",
        "NOT_CONFIGURED",
        "IDENTITY_LOGIN_REQUIRED",
    }
)


class MonitorError(RuntimeError):
    """Every failure this tool can produce, normalized to one code."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        details: Mapping[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retry_after = retry_after

    @property
    def is_gate(self) -> bool:
        return self.code in GATE_CODES

    @property
    def is_config(self) -> bool:
        return self.code in CONFIG_CODES

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


__all__ = ["CONFIG_CODES", "GATE_CODES", "MonitorError"]
