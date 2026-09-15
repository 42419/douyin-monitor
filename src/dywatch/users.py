"""`users.conf`：一行一个账号，格式 `sec_user_id|昵称`。

这个文件是**给人手改的**，所以解析要容错而不是严格：注释、空行、多余空白都吃掉，
重复的 ID 只保留第一次并在日志里点名。但它同时是**输入校验点**——`sec_user_id` 会进
SQLite、进日志、进通知，所以这里拒绝空白与控制字符（旧项目还额外拒绝了 Windows 文件名
非法字符，本工具只跑 Linux，那一条不再需要）。

热加载由调用方按 mtime 决定（见 `loop.py`），本模块只负责"把文件读成条目"。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 抖音的 `sec_user_id` 是 base64 风格的串，实测长度在 40~60 之间；
#: 放宽到 200 是为了不在上游改格式时误杀，同时也挡住了明显的手滑粘贴。
MAX_ID_LENGTH = 200


@dataclass(frozen=True, slots=True)
class UserEntry:
    sec_user_id: str
    nickname: str


def is_safe_id(value: str) -> bool:
    if not value or len(value) > MAX_ID_LENGTH:
        return False
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        return False
    # `|` 是分隔符，`/` 与 `\` 没有任何理由出现在 ID 里
    return not any(ch in value for ch in "|/\\")


def parse_users(text: str, *, logger: Any = None) -> list[UserEntry]:
    """Parse the file body. Kept separate from IO so it is testable.

    `logger` is the structured logger from `runtime` (or anything with the same
    `warning(event, **fields)` shape). Passing None silences the diagnostics —
    which is what `--config-check` wants.
    """
    log = logger
    entries: list[UserEntry] = []
    seen: dict[str, str] = {}

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            if log:
                log.warning("users.bad_line", lineno=lineno, reason="缺少 | 分隔符")
            continue
        sec_user_id, _, nickname = line.partition("|")
        sec_user_id, nickname = sec_user_id.strip(), nickname.strip()
        if not nickname:
            nickname = sec_user_id[-8:]
        if not is_safe_id(sec_user_id):
            if log:
                log.warning("users.bad_id", lineno=lineno, reason="ID 含空白/控制字符或超长")
            continue
        if sec_user_id in seen:
            if log:
                log.warning(
                    "users.duplicate", lineno=lineno, kept=seen[sec_user_id], ignored=nickname
                )
            continue
        seen[sec_user_id] = nickname
        entries.append(UserEntry(sec_user_id=sec_user_id, nickname=nickname))
    return entries


def load_users_conf(path: Path, *, logger: Any = None) -> list[UserEntry]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_users(text, logger=logger)


def resolve_input(raw: str) -> str:
    """Accept a bare id or a profile URL's tail, and return the bare id.

    A profile link is handled by DTK's `/tools/parse-url` (see `cli add`) because that
    endpoint knows every link shape; this helper only strips what is unambiguous.
    """
    text = raw.strip()
    if "/" in text:
        text = text.rstrip("/").rsplit("/", 1)[-1]
    return text.split("?", 1)[0]


__all__ = ["MAX_ID_LENGTH", "UserEntry", "is_safe_id", "load_users_conf", "parse_users", "resolve_input"]
