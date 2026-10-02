"""面板的只读查询。

**这一层只做"从状态库拿数据"，不做任何展示文案**：文案在 `page_*` 里，HTML 在 `theme` 里。
分开的原因是这两件事的测试方式完全不同——查询要拿真库喂畸形数据，文案要拿载荷喂畸形字段，
混在一起就会写成"既开数据库又断言 HTML"的测试。

两条纪律：

* **只 `SELECT`。** 面板是只读的，它不该因为被打开一次而改变任何状态。连接是短命的
  普通连接（不是 `?mode=ro`：库是 WAL，只读连接要求 `-shm` 已存在且可写，
  监控进程没在跑的时候反而打不开），用完立刻关。
* **"库不存在"和"库里没有这条"是两件事。** 前者返回空值（面板显示"还没跑过一轮"），
  后者返回 `None`（面板显示"查无此人"）；而**库读不出来**（损坏、被锁）必须原样抛
  `sqlite3.Error` 让 HTTP 层回 503——把它当"没有"会让人去 users.conf 里找一个
  明明还在的账号。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Sequence

from ..messages import (
    event_label,
    fmt_time,
    freq_hint,
    frequency_stats,
    hours_since,
    kind_label,
    newest_post_at,
    tombstone_reason,
)
from ..models import Kind, PostState
from ..settings import Settings
from ..state import (
    read_event_ticks,
    read_events,
    read_latest_post_metrics,
    read_metrics_series,
)
from ..users import load_users_conf
from . import charts
from .common import _format_post_age, classify_account


@contextmanager
def open_store(settings: Settings) -> Iterator[sqlite3.Connection]:
    """状态库的只读连接。**调用方必须先确认文件存在**（见 `has_store`）。"""
    conn = sqlite3.connect(str(settings.db_path), timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def has_store(settings: Settings) -> bool:
    """库文件在不在。**要先问这一句再连**：`sqlite3.connect` 会给不存在的路径
    建一个空库，于是"打开面板看一眼"这个只读动作会在数据目录里留下一个空文件，
    之后所有查询都变成 `no such table`——排障时看起来像"库被删了"。"""
    return settings.db_path.is_file()


# =================== 账号详情 ===================

def user_detail(settings: Settings, sec_user_id: str) -> dict[str, Any] | None:
    """一个账号的详情：作者行 + 作品 + 已消失作品 + 最近事件 + 互动量曲线。

    `None` 表示库里没有这个账号；`sqlite3.Error` 原样抛出，由调用方转成 503——
    "读不出来"和"没有这个账号"是两回事。
    """
    if not has_store(settings):
        return None

    with open_store(settings) as conn:
        author = conn.execute(
            "SELECT * FROM authors WHERE sec_user_id = ?", (sec_user_id,)
        ).fetchone()
        if author is None:
            return None
        posts = conn.execute(
            # 置顶排最前（作者自己摆在最上面的东西，看的人往往就是想知道那几条），
            # 其余按发布时间倒序；NULL 发布时间排最后：抖音偶尔不返回 created_at，
            # 它不该因此排到最新。
            "SELECT * FROM posts WHERE sec_user_id = ?"
            " ORDER BY is_top DESC, created_at IS NULL, created_at DESC",
            (sec_user_id,),
        ).fetchall()
        removed = conn.execute(
            "SELECT * FROM tombstones WHERE sec_user_id = ? ORDER BY removed_at DESC LIMIT 20",
            (sec_user_id,),
        ).fetchall()
        removed_total = int(
            conn.execute(
                "SELECT COUNT(*) FROM tombstones WHERE sec_user_id = ?", (sec_user_id,)
            ).fetchone()[0]
        )
        events = conn.execute(
            "SELECT ts, content_id, kind FROM events WHERE sec_user_id = ? ORDER BY id DESC LIMIT 12",
            (sec_user_id,),
        ).fetchall()
        metrics_enabled = bool(settings.get("METRICS_ENABLED", True))
        keep_days = int(settings.get("METRICS_KEEP_DAYS", 14) or 14)
        series: list[dict[str, Any]] = []
        latest: dict[str, dict[str, Any]] = {}
        if metrics_enabled:
            since = datetime.now(timezone.utc) - timedelta(days=keep_days)
            series = read_metrics_series(conn, sec_user_id=sec_user_id, since=since)
            latest = read_latest_post_metrics(conn, sec_user_id=sec_user_id)

    # "还在不在 users.conf 里"要和列表页口径一致：列表读快照里的 configured，而快照就是
    # 拿 users.conf 比对出来的。所以这里不去猜"文件读不到就当它还在"——那会让同一个账号
    # 在列表上写"已移除"、点开却写"正常"。
    known_ids = {entry.sec_user_id for entry in load_users_conf(settings.users_conf)}
    configured = sec_user_id in known_ids
    post_states = [_post_state(post) for post in posts]
    newest = newest_post_at(post_states)
    hours = hours_since(newest)
    row = {
        "sec_user_id": sec_user_id,
        "nickname": author["nickname"] or sec_user_id,
        "configured": configured,
        "consecutive_fails": int(author["consecutive_fails"] or 0),
        "ever_had_posts": bool(author["ever_had_posts"]),
        "hours_since_newest_post": hours,
    }
    color, status_text = classify_account(row, int(settings.get("STALE_FALLBACK_DAYS", 14)))

    freq = frequency_stats(post_states)

    # 作品列表先建好，再让「对访客不可见」那格从**同一份数据**里数——两个读数各算一遍
    # 是错过的（`_post_state` 掉了列 → 统计恒 0、徽章却是对的，同一页自相矛盾）
    post_list = [
        {
            "content_id": post["content_id"],
            "title": post["title"] or "(无标题)",
            "kind": kind_label(Kind.parse(post["kind"])),
            "is_top": bool(post["is_top"]),
            "date": fmt_time(_parse_dt(post["created_at"])),
            "first_seen": fmt_time(_parse_dt(post["first_seen_at"])),
            "absent_rounds": int(post["absent_rounds"] or 0),
            "hidden": bool(post["hidden_from_guest_at"]),
            "hidden_since": fmt_time(_parse_dt(post["hidden_from_guest_at"])),
            "metrics": _metrics_json(latest.get(str(post["content_id"]))),
        }
        for post in posts
    ]

    return {
        "sec_user_id": sec_user_id,
        "nickname": row["nickname"],
        "status_text": status_text,
        "status_color": color,
        "known_posts": len(posts),
        "tombstones": removed_total,
        # 核验确认过"登录可见、未登录看不到"的那些（它们仍在 known_posts 里——
        # 作品没消失，只是访客视角看不到，这也是不在"已消失"里计数的原因）
        "hidden_posts": sum(1 for p in post_list if p["hidden"]),
        "consecutive_fails": row["consecutive_fails"],
        "newest_post_ago": _format_post_age(hours),
        "newest_post_at": fmt_time(newest),
        "initialized_at": fmt_time(_parse_dt(author["initialized_at"])),
        "last_seen_at": fmt_time(_parse_dt(author["last_seen_at"])),
        "update_frequency": freq[0] if freq else None,
        "freq_hint": freq_hint(freq),
        "runs": int(author["runs"] or 0),
        "last_error": author["last_error"],
        "last_error_code": author["last_error_code"],
        "posts": post_list,
        "removed": [
            {
                "content_id": row_["content_id"],
                "date": fmt_time(_parse_dt(row_["removed_at"])),
                "reason": tombstone_reason(row_["reason"]),
            }
            for row_ in removed
        ],
        "events": [
            {
                "ts": fmt_time(_parse_dt(event["ts"])),
                "label": event_label(event["kind"]),
                "content_id": event["content_id"],
            }
            for event in events
        ],
        # 互动量：小时级序列 + 是否开着记录。三条都要给——只给序列的话，
        # "没开记录"和"开了但还没采到"在画面上长得一样，而这两种情况的处理方式完全不同。
        "metrics_enabled": metrics_enabled,
        "metrics_keep_days": keep_days,
        "metrics_series": [
            {
                "hour": item["hour"].isoformat() if item["hour"] else None,
                "digg": item["digg"],
                "comment": item["comment"],
                "share": item["share"],
                "collect": item["collect"],
                "posts": item["posts"],
            }
            for item in series
        ],
        # 图表载荷由服务端算：分桶与配色规则是纯函数，在 Python 里能直接测；
        # 丢给前端算就等于把这部分逻辑放进一段没法单测的字符串里
        "metrics_chart": charts.metrics_chart_payload(series) if series else None,
    }


def _metrics_json(item: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """一条作品最新一行的互动量，转成 **JSON-ready** 的字典。

    `state.read_latest_post_metrics` 给的 `hour` 是 `datetime`（Python 侧比大小、
    分桶都方便，`charts.metrics_chart_payload` 也确实吃这个类型）。但它会被原样放进
    `/api/user/<id>` 的响应体，而 `json.dumps` 对 `datetime` 是抛 `TypeError` 的——
    异常发生在发出响应头之前，客户端看到的是**连接被掐断**（不是 500、没有 body，
    浏览器只显示"请求失败"）。所以归一化放在数据层：谁把这两个接口连起来，
    都不该需要知道"这个字段恰好不是 JSON 原生类型"。

    只单独转 `hour`、其余字段原样带走（复制一份再改），这样 `state` 那边以后多给
    一个字段时这里不用跟着改。
    """
    if item is None:
        return None
    out = dict(item)
    hour = out.get("hour")
    if hasattr(hour, "isoformat"):
        out["hour"] = hour.isoformat()
    return out


def _post_state(post: sqlite3.Row) -> PostState:
    """行 → 模型，**只映射下面几处读数要用的列**（最新作品时间、更新频率）。

    刻意不是"把每一列都搬过来"：逐条作品的展示字段（尤其是 `hidden_from_guest_at`
    这种标志位）在 `user_detail` 里直接从原始行建列表，不走这里。曾经因为有人拿这个
    局部视图去数"对访客不可见"的作品，掉了一个列 → 统计恒为 0、而同一页的徽章仍然
    是对的（徽章读原始行），一个弹窗里两个互相矛盾的说法。要数逐条作品的属性，
    数 `post_list` 那份，别数这里。
    """
    return PostState(
        content_id=post["content_id"],
        kind=Kind.parse(post["kind"]),
        title=post["title"] or "",
        created_at=_parse_dt(post["created_at"]),
        is_top=bool(post["is_top"]),
    )


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# =================== 事件 ===================

def author_names(conn: sqlite3.Connection) -> dict[str, str]:
    """`sec_user_id → 昵称`。事件表里只存 id（当时没存昵称），列表要显示人看得懂的名字。"""
    return {
        str(row["sec_user_id"]): str(row["nickname"] or "")
        for row in conn.execute("SELECT sec_user_id, nickname FROM authors")
    }


def events(
    settings: Settings,
    *,
    since: datetime,
    until: datetime,
    kinds: Sequence[str] = (),
    author: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """窗口内的事件，按时间倒序。库不存在时返回空列表（面板显示"还没有事件"）。

    `author` 交给 SQL 过滤而不是取回来再筛：先 `LIMIT` 再筛会少给结果
    （"最近 200 条里够巧没有他的"），而面板显示的"200 条"是有意义的读数。
    """
    if not has_store(settings):
        return []
    with open_store(settings) as conn:
        rows = read_events(
            conn, since=since, until=until, kinds=kinds, sec_user_id=author or None, limit=limit
        )
        names = author_names(conn)
    for row in rows:
        row["nickname"] = names.get(str(row.get("sec_user_id") or ""), "")
    return rows


def author_name(settings: Settings, sec_user_id: str) -> str:
    """一个账号的昵称（事件页的标题要用）。查不到就返回空串，由调用方决定显示什么。"""
    if not sec_user_id or not has_store(settings):
        return ""
    with open_store(settings) as conn:
        row = conn.execute(
            "SELECT nickname FROM authors WHERE sec_user_id = ?", (sec_user_id,)
        ).fetchone()
    return str(row["nickname"] or "") if row is not None else ""


def event_ticks(
    settings: Settings,
    *,
    since: datetime,
    until: datetime,
    kinds: Sequence[str] = (),
    author: str = "",
) -> list[tuple[datetime, str]]:
    """窗口内每条的 `(时间, 类型)`，给柱状图数格子。库不存在时返回空列表。"""
    if not has_store(settings):
        return []
    with open_store(settings) as conn:
        return read_event_ticks(
            conn, since=since, until=until, kinds=kinds, sec_user_id=author or None
        )


# =================== 给 /metrics 用的统计 ===================

def store_counts(
    settings: Settings, *, events_since: datetime | None = None
) -> dict[str, Any]:
    """状态库的规模与"最近各类事件有多少条"。

    刻意**不抛异常**：`/metrics` 是抓取端点，库读不出来时应该少几行指标、
    并用 `dywatch_state_readable 0` 说出来，而不是让整次抓取变成 500——
    那会让所有指标（包括"上游挂了"这类与库无关的）一起消失。
    """
    out: dict[str, Any] = {
        "readable": False,
        "authors": 0,
        "posts": 0,
        "tombstones": 0,
        "events": 0,
        "events_by_kind": {},
        "metric_rows": 0,
        "metric_posts": 0,
    }
    if not has_store(settings):
        return out
    try:
        with open_store(settings) as conn:
            for key, table in (("authors", "authors"), ("posts", "posts"),
                               ("tombstones", "tombstones"), ("events", "events")):
                out[key] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM events"
                + (" WHERE ts >= ?" if events_since else "")
                + " GROUP BY kind",
                ((events_since.isoformat(),) if events_since else ()),
            ).fetchall()
            out["events_by_kind"] = {str(row["kind"]): int(row["n"]) for row in rows}
            out["metric_rows"] = int(conn.execute("SELECT COUNT(*) FROM post_metrics").fetchone()[0])
            out["metric_posts"] = int(
                conn.execute("SELECT COUNT(DISTINCT content_id) FROM post_metrics").fetchone()[0]
            )
    except sqlite3.Error:
        out["readable"] = False
        return out
    out["readable"] = True
    return out


__all__ = [
    "author_name",
    "author_names",
    "event_ticks",
    "events",
    "has_store",
    "open_store",
    "store_counts",
    "user_detail",
]
