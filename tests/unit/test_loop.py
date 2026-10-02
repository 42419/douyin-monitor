"""轮次日志：开始 / 结束 / 跳过三行的口径。

排障时这三行是"这轮到底有没有在跑、跑了什么、为什么没跑"的唯一线索，
所以钉住两件事：**都带轮次号**（能把开始和结束对上，也能对上面板的"本次运行第 N 轮"），
以及 **`round.start` 只属于"真的开始跑了"的轮次**（被跳过的一轮只有 `round.skipped`）。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
from typing import Any

import pytest

from dywatch.alerts import Deduplicator
from dywatch.dtk import MonitorError
from dywatch.loop import MonitorLoop
from dywatch.models import Page
from dywatch.pacer import RoundWaiter
from dywatch.scheduler import GlobalGate
from dywatch.settings import load_settings
from dywatch.state import StateStore

UID = "MS4wLjABAAAAxxx"


class Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _record(event: str, **fields: Any) -> None:
            self.records.append((level, event, fields))

        return _record

    def names(self) -> list[str]:
        return [event for _level, event, _fields in self.records]

    def events(self, name: str) -> list[dict]:
        return [fields for _level, event, fields in self.records if event == name]


class Client:
    """只回空列表：够跑完一轮，且不会碰 `author_profile`（核验没打开）。"""

    async def author_posts(self, sec_user_id: str, count: int, **kwargs: Any) -> Page:
        return Page(items=(), raw_included=False)


class GateFailingClient:
    """上游在限流：每个账号都撞一次 429，带 `retry_after`。"""

    def __init__(self, code: str = "RATE_LIMITED", retry_after: int | None = 30) -> None:
        self.code = code
        self.retry_after = retry_after
        self.calls = 0

    async def author_posts(self, sec_user_id: str, count: int, **kwargs: Any) -> Page:
        self.calls += 1
        raise MonitorError(self.code, "上游说现在别发请求", retry_after=self.retry_after)


class SimultaneousFailingClient:
    """所有账号**同时在途**、然后同时撞上同一次故障（没有 `retry_after`）。

    真实的并发就是这个形状：`MAX_CONCURRENT` 个请求一起发出去，上游一次故障让它们几乎
    同时失败。这里用一个事件把"都已经在途"卡死，避免靠调度时序碰运气。
    """

    def __init__(self, in_flight: int, code: str = "QUEUE_FULL") -> None:
        self.in_flight = in_flight
        self.code = code
        self.arrived = 0
        self.ready = asyncio.Event()

    async def author_posts(self, sec_user_id: str, count: int, **kwargs: Any) -> Page:
        self.arrived += 1
        if self.arrived >= self.in_flight:
            self.ready.set()
        await self.ready.wait()
        raise MonitorError(self.code, "队列满了")


class Notifier:
    #: 面板快照会把它写进 status.json（`channels`）
    names = ("test",)

    async def send(self, message: Any) -> Any:
        class _Delivery:
            def as_dict(self) -> dict:
                return {"sent": 1, "failed": 0}

        return _Delivery()


class Pacer:
    async def wait_for_turn(self) -> None:
        return None


class Waiter:
    async def sleep(self, seconds: float) -> None:  # pragma: no cover - 只给 run() 用
        return None


def make_loop(
    tmp_path: pathlib.Path,
    logger: Logger,
    *,
    users: str | None = None,
    client: Any = None,
    env: dict[str, str] | None = None,
) -> MonitorLoop:
    environ = {"MONITOR_HOME": str(tmp_path)}
    environ.update(env or {})
    settings = load_settings(None, environ=environ)
    if users is not None:
        settings.users_conf.write_text(users, encoding="utf-8")
    store = StateStore(settings.db_path)
    store.migrate()
    return MonitorLoop(
        settings=settings,
        store=store,
        client=client if client is not None else Client(),
        notifier=Notifier(),
        pacer=Pacer(),  # type: ignore[arg-type]
        waiter=Waiter(),  # type: ignore[arg-type]
        gate=GlobalGate(),
        dedup=Deduplicator(),
        logger=logger,
    )


async def test_round_start_is_logged_before_done_and_both_carry_the_round_number(tmp_path):
    logger = Logger()
    loop = make_loop(tmp_path, logger, users=f"{UID}|示例账号\n")
    loop.reload_users(force=True)

    await loop.run_round()

    names = logger.names()
    assert "round.start" in names, "每轮开始要有一行"
    assert "round.done" in names
    assert names.index("round.start") < names.index("round.done"), "start 必须在 done 之前"

    start = logger.events("round.start")[0]
    done = logger.events("round.done")[0]
    assert start["round"] == 1 and done["round"] == 1, "两行都要带轮次号才配得上"
    assert start["users"] == 1

    await loop.run_round()
    assert logger.events("round.start")[1]["round"] == 2, "轮次号要递增"


async def test_a_skipped_round_does_not_claim_to_have_started(tmp_path):
    """闸门关着的时候这一轮什么都没做：只有 `round.skipped`，不该出现 `round.start`。"""
    logger = Logger()
    loop = make_loop(tmp_path, logger, users=f"{UID}|示例账号\n")
    loop.reload_users(force=True)
    loop.gate.close(60, reason="upstream_degraded")

    await loop.run_round()

    names = logger.names()
    assert "round.skipped" in names
    assert "round.start" not in names, "没真的跑就不该记 start"
    assert "round.done" not in names
    assert logger.events("round.skipped")[0]["round"] == 1
    assert logger.events("round.skipped")[0]["reason"] == "gate closed"


async def test_no_users_also_logs_the_round_number(tmp_path):
    logger = Logger()
    loop = make_loop(tmp_path, logger, users=None)   # 没有 users.conf
    loop.reload_users(force=True)

    await loop.run_round()

    skipped = logger.events("round.skipped")
    assert skipped and skipped[0]["reason"] == "no users configured"
    assert skipped[0]["round"] == 1
    assert "round.start" not in logger.names()


async def test_upstream_rate_limit_actually_closes_the_gate(tmp_path):
    """上游限流 → 闸门必须真的关上。

    曾经的形状是"算一下退避秒数、打一行 `gate.closed`、发一条全局告警"，
    但**没有调用 `close()`**：`_until` 恒为 0，闸门永远是开的，被限流的上游继续
    以约 11 次/分钟挨砸，`retry_after` 全被无视。这条测试钉的就是那一行调用。
    """
    logger = Logger()
    client = GateFailingClient(retry_after=30)
    loop = make_loop(tmp_path, logger, users=f"{UID}|示例账号\n", client=client)
    loop.reload_users(force=True)

    await loop.run_round()

    assert logger.names().count("gate.closed") == 1
    closed = logger.events("gate.closed")[0]
    assert closed["remaining"] > 0, "自己说关了、余量却是 0，就是那个 bug 的现场特征"
    assert loop.gate.is_open() is False, "关闸之后闸门必须处于关闭状态"
    assert loop.gate.remaining() >= 29


async def test_one_upstream_incident_hit_by_several_in_flight_accounts_closes_the_gate_once(tmp_path):
    """4 个账号同时在途、撞上同一次 503：只算**一次**关闸，退避不叠加。

    以前每个失败的请求都各自推进一次"连续失败"计数：第 3 个起 120 秒、第 4 个 240 秒，
    闸门最终停多久取决于有几个请求恰好在途。
    """
    logger = Logger()
    users = "".join(f"{UID}{n}|账号{n}\n" for n in range(4))
    client = SimultaneousFailingClient(in_flight=4)
    loop = make_loop(tmp_path, logger, users=users, client=client)
    loop.reload_users(force=True)

    await loop.run_round()

    assert client.arrived == 4, "前提：4 个请求确实同时在途"
    assert logger.names().count("gate.closed") == 1, "真正关闸的只有第一个"
    assert logger.names().count("gate.already_closed") == 3, "后到的 3 个只确认"
    assert loop.gate.times_closed == 1
    assert 55 <= loop.gate.remaining() <= 60, "默认 60 秒，不该被叠加成 120/240"


async def test_the_round_after_a_gate_close_is_skipped_without_touching_upstream(tmp_path):
    """闸门关着的那一轮应当整轮跳过：不再向上游发请求，也不假装这一轮"开始跑过"。"""
    logger = Logger()
    client = GateFailingClient()
    loop = make_loop(tmp_path, logger, users=f"{UID}|示例账号\n", client=client)
    loop.reload_users(force=True)

    await loop.run_round()
    first_round_calls = client.calls
    await loop.run_round()

    assert client.calls == first_round_calls, "闸门关着还去打上游，等于没关"
    assert logger.events("round.skipped")[-1]["reason"] == "gate closed"


async def test_a_bad_sec_user_id_does_not_stop_the_other_accounts(tmp_path):
    """`INVALID_PARAM` 是**单个账号**的问题，不能连坐全局。

    DTK 对一个写错的 `sec_user_id` 回 400 `INVALID_PARAM`；它曾经被归进"配置类错误"，
    于是一个坏 ID 让其余全部账号每小时停摆一小时（而且每轮到期后又被撞一次，
    闸门再也开不回来）。现在它只按这个账号自己的失败计数处理。
    """
    logger = Logger()
    client = GateFailingClient(code="INVALID_PARAM", retry_after=None)
    loop = make_loop(tmp_path, logger, users=f"{UID}|示例账号\n", client=client)
    loop.reload_users(force=True)

    await loop.run_round()

    assert loop.gate.is_open() is True, "一个账号的 ID 写错不该关掉全局闸门"
    assert "gate.closed" not in logger.names()
    assert "round.start" in logger.names(), "这一轮是真的跑了（只是那个账号失败了）"


async def test_an_auth_failure_still_stops_everything(tmp_path):
    """凭证错是**实例级**问题：所有账号都会 401，所以仍然要关闸并只告警一次。"""
    logger = Logger()
    client = GateFailingClient(code="UNAUTHENTICATED", retry_after=None)
    loop = make_loop(tmp_path, logger, users=f"{UID}|示例账号\n", client=client)
    loop.reload_users(force=True)

    await loop.run_round()

    assert loop.gate.is_open() is False
    assert loop.gate.remaining() > 3000, "凭证类问题关得久一点（一小时），而不是 60 秒"


# ------------------------------------------------------------------ 自身检查
#
# 这一节盯的是"监控工具自己的健康"，它有两半：**读数每轮都进快照**（面板上的横幅与
# `/metrics` 读它），以及**事件只在进入降级时报一次**（条件是持续为真的，按轮报会在
# `events` 表里每轮堆一行一模一样的记录）。

def _snapshot(loop: MonitorLoop) -> dict:
    return json.loads(loop.settings.status_path.read_text(encoding="utf-8"))


async def test_self_check_lands_in_the_snapshot_but_reports_only_once(tmp_path):
    logger = Logger()
    # 阈值抬到不可能达到的值 → 恒定 `disk_low`；顺手把上游探测关掉（`0` = 不取），
    # 这条用例不该顺带验证那台假 client 没有 `system_status` 的异常路径
    loop = make_loop(
        tmp_path, logger,
        users=f"{UID}|示例账号\n",
        env={"SELF_CHECK_FREE_MB": "999999999", "UPSTREAM_STATUS_INTERVAL_SECONDS": "0"},
    )
    loop.reload_users(force=True)

    await loop.run_round()
    first = _snapshot(loop)["self_check"]
    await loop.run_round()
    second = _snapshot(loop)["self_check"]

    for reading in (first, second):
        assert reading["ok"] is False
        assert reading["reasons"] == ["disk_low"], reading
        assert reading["free_limit_mb"] == 999999999, "读数要能解释为什么判成降级"
        assert reading["free_mb"] is not None

    kinds = [row["kind"] for row in loop.store.recent_events(limit=50)]
    assert kinds.count("self_degraded") == 1, "持续为真的条件只能报一次，但读数每轮都在"

    # **边沿触发本身要看日志，不能只看事件表。** `should_send` 的 6 小时窗口会在落库
    # 之前把多出来的那几次挡掉，于是"每轮都报"在 `events` 表里也长得像"只报了一次"——
    # 这两条断言合起来才真正钉住"事件只在进入降级时报"。
    assert logger.names().count("self_check.degraded") == 1, "只有进入降级的那一轮该记这条"
    assert logger.events("self_check.degraded")[0]["reason"] == "disk_low", \
        "原因要在日志里出现（抑制窗口就是按它分桶的）"


async def test_self_check_does_not_announce_a_recovery(tmp_path):
    """恢复正常时只记日志、不发事件——横幅跟着快照消失就够了，不用再打扰一次。"""
    logger = Logger()
    loop = make_loop(
        tmp_path, logger,
        users=f"{UID}|示例账号\n",
        env={"SELF_CHECK_FREE_MB": "0", "UPSTREAM_STATUS_INTERVAL_SECONDS": "0"},
    )
    loop.reload_users(force=True)
    loop._degraded = {"disk_low"}   # 预置成"上一轮报过"，本轮条件已恢复

    await loop.run_round()

    assert _snapshot(loop)["self_check"]["ok"] is True
    kinds = [row["kind"] for row in loop.store.recent_events(limit=50)]
    assert "self_degraded" not in kinds
    assert logger.names().count("self_check.recovered") == 1
    assert loop._degraded == set()


async def test_a_failed_state_store_write_is_visible_in_the_same_round(tmp_path, monkeypatch):
    """回归：状态库写失败必须**当轮**就出现在快照里。

    曾经 `_maintenance` 排在 `_run_self_check` 之后：它置的 `_store_write_failed`
    当轮读不到，下一轮开头又被清零 —— `state_store_write_failed` 这个原因永远不会出现在
    快照里，等于信号不存在。顺序是"两个写点都在读点之前"。
    """
    logger = Logger()
    loop = make_loop(
        tmp_path, logger,
        users=f"{UID}|示例账号\n",
        env={"SELF_CHECK_FREE_MB": "0", "UPSTREAM_STATUS_INTERVAL_SECONDS": "0"},
    )
    loop.reload_users(force=True)

    def boom(**kwargs: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(loop.store, "maintenance", boom)

    await loop.run_round()

    reading = _snapshot(loop)["self_check"]
    assert reading["reasons"] == ["state_store_write_failed"], reading
    assert logger.names().count("state.write_failed") == 1

    await loop.run_round()
    assert _snapshot(loop)["self_check"]["reasons"] == ["state_store_write_failed"], \
        "持续写不进去就要持续为真，而不是只报一轮"


# ---------------------------------------------------------------- 上游健康读数

class StatusClient:
    """`system/status` 的形状按 DTK 5.1.2 抄（含一个 `ok: None` 的组件与一个多余字段）。"""

    def __init__(self, *, fail: MonitorError | None = None) -> None:
        self.fail = fail
        self.calls = 0

    async def author_posts(self, sec_user_id: str, count: int, **kwargs: Any) -> Page:
        return Page(items=(), raw_included=False)

    async def system_status(self) -> dict:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return {
            "version": "5.1.2",
            "uptime_seconds": 86400,
            "components": {"postgres": {"ok": True, "latency_ms": 3},
                           "browser_rpc": {"ok": None, "latency_ms": None}},
            "pool": {"douyin": {"active": 7, "minting": 1}, "total_active": 7},
            "storage": {"db_size_bytes": 1024, "identities": 8, "internal_note": "不该被搬运"},
        }


async def test_upstream_readings_are_snapshotted(tmp_path):
    logger = Logger()
    client = StatusClient()
    loop = make_loop(tmp_path, logger, users=f"{UID}|示例账号\n", client=client)
    loop.reload_users(force=True)

    await loop.run_round()

    upstream = _snapshot(loop)["upstream"]
    assert upstream["ok"] is True and upstream["version"] == "5.1.2"
    assert upstream["uptime_seconds"] == 86400
    assert upstream["components"]["postgres"]["ok"] is True
    assert upstream["components"]["browser_rpc"]["ok"] is None, "未配置是'不知道'，不是'坏了'"
    assert upstream["pool"]["douyin"]["active"] == 7
    assert upstream["pool"]["total_active"] == 7
    assert "internal_note" not in upstream["storage"], "白名单之外的字段不该被搬进快照"


async def test_upstream_readings_keep_the_previous_value_when_the_call_fails(tmp_path):
    """取不到新读数时**保留上一次的**，只把 `ok`/`error`/`checked_at` 换掉。

    网络抖一下就把版本号与身份池读数抹掉，比显示一个稍微过时的读数更糟。
    """
    logger = Logger()
    client = StatusClient()
    loop = make_loop(tmp_path, logger, users=f"{UID}|示例账号\n", client=client)
    loop.reload_users(force=True)
    await loop.run_round()

    client.fail = MonitorError("UPSTREAM_UNREACHABLE", "连接不上")
    # 探测默认按 `UPSTREAM_STATUS_INTERVAL_SECONDS` 节流（300 秒），测试里两轮之间是 0 秒，
    # 所以直接把节流时间戳清掉强制它再取一次——比把间隔配成 1 秒再 sleep 稳。
    loop._upstream_status_at = None
    await loop.run_round()

    upstream = _snapshot(loop)["upstream"]
    assert upstream["ok"] is False and upstream["error"] == "UPSTREAM_UNREACHABLE"
    assert upstream["version"] == "5.1.2", "取不到新读数时保留旧的"
    assert upstream["pool"]["douyin"]["active"] == 7
