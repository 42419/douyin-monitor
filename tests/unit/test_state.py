"""状态库：一轮一个事务、整段替换、投递结果回填。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dywatch.models import (
    AuthorState,
    Event,
    EventKind,
    Kind,
    PostState,
    RoundResult,
    Tombstone,
)
from dywatch.state import StateStore

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    db = StateStore(tmp_path / "dywatch.db")
    db.migrate()
    yield db
    db.close()


def sample_state(**overrides) -> AuthorState:
    base = dict(
        sec_user_id="u1",
        nickname="示例账号",
        initialized_at=NOW,
        ever_had_posts=True,
        consecutive_fails=2,
        last_error="boom",
        last_error_code="UPSTREAM_RISK_CONTROL",
        fail_alerted=True,
        last_fail_alert_at=NOW,
        all_gone_rounds=1,
        empty_rounds=3,
        never_seen_alerted=True,
        newest_seen_created_at=NOW - timedelta(days=3),
        raw_refresh_round=7,
        last_new_video_at=NOW,
        last_update_at=NOW,
        stale_alerted=True,
        last_seen_at=NOW,
        runs=42,
        posts=(
            PostState(
                content_id="p1",
                kind=Kind.VIDEO,
                title="作品一",
                created_at=NOW - timedelta(days=3),
                is_top=True,
                first_seen_at=NOW - timedelta(days=4),
                last_seen_at=NOW,
                absent_rounds=1,
                absent_is_top=True,
            ),
        ),
        tombstones=(Tombstone(content_id="old", removed_at=NOW - timedelta(days=1),
                              reason="scrolled_out"),),
    )
    base.update(overrides)
    return AuthorState(**base)


def test_roundtrip_preserves_every_field(store):
    state = sample_state()
    store.save_round("u1", state, events=[], now=NOW)

    loaded = store.load_authors()["u1"]
    assert loaded.nickname == state.nickname
    assert loaded.ever_had_posts is True
    assert loaded.consecutive_fails == 2
    assert loaded.last_error_code == "UPSTREAM_RISK_CONTROL"
    assert loaded.fail_alerted is True
    assert loaded.never_seen_alerted is True
    assert loaded.raw_refresh_round == 7
    assert loaded.runs == 42
    assert loaded.newest_seen_created_at == state.newest_seen_created_at
    assert loaded.all_gone_rounds == 1 and loaded.empty_rounds == 3

    post = loaded.post("p1")
    assert post.kind is Kind.VIDEO
    assert post.is_top is True
    assert post.absent_rounds == 1
    assert post.absent_is_top is True
    assert post.created_at == NOW - timedelta(days=3)

    assert loaded.tombstones[0].content_id == "old"
    assert loaded.tombstones[0].reason == "scrolled_out"


def test_second_round_replaces_rather_than_appends(store):
    store.save_round("u1", sample_state(), events=[], now=NOW)

    updated = sample_state(
        posts=(PostState(content_id="p2", kind=Kind.IMAGE_ALBUM, title="作品二"),),
        tombstones=(),
    )
    store.save_round("u1", updated, events=[], now=NOW + timedelta(minutes=2))

    loaded = store.load_authors()["u1"]
    assert loaded.known_ids == {"p2"}
    assert loaded.tombstones == ()
    assert loaded.post("p2").kind is Kind.IMAGE_ALBUM


def test_events_are_persisted_with_their_payload_and_delivery(store):
    events = (
        Event(EventKind.NEW_POST, sec_user_id="u1", nickname="A", content_id="p9",
              payload={"content": {"content_id": "p9"}, "gap_days": 3}),
        Event(EventKind.GAP_DETECTED, sec_user_id="u1", payload={"fetch_count": 15}),
    )
    ids = store.save_round("u1", sample_state(), events, now=NOW)
    assert len(ids) == 2 and ids[0] != ids[1]

    store.record_deliveries({ids[0]: {"sent": ["dingtalk"], "failed": {}}})

    rows = store.recent_events(limit=10)
    assert len(rows) == 2
    by_kind = {row["kind"]: row for row in rows}
    assert by_kind["new_post"]["delivery_json"] == '{"sent": ["dingtalk"], "failed": {}}'
    assert by_kind["gap_detected"]["delivery_json"] is None


def test_record_round_summarises_results(store):
    results = [
        RoundResult(sec_user_id="u1", nickname="A", status="ok", new_count=2, deleted_count=1,
                    title_changed=1),
        RoundResult(sec_user_id="u2", nickname="B", status="fail", error_code="RATE_LIMITED"),
        RoundResult(sec_user_id="u3", nickname="C", status="skipped"),
        RoundResult(sec_user_id="u4", nickname="D", status="init"),
    ]
    summary = store.record_round(now=NOW, results=results, gate_state="open", duration_ms=1234)

    assert summary == {
        "checked": 3,
        "initialized": 1,
        "new": 2,
        "deleted": 1,
        "title_changed": 1,
        "failed": 1,
    }
    rounds = store.recent_rounds(1)
    assert rounds[0]["gate_state"] == "open"
    assert rounds[0]["duration_ms"] == 1234


def test_rounds_total_counts_across_restarts_and_survives_pruning(store):
    """累计轮数跨进程、且不被保留期裁剪影响——面板"累计 N 轮"就是它。

    用 `COUNT(*)` 会随 `maintenance()` 删旧行而变小，看着像"轮数丢了"；
    `MAX(id)` 在旧行被删光后会掉回 0。这正是面板与数据库对不上的成因之一。
    """
    assert store.rounds_total() == 0  # 一轮都没跑过时是 0，不该抛

    for _ in range(3):
        store.record_round(now=NOW, results=[], gate_state="open", duration_ms=1)
    assert store.rounds_total() == 3

    # 换一个 StateStore 实例（= 重启后新进程），累计值必须接着数
    reopened = StateStore(store.path)
    reopened.migrate()
    try:
        assert reopened.rounds_total() == 3
        reopened.record_round(now=NOW, results=[], gate_state="open", duration_ms=1)
        assert reopened.rounds_total() == 4
    finally:
        reopened.close()

    # 保留期把 3 行旧的删掉后：COUNT(*) 归 0，累计值仍然是 4
    store.maintenance(now=NOW + timedelta(days=100), events_days=90, rounds_days=30)
    assert store.recent_rounds(10) == []
    assert store.rounds_total() == 4


def test_ensure_author_creates_then_updates_the_nickname(store):
    created = store.ensure_author("u9", "老昵称", NOW)
    assert created.nickname == "老昵称"
    assert store.load_authors()["u9"].nickname == "老昵称"

    store.ensure_author("u9", "新昵称", NOW)
    assert store.load_authors()["u9"].nickname == "新昵称"

    # 空昵称不该把已有的覆盖掉
    store.ensure_author("u9", "", NOW)
    assert store.load_authors()["u9"].nickname == "新昵称"


def test_drop_author_removes_posts_and_tombstones(store):
    store.save_round("u1", sample_state(), events=[], now=NOW)
    store.drop_author("u1")
    assert store.load_authors() == {}


def test_maintenance_ages_out_events_and_rounds(store):
    store.save_round("u1", sample_state(),
                     events=[Event(EventKind.NEW_POST, sec_user_id="u1")], now=NOW)
    store.record_round(now=NOW, results=[], gate_state="open", duration_ms=1)

    store.maintenance(now=NOW + timedelta(days=100), events_days=90, rounds_days=30)
    assert store.recent_events(10) == []
    assert store.recent_rounds(10) == []


def test_migrate_is_idempotent(store):
    store.migrate()
    store.migrate()
    store.save_round("u1", sample_state(), events=[], now=NOW)
    assert "u1" in store.load_authors()


def test_a_failed_write_leaves_the_previous_round_intact(store):
    """事务的意义：写坏一半不能留下"作品已记入但事件没发"的状态。"""
    store.save_round("u1", sample_state(), events=[], now=NOW)
    before = store.load_authors()["u1"]

    class Boom:
        def __len__(self) -> int:
            return 1

        def __iter__(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.save_round("u1", sample_state(posts=()), events=Boom(), now=NOW + timedelta(minutes=1))

    after = store.load_authors()["u1"]
    assert after.known_ids == before.known_ids
    assert after.runs == before.runs
