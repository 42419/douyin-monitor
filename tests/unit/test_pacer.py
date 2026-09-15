"""节奏：并发的意义是"不被慢账号拖累"，不是"更快地打请求"。"""

from __future__ import annotations

import random

import pytest

from dywatch.pacer import RequestPacer, RoundWaiter


async def test_pacer_spaces_request_starts_by_the_configured_window():
    pacer = RequestPacer(3.0, 8.0, rng=random.Random(0))
    waits = [await pacer.wait_for_turn() for _ in range(6)]

    # 第一次立刻放行，之后每一次都至少等到上一个 slot 的末尾
    assert waits[0] == 0
    assert all(wait >= 0 for wait in waits[1:])
    # 6 次报到累计等待应接近 5 个间隔（均值 5.5 秒）
    assert sum(waits[1:]) == pytest.approx(5 * 5.5, abs=8.0)


def test_rate_ceiling_is_independent_of_concurrency():
    """这是整个节奏设计的关键性质：账号数不影响请求速率。"""
    pacer = RequestPacer(3.0, 8.0)
    assert pacer.per_minute_ceiling() == pytest.approx(60 / 5.5, rel=0.01)
    assert pacer.per_minute_ceiling() < 12  # 约 10.9 次/分钟


def test_pacer_rejects_an_impossible_window():
    with pytest.raises(ValueError):
        RequestPacer(0, 5)
    with pytest.raises(ValueError):
        RequestPacer(8, 3)


def test_round_waiter_stays_inside_the_window_and_varies():
    waiter = RoundWaiter(15, 40, rng=random.Random(1))
    values = [waiter.next_wait() for _ in range(50)]
    assert all(15 <= value <= 40 for value in values)
    assert len(set(values)) > 3, "必须是随机的，不能固定"


def test_round_waiter_with_equal_bounds_is_constant():
    waiter = RoundWaiter(20, 20)
    assert {waiter.next_wait() for _ in range(10)} == {20}


def test_round_waiter_survives_an_inverted_window():
    """有人把 min 写得比 max 大时不能崩：退化成 min。"""
    waiter = RoundWaiter(40, 15)
    assert waiter.next_wait() == 40
