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


# ------------------------------------------------------------------ retry_after 的边界
def test_retry_after_is_capped_at_its_own_ceiling_not_the_backoff_one():
    """上游一个异常大的 `retry_after` 不能把监控停摆一天，但也不该被截到十分钟。

    `retry_after` 是**上游明说要等多久**，与 `backoff_max`（我们自己退避的封顶）不是一回事：
    早先两者共用 600 秒，于是上游要求等一小时时我们只等十分钟就放出一轮请求（默认 5 并发）
    去撞同一堵墙。现在它有自己的上限（默认 3600），且与 `backoff_max` 相互独立。
    """
    g = gate(default_seconds=60, backoff_max=600, retry_after_max=3600)
    assert g.backoff_for(MonitorError("RATE_LIMITED", retry_after=86400)) == 3600

    # 上游要求的时间**在**上限之内就原样照办（这才是"听上游的"）
    g2 = gate(default_seconds=60, backoff_max=600, retry_after_max=3600)
    assert g2.backoff_for(MonitorError("RATE_LIMITED", retry_after=1800)) == 1800

    # 两个上限各自独立：把退避上限调大不会顺带放大 retry_after 的封顶
    g3 = gate(default_seconds=60, backoff_max=1200, retry_after_max=1800)
    assert g3.backoff_for(MonitorError("RATE_LIMITED", retry_after=9999)) == 1800


class _Log:
    """最小的结构化日志替身：只记事件名与字段。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def debug(self, event: str, **fields) -> None:
        self.records.append((event, fields))

    def info(self, event: str, **fields) -> None:      # pragma: no cover - 闸门只打 debug
        self.records.append((event, fields))

    def warning(self, event: str, **fields) -> None:   # pragma: no cover
        self.records.append((event, fields))


def test_capping_leaves_a_trace_in_the_log_and_the_snapshot():
    """封顶必须留痕：否则"闸门为什么每十分钟开一次"只能靠读代码才知道。"""
    log = _Log()
    g = gate(default_seconds=60, retry_after_max=600, logger=log)

    g.backoff_for(MonitorError("RATE_LIMITED", retry_after=3600))

    capped = [fields for event, fields in log.records if event == "gate.retry_after_capped"]
    assert capped, "封顶时要有日志"
    assert capped[0]["raw"] == 3600 and capped[0]["capped"] == 600
    assert capped[0]["code"] == "RATE_LIMITED"
    assert g.snapshot()["retry_after_capped"] == 1, "快照里要有计数（面板与 /metrics 靠它）"


def test_the_cap_counter_never_moves_when_nothing_was_capped():
    log = _Log()
    g = gate(default_seconds=60, retry_after_max=3600, logger=log)

    assert g.backoff_for(MonitorError("RATE_LIMITED", retry_after=120)) == 120
    assert g.backoff_for(MonitorError("RATE_LIMITED")) == 60          # 没给 retry_after

    assert g.snapshot()["retry_after_capped"] == 0
    assert [e for e, _ in log.records] == [], "没封顶就不该有日志（否则日志会被刷满）"


def test_a_gate_without_a_logger_still_works():
    """单测可以直接构造闸门，不必为了日志多搭一层（`logger=None` 是默认值）。"""
    g = gate(backoff_max=600, retry_after_max=600)

    assert g.backoff_for(MonitorError("RATE_LIMITED", retry_after=9999)) == 600
    assert g.snapshot()["retry_after_capped"] == 1


def test_a_fractional_retry_after_from_json_is_honoured_rounded_up():
    """`retry_after` 是原样透传的 JSON 值，`"30.5"` / `30.5` 都可能出现。"""
    g = gate()
    assert g.backoff_for(MonitorError("RATE_LIMITED", retry_after="30.5")) == 31
    assert g.backoff_for(MonitorError("RATE_LIMITED", retry_after=12.2)) == 13


@pytest.mark.parametrize("junk", ["abc", "", "nan", "inf", -5, 0, True, [], {}])
def test_an_unusable_retry_after_falls_back_to_our_own_backoff(junk):
    """转不成数字、非正数、布尔：当作没给，**不能抛**（它发生在 `except MonitorError` 分支里）。"""
    g = gate(default_seconds=60)
    assert g.backoff_for(MonitorError("RATE_LIMITED", retry_after=junk)) == 60


# ------------------------------------------------------------------ trip：同一次故障只算一次
def test_trip_closes_an_open_gate_and_reports_the_seconds():
    g = gate(default_seconds=60)

    seconds = g.trip(MonitorError("QUEUE_FULL"))

    assert seconds == 60
    assert g.is_open() is False
    assert g.reason == "QUEUE_FULL"
    assert g.times_closed == 1


def test_stragglers_of_the_same_incident_do_not_escalate_the_backoff():
    """5 个并发请求撞上**同一次**故障，不该被当成"连续 5 次故障"而翻倍。

    以前每个失败的请求都走一遍 `backoff_for()`：第 3 个起 120 秒，第 4 个 240 秒——
    实际停多久取决于有几个请求恰好在途。
    """
    g = gate(default_seconds=60, backoff_after=2, backoff_max=600)

    results = [g.trip(MonitorError("QUEUE_FULL")) for _ in range(5)]

    assert results[0] == 60
    assert all(59 <= seconds <= 60 for seconds in results), results
    assert g.remaining() <= 60
    assert g.times_closed == 1, "一次事故只记一次关闸"


def test_the_next_real_incident_still_escalates_after_stragglers():
    """后到者不推进计数，但**下一次真的事故**（闸门开回来之后又失败）照常翻倍。"""
    g = gate(default_seconds=60, backoff_after=1, backoff_max=600)
    g.trip(MonitorError("QUEUE_FULL"))
    for _ in range(4):
        g.trip(MonitorError("QUEUE_FULL"))      # 同一次事故的后到者
    g._until = 0.0                               # 时间过去了，闸门开回来

    assert g.trip(MonitorError("QUEUE_FULL")) == 120, "第二次事故：连续 2 > 阈值 1，翻倍"


def test_a_straggler_with_a_longer_retry_after_extends_the_gate():
    g = gate(default_seconds=60, backoff_max=600)
    g.trip(MonitorError("QUEUE_FULL"))

    seconds = g.trip(MonitorError("RATE_LIMITED", retry_after=300))

    assert seconds == 300
    assert g.remaining() > 290
    assert g.reason == "RATE_LIMITED", "理由跟着更长的截止时间走"


def test_a_straggler_with_a_shorter_retry_after_never_shortens_the_gate():
    g = gate(default_seconds=60, backoff_max=600)
    g.trip(MonitorError("RATE_LIMITED", retry_after=300))

    seconds = g.trip(MonitorError("RATE_LIMITED", retry_after=10))

    assert 299 <= seconds <= 300
    assert g.remaining() > 290
    assert g.times_closed == 1


# ------------------------------------------------------------------ 同值 retry_after / 封顶只记一次
class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def debug(self, event: str, **fields) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [name for name, _fields in self.events]


def test_stragglers_with_the_same_retry_after_do_not_each_extend_the_gate():
    """最常见的真实形状：5 个并发请求收到**同一个** 429 + `retry_after=30`。

    第一个把闸门关到 30 秒；后到的那个一算 `wanted=30`、而浮点余量已经是 29.99——
    按浮点比较它"更长"，于是每个后到者都"延长"一次，`times_closed` 记成 5、
    `_history`（只留最近 20 条）被同一次事故灌满。
    """
    g = gate(default_seconds=60, backoff_max=600)

    results = [g.trip(MonitorError("RATE_LIMITED", retry_after=30)) for _ in range(5)]

    assert all(29 <= seconds <= 30 for seconds in results), results
    assert g.times_closed == 1, "一次事故只记一次关闸"
    assert len(g._history) == 1


def test_one_capped_incident_is_counted_once_however_many_requests_hit_it():
    """封顶计数的口径是"事故"，不是"撞上它的请求数"。

    上游说等一天（86400），上限 3600：5 个并发请求都带着这个值。面板横幅会写
    "已被封顶 N 次"，N 不该随并发数变化。
    """
    log = _Recorder()
    g = gate(retry_after_max=3600, logger=log)

    for _ in range(5):
        g.trip(MonitorError("RATE_LIMITED", retry_after=86400))

    assert g.snapshot()["retry_after_capped"] == 1
    assert log.names().count("gate.retry_after_capped") == 1
    assert g.times_closed == 1
    assert 3599 <= g.remaining() <= 3600


def test_a_straggler_that_really_extends_the_gate_to_the_cap_is_counted():
    """后到者带着更长的值、**确实**把闸门延长了（而且延长到了上限）：这一次才算封顶。"""
    log = _Recorder()
    g = gate(retry_after_max=3600, logger=log)
    g.trip(MonitorError("RATE_LIMITED", retry_after=300))

    seconds = g.trip(MonitorError("RATE_LIMITED", retry_after=86400))

    assert seconds == 3600
    assert g.snapshot()["retry_after_capped"] == 1
    assert g.times_closed == 2, "关了一次、延长了一次"


def test_comparing_a_straggler_leaves_no_trace_when_it_does_not_extend():
    """"只是比较一下要不要延长"不能产生任何副作用：无日志、无计数。"""
    log = _Recorder()
    g = gate(retry_after_max=3600, logger=log)
    g.trip(MonitorError("RATE_LIMITED", retry_after=3600))
    log.events.clear()

    for _ in range(3):
        g.trip(MonitorError("RATE_LIMITED", retry_after=999999))

    assert log.events == []
    assert g.snapshot()["retry_after_capped"] == 0, "第一次的 3600 恰好等于上限，没被封顶"


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
