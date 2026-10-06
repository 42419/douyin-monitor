"""互动量的「新增量」趋势：纯函数，没有 IO。

**为什么不直接画累计总数。** 点赞 1500、评论 250、收藏 80、分享 20，每小时只涨个位数——涨幅
占总数不到 1%，四条线又共用一根纵轴，画出来就是四条水平线，什么都看不出。放大纵轴也没用：
四个数的量级差了几十倍。真正想看的是「这一格里涨了多少」，所以这里算的是**新增量**。

**怎么算。** 一条作品在相邻两个小时的互动量之差，就是它这一小时的新增量；一个账号的新增量是
它各作品新增量之和。顺序不能反——先求和再做差，会把「这个小时多了/少了一条作品」当成互动量的
涨跌：新作品一出现，合计就凭空跳高一截，而那一截不是任何人点的赞。

**三条约定**（都是为了不画出平台从没说过的东西）：

* 一条作品**第一次出现**的那一小时只是起点，不算新增；
* 相邻两次采样之间隔了不止一个小时（比如服务停过），这一对**不算**——把停机期间攒下的涨幅
  全记在恢复后的那一小时，曲线上会凭空多出一座山；
* 没有数据的格子是 `None`（断点），不是 0——「这一格里没涨」和「这一格里不知道」是两回事。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from . import charts

#: 四个指标的 key，顺序与图例一致。
FIELDS: tuple[str, ...] = tuple(key for key, _label in charts.METRIC_SERIES)
#: 近 24 小时（按小时一格）、近 7 天（按天一格）、总览（保留期内，按天一格）。
RANGES: tuple[str, ...] = ("24h", "7d", "all")

_HOUR = timedelta(hours=1)


def increments(
    rows_by_post: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[datetime, dict[str, int]]:
    """逐小时的新增量：`{小时: {指标: 该小时各作品新增量之和}}`。

    只含「至少有一对有效相邻采样」的小时和指标；其余缺席（调用方当 `None` 处理）。
    某个指标在某条作品上缺值（`None`），那一对就不参与这个指标——不是按 0 算。
    """
    out: dict[datetime, dict[str, int]] = {}
    for rows in rows_by_post.values():
        ordered = sorted((r for r in rows if r.get("hour")), key=lambda r: r["hour"])
        for prev, cur in zip(ordered, ordered[1:]):
            if cur["hour"] - prev["hour"] != _HOUR:
                continue
            for field in FIELDS:
                before, after = prev.get(field), cur.get(field)
                if before is None or after is None:
                    continue
                bucket = out.setdefault(cur["hour"], {})
                bucket[field] = bucket.get(field, 0) + (int(after) - int(before))
    return out


def _local_day(moment: datetime) -> date:
    return moment.astimezone().date()


def bucketize(
    inc: Mapping[datetime, Mapping[str, int]], *, range_key: str, now: datetime
) -> tuple[list[str], str, dict[str, list[int | None]]]:
    """把逐小时的新增量装进一个范围的格子里：`(标签, 单位, {指标: 每格的值})`。

    * `24h`：最近 24 个小时格，标签 `HH:MM`（`13:00` 表示 13:00 到 14:00 这一格）；
    * `7d`：最近 7 个本地日历日，标签 `MM-DD`（今天那一格还没过完）；
    * `all`：从有数据的第一天到今天，按本地日历日。

    一格里没有任何有效读数就是 `None`；有就是它们的和（可能是 0，也可能是负的——
    有人取消点赞是真实发生的事，不该被截成 0）。
    """
    if range_key == "24h":
        end = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        keys = [end - timedelta(hours=i) for i in range(23, -1, -1)]
        labels = [key.astimezone().strftime("%H:%M") for key in keys]
        values = {f: [inc.get(key, {}).get(f) for key in keys] for f in FIELDS}
        return labels, "每小时", values

    today = _local_day(now)
    if range_key == "7d":
        days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    elif range_key == "all":
        first = min((_local_day(hour) for hour in inc), default=today)
        days = [first + timedelta(days=i) for i in range((today - first).days + 1)]
    else:
        raise ValueError(f"unknown range: {range_key!r}")

    per_day: dict[date, dict[str, int]] = defaultdict(dict)
    for hour, readings in inc.items():
        day = per_day[_local_day(hour)]
        for field, value in readings.items():
            day[field] = day.get(field, 0) + value
    labels = [day.strftime("%m-%d") for day in days]
    values = {f: [per_day.get(day, {}).get(f) for day in days] for f in FIELDS}
    return labels, "每天", values


def cumulative(values: Sequence[int | None]) -> list[int | None]:
    """从这段时间的起点算起的累计新增（每个指标都从 0 出发，所以四条线量级可比）。

    **没有读数的格子仍是 `None`**：累计值只在有读数的格子之间累加，断点之后接着上一个累计值
    往下走。不能沿用前一个值把断点填平——采集停了，图上却是一条平稳的线，等于在说「没有增长」，
    而那些格子是「不知道」。
    """
    total = 0
    out: list[int | None] = []
    for value in values:
        if value is None:
            out.append(None)
        else:
            total += value
            out.append(total)
    return out


def trend_views(
    rows_by_post: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    now: datetime,
    posts: int,
) -> dict[str, Any]:
    """一个账号（或一条作品）的全部趋势视图：3 个范围 × 2 种口径。

    载荷是**数据**而不是 Chart.js 的配置：前端按用户选的范围和口径挑一份、套上图例和颜色。
    所有值都是 JSON 原生类型（`int` / `None` / `str`）。
    """
    inc = increments(rows_by_post)
    views: dict[str, Any] = {}
    for range_key in RANGES:
        labels, unit, delta = bucketize(inc, range_key=range_key, now=now)
        views[range_key] = {
            "labels": labels,
            "unit": unit,
            "delta": delta,
            "cumulative": {f: cumulative(values) for f, values in delta.items()},
        }
    return {
        "posts": posts,
        "has_data": bool(inc),
        "series": charts.metric_series_meta(),
        "views": views,
    }


__all__ = ["FIELDS", "RANGES", "bucketize", "cumulative", "increments", "trend_views"]
