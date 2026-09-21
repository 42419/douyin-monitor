"""轮次日志：开始 / 结束 / 跳过三行的口径。

排障时这三行是"这轮到底有没有在跑、跑了什么、为什么没跑"的唯一线索，
所以钉住两件事：**都带轮次号**（能把开始和结束对上，也能对上面板的"本次运行第 N 轮"），
以及 **`round.start` 只属于"真的开始跑了"的轮次**（被跳过的一轮只有 `round.skipped`）。
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from dywatch.alerts import Deduplicator
from dywatch.loop import MonitorLoop
from dywatch.models import Page
from dywatch.pacer import RoundWaiter
from dywatch.scheduler import GlobalGate
from dywatch.settings import load_settings
from dywatch.state import StateStore

UID = "MS4wLjABAAAAxxx"


class Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _record(event: str, **fields: Any) -> None:
            self.records.append((level, event, fields))

        return _record

    def names(self) -> list[str]:
        return [event for _level, event, _fields in self.records]

    def events(self, name: str) -> list[dict]:
        return [fields for _level, event, fields in self.records if event == name]


class Client:
    """只回空列表：够跑完一轮，且不会碰 `author_profile`（核验没打开）。"""

    async def author_posts(self, sec_user_id: str, count: int, **kwargs: Any) -> Page:
        return Page(items=(), raw_included=False)


class Notifier:
    #: 面板快照会把它写进 status.json（`channels`）
    names = ("test",)

    async def send(self, message: Any) -> Any:
        class _Delivery:
            def as_dict(self) -> dict:
                return {"sent": 1, "failed": 0}

        return _Delivery()


class Pacer:
    async def wait_for_turn(self) -> None:
        return None


class Waiter:
    async def sleep(self, seconds: float) -> None:  # pragma: no cover - 只给 run() 用
        return None


def make_loop(tmp_path: pathlib.Path, logger: Logger, *, users: str | None = None) -> MonitorLoop:
    settings = load_settings(None, environ={"MONITOR_HOME": str(tmp_path)})
    if users is not None:
        settings.users_conf.write_text(users, encoding="utf-8")
    store = StateStore(settings.db_path)
    store.migrate()
    return MonitorLoop(
        settings=settings,
        store=store,
        client=Client(),
        notifier=Notifier(),
        pacer=Pacer(),  # type: ignore[arg-type]
        waiter=Waiter(),  # type: ignore[arg-type]
        gate=GlobalGate(),
        dedup=Deduplicator(),
        logger=logger,
    )


async def test_round_start_is_logged_before_done_and_both_carry_the_round_number(tmp_path):
    logger = Logger()
    loop = make_loop(tmp_path, logger, users=f"{UID}|示例账号\n")
    loop.reload_users(force=True)

    await loop.run_round()

    names = logger.names()
    assert "round.start" in names, "每轮开始要有一行"
    assert "round.done" in names
    assert names.index("round.start") < names.index("round.done"), "start 必须在 done 之前"

    start = logger.events("round.start")[0]
    done = logger.events("round.done")[0]
    assert start["round"] == 1 and done["round"] == 1, "两行都要带轮次号才配得上"
    assert start["users"] == 1

    await loop.run_round()
    assert logger.events("round.start")[1]["round"] == 2, "轮次号要递增"


async def test_a_skipped_round_does_not_claim_to_have_started(tmp_path):
    """闸门关着的时候这一轮什么都没做：只有 `round.skipped`，不该出现 `round.start`。"""
    logger = Logger()
    loop = make_loop(tmp_path, logger, users=f"{UID}|示例账号\n")
    loop.reload_users(force=True)
    loop.gate.close(60, reason="upstream_degraded")

    await loop.run_round()

    names = logger.names()
    assert "round.skipped" in names
    assert "round.start" not in names, "没真的跑就不该记 start"
    assert "round.done" not in names
    assert logger.events("round.skipped")[0]["round"] == 1
    assert logger.events("round.skipped")[0]["reason"] == "gate closed"


async def test_no_users_also_logs_the_round_number(tmp_path):
    logger = Logger()
    loop = make_loop(tmp_path, logger, users=None)   # 没有 users.conf
    loop.reload_users(force=True)

    await loop.run_round()

    skipped = logger.events("round.skipped")
    assert skipped and skipped[0]["reason"] == "no users configured"
    assert skipped[0]["round"] == 1
    assert "round.start" not in logger.names()
