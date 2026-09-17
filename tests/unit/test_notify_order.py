"""通知投递顺序：**`new_post` 必须第一个发出去**。

它是这个工具存在的理由，而且错过就补不回来（用户不会知道曾经有过这条作品）。而一轮里
所有事件共用同一条投递通道——每条都要等渠道间隔（`NOTIFY_GAP`）、每个渠道最多 8 秒超时 ×
2 次重试，排在 `new_post` 前面的每一条都可能把它推到几十秒之后，甚至撞上渠道限流让它变成
"发送失败"那一条。所以投递顺序由 `alerts.NOTIFY_PRIORITY` 决定，不是 `diff` 的输出顺序。
"""

from __future__ import annotations

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
from dywatch.pipeline import run_author
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
                Content(content_id="p-old", title="新标题", created_at=NOW - timedelta(days=2)),
                Content(content_id="p-new", title="全新作品", created_at=NOW),
            ),
        )


async def _run_round(tmp_path, notifier: RecordingNotifier | None = None):
    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    author = AuthorState(
        sec_user_id=UID, nickname="阿直", initialized_at=NOW - timedelta(days=30),
        ever_had_posts=True, runs=5, last_seen_at=NOW - timedelta(minutes=1),
        posts=(PostState(content_id="p-old", title="旧标题", created_at=NOW - timedelta(days=2)),),
    )
    notifier = notifier or RecordingNotifier()
    result = await run_author(
        author=author, nickname="阿直", client=Client(), store=store, notifier=notifier,
        dedup=Deduplicator(),
        pacer=Pacer(), gate=GlobalGate(default_seconds=60, backoff_after=2, backoff_max=600),
        cfg=DiffConfig(), now=NOW, archive_enabled=False, logger=Logger(),
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
    assert {row["kind"] for row in store.recent_events()} == {"new_post", "title_changed"}


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


@pytest.mark.parametrize(
    "kind",
    [EventKind.ALL_GONE, EventKind.POST_REMOVED, EventKind.REVIVED, EventKind.TITLE_CHANGED],
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
