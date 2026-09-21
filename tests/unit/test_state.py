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


# ---------------------------------------------------------------- schema 迁移
def test_migrate_upgrades_a_v1_database_without_losing_data(tmp_path):
    """模拟一个升级前的旧库（没有 baseline_content_count 两列，version=1），
    验证 `migrate()` 能把它升到 v2、不报错、也不丢已有数据——这是这个项目
    第一条真正的迁移路径（v1 → v2），值得单独锁住。
    """
    import sqlite3

    db_path = tmp_path / "old.db"
    # 手写一份 v1 版本的 authors 表（没有 baseline_content_count / _at 两列）——
    # 这样等下 StateStore.migrate() 里的 `CREATE TABLE IF NOT EXISTS` 才会因为
    # 表已经存在而跳过，真正走到 `_migrate_1_to_2` 的 `ALTER TABLE` 那条路径，
    # 而不是被新版 SCHEMA 一次性建成新结构，测不出迁移代码本身有没有 bug。
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (1);

        CREATE TABLE authors (
          sec_user_id           TEXT PRIMARY KEY,
          nickname              TEXT NOT NULL DEFAULT '',
          initialized_at        TEXT,
          ever_had_posts        INTEGER NOT NULL DEFAULT 0,
          consecutive_fails     INTEGER NOT NULL DEFAULT 0,
          last_error            TEXT,
          last_error_code       TEXT,
          fail_alerted          INTEGER NOT NULL DEFAULT 0,
          last_fail_alert_at    TEXT,
          all_gone_rounds       INTEGER NOT NULL DEFAULT 0,
          empty_rounds          INTEGER NOT NULL DEFAULT 0,
          never_seen_alerted    INTEGER NOT NULL DEFAULT 0,
          newest_seen_created_at TEXT,
          raw_refresh_round     INTEGER NOT NULL DEFAULT 0,
          last_new_video_at     TEXT,
          last_update_at        TEXT,
          stale_alerted         INTEGER NOT NULL DEFAULT 0,
          last_seen_at          TEXT,
          runs                  INTEGER NOT NULL DEFAULT 0,
          created_at            TEXT NOT NULL,
          updated_at            TEXT NOT NULL
        );
        INSERT INTO authors (
          sec_user_id, nickname, initialized_at, ever_had_posts, runs,
          created_at, updated_at
        ) VALUES (
          'old_user', '老账号', '2026-01-01T00:00:00+00:00', 1, 42,
          '2026-01-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00'
        );

        -- 同理：posts 写成 v3 的形状（没有 hidden_from_guest_at），
        -- 好让 `_migrate_3_to_4` 的 ALTER 真的被执行到
        CREATE TABLE posts (
          sec_user_id     TEXT NOT NULL,
          content_id      TEXT NOT NULL,
          kind            TEXT NOT NULL DEFAULT 'unknown',
          title           TEXT NOT NULL DEFAULT '',
          created_at      TEXT,
          is_top          INTEGER NOT NULL DEFAULT 0,
          first_seen_at   TEXT,
          last_seen_at    TEXT,
          absent_rounds   INTEGER NOT NULL DEFAULT 0,
          absent_is_top   INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (sec_user_id, content_id)
        );
        INSERT INTO posts (sec_user_id, content_id, title, created_at, absent_rounds)
        VALUES ('old_user', 'old_post', '老作品', '2026-01-02T00:00:00+00:00', 1);
        """
    )
    conn.commit()
    conn.close()

    store = StateStore(db_path)
    store.migrate()  # 不该报错——一路从 v1 经 v2 升到当前的 v3

    with store._tx() as tx:  # noqa: SLF001 —— 就是要验证迁移后的原始表结构
        version = tx.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 5
        columns = {row[1] for row in tx.execute("PRAGMA table_info(authors)")}
        assert "baseline_content_count" in columns
        assert "baseline_content_count_at" in columns
        assert "content_count_drift_rounds" in columns
        post_columns = {row[1] for row in tx.execute("PRAGMA table_info(posts)")}
        assert "hidden_from_guest_at" in post_columns
        assert "hidden_seen_streak" in post_columns
        # 老行要原样留下，新列是 NULL（"没有这个已知情况"），不能变成别的值
        old_post = tx.execute(
            "SELECT absent_rounds, hidden_from_guest_at FROM posts WHERE content_id = 'old_post'"
        ).fetchone()
        assert old_post[0] == 1
        assert old_post[1] is None

    authors = store.load_authors()
    assert "old_user" in authors
    old = authors["old_user"]
    assert old.nickname == "老账号"
    assert old.runs == 42
    assert old.ever_had_posts is True
    # 老账号从没做过隐藏作品核验，新列必须是 None（"还没确认过"），不能悄悄变成 0
    # （0 会被读成"账号发布数是 0"，是另一件事）
    assert old.baseline_content_count is None
    assert old.baseline_content_count_at is None
    # drift 计数器默认应该是 0（"目前没有未解决的缺口"），不是 NULL
    assert old.content_count_drift_rounds == 0
    # 老作品同样：没被核验标记过，所以是 None
    assert [p.hidden_from_guest_at for p in old.posts] == [None]

    # 迁移之后新列要能正常读写，不是只加了个空壳
    updated = old.with_updates(baseline_content_count=7, baseline_content_count_at=NOW)
    store.save_round("old_user", updated, events=[], now=NOW)
    reloaded = store.load_authors()["old_user"]
    assert reloaded.baseline_content_count == 7

    # 新列（posts.hidden_from_guest_at）也要能往返
    marked = reloaded.with_updates(
        posts=tuple(p.with_updates(hidden_from_guest_at=NOW) for p in reloaded.posts)
    )
    store.save_round("old_user", marked, events=[], now=NOW)
    assert store.load_authors()["old_user"].posts[0].hidden_from_guest_at == NOW
