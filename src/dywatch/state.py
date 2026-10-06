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

import contextlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Iterator, Mapping, Sequence

from .models import (
    AuthorState,
    Content,
    Event,
    EventKind,
    Kind,
    PostMetrics,
    PostState,
    RoundResult,
    Tombstone,
)

SCHEMA_VERSION = 6

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
  updated_at            TEXT NOT NULL,
  baseline_content_count    INTEGER,
  baseline_content_count_at TEXT,
  content_count_drift_rounds INTEGER NOT NULL DEFAULT 0
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
  hidden_from_guest_at TEXT,
  hidden_seen_streak  INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS post_metrics (
  sec_user_id   TEXT NOT NULL,
  content_id    TEXT NOT NULL,
  hour          TEXT NOT NULL,
  captured_at   TEXT NOT NULL,
  play_count    INTEGER,
  digg_count    INTEGER,
  comment_count INTEGER,
  share_count   INTEGER,
  collect_count INTEGER,
  PRIMARY KEY (sec_user_id, content_id, hour)
);

CREATE INDEX IF NOT EXISTS ix_post_metrics_hour ON post_metrics (hour);
"""


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """加隐藏作品核验要用的两列。`ADD COLUMN` 缺省值为 NULL == `None`，正合适：

    老账号在这次升级之前从没做过核验，`baseline_content_count` 本来就该是
    "还没确认过"，而不是 0（0 会被读成"账号发布数是 0"，是另一件事）。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(authors)")}
    if "baseline_content_count" not in existing:
        conn.execute("ALTER TABLE authors ADD COLUMN baseline_content_count INTEGER")
    if "baseline_content_count_at" not in existing:
        conn.execute("ALTER TABLE authors ADD COLUMN baseline_content_count_at TEXT")


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """加"连续几轮解释不了缺口"的计数器。默认 0（跟全新账号的初始值一致，
    老账号升级过来当然也是"目前没有未解决的缺口"）。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(authors)")}
    if "content_count_drift_rounds" not in existing:
        conn.execute(
            "ALTER TABLE authors ADD COLUMN content_count_drift_rounds INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_3_to_4(conn: sqlite3.Connection) -> None:
    """给 `posts` 加"核验确认过对访客不可见"的时间列。

    NULL == `None` == "没有这个已知情况"，正是老账号该有的初始值：它们升级之前
    从没做过核验，所有作品都还是"按访客视角正常判定"。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
    if "hidden_from_guest_at" not in existing:
        conn.execute("ALTER TABLE posts ADD COLUMN hidden_from_guest_at TEXT")


def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """给 `posts` 加"带标记期间连续可见轮数"。

    默认 0 == "还没有连续见到过"，正是老库该有的初始值。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
    if "hidden_seen_streak" not in existing:
        conn.execute(
            "ALTER TABLE posts ADD COLUMN hidden_seen_streak INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_5_to_6(conn: sqlite3.Connection) -> None:
    """加 `post_metrics`：作品互动量的小时级时间序列。

    **它和 `posts` 的关系是刻意相反的**：`posts` 每轮整段替换（只保当前事实），而这张表
    只增不改地累积历史——问"这条作品的点赞是怎么涨起来的"，答案只能来自历史，不能来自
    一张每轮被覆盖的表。

    全新库不用跑这一步（`SCHEMA` 常量里已经有这张表），但老库必须显式建：`executescript`
    里的 `CREATE TABLE IF NOT EXISTS` 虽然也会把它建出来，但**建表与版本号必须一起走**——
    否则"版本是 5 但表已经存在"这种中间态会在回滚到旧版时露出来（旧版会以为库是它认识的
    形状）。所以这里和 SCHEMA 里那份定义重复一次，是有意的。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS post_metrics (
          sec_user_id   TEXT NOT NULL,
          content_id    TEXT NOT NULL,
          hour          TEXT NOT NULL,
          captured_at   TEXT NOT NULL,
          play_count    INTEGER,
          digg_count    INTEGER,
          comment_count INTEGER,
          share_count   INTEGER,
          collect_count INTEGER,
          PRIMARY KEY (sec_user_id, content_id, hour)
        );
        CREATE INDEX IF NOT EXISTS ix_post_metrics_hour ON post_metrics (hour);
        """
    )


#: 版本号 -> "从这个版本升到下一个版本"的步骤。新增迁移时按顺序追加，
#: 键是**升级前**的版本号（比如从 2 升到 3 的步骤，键是 2）。
_MIGRATIONS: Final[Mapping[int, Callable[[sqlite3.Connection], None]]] = {
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
    4: _migrate_4_to_5,
    5: _migrate_5_to_6,
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


#: 一个账号一轮最多写多少行 `post_metrics`。一页 `FETCH_COUNT` 条，且只写"至少有一个数"的
#: 那些行，所以这个上限只是防御性的（防止调用方传进来一个意想不到的长列表）。
METRICS_MAX_ROWS_PER_ROUND = 200


def _metrics_hour(now: datetime) -> str:
    """互动量快照落在哪个小时桶里。

    **按小时聚合不是省事，是必需的**：一轮约 1.5 分钟，逐轮写就是每账号每小时约 40 行 ×
    最多 `FETCH_COUNT` 条作品——一天下来单账号上万行、一个月几十万行，而曲线的分辨率并不会
    因此变好（作品的点赞数在一个小时内本来就没什么可看的形状）。压到小时桶之后写入量降到
    1/40，而"一条作品的点赞是怎么涨起来的"这个问题仍然答得上来。
    """
    return (now.replace(minute=0, second=0, microsecond=0)).isoformat()


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
        # 状态库里是"监控了谁 + 他们发了什么"，DESIGN §1.1 要求它 0600。
        # systemd 单元的 `ProtectSystem=strict` + 单独账号不是同一件事：共享主机、
        # 或者工作目录被别的东西挂在同一个 umask 下时，默认的 0644 就是"同机任何用户
        # 都能读你的监控名单"。失败不致命（某些文件系统不支持 chmod），所以只试一次。
        #
        # **目录一起收**：WAL 模式会在同目录再长出 `-wal` / `-shm`（里面是还没 checkpoint
        # 的**已提交数据**），面板读的 `status.json` 也在那儿——只把主库设成 0600，
        # 旁路文件照样按 umask（通常 0644）落盘，等于白设。`data/` 是本工具自己建的目录
        # （`settings.db_path` 恒为 `<MONITOR_HOME>/data/dywatch.db`），收它不影响别人。
        #
        # **两次 chmod 各自兜底，主库文件在前**：目录不归当前用户时 `chmod(目录)` 会失败，
        # 放在同一个 `suppress` 里的话它一抛，后面的 `chmod(库文件, 0600)` 就被跳过了——
        # 而文件本身明明改得动，白白少了最要紧的那一层。
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)

    # ---------------------------------------------------------------- 生命周期
    def migrate(self) -> None:
        """建表 + 版本升级。

        `executescript(SCHEMA)` 对已存在的表是 no-op —— `CREATE TABLE IF NOT EXISTS`
        不会给一张已经存在的表补列。所以**版本升级只能靠显式的 `ALTER TABLE`**，
        不能指望重跑一遍 `SCHEMA` 常量就把旧库补齐。

        这是这个项目第一次真的需要一条迁移路径（v1 → v2，加两列存
        `baseline_content_count` / `_at`，给隐藏作品核验用）。以前只有一个版本，
        版本不对直接报错就够了；现在开始，`_MIGRATIONS` 按顺序追加，"当前版本"
        永远是最后一步迁移完成后的版本。
        """
        with self._tx() as conn:
            conn.executescript(SCHEMA)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                # 全新库：SCHEMA 本身已经是最新形状，不需要跑任何一步迁移
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
                return

            version = int(row["version"])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"状态库 schema 版本为 {version}，比本程序期望的 {SCHEMA_VERSION} 还新——"
                    "大概率是用了更新的 dywatch 跑过这个库，当前这个版本读不了，请升级 dywatch。"
                )
            while version < SCHEMA_VERSION:
                step = _MIGRATIONS.get(version)
                if step is None:
                    raise RuntimeError(
                        f"状态库 schema 版本为 {version}，没有找到从它升到"
                        f" {version + 1} 的迁移步骤——这是代码 bug，不是配置问题。"
                    )
                step(conn)
                version += 1
                conn.execute("UPDATE schema_version SET version = ?", (version,))

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
        authors = {
            row["sec_user_id"]: row
            for row in self._conn.execute("SELECT * FROM authors")
        }
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

    def ensure_author(
        self, sec_user_id: str, nickname: str, now: datetime
    ) -> AuthorState:
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
        metrics: Sequence[PostMetrics] | None = None,
    ) -> list[int]:
        """One author, one round, one transaction: state + posts + tombstones + events.

        Returns the new `events.id` values, in the same order as `events`, so the
        caller can come back and attach a delivery outcome after it has actually
        delivered — the write happens first, the outcome second, and a crash between
        them costs a missing delivery note rather than a duplicate notification.

        `metrics` 是同一个事务里的第四条写入（见 `post_metrics`）。放在这里而不是单开一个
        方法，是为了保住"一个账号一轮一个事务"：单开一次写要多一次 fsync，而且会多出
        "状态写进去了、互动量没写"这种半截状态——它无害，但没有理由留一个。
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
                  last_seen_at, runs, created_at, updated_at,
                  baseline_content_count, baseline_content_count_at, content_count_drift_rounds
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                  updated_at=excluded.updated_at,
                  baseline_content_count=excluded.baseline_content_count,
                  baseline_content_count_at=excluded.baseline_content_count_at,
                  content_count_drift_rounds=excluded.content_count_drift_rounds
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
                    state.baseline_content_count,
                    _iso(state.baseline_content_count_at),
                    state.content_count_drift_rounds,
                ),
            )

            # 整段替换：见模块头注释
            conn.execute("DELETE FROM posts WHERE sec_user_id = ?", (sec_user_id,))
            conn.executemany(
                "INSERT INTO posts (sec_user_id, content_id, kind, title, created_at, is_top,"
                " first_seen_at, last_seen_at, absent_rounds, absent_is_top, hidden_from_guest_at,"
                " hidden_seen_streak)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        _iso(post.hidden_from_guest_at),
                        post.hidden_seen_streak,
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
                        json.dumps(
                            event.payload, ensure_ascii=False, default=_json_default
                        ),
                        json.dumps(
                            dict((deliveries or {}).get(index, {})), ensure_ascii=False
                        )
                        if deliveries
                        else None,
                    ),
                )
                event_ids.append(int(cursor.lastrowid or 0))

            if metrics:
                self._upsert_metrics(conn, sec_user_id, metrics, now)
        return event_ids

    @staticmethod
    def _upsert_metrics(
        conn: sqlite3.Connection,
        sec_user_id: str,
        metrics: Sequence[PostMetrics],
        now: datetime,
    ) -> int:
        """把这一轮的互动量并进小时桶。返回真正写了几行。

        两处刻意的写法：

        * **`COALESCE(excluded.x, post_metrics.x)`** —— 更新已有桶时保留旧值。DTK 缺字段
          （`play_count` 实测恒为 null）是它承认的合法表达，`None` 的意思是"这个字段这次
          没有"，**不是"它变成 0 了"**。直接覆盖会把已经记下的数字擦掉，而曲线看起来只是
          少了几个点，没人会发现是代码擦的。
        * 同一小时内重复跑（一轮 1.5 分钟，一小时约 40 轮）只更新不追加：这就是小时桶的
          全部意思——`hour` 是主键的一部分，不是普通的一列。
        """
        hour = _metrics_hour(now)
        rows = [
            (
                sec_user_id,
                item.content_id,
                hour,
                _iso(now),
                item.play_count,
                item.digg_count,
                item.comment_count,
                item.share_count,
                item.collect_count,
            )
            for item in metrics[:METRICS_MAX_ROWS_PER_ROUND]
            if item.content_id and not item.is_empty
        ]
        if not rows:
            return 0
        conn.executemany(
            """
            INSERT INTO post_metrics (
              sec_user_id, content_id, hour, captured_at,
              play_count, digg_count, comment_count, share_count, collect_count
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(sec_user_id, content_id, hour) DO UPDATE SET
              captured_at=excluded.captured_at,
              play_count=COALESCE(excluded.play_count, post_metrics.play_count),
              digg_count=COALESCE(excluded.digg_count, post_metrics.digg_count),
              comment_count=COALESCE(excluded.comment_count, post_metrics.comment_count),
              share_count=COALESCE(excluded.share_count, post_metrics.share_count),
              collect_count=COALESCE(excluded.collect_count, post_metrics.collect_count)
            """,
            rows,
        )
        return len(rows)

    def record_system_event(self, event: Event, *, now: datetime) -> int:
        """Persist an event that belongs to no single author（上游异常 / 自身降级）。

        这类事件以前**只在通知里存在**：投递出去就没了，面板与 `/events` 都看不到，事后
        想查"那次闸门是什么时候关的、持续了多久"只能翻日志。它们不属于任何账号，所以不进
        `save_round` 那条"一个账号一轮一个事务"的路径，单独一行即可。

        返回新行的 id，调用方投递完再回来 `record_deliveries` 补投递结果。
        """
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO events (ts, sec_user_id, content_id, kind, payload_json, delivery_json)"
                " VALUES (?,?,?,?,?,?)",
                (
                    _iso(now),
                    event.sec_user_id or None,
                    event.content_id,
                    event.kind.value,
                    json.dumps(
                        event.payload, ensure_ascii=False, default=_json_default
                    ),
                    None,
                ),
            )
            return int(cursor.lastrowid or 0)

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
                (
                    _iso(now),
                    checked,
                    new,
                    deleted,
                    titles,
                    failed,
                    gate_state,
                    duration_ms,
                ),
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

    def maintenance(
        self,
        *,
        now: datetime,
        events_days: int,
        rounds_days: int,
        metrics_days: int = 0,
    ) -> None:
        """按保留期裁掉审计类与历史类数据。

        `metrics_days=0` 表示**不清理**（默认值只服务不关心这件事的调用方；主循环永远传
        配置值，而 `METRICS_KEEP_DAYS` 在 `settings.validate()` 里不许为 0）。
        `events` / `rounds` / `post_metrics` 都不参与判定，删掉任何一行都不会改变判定结果——
        这也是它们可以裁、而 `authors` / `posts` / `tombstones` 不能裁的原因。

        注意删行**不缩文件**：SQLite 会把腾出来的页留在空闲列表里给后续写入复用，
        文件大小只涨不跌，要真正回收得跑 `VACUUM`（面板的磁盘读数与 SELF_DEGRADED 就是
        为这件事准备的）。
        """
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM events WHERE ts < ?",
                (_iso(now - timedelta(days=events_days)),),
            )
            conn.execute(
                "DELETE FROM rounds WHERE ts < ?",
                (_iso(now - timedelta(days=rounds_days)),),
            )
            if metrics_days > 0:
                conn.execute(
                    "DELETE FROM post_metrics WHERE hour < ?",
                    (_metrics_hour(now - timedelta(days=metrics_days)),),
                )


# =================== 面板与时间线用的只读查询 ===================
#
# 放在这里而不是散在面板里：这些 SQL 编码的是"哪张表、哪一列、保留期什么口径"的知识，
# 和写入端同源才不会读错（`post_metrics.hour` 是主键的一部分、`events.sec_user_id` 可以为
# NULL 表示系统事件——这类事实只有一处说清楚才安全）。
#
# 它们**只接受调用方建好的连接**：面板自己开一个只读连接传进来，这些函数不持有句柄、
# 不负责关闭、也不做任何写操作。


def _safe_json(raw: Any) -> dict[str, Any]:
    """`payload_json` → 字典。**读坏了就返回空字典**，不抛。

    面板是"出问题时打开的东西"：一条手工改坏或旧版本写下的载荷，不该让整页 500。
    跟渲染层同一个纪律——对外部数据宁可少显示一点，也不能让页面打不开。
    """
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _kind_names(kinds: Iterable[Any]) -> list[str] | None:
    """事件类型过滤器 → 合法名字列表；`None` 表示"一个都不合法，结果必然为空"。"""
    names: list[str] = []
    for kind in kinds:
        try:
            name = (kind if isinstance(kind, EventKind) else EventKind(str(kind))).value
        except ValueError:
            continue
        if name not in names:
            names.append(name)
    return names or None


def read_events(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    kinds: Iterable[Any] = (),
    sec_user_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """事件行，按时间倒序。时间倒序而不是 id 倒序的原因：`id` 在并发写账号时**不等于**
    时间顺序（多个账号各写各的），排序靠它会把同一秒里的前后关系搞反。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(_iso(since))
    if until is not None:
        clauses.append("ts < ?")
        params.append(_iso(until))
    if kinds:
        names = _kind_names(kinds)
        if names is None:
            # 传了过滤器、但一个都不是合法类型：结果就是空，不能退化成"不过滤"
            return []
        clauses.append(f"kind IN ({','.join('?' * len(names))})")
        params.extend(names)
    if sec_user_id:
        clauses.append("sec_user_id = ?")
        params.append(sec_user_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    rows = conn.execute(
        "SELECT id, ts, sec_user_id, content_id, kind, payload_json, delivery_json"
        f" FROM events{where} ORDER BY ts DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row["id"]),
                "ts": _dt(row["ts"]),
                "sec_user_id": row["sec_user_id"],
                "content_id": row["content_id"],
                "kind": str(row["kind"]),
                "payload": _safe_json(row["payload_json"]),
                "delivery": _safe_json(row["delivery_json"]),
            }
        )
    return out


def read_event_ticks(
    conn: sqlite3.Connection,
    *,
    since: datetime,
    until: datetime,
    kinds: Iterable[Any] = (),
    sec_user_id: str | None = None,
) -> list[tuple[datetime, str]]:
    """区间内每条事件只取 `(时间, 类型)`，给柱状图数格子用。

    刻意不设 `LIMIT`：它是聚合的输入，截断了就是在图上少画几根柱子，而图上**看不出来**
    自己少画了。区间由调用方给（面板给的是 24 小时 / 7 天），所以行数是有界的。

    `sec_user_id` 与 `read_events` 同名同义：图上和下面的列表必须用**同一组过滤条件**，
    否则"柱子上有 5 根、列表里只有 1 条"会让人以为列表漏了数据。
    """
    params: list[Any] = [_iso(since), _iso(until)]
    clause = "WHERE ts >= ? AND ts < ?"
    if kinds:
        names = _kind_names(kinds)
        if names is None:
            return []
        clause += f" AND kind IN ({','.join('?' * len(names))})"
        params.extend(names)
    if sec_user_id:
        clause += " AND sec_user_id = ?"
        params.append(sec_user_id)
    rows = conn.execute(
        f"SELECT ts, kind FROM events {clause} ORDER BY ts", params
    ).fetchall()
    out: list[tuple[datetime, str]] = []
    for row in rows:
        stamp = _dt(row["ts"])
        if stamp is not None:
            out.append((stamp, str(row["kind"])))
    return out


def read_metrics_series(
    conn: sqlite3.Connection, *, sec_user_id: str, since: datetime | None = None
) -> list[dict[str, Any]]:
    """一个账号**逐小时**的互动量合计（该账号已知作品在同一小时里的和）。

    `SUM` 会跳过 NULL，整列全 NULL 时返回 NULL —— 这正是要的：缺值的字段在图上应当
    是断点，而不是 0。写 0 会在曲线里造出一个"数据突然掉到零"的假象，
    而平台从没这么说过（`models.py` 的第一条契约）。
    """
    params: list[Any] = [sec_user_id]
    clause = "WHERE sec_user_id = ?"
    if since is not None:
        clause += " AND hour >= ?"
        params.append(_metrics_hour(since))
    rows = conn.execute(
        "SELECT hour,"
        " SUM(digg_count) AS digg, SUM(comment_count) AS comment,"
        " SUM(share_count) AS share, SUM(collect_count) AS collect,"
        " COUNT(*) AS posts"
        f" FROM post_metrics {clause} GROUP BY hour ORDER BY hour",
        params,
    ).fetchall()
    return [
        {
            "hour": _dt(row["hour"]),
            "digg": row["digg"],
            "comment": row["comment"],
            "share": row["share"],
            "collect": row["collect"],
            "posts": int(row["posts"] or 0),
        }
        for row in rows
    ]


def read_post_metric_series(
    conn: sqlite3.Connection,
    *,
    sec_user_id: str,
    content_id: str,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """**单条作品**的互动量曲线（面板详情里选中一条作品时用）。"""
    params: list[Any] = [sec_user_id, content_id]
    clause = "WHERE sec_user_id = ? AND content_id = ?"
    if since is not None:
        clause += " AND hour >= ?"
        params.append(_metrics_hour(since))
    rows = conn.execute(
        "SELECT hour, play_count, digg_count, comment_count, share_count, collect_count"
        f" FROM post_metrics {clause} ORDER BY hour",
        params,
    ).fetchall()
    return [
        {
            "hour": _dt(row["hour"]),
            "play": row["play_count"],
            "digg": row["digg_count"],
            "comment": row["comment_count"],
            "share": row["share_count"],
            "collect": row["collect_count"],
        }
        for row in rows
    ]


def read_all_post_metric_series(
    conn: sqlite3.Connection,
    *,
    sec_user_id: str,
    since: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """该账号**每条作品**的逐小时互动量，按 `content_id` 分组（算「新增量」用）。

    和 `read_metrics_series`（按小时求和）的区别是**不求和**：新增量必须先在单条作品内
    做相邻小时的差、再求和。先求和再做差，会把「这个小时多了/少了一条作品」当成互动量的
    涨跌——新作品一出现，合计就凭空跳高一截，而那一截不是任何人点的赞。
    """
    params: list[Any] = [sec_user_id]
    clause = "WHERE sec_user_id = ?"
    if since is not None:
        clause += " AND hour >= ?"
        params.append(_metrics_hour(since))
    rows = conn.execute(
        "SELECT content_id, hour, play_count, digg_count, comment_count,"
        " share_count, collect_count"
        f" FROM post_metrics {clause} ORDER BY content_id, hour",
        params,
    ).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["content_id"]), []).append(
            {
                "hour": _dt(row["hour"]),
                "play": row["play_count"],
                "digg": row["digg_count"],
                "comment": row["comment_count"],
                "share": row["share_count"],
                "collect": row["collect_count"],
            }
        )
    return out


def read_latest_post_metrics(
    conn: sqlite3.Connection, *, sec_user_id: str
) -> dict[str, dict[str, Any]]:
    """每条作品**最新**一行的互动量，按 `content_id` 索引（详情页的作品列表用它）。

    用 `JOIN` 取每条的 `MAX(hour)`，而不是 `GROUP BY` 里直接取"任意一行"：SQLite 的
    `bare column` 行为会从组里挑一行，挑到哪一行**没有保证**——图上就会时好时坏。
    """
    rows = conn.execute(
        "SELECT m.content_id, m.hour, m.play_count, m.digg_count, m.comment_count,"
        " m.share_count, m.collect_count"
        " FROM post_metrics m"
        " JOIN (SELECT content_id, MAX(hour) AS latest FROM post_metrics"
        "       WHERE sec_user_id = ? GROUP BY content_id) t"
        "   ON m.content_id = t.content_id AND m.hour = t.latest"
        " WHERE m.sec_user_id = ?",
        (sec_user_id, sec_user_id),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[str(row["content_id"])] = {
            "hour": _dt(row["hour"]),
            "play": row["play_count"],
            "digg": row["digg_count"],
            "comment": row["comment_count"],
            "share": row["share_count"],
            "collect": row["collect_count"],
        }
    return out


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
        baseline_content_count=(
            int(row["baseline_content_count"])
            if row["baseline_content_count"] is not None
            else None
        ),
        baseline_content_count_at=_dt(row["baseline_content_count_at"]),
        content_count_drift_rounds=int(row["content_count_drift_rounds"] or 0),
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
                # 旧库在迁移前没有这一列；`migrate()` 会补上，但读一条还没迁移的行时
                # 不该炸——按"没有这个已知情况"处理（`None`）
                hidden_from_guest_at=(
                    _dt(p["hidden_from_guest_at"])
                    if "hidden_from_guest_at" in p.keys()
                    else None
                ),
                hidden_seen_streak=(
                    int(p["hidden_seen_streak"] or 0)
                    if "hidden_seen_streak" in p.keys()
                    else 0
                ),
            )
            for p in posts
        ),
        tombstones=tuple(
            Tombstone(
                content_id=t["content_id"],
                removed_at=_dt(t["removed_at"]) or datetime.now(timezone.utc),
                reason=t["reason"],
            )
            for t in tombs
        ),
    )


__all__ = [
    "SCHEMA_VERSION",
    "StateStore",
    "read_all_post_metric_series",
    "read_event_ticks",
    "read_events",
    "read_latest_post_metrics",
    "read_metrics_series",
    "read_post_metric_series",
]
