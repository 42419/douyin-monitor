"""Server 酱（sctapi.ftqq.com）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import HttpChannel

#: Server 酱的标题上限。**单位是字节，不是字符**：官方限制 32 字节，而一个汉字 3 字节，
#: 于是"【新作品】某某 发布了新视频"这种标题本来就超了。早先按字符切（`subject[:32]`）
#: 对中文等于没截——标题发过去要么被对方截断成乱码状的半截，要么被拒，而我们这边
#: 记的是"发送成功"。按 UTF-8 字节切，并且不把一个字符切成两半。
TITLE_BYTES = 32


def _clip_bytes(text: str, limit: int = TITLE_BYTES) -> str:
    """按 UTF-8 字节数截断，且不切坏多字节字符。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


@dataclass(frozen=True, slots=True)
class ServerChanChannel(HttpChannel):
    name: str = "serverchan"
    sendkey: str = ""
    error_field: str | None = "code"

    def request(self, message: Any) -> tuple[str, dict[str, Any]]:
        return (
            f"https://sctapi.ftqq.com/{self.sendkey}.send",
            {"title": _clip_bytes(message.subject), "desp": message.markdown},
        )
