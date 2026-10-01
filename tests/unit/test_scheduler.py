"""全局闸门：什么时候整体停下来、停多久、什么时候开回来。

这个模块此前**一个测试都没有**，而它恰好是"上游说现在别发请求"的唯一执行者。
代价已经付过一次：`pipeline` 里那条"上游 429/503"分支只算了退避秒数、打了
`gate.closed` 日志、还发了"全局闸门关闭"的通知，却从来没调用 `close()`——
`_until` 恒为 0，闸门永远是开的，于是被限流的上游继续被以约 11 次/分钟砸，
`retry_after` 全被无视（现场特征就是那条日志里的 `remaining=0.0`）。
所以这里把"关"和"退避时长"两件事都钉住。
"""

from __future__ import annotations

import pytest

from dywatch.dtk import MonitorError
from dywatch.scheduler import GlobalGate


def gate(**kwargs) -> GlobalGate:
    return GlobalGate(**kwargs)


# ------------------------------------------------------------------ 开关
def test_a_fresh_gate_is_open():
    g = gate()
    assert g.is_open() is True
    assert g.remaining() == 0.0
    assert g.reason == ""
    assert g.times_closed == 0


def test_close_actually_shuts_the_gate():
    """这条就是这个文件存在的理由：`close()` 必须让 `is_open()` 变假。"""
    g = gate()
    seconds = g.close(60, reason="RATE_LIMITED")

    assert seconds == 60
    assert g.is_open() is False, "关闸之后还说自己开着，就是当初那个 bug"
    assert g.remaining() > 0
    assert g.reason == "RATE_LIMITED"
    assert g.times_closed == 1


def test_close_never_shortens_an_existing_deadline():
    g = gate()
    g.close(600, reason="ENDPOINT_CIRCUIT_OPEN")
    g.close(10, reason="RATE_LIMITED")

    assert g.remaining() > 500, "短的那次不能把长的那个截止时间提前"
    assert g.times_closed == 2


def test_close_raises_the_floor_to_one_second():
    g = gate()
    assert g.close(0, reason="RATE_LIMITED") == 1, "0 秒等于没关，抬到 1 秒"


def test_gate_reopens_by_itself(monkeypatch):
    """恢复必须是自动的：不需要人介入，也不需要哪一轮"顺手"把它打开。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr("dywatch.scheduler.time.monotonic", lambda: clock["now"])
    g = gate()
    g.close(60, reason="RATE_LIMITED")
    assert g.is_open() is False

    clock["now"] += 59
    assert g.is_open() is False
    clock["now"] += 2
    assert g.is_open() is True
    assert g.remaining() == 0.0
    assert g.reason == "", "开回来之后不该还挂着上次的理由"


# ------------------------------------------------------------------ 退避时长
def test_backoff_honours_the_upstream_retry_after():
    """上游自己给的 `retry_after` 比我们猜的准，必须优先用它。"""
    g = gate(default_seconds=60)
    assert g.backoff_for(MonitorError("RATE_LIMITED", retry_after=12)) == 12


def test_backoff_doubles_after_the_threshold_and_then_caps():
    g = gate(default_seconds=60, backoff_after=2, backoff_max=600)
    first = g.backoff_for(MonitorError("IDENTITY_POOL_EXHAUSTED"))  # 连续 1
    second = g.backoff_for(MonitorError("IDENTITY_POOL_EXHAUSTED"))  # 连续 2
    third = g.backoff_for(MonitorError("IDENTITY_POOL_EXHAUSTED"))  # 连续 3 → 翻倍
    for _ in range(10):
        capped = g.backoff_for(MonitorError("IDENTITY_POOL_EXHAUSTED"))

    assert (first, second) == (60, 60), "阈值之内保持基准值"
    assert third == 120, "超过阈值之后翻倍"
    assert capped == 600, "翻倍必须有上限，否则一次长时间故障会等到天荒地老"


def test_a_successful_request_resets_the_doubling():
    g = gate(default_seconds=60, backoff_after=2, backoff_max=600)
    g.backoff_for(MonitorError("RATE_LIMITED"))
    g.backoff_for(MonitorError("RATE_LIMITED"))
    g.backoff_for(MonitorError("RATE_LIMITED"))
    g.note_success()

    assert g.backoff_for(MonitorError("RATE_LIMITED")) == 60, "恢复之后重新从基准值开始"


def test_retry_after_also_counts_as_a_consecutive_failure():
    """一次 429 也是"上游出问题了"的一次证据，不该被排除在连续计数之外。"""
    g = gate(default_seconds=60, backoff_after=1, backoff_max=600)
    g.backoff_for(MonitorError("RATE_LIMITED", retry_after=5))
    assert g.backoff_for(MonitorError("RATE_LIMITED")) == 120


# ------------------------------------------------------------------ 快照
def test_snapshot_exposes_what_the_panel_and_logs_need():
    g = gate()
    g.close(120, reason="QUEUE_FULL")
    snapshot = g.snapshot()

    assert snapshot["open"] is False
    assert snapshot["reason"] == "QUEUE_FULL"
    assert snapshot["remaining_seconds"] > 0
    assert snapshot["times_closed"] == 1


def test_a_long_close_then_a_short_one_keeps_the_long_reason(monkeypatch):
    """截止时间取更长的那个，**理由也要跟着那个更长的走**。

    曾经 `close()` 无条件覆盖 `_reason`：一个 600 秒的熔断之后又来一个 60 秒的限流，
    倒计时显示的是熔断的，面板横幅和日志里写的却是限流——看的人会以为"再过一分钟就好"。
    """
    clock = {"now": 1000.0}
    monkeypatch.setattr("dywatch.scheduler.time.monotonic", lambda: clock["now"])
    g = gate()
    g.close(600, reason="ENDPOINT_CIRCUIT_OPEN")
    g.close(60, reason="RATE_LIMITED")

    assert g.reason == "ENDPOINT_CIRCUIT_OPEN"
    assert g.remaining() == pytest.approx(600.0)


def test_the_reason_follows_the_longer_deadline_even_in_the_other_order(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr("dywatch.scheduler.time.monotonic", lambda: clock["now"])
    g = gate()
    g.close(60, reason="RATE_LIMITED")
    g.close(600, reason="ENDPOINT_CIRCUIT_OPEN")

    assert g.reason == "ENDPOINT_CIRCUIT_OPEN"
    assert g.remaining() == pytest.approx(600.0)


def test_history_is_bounded():
    g = gate()
    for index in range(30):
        g.close(1, reason=f"code-{index}")

    assert len(g._history) == 20, "历史只留最近 20 次，不能无限长"
    assert g._history[-1][1] == "code-29", "留下的该是最近的"
