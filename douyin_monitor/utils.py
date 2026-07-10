"""时间工具和通用辅助函数。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def now() -> datetime:
    return datetime.now()


def now_iso() -> str:
    return now().isoformat()


def now_str() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def seconds_since(iso_str: Optional[str]) -> Optional[float]:
    dt = parse_iso(iso_str)
    if dt is None:
        return None
    delta = (now() - dt).total_seconds()
    return max(0.0, delta)


def md_escape(text: Optional[str]) -> str:
    if not text:
        return ""
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
