"""组装与生命周期：把配置变成一堆装配好的部件，并管好它们的开关。

这里的每一件事都只在"进程"这个尺度上发生一次：

* **结构化日志**。事件名 + 键值对（`round.done checked=3 new=2 duration_ms=1400`），
  而不是拼好的句子——这样 `grep round.done` 和 `grep author.crashed` 都能用。
  应用只负责**分级写入**，轮转与压缩交给 logrotate（Linux 的标准分工）。
* **单实例锁**。两个实例同时跑会把同一批账号查两遍，请求速率翻倍，
  而这正是本工具最不该发生的事。所以拿不到锁就直接退出，并且说清为什么。
* **装配**。`Runtime` 持有所有长生命周期对象，`aclose()` 负责把它们关干净。
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .alerts import Deduplicator
from .dtk import DtkClient
from .loop import MonitorLoop
from .messages import one_line
from .notifiers import build_notifier
from .pacer import RequestPacer, RoundWaiter
from .pipeline import ArchiveTrigger
from .scheduler import GlobalGate
from .settings import Settings
from .state import StateStore

LOGGER_NAME = "dywatch"


class StructuredLogger:
    """`logger.info("event.name", key=value, ...)` —— 事件名在前，数据在后。"""

    __slots__ = ("_log",)

    def __init__(self, log: logging.Logger) -> None:
        self._log = log

    def _emit(self, level: int, event: str, **fields: Any) -> None:
        if not self._log.isEnabledFor(level):
            return
        if fields:
            rendered = " ".join(f"{key}={_short(value)}" for key, value in fields.items())
            self._log.log(level, "%s %s", event, rendered)
        else:
            self._log.log(level, "%s", event)

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, **fields)

    def exception(self, event: str, **fields: Any) -> None:
        if fields:
            rendered = " ".join(f"{key}={_short(value)}" for key, value in fields.items())
            self._log.exception("%s %s", event, rendered)
        else:
            self._log.exception("%s", event)


def _short(value: Any, limit: int = 120) -> str:
    """日志字段统一压成一行（控制字符换空格、超长截断）。

    昵称与标题来自上游，可能带换行、回车或 ANSI 转义——那些东西进了终端能把日志行覆盖成
    另一副样子，日志一旦可以被输入伪造就不值得信任了。
    """
    return one_line(value, limit)


def setup_logging(settings: Settings) -> tuple[StructuredLogger, logging.Logger]:
    """Console + `log/info/monitor.log` + `log/debug/monitor.log`.

    `LOG_LEVEL` 只影响终端——这是有意的：终端是给人看现场的，文件是给事后查问题的，
    让一个人为了"终端安静一点"而丢掉排障所需的细节是错的取舍。
    """
    log_dir = settings.log_dir
    (log_dir / "info").mkdir(parents=True, exist_ok=True)
    (log_dir / "debug").mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    info_handler = logging.FileHandler(log_dir / "info" / "monitor.log", encoding="utf-8")
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(fmt)
    logger.addHandler(info_handler)

    debug_handler = logging.FileHandler(log_dir / "debug" / "monitor.log", encoding="utf-8")
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(fmt)
    logger.addHandler(debug_handler)

    console = logging.StreamHandler(sys.stderr)
    level_name = str(settings["LOG_LEVEL"]).upper()
    console.setLevel(getattr(logging, level_name, logging.INFO))
    console.setFormatter(fmt)
    logger.addHandler(console)

    logger.propagate = False
    return StructuredLogger(logger), logger


@contextlib.contextmanager
def single_instance_lock(path: Path) -> Iterator[bool]:
    """Hold an exclusive lock for the lifetime of the process.

    Yields whether the lock was actually taken. On a platform without `fcntl` the
    answer is `False` and the caller decides — this is a Linux-first tool, and a
    silent "pretend we locked it" would be worse than saying so.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - dev on Windows
            yield False
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@dataclass
class Runtime:
    """Everything long-lived, wired once."""

    settings: Settings
    logger: StructuredLogger
    store: StateStore
    client: DtkClient
    notifier: Any
    pacer: RequestPacer
    waiter: RoundWaiter
    gate: GlobalGate
    dedup: Deduplicator
    loop: MonitorLoop

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.aclose()
        with contextlib.suppress(Exception):
            await self.notifier.aclose()
        self.store.close()

    def describe(self) -> list[str]:
        return [
            f"  DTK 实例      : {self.settings['DTK_BASE_URL']}",
            f"  抓取窗口      : {self.settings['FETCH_COUNT']} 条/账号",
            f"  请求节奏      : 账号间 {self.settings['REQUEST_INTERVAL_MIN']}~"
            f"{self.settings['REQUEST_INTERVAL_MAX']} 秒"
            f" | 轮询间隔 {self.settings['POLL_INTERVAL_MIN']}~{self.settings['POLL_INTERVAL_MAX']} 秒"
            f" | 并发 {self.settings['MAX_CONCURRENT']}",
            f"  确认轮数      : 普通 {self.settings['DELETE_CONFIRM_ROUNDS']}"
            f" / 置顶 {self.settings['DELETE_CONFIRM_ROUNDS_TOP']}"
            f" / 全部消失 {self.settings['DELETE_CONFIRM_ROUNDS_ALL']}",
            f"  置顶标志      : INCLUDE_RAW={self.settings['INCLUDE_RAW']}"
            f"（每 {self.settings['RAW_REFRESH_ROUNDS']} 轮最多取一次）",
            f"  归档交叉确认  : {'开启' if self.settings['ARCHIVE_ENABLED'] else '关闭'}",
            f"  归档下载      : "
            + (
                f"开启（pin={'是' if self.settings['ARCHIVE_DOWNLOAD_PIN'] else '否'}，"
                f"每轮最多 {self.settings['ARCHIVE_DOWNLOAD_MAX_PER_ROUND']} 条，"
                "需 media:write scope）"
                if self.settings["ARCHIVE_DOWNLOAD_ENABLED"]
                else "关闭（新作品不触发媒体下载）"
            ),
            f"  推送渠道      : "
            + ("静默模式（不推送）" if self.settings["SILENT_MODE"] else ", ".join(self.notifier.names) or "（无）"),
            f"  状态库        : {self.settings.db_path}",
            f"  工作目录      : {self.settings.home}",
        ]


def build_runtime(settings: Settings, *, logger: StructuredLogger) -> Runtime:
    store = StateStore(settings.db_path)
    store.migrate()

    client = DtkClient(
        settings["DTK_BASE_URL"],
        settings["DTK_API_KEY"],
        wait=float(settings["DTK_WAIT"]),
        timeout=float(settings["DTK_TIMEOUT"]),
        refresh=bool(settings["DTK_REFRESH"]),
        user_agent=str(settings["DTK_USER_AGENT"]),
    )
    notifier = build_notifier(settings)
    pacer = RequestPacer(
        float(settings["REQUEST_INTERVAL_MIN"]), float(settings["REQUEST_INTERVAL_MAX"])
    )
    waiter = RoundWaiter(int(settings["POLL_INTERVAL_MIN"]), int(settings["POLL_INTERVAL_MAX"]))
    gate = GlobalGate(
        default_seconds=60,
        backoff_after=int(settings["BACKOFF_AFTER"]),
        backoff_max=int(settings["BACKOFF_MAX_SECONDS"]),
    )
    dedup = Deduplicator()
    # 归档下载旁路：关着的时候连对象都不建，主链路上就是一个 None 判断
    archive_trigger = None
    if settings["ARCHIVE_DOWNLOAD_ENABLED"]:
        archive_trigger = ArchiveTrigger(
            client=client,
            pacer=pacer,
            pin=bool(settings["ARCHIVE_DOWNLOAD_PIN"]),
            max_per_round=int(settings["ARCHIVE_DOWNLOAD_MAX_PER_ROUND"]),
            logger=logger,
        )
    loop = MonitorLoop(
        settings=settings,
        store=store,
        client=client,
        notifier=notifier,
        pacer=pacer,
        waiter=waiter,
        gate=gate,
        dedup=dedup,
        logger=logger,
        archive_trigger=archive_trigger,
    )
    return Runtime(
        settings=settings,
        logger=logger,
        store=store,
        client=client,
        notifier=notifier,
        pacer=pacer,
        waiter=waiter,
        gate=gate,
        dedup=dedup,
        loop=loop,
    )


__all__ = [
    "LOGGER_NAME",
    "Runtime",
    "StructuredLogger",
    "build_runtime",
    "setup_logging",
    "single_instance_lock",
]
