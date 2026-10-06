"""互动量「新增量」趋势：纯函数，不碰数据库、不碰时钟（`now` 由调用方传入）。

核心约定（见 `dywatch.webui.trend` 的模块文档）：新增量是**单条作品内**相邻小时的差、再求和；
作品第一次出现只是起点；隔了不止一小时的一对不算；没有数据是 `None` 而不是 0。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from dywatch.webui import trend

NOW = datetime(2026, 9, 15, 12, 30, tzinfo=timezone.utc)
H0 = NOW.replace(minute=0)


def row(hours_ago: int, **counts: int | None) -> dict[str, Any]:
    base: dict[str, Any] = {"digg": 0, "comment": 0, "collect": 0, "share": 0}
    base.update(counts)
    base["hour"] = H0 - timedelta(hours=hours_ago)
    return base


# --------------------------------------------------------------------- increments


def test_increments_are_per_post_differences_summed_across_posts():
    rows = {
        "a": [row(2, digg=100), row(1, digg=103), row(0, digg=110)],
        "b": [row(2, digg=50), row(1, digg=52), row(0, digg=52)],
    }

    inc = trend.increments(rows)

    assert inc[H0 - timedelta(hours=1)]["digg"] == 3 + 2
    assert inc[H0]["digg"] == 7 + 0


def test_a_post_appearing_for_the_first_time_is_a_baseline_not_growth():
    """新作品一出现，**合计**会凭空跳高一截，而那一截不是任何人点的赞。

    先求和再做差会把它当成涨幅；先在单条作品内做差，它的第一个采样没有「前一小时」可减。
    """
    rows = {
        "old": [row(1, digg=100), row(0, digg=105)],
        "new": [row(0, digg=1000)],
    }

    inc = trend.increments(rows)

    assert inc[H0]["digg"] == 5, "不是 1005"


def test_a_post_leaving_the_window_does_not_register_as_a_drop():
    """合计会因为一条作品滑出窗口而掉下去；单条作品内的差不受影响。"""
    rows = {
        "stays": [row(2, digg=10), row(1, digg=12), row(0, digg=15)],
        "leaves": [row(2, digg=500), row(1, digg=510)],
    }

    inc = trend.increments(rows)

    assert inc[H0 - timedelta(hours=1)]["digg"] == 2 + 10
    assert inc[H0]["digg"] == 3


def test_a_pair_more_than_one_hour_apart_is_not_counted():
    """服务停过：把停机期间攒下的涨幅全记在恢复后的那一小时，曲线上会凭空多出一座山。"""
    rows = {"a": [row(3, digg=100), row(0, digg=190)]}

    assert trend.increments(rows) == {}


def test_a_missing_value_drops_only_that_field_for_that_pair():
    rows = {
        "a": [row(1, digg=10, comment=None), row(0, digg=14, comment=7)],
        "b": [row(1, digg=1, comment=2), row(0, digg=2, comment=5)],
    }

    inc = trend.increments(rows)

    assert inc[H0]["digg"] == 4 + 1
    assert inc[H0]["comment"] == 3, "a 的评论缺值，只有 b 参与"


def test_a_field_nobody_ever_reported_is_absent_not_zero():
    rows = {"a": [row(1, share=None), row(0, share=None)]}

    inc = trend.increments(rows)

    assert "share" not in inc.get(H0, {})


def test_negative_increments_are_kept():
    """有人取消点赞是真实发生的事，不该被截成 0。"""
    rows = {"a": [row(1, digg=50), row(0, digg=47)]}

    assert trend.increments(rows)[H0]["digg"] == -3


def test_rows_may_arrive_unsorted():
    rows = {"a": [row(0, digg=9), row(2, digg=1), row(1, digg=4)]}

    inc = trend.increments(rows)

    assert inc[H0]["digg"] == 5 and inc[H0 - timedelta(hours=1)]["digg"] == 3


@pytest.mark.parametrize("rows", [{}, {"a": []}, {"a": [row(0, digg=5)]}])
def test_nothing_to_difference_means_no_increments(rows):
    assert trend.increments(rows) == {}


# --------------------------------------------------------------------- bucketize


def test_24h_has_24_hourly_buckets_ending_at_the_current_hour():
    inc = {H0: {"digg": 4}, H0 - timedelta(hours=5): {"digg": 1}}

    labels, unit, values = trend.bucketize(inc, range_key="24h", now=NOW)

    assert unit == "每小时" and len(labels) == 24
    assert all(len(series) == 24 for series in values.values())
    assert values["digg"][-1] == 4 and values["digg"][-6] == 1
    assert values["digg"].count(None) == 22, "没有读数的格子是断点，不是 0"
    assert labels[-1] == H0.astimezone().strftime("%H:%M")


def test_24h_ignores_increments_older_than_the_window():
    inc = {H0 - timedelta(hours=30): {"digg": 9}}

    _labels, _unit, values = trend.bucketize(inc, range_key="24h", now=NOW)

    assert all(v is None for v in values["digg"])


def test_7d_sums_a_days_hours_into_one_bucket_per_local_day():
    h1, h2 = H0 - timedelta(hours=1), H0
    day = lambda moment: moment.astimezone().date()  # noqa: E731
    inc = {h1: {"digg": 3}, h2: {"digg": 4}}

    labels, unit, values = trend.bucketize(inc, range_key="7d", now=NOW)

    assert unit == "每天" and len(labels) == 7
    if day(h1) == day(h2):
        assert values["digg"][-1] == 7
    else:
        assert values["digg"][-2:] == [3, 4]
    assert labels[-1] == day(NOW).strftime("%m-%d")


def test_all_spans_from_the_first_day_with_data_to_today():
    inc = {H0 - timedelta(days=4): {"digg": 2}}

    labels, unit, values = trend.bucketize(inc, range_key="all", now=NOW)

    assert unit == "每天"
    assert 4 <= len(labels) <= 6, "跨本地日历日，边界上可能多一格"
    assert sum(v for v in values["digg"] if v is not None) == 2


def test_all_with_no_data_is_a_single_empty_bucket_for_today():
    labels, _unit, values = trend.bucketize({}, range_key="all", now=NOW)

    assert len(labels) == 1 and values["digg"] == [None]


def test_an_unknown_range_is_an_error_not_a_silent_default():
    with pytest.raises(ValueError):
        trend.bucketize({}, range_key="1y", now=NOW)


# --------------------------------------------------------------------- cumulative


def test_cumulative_starts_from_zero_at_the_first_reading():
    assert trend.cumulative([None, None, 3, 4, 2]) == [None, None, 3, 7, 9]


def test_cumulative_keeps_a_gap_as_a_gap_and_resumes_the_running_total():
    """断点是「不知道」，不是「没涨」：不能用上一个值把它填平。"""
    assert trend.cumulative([2, None, None, 5]) == [2, None, None, 7]


def test_cumulative_does_not_extend_a_flat_line_past_the_last_reading():
    """采集停了之后，图上不该是一条平稳的线——那等于在说「没有增长」。"""
    assert trend.cumulative([2, 3, None, None]) == [2, 5, None, None]


def test_cumulative_follows_negative_increments():
    assert trend.cumulative([5, -2, -1]) == [5, 3, 2]


def test_cumulative_of_nothing_is_nothing():
    assert trend.cumulative([None, None]) == [None, None]
    assert trend.cumulative([]) == []


# --------------------------------------------------------------------- trend_views


def test_trend_views_cover_every_range_and_both_modes_for_every_series():
    rows = {"a": [row(1, digg=10, comment=1), row(0, digg=13, comment=1)]}

    out = trend.trend_views(rows, now=NOW, posts=7)

    assert out["posts"] == 7 and out["has_data"] is True
    assert [s["key"] for s in out["series"]] == ["digg", "comment", "collect", "share"]
    assert all(s["color"].startswith("--") for s in out["series"])
    assert set(out["views"]) == {"24h", "7d", "all"}
    for view in out["views"].values():
        n = len(view["labels"])
        for mode in ("delta", "cumulative"):
            assert set(view[mode]) == set(trend.FIELDS)
            assert all(len(series) == n for series in view[mode].values())


def test_trend_views_say_when_there_is_nothing_to_draw():
    out = trend.trend_views({"a": [row(0, digg=5)]}, now=NOW, posts=1)

    assert out["has_data"] is False


def test_trend_views_are_plain_json():
    """载荷原样进 /api/user 的 JSON：不能带 datetime / tuple 之类的东西。"""
    rows = {"a": [row(1, digg=1), row(0, digg=2)]}

    out = trend.trend_views(rows, now=NOW, posts=1)

    assert json.loads(json.dumps(out)) == out
