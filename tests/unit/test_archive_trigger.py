"""`ArchiveTrigger`：新作品 → DTK 归档下载的旁路。

这个文件锁的是四条不变量，按重要性排序（也是设计 D17 里写下的那四条）：

1. **不拖住主链路** —— 每次请求都过节奏器、写请求用短超时、**任何**失败都不外抛
2. **不丢档** —— `NEW_POST` 只出现一次，所以失败/退避/超预算的条目必须留在队列里，
   下一轮接着发；只有"确实没有可下载的媒体"和"试满次数"才出队
3. **不突发** —— 预算按轮算（不是按账号算），且每个请求都排在节奏器后面
4. **不刷屏** —— 容量满 / 配置不对进入退避窗口，窗口内一条请求都不发
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from dywatch.alerts import Deduplicator
from dywatch.dtk import MonitorError
from dywatch.models import (
    AuthorState,
    Content,
    DiffConfig,
    Event,
    EventKind,
    Page,
    PostState,
)
from dywatch.pipeline import (
    ARCHIVE_MAX_ATTEMPTS,
    ArchiveTrigger,
    run_author,
)
from dywatch.scheduler import GlobalGate
from dywatch.state import StateStore


class Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _record(event: str, **fields: Any) -> None:
            self.records.append((level, event, fields))

        return _record

    def events(self, name: str) -> list[dict]:
        return [fields for _level, event, fields in self.records if event == name]


class Pacer:
    """只数次数，不真的等——节奏本身由 test_pacer.py 负责。"""

    def __init__(self) -> None:
        self.calls = 0

    async def wait_for_turn(self) -> float:
        self.calls += 1
        return 0.0


class Client:
    def __init__(self, *, fail: Exception | None = None, pin_fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail
        self._pin_fail = pin_fail

    async def start_download(self, content_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start", content_id))
        if self._fail is not None:
            raise self._fail
        return {"download_id": f"d-{content_id}", "task_id": "t1", "state": "queued",
                "archived": True, "reused": None}

    async def pin_download(self, download_id: str, pinned: bool) -> dict[str, Any]:
        self.calls.append(("pin", download_id))
        if self._pin_fail is not None:
            raise self._pin_fail
        return {"download_id": download_id, "pinned": pinned}


def make_trigger(client: Client, *, pin: bool = False, max_per_round: int = 10,
                 pacer: Pacer | None = None, logger: Logger | None = None) -> ArchiveTrigger:
    return ArchiveTrigger(
        client=client, pacer=pacer or Pacer(), pin=pin, max_per_round=max_per_round,
        logger=logger or Logger(),
    )


def new_post(content_id: str, sec_user_id: str = "u1") -> Event:
    return Event(EventKind.NEW_POST, sec_user_id=sec_user_id, content_id=content_id)


# --------------------------------------------------------------------- 触发范围

async def test_only_new_post_events_are_queued():
    client, logger = Client(), Logger()
    trigger = make_trigger(client, logger=logger)

    await trigger.trigger([
        new_post("c1"),
        Event(EventKind.POST_REMOVED, sec_user_id="u1", content_id="c2"),
        Event(EventKind.TITLE_CHANGED, sec_user_id="u1", content_id="c3"),
        Event(EventKind.REVIVED, sec_user_id="u1", content_id="c4"),
        new_post("c5"),
    ])

    assert [cid for _kind, cid in client.calls] == ["c1", "c5"]


async def test_event_without_content_id_is_ignored():
    client = Client()
    trigger = make_trigger(client)
    await trigger.trigger([Event(EventKind.NEW_POST, sec_user_id="u1", content_id=None)])
    assert client.calls == []


async def test_same_content_id_is_queued_once():
    client = Client(fail=MonitorError("INTERNAL", "boom"))
    trigger = make_trigger(client)

    await trigger.trigger([new_post("c1")])
    await trigger.trigger([new_post("c1")])  # 同一轮里重复出现（多账号同名 id 等）

    assert trigger.pending == 1  # 不会被排两遍


# --------------------------------------------------------------------- 节奏

async def test_every_request_goes_through_the_pacer():
    pacer = Pacer()
    client = Client()
    trigger = make_trigger(client, pacer=pacer)
    await trigger.trigger([new_post("c1"), new_post("c2")])
    assert pacer.calls == 2, "每条下载都要过节奏器，不能突然连发"

    pacer, client = Pacer(), Client()
    trigger = make_trigger(client, pin=True, pacer=pacer)
    await trigger.trigger([new_post("c1")])
    assert pacer.calls == 2, "pin 是第二个写请求，也要单独过节奏器"


async def test_pin_is_a_separate_call_after_start():
    client = Client()
    trigger = make_trigger(client, pin=True)
    await trigger.trigger([new_post("c1")])
    assert client.calls == [("start", "c1"), ("pin", "d-c1")]


async def test_pin_failure_does_not_label_the_download_as_failed():
    """下载已经受理并正在跑，pin 失败是另一件事（这条档不豁免淘汰而已）。"""
    client = Client(pin_fail=MonitorError("FORBIDDEN_SCOPE", "no media:write"))
    logger = Logger()
    trigger = make_trigger(client, pin=True, logger=logger)

    await trigger.trigger([new_post("c1")])

    assert logger.events("archive.download_started"), "受理本身要记成 started"
    assert logger.events("archive.pin_failed")[0]["code"] == "FORBIDDEN_SCOPE"
    assert not logger.events("archive.download_failed"), "别把它说成下载失败"
    assert trigger.pending == 0


# --------------------------------------------------------------------- 不丢档

async def test_invalid_param_drops_the_item_without_retrying():
    """这条作品确实没有可下载的媒体，重试一百次也一样。"""
    client = Client(fail=MonitorError("INVALID_PARAM", "nothing to fetch"))
    logger = Logger()
    trigger = make_trigger(client, logger=logger)

    await trigger.trigger([new_post("c1")])
    await trigger.trigger([])

    assert len(client.calls) == 1, "不该反复重试"
    assert trigger.pending == 0
    assert logger.events("archive.download_skipped")[0]["code"] == "INVALID_PARAM"


async def test_transient_failure_keeps_the_item_and_retries_next_round():
    client = Client(fail=MonitorError("DTK_UNREACHABLE", "connection refused"))
    logger = Logger()
    trigger = make_trigger(client, logger=logger)

    await trigger.trigger([new_post("c1")])
    assert trigger.pending == 1, "网络抖动不能把档弄丢"
    assert logger.events("archive.download_failed")[0]["attempts"] == 1

    await trigger.trigger([])  # 下一轮（还有预算）
    assert len([c for c in client.calls if c[0] == "start"]) == 2


async def test_item_is_given_up_after_max_attempts():
    """一条永远失败的条目不能占着队首：每轮只花一个预算，后面的永远排不上。"""
    client = Client(fail=MonitorError("INTERNAL", "boom"))
    logger = Logger()
    trigger = make_trigger(client, logger=logger)

    for _ in range(ARCHIVE_MAX_ATTEMPTS):
        trigger.start_round()
        await trigger.trigger([new_post("c1")])

    assert trigger.pending == 0
    assert logger.events("archive.download_given_up")[0]["attempts"] == ARCHIVE_MAX_ATTEMPTS


async def test_unexpected_client_error_still_never_propagates():
    """旁路唯一不可接受的行为是"把主流程带崩"，连客户端自己出 bug 都要兜住。"""
    client = Client(fail=RuntimeError("client bug"))
    logger = Logger()
    trigger = make_trigger(client, logger=logger)

    await trigger.trigger([new_post("c1")])  # 不抛就是这条测试要的结论

    assert trigger.pending == 1, "意外也要留队列，下一轮再试"
    assert logger.events("archive.download_failed")[0]["code"] == "RuntimeError"


async def test_cancellation_is_not_swallowed():
    """停机时的 CancelledError 必须照常往上走（所以只 catch Exception，不 catch BaseException）。"""
    import asyncio

    class CancellingClient(Client):
        async def start_download(self, content_id: str, **kwargs: Any) -> dict[str, Any]:
            raise asyncio.CancelledError()

    trigger = make_trigger(CancellingClient())
    with pytest.raises(asyncio.CancelledError):
        await trigger.trigger([new_post("c1")])


# --------------------------------------------------------------------- 退避

async def test_capacity_failure_mutes_the_whole_path(monkeypatch):
    client = Client(fail=MonitorError("QUEUE_FULL", "storage at capacity", retry_after=300))
    logger = Logger()
    trigger = make_trigger(client, logger=logger)

    await trigger.trigger([new_post("c1")])

    assert logger.events("archive.muted_raised")[0]["seconds"] == 300
    assert trigger.muted_code == "QUEUE_FULL"
    assert trigger.pending == 1, "容量满不是丢掉这条档的理由"

    # 窗口内：一条请求都不该发出去
    await trigger.trigger([new_post("c2")])
    assert len(client.calls) == 1
    assert logger.events("archive.muted")

    # 窗口过后：接着发（c1 在前，c2 也在队列里）
    pacer = Pacer()
    trigger._pacer = pacer
    monkeypatch.setattr(time, "monotonic", lambda: trigger._muted_until + 1)
    fresh = Client()
    trigger._client = fresh
    await trigger.trigger([])
    assert [cid for _kind, cid in fresh.calls] == ["c1", "c2"]


async def test_config_failure_mutes_for_an_hour():
    """缺 scope / 没配下载器这类问题重试没有意义，等够久就行。"""
    client = Client(fail=MonitorError("NOT_CONFIGURED", "no downloader"))
    logger = Logger()
    trigger = make_trigger(client, logger=logger)

    await trigger.trigger([new_post("c1")])

    assert logger.events("archive.muted_raised")[0]["seconds"] == 3600
    assert logger.events("archive.muted_raised")[0]["code"] == "NOT_CONFIGURED"


# --------------------------------------------------------------------- 预算

async def test_budget_is_per_round_not_per_account(logger=None):
    client, log = Client(), Logger()
    trigger = make_trigger(client, max_per_round=2, logger=log)

    trigger.start_round()
    await trigger.trigger([new_post(f"c{i}") for i in range(5)])

    assert [cid for _kind, cid in client.calls] == ["c0", "c1"], "一轮只发 2 条"
    assert trigger.pending == 3
    assert log.events("archive.pending")[0]["pending"] == 3

    trigger.start_round()
    await trigger.trigger([])
    assert [cid for _kind, cid in client.calls] == ["c0", "c1", "c2", "c3"]


async def test_budget_is_not_reset_by_a_second_account_in_the_same_round():
    """预算是按轮算的：第 2 个账号触发时不该又拿到满满一份。"""
    client = Client()
    trigger = make_trigger(client, max_per_round=1)

    trigger.start_round()
    await trigger.trigger([new_post("c1", sec_user_id="u1")])
    await trigger.trigger([new_post("c2", sec_user_id="u2")])

    assert len(client.calls) == 1
    assert trigger.pending == 1


async def test_queue_overflow_drops_the_oldest_with_a_warning():
    """队列有上限，但只在极端积压时才会碰到（正常情况下一轮就发完了）。"""
    from dywatch.pipeline import ARCHIVE_QUEUE_MAX

    # 先让它进一个长退避窗口，这样后面的新作品只积压、不会被发出去
    client = Client(fail=MonitorError("QUEUE_FULL", "full", retry_after=3600))
    logger = Logger()
    trigger = make_trigger(client, logger=logger)
    await trigger.trigger([new_post("c0")])

    for index in range(1, ARCHIVE_QUEUE_MAX + 5):
        await trigger.trigger([new_post(f"c{index}")])

    assert trigger.pending == ARCHIVE_QUEUE_MAX
    assert logger.events("archive.queue_overflow")
    # 丢的是最旧的（队首已经不再是 c0），保留最新的一批
    assert trigger._pending[0][0] == "c5"


async def test_pending_survives_a_muted_round_and_is_reported():
    client = Client(fail=MonitorError("QUEUE_FULL", "full", retry_after=60))
    logger = Logger()
    trigger = make_trigger(client, logger=logger)

    await trigger.trigger([new_post("c1")])
    await trigger.trigger([new_post("c2")])

    assert trigger.pending == 2
    assert logger.events("archive.muted")[0]["pending"] == 2


# ------------------------------------------------------- 与 run_author 的集成

class Delivery:
    def as_dict(self) -> dict[str, Any]:
        return {"sent": 1, "failed": 0}


class RoundClient:
    """同时扮演"抓列表"与"触发下载"两个角色，并按发生顺序记下来。"""

    def __init__(self, order: list[tuple[str, Any]], *, download_error: Exception | None = None) -> None:
        self.order = order
        self._download_error = download_error

    async def author_posts(self, sec_user_id: str, count: int, **kwargs: Any):
        return Page(
            items=(Content(content_id="new1", title="新作品", created_at=datetime.now(timezone.utc)),),
        )

    async def start_download(self, content_id: str, **kwargs: Any) -> dict[str, Any]:
        self.order.append(("download", content_id))
        if self._download_error is not None:
            raise self._download_error
        return {"download_id": "d1", "state": "queued", "archived": True, "reused": None}


class Notifier:
    def __init__(self, order: list[tuple[str, Any]]) -> None:
        self.order = order

    async def send(self, message: Any) -> Delivery:
        self.order.append(("notify", getattr(message, "title", None)))
        return Delivery()


async def _run_one_round(tmp_path, *, download_error: Exception | None = None):
    order: list[tuple[str, Any]] = []
    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    now = datetime.now(timezone.utc)
    author = AuthorState(
        sec_user_id="u1", nickname="示例", initialized_at=now - timedelta(days=30),
        ever_had_posts=True, runs=5, last_seen_at=now - timedelta(minutes=1),
        posts=(PostState(content_id="old1", created_at=now - timedelta(days=2)),),
    )
    client = RoundClient(order, download_error=download_error)
    pacer = Pacer()   # 只数次数；节奏本身由 test_pacer.py 负责
    gate = GlobalGate(default_seconds=60, backoff_after=2, backoff_max=600)
    trigger = ArchiveTrigger(client=client, pacer=pacer, logger=Logger())

    result = await run_author(
        author=author, nickname="示例", client=client, store=store,
        notifier=Notifier(order), dedup=Deduplicator(), pacer=pacer, gate=gate,
        cfg=DiffConfig(), now=now, archive_enabled=False, archive_trigger=trigger,
        logger=Logger(),
    )
    return result, order, store, gate, pacer, trigger


async def test_notification_is_sent_before_the_archive_is_triggered(tmp_path):
    """顺序不能换：通知是主链路，归档是旁路。旁路慢，只该推迟下一轮，不该推迟消息。"""
    result, order, store, _gate, pacer, _trigger = await _run_one_round(tmp_path)

    assert [step for step, _payload in order] == ["notify", "download"]
    assert result.status == "ok" and result.new_count == 1
    # 抓列表与下载各过一次节奏器，所以旁路没有绕开限速
    assert pacer.calls == 2
    # 事件已经落库（`save_round` 在通知与归档之前）：归档这条路无论出什么事都丢不了它。
    # 这里还会看到一条 scrolled_out——新作品把旧作品挤出了窗口，属于正常判定。
    kinds = [row["kind"] for row in store.recent_events()]
    assert "new_post" in kinds
    assert set(kinds) <= {"new_post", "scrolled_out"}


async def test_failing_archive_leaves_the_round_and_the_gate_untouched(tmp_path):
    result, order, _store, gate, _pacer, trigger = await _run_one_round(
        tmp_path, download_error=MonitorError("QUEUE_FULL", "capacity", retry_after=120)
    )

    assert [step for step, _payload in order] == ["notify", "download"]
    assert result.status == "ok" and result.new_count == 1
    assert gate.is_open(), "归档的问题不该关监控的闸门"
    assert trigger.muted_code == "QUEUE_FULL"
