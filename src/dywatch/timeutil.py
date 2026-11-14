"""时间值的三种转换：解析、取整、序列化。

它们本来各写了两份（`dtk` 与 `webui.queries`、`dtk` 与 `loop`、`loop` 与 `state`）。
合并的依据是**逐行比对过语义完全一致**，不是"看起来差不多"——同名的
`_clip` / `_cap` / `_summarize` 就没有合，因为它们各自服务于不同的输入形状。

这三条都不是"顺手写个工具"意义上的方便函数，而是**契约**：`None` 与 `0` 的区别、
裸时间按 UTC 处理、`Z` 后缀的兼容，都是对着上游实测出来的。改这里要一起改测试。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["int_or_none", "iso", "parse_dt"]


def parse_dt(raw: Any) -> datetime | None:
    """ISO 字符串 → 带时区的 `datetime`；认不出来返回 `None`。

    三个细节都是踩过的：`Z` 后缀要显式换成 `+00:00`（3.11 之前 `fromisoformat`
    不认）、裸时间按 UTC 处理（上游给的都是 UTC）、非字符串一律当"没给"。
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def int_or_none(raw: Any) -> int | None:
    """缺值是 `None`，不是 0 —— 与 `models.py` 的第一条契约同源。

    `bool` 要单独挡掉：`isinstance(True, int)` 为真，而 `True` 在上游是标志位、
    不是数量；写成 1 会让通知里出现一个平台从未给过的数字。
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def iso(value: datetime | None) -> str | None:
    """`datetime` → ISO 字符串；`None` 原样传回去（不写成 `"None"`）。"""
    return value.isoformat() if value is not None else None
