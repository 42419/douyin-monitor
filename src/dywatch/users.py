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

#: "看起来什么都没显示、但确实是一个字符"的那些。它们骗得过肉眼，却会进 SQLite、
#: 进日志、进通知，也会让上游一直拒绝这个 ID：
#:
#: * `U+FEFF` BOM —— Windows 编辑器保存 UTF-8 时最爱加的那个，**只污染第一行**：
#:   于是"明明配了两个账号，只有一个在工作"，而那个 ID 在面板和日志里看起来一模一样。
#:   `.env` 那边早就用 `utf-8-sig` + `_dropped_env_keys` 防了这一手，users.conf 漏了。
#: * `U+200B` 零宽空格 / `U+200C` 零宽非连接符 / `U+200D` 零宽连接符 —— 从网页上复制
#:   ID 时最容易夹进来的东西。
#: * `U+200E`/`U+200F` 方向标记、`U+2060` 词连接符、`U+00A0` 不换行空格、
#:   `U+3000` 全角空格（`str.isspace()` 认得出其中一部分，认不出零宽那一类）。
INVISIBLE_CHARS = frozenset("\ufeff\u200b\u200c\u200d\u200e\u200f\u2060\u00a0\u3000")


@dataclass(frozen=True, slots=True)
class UserEntry:
    sec_user_id: str
    nickname: str


def strip_inline_comment(text: str) -> str:
    """去掉行尾注释：`#` 前面是空白就算注释，直到行尾；没有就原样返回。

    **这是 users.conf 里"什么算注释"的唯一判定**，解析（`parse_users`）与写入
    （`dywatch add` 校验昵称）共用同一个函数——两边各写一份的话，写进去的昵称和读回来的
    昵称就可能不是同一个东西（`小王 #1` 写入后读回只剩 `小王`，且没有任何提示）。

    规则与 `NOTIFY_TARGETS` 一致：行首的 `#`、或前面有空白的 `#` 是注释；
    `账号#1` / `|#热门` 这种 `#` 紧贴前一个字符的，是内容，原样保留。
    判定对象是**未经 strip 的原始行**：`ID| # 主账号` 里 `|` 与 `#` 之间的空格同样算空白。
    """
    for index, char in enumerate(text):
        if char == "#" and index and text[index - 1].isspace():
            return text[:index]
    return text


def is_safe_id(value: str) -> bool:
    if not value or len(value) > MAX_ID_LENGTH:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ch in INVISIBLE_CHARS for ch in value):
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
        # 行内注释：`ID|市场部 # 主账号` 里的 `# 主账号` 是给人看的，不是昵称的一部分。
        # 不剥掉的话它会跟着进**每一条通知**、面板和 /metrics 的 label。
        # 对**整行**做、且在 strip 之前：`ID| # 主账号` 里 `|` 后面的空格也算"前面有空白"，
        # 否则 strip 之后 `#` 成了昵称的第一个字符，注释就变成了昵称。
        line = strip_inline_comment(raw_line).strip()
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
                log.warning(
                    "users.bad_id", lineno=lineno, reason="ID 含空白/控制字符或超长"
                )
            continue
        if sec_user_id in seen:
            if log:
                log.warning(
                    "users.duplicate",
                    lineno=lineno,
                    kept=seen[sec_user_id],
                    ignored=nickname,
                )
            continue
        seen[sec_user_id] = nickname
        entries.append(UserEntry(sec_user_id=sec_user_id, nickname=nickname))
    return entries


def load_users_conf(path: Path, *, logger: Any = None) -> list[UserEntry]:
    try:
        # `utf-8-sig`：Windows 编辑器存出来的 users.conf 常带 BOM，不去掉的话**第一个**
        # 账号的 ID 会多出一个 `\ufeff`——它现在会被 `is_safe_id` 拦下（进日志点名），
        # 但更好的做法是根本不把它当内容：`.env` 那边用的也是这个编码。
        text = path.read_text(encoding="utf-8-sig")
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


__all__ = [
    "MAX_ID_LENGTH",
    "UserEntry",
    "is_safe_id",
    "load_users_conf",
    "parse_users",
    "resolve_input",
]
