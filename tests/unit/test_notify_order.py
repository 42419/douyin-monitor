"""通知投递顺序：**`new_post` 必须第一个发出去**。

它是这个工具存在的理由，而且错过就补不回来（用户不会知道曾经有过这条作品）。而一轮里
所有事件共用同一条投递通道——每条都要等渠道间隔（`NOTIFY_GAP`）、每个渠道最多 8 秒超时 ×
2 次重试，排在 `new_post` 前面的每一条都可能把它推到几十秒之后，甚至撞上渠道限流让它变成
"发送失败"那一条。所以投递顺序由 `alerts.NOTIFY_PRIORITY` 决定，不是 `diff` 的输出顺序。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from dywatch.alerts import Deduplicator, NOTIFY_PRIORITY, priority_of, should_send
from dywatch.models import (
    NOTIFY_KINDS,
    SILENT_KINDS,
    AuthorState,
    Content,
    DiffConfig,
    Event,
    EventKind,
    Page,
    PostState,
)
from dywatch.pipeline import notify_system_event, run_author
from dywatch.scheduler import GlobalGate
from dywatch.state import StateStore

NOW = datetime.now(timezone.utc)
UID = "MS4wLjABAAAAorder"


class Logger:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _record(event: str, **fields: Any) -> None:
            self.rows.append((level, event, fields))

        return _record


class Pacer:
    async def wait_for_turn(self) -> float:
        return 0.0


class Delivery:
    def as_dict(self) -> dict[str, Any]:
        return {"sent": ["stub"], "failed": {}}


class RecordingNotifier:
    """记下每条通知的**顺序**与内容。"""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> Delivery:
        self.sent.append(message)
        return Delivery()

    @property
    def kinds(self) -> list[EventKind]:
        return [message.event for message in self.sent]


class Client:
    """一次抓取返回：一条改了标题的老作品 + 一条全新作品。"""

    async def author_posts(self, sec_user_id: str, count: int, **kwargs: Any) -> Page:
        return Page(
            items=(
                Content(
                    content_id="p-old",
                    title="新标题",
                    created_at=NOW - timedelta(days=2),
                ),
                Content(content_id="p-new", title="全新作品", created_at=NOW),
            ),
        )


async def _run_round(tmp_path, notifier: RecordingNotifier | None = None):
    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    author = AuthorState(
        sec_user_id=UID,
        nickname="阿直",
        initialized_at=NOW - timedelta(days=30),
        ever_had_posts=True,
        runs=5,
        last_seen_at=NOW - timedelta(minutes=1),
        posts=(
            PostState(
                content_id="p-old", title="旧标题", created_at=NOW - timedelta(days=2)
            ),
        ),
    )
    notifier = notifier or RecordingNotifier()
    result = await run_author(
        author=author,
        nickname="阿直",
        client=Client(),
        store=store,
        notifier=notifier,
        dedup=Deduplicator(),
        pacer=Pacer(),
        gate=GlobalGate(default_seconds=60, backoff_after=2, backoff_max=600),
        cfg=DiffConfig(),
        now=NOW,
        archive_enabled=False,
        logger=Logger(),
    )
    return result, notifier, store


async def test_new_post_is_delivered_before_everything_else(tmp_path):
    result, notifier, _store = await _run_round(tmp_path)

    # `diff` 的顺序是先同步标题、后建新作品，所以这条断言只有在按优先级排序后才成立
    assert notifier.kinds == [EventKind.NEW_POST, EventKind.TITLE_CHANGED]
    assert result.status == "ok"
    assert notifier.sent[0].subject.startswith("【新作品】")


async def test_title_changed_is_now_notified_with_old_and_new(tmp_path):
    _result, notifier, store = await _run_round(tmp_path)

    body = notifier.sent[1].markdown
    assert "旧标题" in body and "新标题" in body
    # 事件本身照旧落库（推送与否不影响审计）
    assert {row["kind"] for row in store.recent_events()} == {
        "new_post",
        "title_changed",
    }


async def test_title_changed_is_windowed_but_new_post_is_not(tmp_path):
    """标题变更连着来（作者补话题标签）只报一次；新作品永远不抑制。"""
    dedup = Deduplicator()
    changed = Event(EventKind.TITLE_CHANGED, sec_user_id=UID, content_id="p-old")
    fresh = Event(EventKind.NEW_POST, sec_user_id=UID, content_id="p-new")

    allowed, key = should_send(changed, dedup)
    assert allowed and key == f"title_changed:{UID}"
    assert should_send(changed, dedup)[0] is False, "同一账号 1 小时内第二次应被抑制"
    assert should_send(fresh, dedup) == (True, "")
    assert should_send(fresh, dedup)[0] is True, "新作品没有窗口，不该被抑制"


async def test_revived_window_is_per_post_not_per_author(tmp_path):
    """ "作品回归"的 6 小时窗口要按**作品**分桶，不是按账号。

    线上踩过：一个账号的两条作品同时被恢复 → 第一条占了 `revived:作者` 这个桶，第二条被压掉，
    且窗口不会补发（面板里两条"作品回归"、通知只来一条，等于永久丢一条）。
    这个窗口本来要压的是"**同一条**作品反复出去又回来"，那就该按作品分桶。
    """
    dedup = Deduplicator()

    def revived(content_id):
        return Event(
            EventKind.REVIVED,
            sec_user_id=UID,
            content_id=content_id,
            payload={"title": f"标题{content_id}"},
        )

    first, key_a = should_send(revived("a"), dedup)
    second, key_b = should_send(revived("b"), dedup)

    assert first is True and key_a == f"revived:{UID}:a"
    assert second is True, "同一个账号的另一条作品回归不该被连坐"
    assert key_b == f"revived:{UID}:b"
    assert should_send(revived("a"), dedup)[0] is False, "同一条作品 6 小时内重复才该压"


@pytest.mark.parametrize(
    "kind",
    [
        EventKind.ALL_GONE,
        EventKind.POST_REMOVED,
        EventKind.REVIVED,
        EventKind.TITLE_CHANGED,
    ],
)
def test_new_post_sorts_first(kind):
    assert priority_of(EventKind.NEW_POST) < priority_of(kind)


def test_priority_table_covers_every_notify_kind():
    assert set(NOTIFY_PRIORITY) == set(NOTIFY_KINDS), "通知类事件必须都在优先级表里"


def test_notify_and_silent_kinds_partition_every_event():
    """每一种事件要么会推送、要么明确静默，不能两边都不在（那就等于悄悄丢事件）。"""
    from dywatch.models import EventKind as Kind

    assert not (NOTIFY_KINDS & SILENT_KINDS)
    assert NOTIFY_KINDS | SILENT_KINDS == set(Kind)


class ExplodingNotifier(RecordingNotifier):
    """在指定事件上炸一次（模拟渠道客户端 bug 或渲染意外）。"""

    def __init__(self, explode_on: EventKind) -> None:
        super().__init__()
        self._explode_on = explode_on
        self.crashed = 0

    async def send(self, message: Any) -> Delivery:
        if message.event is self._explode_on:
            self.crashed += 1
            raise RuntimeError("渠道客户端炸了")
        self.sent.append(message)
        return Delivery()


async def test_one_notification_crashing_does_not_hold_back_the_rest(tmp_path):
    """单条通知出意外不能连坐后面的——这是"给 new_post 让路"的最后一道保险。

    通知循环是唯一不能因单条失败而中断的地方：中断意味着排在后面的（尤其 new_post）
    连尝试的机会都没有。这里让"标题变更"炸掉，验证新作品照发、这一轮也不被拖成失败。
    """
    notifier = ExplodingNotifier(EventKind.TITLE_CHANGED)
    result, _notifier, _store = await _run_round(tmp_path, notifier=notifier)

    assert notifier.crashed == 1
    assert [message.event for message in notifier.sent] == [EventKind.NEW_POST]
    assert result.status == "ok", "通知里的意外不该把这一轮判成失败"


# ------------------------------------------------------- 全局事件按原因分桶
#
# `TRIGGERS` 给全局事件的窗口是**小时级**的，所以"分桶分错了"的代价不是多一条通知，
# 而是另一件故障被静默整整一个窗口——磁盘满会把"状态库写不进去"压掉一小时，
# 池子空了会把"接口熔断"压掉一小时。两者的处置方式完全不同。


def test_global_events_are_bucketed_by_cause_not_by_kind():
    dedup = Deduplicator()

    pool_empty = Event(
        EventKind.UPSTREAM_DEGRADED,
        sec_user_id="",
        payload={"code": "IDENTITY_POOL_EXHAUSTED"},
    )
    circuit_open = Event(
        EventKind.UPSTREAM_DEGRADED,
        sec_user_id="",
        payload={"code": "ENDPOINT_CIRCUIT_OPEN"},
    )
    assert should_send(pool_empty, dedup) == (
        True,
        "upstream_degraded:IDENTITY_POOL_EXHAUSTED",
    )
    allowed, key = should_send(circuit_open, dedup)
    assert allowed is True, "另一种上游故障不该被前一种的窗口压掉"
    assert key == "upstream_degraded:ENDPOINT_CIRCUIT_OPEN"
    assert should_send(pool_empty, dedup)[0] is False, "同一个原因才该压在窗口里"

    disk_low = Event(
        EventKind.SELF_DEGRADED, sec_user_id="", payload={"reason": "disk_low"}
    )
    store_failed = Event(
        EventKind.SELF_DEGRADED,
        sec_user_id="",
        payload={"reason": "state_store_write_failed"},
    )
    assert should_send(disk_low, dedup)[0] is True
    assert should_send(store_failed, dedup)[0] is True, "磁盘满和库写不进去是两件事"
    assert should_send(disk_low, dedup)[0] is False


def test_a_global_event_without_a_cause_shares_one_bucket():
    """生产点漏带 `reason`/`code` 时共用 `unknown` 桶——这是**已知的退化**，钉住它。

    它不该被改成"不过滤"（那会让同一个原因每小时刷一次）；真正该守的是"生产点必须带原因"，
    由 `test_loop.py::test_self_check_lands_in_the_snapshot_but_reports_only_once` 那边的
    payload 断言盯着。
    """
    dedup = Deduplicator()
    first, key = should_send(
        Event(EventKind.SELF_DEGRADED, sec_user_id="", payload={}), dedup
    )
    assert first is True and key == "self_degraded:unknown"
    assert (
        should_send(Event(EventKind.SELF_DEGRADED, sec_user_id="", payload={}), dedup)[
            0
        ]
        is False
    )


# ------------------------------------------------------- 系统级事件的落库与投递


async def test_system_events_are_recorded_and_a_suppressed_repeat_is_not(tmp_path):
    """系统级事件：**先落库、再投递、回写结果**；被窗口压掉的重复**不落库**。

    不落库是刻意的：一次上游故障会让同一轮里所有在途账号各自撞上，5 个账号就是 5 行
    一模一样的记录，而闸门关着的那段时间每轮都跳过、不会重复触发——"一次故障一行"
    正是抑制窗口近似出来的口径。
    """
    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    notifier = RecordingNotifier()
    dedup = Deduplicator()
    event = Event(
        EventKind.SELF_DEGRADED,
        sec_user_id="",
        payload={"reason": "disk_low", "free_mb": 10},
    )

    row_id = await notify_system_event(
        event, notifier=notifier, dedup=dedup, store=store, now=NOW, logger=Logger()
    )
    assert row_id is not None
    assert notifier.kinds == [EventKind.SELF_DEGRADED]

    rows = store.recent_events()
    assert [row["kind"] for row in rows] == ["self_degraded"]
    assert json.loads(rows[0]["delivery_json"])["sent"] == ["stub"], (
        "投递结果要回写到那一行"
    )

    again = await notify_system_event(
        event, notifier=notifier, dedup=dedup, store=store, now=NOW, logger=Logger()
    )
    assert again is None, "被抑制的重复没有行 id"
    assert notifier.kinds == [EventKind.SELF_DEGRADED], "也不该再投一次"
    assert len(store.recent_events()) == 1, "被压掉的重复不该在 events 表里堆行"
