"""SQLite：唯一的持久化出口。

三张设计决定：

* **一个账号一轮 = 一个事务。** 判定产出的新状态与它的事件一起提交，所以进程被
  `kill -9` 之后不会有"作品已记入但事件没发"或反过来的半截状态。
* **整段替换而不是逐行 diff。** 每轮把该账号的 posts / tombstones 全删再全插
  （上限 50 + 200 行，代价可以忽略），换掉了一整类"局部更新漏了一列"的 bug。
* **`rounds` 与 `events` 是给人看的。** 它们回答"当时到底推了什么"，
  有保留期，且从不参与判定——判定只读 `authors` / `posts` / `tombstones`。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .models import (
    AuthorState,
    Content,
    Event,
    Kind,
    PostState,
    RoundResult,
    Tombstone,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS authors (
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

CREATE TABLE IF NOT EXISTS posts (
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

CREATE TABLE IF NOT EXISTS tombstones (
  sec_user_id   TEXT NOT NULL,
  content_id    TEXT NOT NULL,
  removed_at    TEXT NOT NULL,
  reason        TEXT NOT NULL DEFAULT 'confirmed',
  PRIMARY KEY (sec_user_id, content_id)
);

CREATE TABLE IF NOT EXISTS rounds (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  checked       INTEGER NOT NULL DEFAULT 0,
  new_count     INTEGER NOT NULL DEFAULT 0,
  deleted_count INTEGER NOT NULL DEFAULT 0,
  title_changed INTEGER NOT NULL DEFAULT 0,
  failed        INTEGER NOT NULL DEFAULT 0,
  gate_state    TEXT,
  duration_ms   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  sec_user_id   TEXT,
  content_id    TEXT,
  kind          TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  delivery_json TEXT
);

CREATE INDEX IF NOT EXISTS ix_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS ix_rounds_ts ON rounds (ts);
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json_default(value: Any) -> Any:
    if isinstance(value, Content):
        return {
            "content_id": value.content_id,
            "kind": value.kind.value,
            "title": value.title,
            "web_url": value.web_url,
            "created_at": _iso(value.created_at),
            "is_top": value.is_top,
        }
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class StateStore:
    """One SQLite file. Single writer (the round loop), plus read-only consumers."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    # ---------------------------------------------------------------- 生命周期
    def migrate(self) -> None:
        with self._tx() as conn:
            conn.executescript(SCHEMA)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            elif int(row["version"]) != SCHEMA_VERSION:
                # 只有一个版本，所以这里不做迁移，只把不一致说出来
                raise RuntimeError(
                    f"状态库 schema 版本为 {row['version']}，本程序期望 {SCHEMA_VERSION}"
                )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - best effort
            pass

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---------------------------------------------------------------- 读
    def load_authors(self) -> dict[str, AuthorState]:
        """Every author with its posts and tombstones. One query per table."""
        authors = {row["sec_user_id"]: row for row in self._conn.execute("SELECT * FROM authors")}
        posts: dict[str, list[sqlite3.Row]] = {}
        for row in self._conn.execute("SELECT * FROM posts"):
            posts.setdefault(row["sec_user_id"], []).append(row)
        tombs: dict[str, list[sqlite3.Row]] = {}
        for row in self._conn.execute("SELECT * FROM tombstones"):
            tombs.setdefault(row["sec_user_id"], []).append(row)

        out: dict[str, AuthorState] = {}
        for sec_user_id, row in authors.items():
            out[sec_user_id] = _row_to_state(
                row,
                posts.get(sec_user_id, []),
                tombs.get(sec_user_id, []),
            )
        return out

    def ensure_author(self, sec_user_id: str, nickname: str, now: datetime) -> AuthorState:
        """Create the row if absent, so a new account is visible on the panel at once."""
        row = self._conn.execute(
            "SELECT * FROM authors WHERE sec_user_id = ?", (sec_user_id,)
        ).fetchone()
        if row is not None:
            if nickname and row["nickname"] != nickname:
                with self._tx() as conn:
                    conn.execute(
                        "UPDATE authors SET nickname = ?, updated_at = ? WHERE sec_user_id = ?",
                        (nickname, _iso(now), sec_user_id),
                    )
            return _row_to_state(row, [], [])
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO authors (sec_user_id, nickname, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (sec_user_id, nickname, _iso(now), _iso(now)),
            )
        return AuthorState(sec_user_id=sec_user_id, nickname=nickname)

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT ts, sec_user_id, content_id, kind, delivery_json FROM events"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_rounds(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM rounds ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def rounds_total(self) -> int:
        """这个实例累计跑过多少轮——**跨重启**的那个数。

        取 `sqlite_sequence`（`AUTOINCREMENT` 的号段计数器），它单调不减：
        `maintenance()` 会按 `ROUNDS_KEEP_DAYS` 删掉更早的 `rounds` 行，
        而删行不动号段。两个看似可以替代的写法都不行：

        * `COUNT(*)` 只数保留窗口内的行，于是它会随天数上下浮动，看着像"轮数丢了"；
        * `MAX(id)` 在**一行都不剩**时是 NULL——裁剪把旧行删光之后这个数会掉回 0。

        还没跑过一轮时 `sqlite_sequence` 里没有这一行，返回 0。

        注意它和 `MonitorLoop._rounds`（进程内存、重启归零）不是一个口径，别互相校验。
        """
        row = self._conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'rounds'"
        ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    # ---------------------------------------------------------------- 写
    def save_round(
        self,
        sec_user_id: str,
        state: AuthorState,
        events: Sequence[Event],
        *,
        now: datetime,
        deliveries: Mapping[int, Mapping[str, Any]] | None = None,
    ) -> list[int]:
        """One author, one round, one transaction: state + posts + tombstones + events.

        Returns the new `events.id` values, in the same order as `events`, so the
        caller can come back and attach a delivery outcome after it has actually
        delivered — the write happens first, the outcome second, and a crash between
        them costs a missing delivery note rather than a duplicate notification.
        """
        event_ids: list[int] = []
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO authors (
                  sec_user_id, nickname, initialized_at, ever_had_posts, consecutive_fails,
                  last_error, last_error_code, fail_alerted, last_fail_alert_at,
                  all_gone_rounds, empty_rounds, never_seen_alerted, newest_seen_created_at,
                  raw_refresh_round, last_new_video_at, last_update_at, stale_alerted,
                  last_seen_at, runs, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(sec_user_id) DO UPDATE SET
                  nickname=excluded.nickname,
                  initialized_at=excluded.initialized_at,
                  ever_had_posts=excluded.ever_had_posts,
                  consecutive_fails=excluded.consecutive_fails,
                  last_error=excluded.last_error,
                  last_error_code=excluded.last_error_code,
                  fail_alerted=excluded.fail_alerted,
                  last_fail_alert_at=excluded.last_fail_alert_at,
                  all_gone_rounds=excluded.all_gone_rounds,
                  empty_rounds=excluded.empty_rounds,
                  never_seen_alerted=excluded.never_seen_alerted,
                  newest_seen_created_at=excluded.newest_seen_created_at,
                  raw_refresh_round=excluded.raw_refresh_round,
                  last_new_video_at=excluded.last_new_video_at,
                  last_update_at=excluded.last_update_at,
                  stale_alerted=excluded.stale_alerted,
                  last_seen_at=excluded.last_seen_at,
                  runs=excluded.runs,
                  updated_at=excluded.updated_at
                """,
                (
                    state.sec_user_id,
                    state.nickname or sec_user_id,
                    _iso(state.initialized_at),
                    1 if state.ever_had_posts else 0,
                    state.consecutive_fails,
                    state.last_error,
                    state.last_error_code,
                    1 if state.fail_alerted else 0,
                    _iso(state.last_fail_alert_at),
                    state.all_gone_rounds,
                    state.empty_rounds,
                    1 if state.never_seen_alerted else 0,
                    _iso(state.newest_seen_created_at),
                    state.raw_refresh_round,
                    _iso(state.last_new_video_at),
                    _iso(state.last_update_at),
                    1 if state.stale_alerted else 0,
                    _iso(state.last_seen_at),
                    state.runs,
                    _iso(now),
                    _iso(now),
                ),
            )

            # 整段替换：见模块头注释
            conn.execute("DELETE FROM posts WHERE sec_user_id = ?", (sec_user_id,))
            conn.executemany(
                "INSERT INTO posts (sec_user_id, content_id, kind, title, created_at, is_top,"
                " first_seen_at, last_seen_at, absent_rounds, absent_is_top)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        sec_user_id,
                        post.content_id,
                        post.kind.value,
                        post.title,
                        _iso(post.created_at),
                        1 if post.is_top else 0,
                        _iso(post.first_seen_at),
                        _iso(post.last_seen_at),
                        post.absent_rounds,
                        1 if post.absent_is_top else 0,
                    )
                    for post in state.posts
                ],
            )

            conn.execute("DELETE FROM tombstones WHERE sec_user_id = ?", (sec_user_id,))
            conn.executemany(
                "INSERT INTO tombstones (sec_user_id, content_id, removed_at, reason)"
                " VALUES (?,?,?,?)",
                [
                    (sec_user_id, t.content_id, _iso(t.removed_at), t.reason)
                    for t in state.tombstones
                ],
            )

            for index, event in enumerate(events):
                cursor = conn.execute(
                    "INSERT INTO events (ts, sec_user_id, content_id, kind, payload_json, delivery_json)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        _iso(now),
                        event.sec_user_id,
                        event.content_id,
                        event.kind.value,
                        json.dumps(event.payload, ensure_ascii=False, default=_json_default),
                        json.dumps(dict((deliveries or {}).get(index, {})), ensure_ascii=False)
                        if deliveries
                        else None,
                    ),
                )
                event_ids.append(int(cursor.lastrowid or 0))
        return event_ids

    def record_deliveries(self, deliveries: Mapping[int, Mapping[str, Any]]) -> None:
        """Attach delivery outcomes to the event rows they belong to."""
        if not deliveries:
            return
        with self._tx() as conn:
            conn.executemany(
                "UPDATE events SET delivery_json = ? WHERE id = ?",
                [
                    (json.dumps(dict(payload), ensure_ascii=False), int(row_id))
                    for row_id, payload in deliveries.items()
                ],
            )

    def record_round(
        self,
        *,
        now: datetime,
        results: Iterable[RoundResult],
        gate_state: str,
        duration_ms: int,
    ) -> dict[str, int]:
        checked = new = deleted = titles = failed = initialized = 0
        for result in results:
            if result.status == "skipped":
                continue
            checked += 1
            new += result.new_count
            deleted += result.deleted_count
            titles += result.title_changed
            if result.status == "init":
                initialized += 1
            if result.status == "fail":
                failed += 1
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO rounds (ts, checked, new_count, deleted_count, title_changed,"
                " failed, gate_state, duration_ms) VALUES (?,?,?,?,?,?,?,?)",
                (_iso(now), checked, new, deleted, titles, failed, gate_state, duration_ms),
            )
        return {
            "checked": checked,
            "initialized": initialized,
            "new": new,
            "deleted": deleted,
            "title_changed": titles,
            "failed": failed,
        }

    def drop_author(self, sec_user_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM posts WHERE sec_user_id = ?", (sec_user_id,))
            conn.execute("DELETE FROM tombstones WHERE sec_user_id = ?", (sec_user_id,))
            conn.execute("DELETE FROM authors WHERE sec_user_id = ?", (sec_user_id,))

    def maintenance(self, *, now: datetime, events_days: int, rounds_days: int) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM events WHERE ts < ?", (_iso(now - timedelta(days=events_days)),)
            )
            conn.execute(
                "DELETE FROM rounds WHERE ts < ?", (_iso(now - timedelta(days=rounds_days)),)
            )


def _row_to_state(
    row: sqlite3.Row, posts: Sequence[sqlite3.Row], tombs: Sequence[sqlite3.Row]
) -> AuthorState:
    return AuthorState(
        sec_user_id=row["sec_user_id"],
        nickname=row["nickname"] or row["sec_user_id"],
        initialized_at=_dt(row["initialized_at"]),
        ever_had_posts=bool(row["ever_had_posts"]),
        consecutive_fails=int(row["consecutive_fails"]),
        last_error=row["last_error"],
        last_error_code=row["last_error_code"],
        fail_alerted=bool(row["fail_alerted"]),
        last_fail_alert_at=_dt(row["last_fail_alert_at"]),
        all_gone_rounds=int(row["all_gone_rounds"]),
        empty_rounds=int(row["empty_rounds"]),
        never_seen_alerted=bool(row["never_seen_alerted"]),
        newest_seen_created_at=_dt(row["newest_seen_created_at"]),
        raw_refresh_round=int(row["raw_refresh_round"]),
        last_new_video_at=_dt(row["last_new_video_at"]),
        last_update_at=_dt(row["last_update_at"]),
        stale_alerted=bool(row["stale_alerted"]),
        last_seen_at=_dt(row["last_seen_at"]),
        runs=int(row["runs"]),
        posts=tuple(
            PostState(
                content_id=p["content_id"],
                kind=Kind.parse(p["kind"]),
                title=p["title"] or "",
                created_at=_dt(p["created_at"]),
                is_top=bool(p["is_top"]),
                first_seen_at=_dt(p["first_seen_at"]),
                last_seen_at=_dt(p["last_seen_at"]),
                absent_rounds=int(p["absent_rounds"]),
                absent_is_top=bool(p["absent_is_top"]),
            )
            for p in posts
        ),
        tombstones=tuple(
            Tombstone(content_id=t["content_id"], removed_at=_dt(t["removed_at"]) or datetime.now(timezone.utc),
                      reason=t["reason"])
            for t in tombs
        ),
    )


__all__ = ["StateStore", "SCHEMA_VERSION"]
