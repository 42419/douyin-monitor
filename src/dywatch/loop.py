"""轮次循环：一圈检查全部账号，然后随机等一会儿再开始下一圈。

这个形状是旧项目跑出来的，不是随便选的：

* **一圈 = 全部账号**，而不是"每个账号一个独立周期"。前者的行为容易解释
  （"1.5 分钟一次"就是"一圈 1.5 分钟"），后者在账号数变化时会悄悄改变整体负载。
* **圈内请求由 pacer 排队**，所以并发只是"不让慢账号拖累别人"，
  整体请求速率与账号数无关（约 11 次/分钟）。
* **`users.conf` 按 mtime 热加载**：改完不用重启，这是运维时最常用的一个动作。

轮次之间还有一个刻意的选择：**闸门关着的时候整圈跳过**。上游整体不可用时，
再跑一遍也只是把同一批请求再撞一次墙。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .alerts import Deduplicator
from .dtk import MonitorError
from .messages import freq_hint, frequency_stats, hours_since, newest_post_at
from .models import AuthorState, DiffConfig, Event, EventKind, RoundResult
from .pacer import RequestPacer, RoundWaiter
from .pipeline import ArchiveTrigger, HiddenCheckConfig, notify_system_event, run_author
from .scheduler import GlobalGate
from .settings import Settings
from .state import StateStore
from .users import UserEntry, load_users_conf


class MonitorLoop:
    """Owns the round; everything else is a collaborator."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: StateStore,
        client: Any,
        notifier: Any,
        pacer: RequestPacer,
        waiter: RoundWaiter,
        gate: GlobalGate,
        dedup: Deduplicator,
        logger: Any,
        archive_trigger: ArchiveTrigger | None = None,
        hidden_check: HiddenCheckConfig | None = None,
        stop: asyncio.Event | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.notifier = notifier
        self.pacer = pacer
        self.waiter = waiter
        self.gate = gate
        self.dedup = dedup
        #: 归档下载旁路（`ARCHIVE_DOWNLOAD_ENABLED=false` 时为 None，主链路一行都不碰它）
        self.archive_trigger = archive_trigger
        #: 隐藏作品核验（`HIDDEN_POST_CHECK_ENABLED=false` 时为 None，同上）
        self.hidden_check = hidden_check
        self.log = logger
        self.stop = stop or asyncio.Event()
        self.cfg = DiffConfig.from_settings(settings)
        self._users_mtime: float | None = None
        self._users: list[UserEntry] = []
        self._rounds = 0
        #: 上一轮快照里的自身检查结论（每轮都写，供面板显示）
        self._self_check: dict[str, Any] = {}
        #: 当前**处于**降级状态的原因集合。事件按边沿触发（进入降级时报一次），
        #: 所以这个集合是"上次报过什么"的记忆；它不代表通知窗口。
        self._degraded: set[str] = set()
        #: 上游 `system/status` 的最近一次结果与取样时刻（单调钟，比较间隔用）
        self._upstream_status: dict[str, Any] = {}
        self._upstream_status_at: float = 0.0
        #: 本轮的状态库写入是否失败过（`record_round` / 快照）。每轮开头清零。
        self._store_write_failed = False

    # ------------------------------------------------------------------ users
    def reload_users(self, *, force: bool = False) -> bool:
        """Reload `users.conf` when it changed. Returns whether it did."""
        path = self.settings.users_conf
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None

        if not force and mtime is not None and mtime == self._users_mtime:
            return False

        entries = load_users_conf(path, logger=self.log)
        if entries:
            self.log.info("users.loaded", count=len(entries), path=str(path))
        else:
            self.log.warning(
                "users.empty", path=str(path), hint="users.conf 为空或不存在"
            )
        self._users = entries
        self._users_mtime = mtime
        return True

    @property
    def users(self) -> list[UserEntry]:
        return list(self._users)

    # ------------------------------------------------------------------ rounds
    async def run(self, *, once: bool = False) -> None:
        self.reload_users(force=True)
        while not self.stop.is_set():
            await self.run_round()
            if once:
                break
            if self.stop.is_set():
                break
            seconds = self.waiter.next_wait()
            self.log.info("round.waiting", seconds=seconds)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                pass
            self.reload_users()

    async def run_round(self) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        self._rounds += 1
        # **每轮以"还没有写失败"开始，并且清零必须发生在任何写之前。**
        # `_store_write_failed` 的读点只有一个（`_run_self_check`），写点有
        # `_record_round` 与 `_maintenance`。所以三条返回路径（无账号 / 闸门关着 /
        # 正常跑）都靠这一行拿到干净的初值，剩下要保证的只有"写点在读点之前"——
        # 见 `run_round` 正常路径里的顺序注释。
        self._store_write_failed = False
        if self.archive_trigger is not None:
            # 归档下载的每轮预算是按轮算的，不按账号算——否则 20 个账号各触发一次
            # 就是 20 倍的量
            self.archive_trigger.start_round()

        if not self._users:
            self.log.warning(
                "round.skipped", round=self._rounds, reason="no users configured"
            )
            await self._run_self_check(started)
            return {
                "checked": 0,
                "initialized": 0,
                "new": 0,
                "deleted": 0,
                "title_changed": 0,
                "failed": 0,
            }

        if not self.gate.is_open():
            remaining = round(self.gate.remaining(), 1)
            self.log.warning(
                "round.skipped",
                round=self._rounds,
                reason="gate closed",
                code=self.gate.reason,
                remaining=remaining,
            )
            try:
                self.store.record_round(
                    now=started,
                    results=[],
                    gate_state=f"closed:{self.gate.reason}",
                    duration_ms=0,
                )
            except sqlite3.Error as exc:
                self._store_write_failed = True
                self.log.error(
                    "state.write_failed",
                    table="rounds",
                    error=f"{type(exc).__name__}: {exc}"[:160],
                )
            # 闸门关着的时候**照样做自身检查并刷新快照**：磁盘满与上游不可用是两件独立的
            # 事，而"上游挂了"这段时间恰恰没人会盯着日志看。快照也照写——否则面板会一直显示
            # 上一次真实轮次的时间戳，看起来像进程已经死了，而它其实活得很好、只是被闸门挡住。
            await self._run_self_check(started)
            self.write_status_snapshot([])
            return {
                "checked": 0,
                "initialized": 0,
                "new": 0,
                "deleted": 0,
                "title_changed": 0,
                "failed": 0,
            }

        # 这一轮真的开始跑了（被跳过的轮次只会记 `round.skipped`，不会到这里）。
        # 带上轮次号：和 `round.done` 配对看，也能对上面板里的"本次运行第 N 轮"。
        self.log.info("round.start", round=self._rounds, users=len(self._users))

        states = self.store.load_authors()
        semaphore = asyncio.Semaphore(max(1, int(self.settings["MAX_CONCURRENT"])))
        tasks = [
            asyncio.create_task(
                self._one(entry, states.get(entry.sec_user_id), semaphore)
            )
            for entry in self._users
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        collected: list[RoundResult] = []
        for entry, result in zip(self._users, results):
            if isinstance(result, BaseException):
                self.log.error(
                    "author.crashed", author=entry.sec_user_id, error=repr(result)
                )
                collected.append(
                    RoundResult(
                        sec_user_id=entry.sec_user_id,
                        nickname=entry.nickname,
                        status="fail",
                        error_code="INTERNAL",
                    )
                )
            else:
                collected.append(result)

        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        summary = self._record_round(started, collected, duration_ms)
        self._log_summary(summary, duration_ms)
        # **两个会置 `_store_write_failed` 的写都必须排在 `_run_self_check` 之前。**
        # 原先 `_maintenance` 排在自检之后：它写失败置的位当轮读不到、下一轮开头又被
        # 清零，于是 `state_store_write_failed` 这个原因**永远不会出现在快照里**——
        # 监控工具自己写不进库，面板与通知却一声不响。
        self._maintenance(started)
        await self._refresh_upstream_status(started)
        await self._run_self_check(started)
        # 快照要在自检之后写：它带的是 `self._self_check`，顺序反了就永远是上一轮的读数
        self.write_status_snapshot(collected)
        return summary

    def _record_round(
        self, started: datetime, collected: list[RoundResult], duration_ms: int
    ) -> dict[str, int]:
        """写轮次汇总。**状态库写不进去时不让整轮崩掉**——这是 SELF_DEGRADED 的输入之一。"""
        try:
            return self.store.record_round(
                now=started,
                results=collected,
                gate_state="open"
                if self.gate.is_open()
                else f"closed:{self.gate.reason}",
                duration_ms=duration_ms,
            )
        except sqlite3.Error as exc:
            self._store_write_failed = True
            self.log.error(
                "state.write_failed",
                table="rounds",
                error=f"{type(exc).__name__}: {exc}"[:160],
            )
            return {
                "checked": len(collected),
                "initialized": 0,
                "new": 0,
                "deleted": 0,
                "title_changed": 0,
                "failed": 0,
            }

    def _maintenance(self, started: datetime) -> None:
        try:
            self.store.maintenance(
                now=started,
                events_days=int(self.settings["EVENTS_KEEP_DAYS"]),
                rounds_days=int(self.settings["ROUNDS_KEEP_DAYS"]),
                metrics_days=int(self.settings["METRICS_KEEP_DAYS"]),
            )
        except sqlite3.Error as exc:
            self._store_write_failed = True
            self.log.error(
                "state.write_failed",
                table="maintenance",
                error=f"{type(exc).__name__}: {exc}"[:160],
            )

    # ------------------------------------------------------------------ 上游健康
    async def _refresh_upstream_status(self, now: datetime) -> None:
        """按 `UPSTREAM_STATUS_INTERVAL_SECONDS` 取一次上游的 `system/status`。

        三条取舍：

        * **走同一个 `RequestPacer`。** 它不需要 scope、不消耗身份，但它仍是一次真实的上游
          请求——"整体请求速率约 11 次/分钟"这个承诺必须把它算进去，否则监控自己就是那个
          绕过节奏器的例外。
        * **失败保留上一次的值**，只记下错误码与时间。网络抖一下就把面板上的版本号与身份池
          读数抹掉，比显示一个稍微过时的读数更糟；`checked_at` 会如实说是什么时候取的。
        * **不落库、不推送。** 它是面板上的一格读数，不是告警：上游整体不可用这件事已经由
          `UPSTREAM_DEGRADED` 负责说了，再说一遍只会让两个信号互相稀释。
        """
        interval = int(self.settings["UPSTREAM_STATUS_INTERVAL_SECONDS"])
        if interval <= 0:
            return
        # 闸门关着 == 上游刚说过"现在别发请求"（限流 / 队列满）。这次读数不是非发不可，
        # 但它仍是对同一个上游的一次真实请求——不能成为唯一绕过闸门的例外（闸门刚被
        # 这一轮的 429 关上时，这一步恰好就在本轮末尾）。
        # 故意不更新 `_upstream_status_at`：闸门一开，下一轮就补上，而不是再等一整个间隔。
        if not self.gate.is_open():
            return
        if (
            self._upstream_status_at
            and time.monotonic() - self._upstream_status_at < interval
        ):
            return
        self._upstream_status_at = time.monotonic()
        try:
            await self.pacer.wait_for_turn()
            data = await self.client.system_status()
        except MonitorError as exc:
            self._mark_upstream_unavailable(exc.code, now)
            self.log.debug("upstream.status_unavailable", code=exc.code)
            return
        except Exception as exc:  # noqa: BLE001 - 面板的一格读数不该让整轮崩掉
            self._mark_upstream_unavailable(type(exc).__name__, now)
            self.log.warning(
                "upstream.status_failed", error=f"{type(exc).__name__}: {exc}"[:160]
            )
            return
        self._upstream_status = summarize_upstream_status(data, checked_at=_iso(now))

    def _mark_upstream_unavailable(self, code: str, now: datetime) -> None:
        previous = dict(self._upstream_status or {})
        previous.update({"ok": False, "error": code, "checked_at": _iso(now)})
        self._upstream_status = previous

    # ------------------------------------------------------------------ 自身检查
    def _self_check_reasons(self) -> tuple[list[str], dict[str, Any]]:
        """当前处于降级状态的原因 + 供面板显示的读数。`([], 读数)` 表示健康。

        只做**能一次 syscall 问出答案**的检查（磁盘余量、是否可写）。刻意不去 `SELECT 1`
        探库：状态库每一轮都在写，它能不能写这件事由 `_record_round` 与 `maintenance` 的
        异常直接回答——再去跑一次探测查询只是多一次 I/O 换一个更弱的信号。
        """
        reasons: list[str] = []
        checks: dict[str, Any] = {"path": str(self.settings.home)}

        limit_mb = int(self.settings["SELF_CHECK_FREE_MB"])
        if limit_mb > 0:
            try:
                usage = shutil.disk_usage(self.settings.home)
                free_mb = int(usage.free // (1024 * 1024))
            except OSError as exc:
                # 问不出来不等于没问题，但也不等于有问题——如实记下来，不当成降级
                checks["free_mb"] = None
                checks["error"] = f"{type(exc).__name__}: {exc}"[:120]
                free_mb = None
            else:
                checks["free_mb"] = free_mb
                checks["free_limit_mb"] = limit_mb
                if free_mb < limit_mb:
                    reasons.append("disk_low")

        try:
            writable = os.access(self.settings.home, os.W_OK)
        except OSError:  # pragma: no cover - os.access 只在极端环境下抛
            writable = True
        checks["writable"] = writable
        if not writable:
            reasons.append("data_dir_read_only")

        if self._store_write_failed:
            reasons.append("state_store_write_failed")

        return reasons, checks

    async def _run_self_check(self, now: datetime) -> None:
        """自身检查：**状态每轮都写进快照，事件只在进入降级时报一次**。

        这个"边沿触发"不是优化，是必需的：条件是持续为真的（磁盘不会自己空出来），
        按轮报会在 `events` 表里每 1.5 分钟堆一行一模一样的记录——一天上千行，把真正的事件
        淹掉。所以 `_degraded` 记的是"上次报过什么"，只有新增的原因才产生事件；
        恢复正常时只记日志、清空标记，**不产生"恢复"事件**（面板上的横幅跟着快照消失，
        而通知链路不需要为"没事了"再打扰一次）。
        """
        reasons, checks = self._self_check_reasons()
        current = set(reasons)
        for reason in sorted(current - self._degraded):
            self.log.warning("self_check.degraded", reason=reason, **checks)
            await notify_system_event(
                Event(
                    EventKind.SELF_DEGRADED,
                    sec_user_id="",
                    payload={
                        "reason": reason,
                        **{k: v for k, v in checks.items() if k != "path"},
                    },
                ),
                notifier=self.notifier,
                dedup=self.dedup,
                store=self.store,
                now=now,
                logger=self.log,
            )
        recovered = self._degraded - current
        if recovered:
            self.log.info("self_check.recovered", reasons=",".join(sorted(recovered)))
        self._degraded = current
        self._self_check = {
            "ok": not reasons,
            "reasons": sorted(reasons),
            "checked_at": _iso(now),
            **checks,
        }

    # ------------------------------------------------------------------ 日志

    async def _one(
        self, entry: UserEntry, state: AuthorState | None, semaphore: asyncio.Semaphore
    ) -> RoundResult:
        async with semaphore:
            now = datetime.now(timezone.utc)
            author = state or AuthorState(
                sec_user_id=entry.sec_user_id, nickname=entry.nickname
            )
            if state is None:
                author = self.store.ensure_author(
                    entry.sec_user_id, entry.nickname, now
                )
            elif entry.nickname and state.nickname != entry.nickname:
                self.store.ensure_author(entry.sec_user_id, entry.nickname, now)
                author = author.with_updates(nickname=entry.nickname)

            return await run_author(
                author=author,
                nickname=entry.nickname,
                client=self.client,
                store=self.store,
                notifier=self.notifier,
                dedup=self.dedup,
                pacer=self.pacer,
                gate=self.gate,
                cfg=self.cfg,
                now=now,
                archive_enabled=bool(self.settings["ARCHIVE_ENABLED"]),
                archive_trigger=self.archive_trigger,
                hidden_check=self.hidden_check,
                metrics_enabled=bool(self.settings["METRICS_ENABLED"]),
                logger=self.log,
            )

    def _log_summary(self, summary: dict[str, int], duration_ms: int) -> None:
        parts = [f"检查 {summary['checked']} 个用户"]
        if summary.get("initialized"):
            parts.append(f"新增初始化 {summary['initialized']} 个")
        if summary["new"]:
            parts.append(f"新作品 {summary['new']} 条")
        if summary["deleted"]:
            parts.append(f"删除 {summary['deleted']} 条")
        if summary["title_changed"]:
            parts.append(f"标题变更 {summary['title_changed']} 条")
        if summary["failed"]:
            parts.append(f"{summary['failed']} 个失败")
        if len(parts) == 1:
            parts.append("均无变化")
        self.log.info(
            "round.done",
            round=self._rounds,
            summary="，".join(parts),
            duration_ms=duration_ms,
            gate="open" if self.gate.is_open() else self.gate.reason,
        )

    # ------------------------------------------------------------------ status
    def _archive_snapshot(self) -> dict[str, Any]:
        """归档下载旁路的当前状态。队列只在内存里，所以这是**唯一**能看到积压的地方。

        以前积压只出现在日志的 `archive.pending` 一行里：归档下载开着、队列里躺着 20 条、
        因为容量退避发不出去——面板上完全看不出来，而"新作品有没有存上"恰恰是打开面板要问的。
        """
        trigger = self.archive_trigger
        if trigger is None:
            return {"enabled": False}
        return {
            "enabled": True,
            "pending": trigger.pending,
            "muted_code": trigger.muted_code,
        }

    def write_status_snapshot(self, results: Iterable[RoundResult]) -> Path:
        """A small JSON file the panel and `--status` both read."""
        by_id = {result.sec_user_id: result for result in results}
        states = self.store.load_authors()
        configured = {entry.sec_user_id for entry in self._users}
        entries: list[dict[str, Any]] = []

        for sec_user_id, state in states.items():
            result = by_id.get(sec_user_id)
            freq = frequency_stats(state.posts)
            newest = newest_post_at(state.posts)
            entries.append(
                {
                    "sec_user_id": sec_user_id,
                    "nickname": state.nickname,
                    "configured": sec_user_id in configured,
                    "known_posts": len(state.posts),
                    "tombstones": len(state.tombstones),
                    "ever_had_posts": state.ever_had_posts,
                    "consecutive_fails": state.consecutive_fails,
                    "last_error": state.last_error,
                    "last_error_code": state.last_error_code,
                    "last_seen_at": _iso(state.last_seen_at),
                    "last_update_at": _iso(state.last_update_at),
                    "last_new_video_at": _iso(state.last_new_video_at),
                    # 面板上的"多久没更新"用这两个：最新作品的发布时间，而不是"上次检测到变化"
                    # 的时间——后者会被一次删除/改名刷新，看起来像账号很活跃，是误导。
                    "newest_post_at": _iso(newest),
                    "hours_since_newest_post": hours_since(newest),
                    "update_frequency": freq[0] if freq else None,
                    # 面板的频率气泡要用到这两个数：只有分级文案说不清"这个分级是怎么来的"
                    "freq_avg_days": round(freq[1], 2) if freq else None,
                    "freq_sample_count": freq[2] if freq else None,
                    "freq_hint": freq_hint(freq),
                    "runs": state.runs,
                    "round_status": result.status if result else None,
                    "round_new": result.new_count if result else 0,
                    "round_deleted": result.deleted_count if result else 0,
                }
            )

        snapshot = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            # 两个轮次数，口径不同，**不能互相校验**：
            #   rounds       本次进程启动以来跑了多少轮（内存计数，重启归零）
            #   rounds_total 状态库里累计记录了多少轮（sqlite_sequence 号段，跨重启，
            #                也不受 ROUNDS_KEEP_DAYS 裁剪影响）
            # 只给其中一个会出事：面板原来只显示前者，放在页面最显眼的位置，
            # 看的人会把它读成"累计轮数"，于是"库里几千轮、面板第 51 轮"看起来像丢了数据。
            "rounds": self._rounds,
            "rounds_total": self.store.rounds_total(),
            "gate": self.gate.snapshot(),
            # `base_url` 是配置事实，后面的字段是主循环定期从 `system/status` 取的读数
            # （取不到时会带着 `ok: false` 与上次的值，见 `_refresh_upstream_status`）。
            # 面板只读这份快照，于是"显示上游健康"这件事不需要面板自己发请求。
            "upstream": {
                "base_url": self.settings["DTK_BASE_URL"],
                **(self._upstream_status or {}),
            },
            "self_check": dict(self._self_check),
            "archive": self._archive_snapshot(),
            "notify": {
                "channels": self.notifier.names,
                "silent": bool(self.settings["SILENT_MODE"]),
            },
            "features": {
                "metrics": bool(self.settings["METRICS_ENABLED"]),
                "metrics_keep_days": int(self.settings["METRICS_KEEP_DAYS"]),
                "hidden_check": self.hidden_check is not None,
                "archive_download": self.archive_trigger is not None,
            },
            "users": sorted(
                entries, key=lambda item: item["nickname"] or item["sec_user_id"]
            ),
        }

        path = self.settings.status_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件名**每个写者都不同**：`dywatch once` 与常驻进程并发是文档承认的正常用法
        # （见 cli 里 once 的说明），而固定的 `status.json.tmp` 会让两个写者交错写同一个
        # 文件，`os.replace` 上去的可能是一份两轮混在一起的快照——直接不可解析。
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(snapshot, ensure_ascii=False, indent=2))
            tmp.replace(path)
        except BaseException:
            # 失败时别把半个临时文件留在工作目录里（面板只读 data/，但垃圾会一直堆）
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        return path


def summarize_upstream_status(
    data: Mapping[str, Any], *, checked_at: str | None
) -> dict[str, Any]:
    """上游 `system/status` 的响应 → 快照里那一小块。

    **只搬我们认得形状的字段，其余一概不复制。** 这份数据会写进 `status.json`，而那个文件
    同时是给人看的和 `/api/state` 的对外形态；原样透传等于把上游的内部结构悄悄变成我们的
    对外契约——上游加一个字段我们就多一个字段，哪天上游把一个内部标识放进池子普查里，
    我们会毫不犹豫地把它公开出去。

    实测字段路径（DTK 5.1.2，`routes/system.py`）：
    `components.{postgres,redis,browser_rpc}.{ok,latency_ms,configured,detail_code,…}`、
    `pool.{douyin,tiktok}.{minting,active,cooling,degraded,retired}` + `pool.total_active`、
    `storage.{db_size_bytes,rows,identities}`。`ok` 允许是 `None`（browser_rpc 未配置时
    它诚实地说"不知道"，那不是"坏了"）。
    """
    components: dict[str, Any] = {}
    raw_components = data.get("components")
    if isinstance(raw_components, Mapping):
        for name, value in raw_components.items():
            if isinstance(value, Mapping):
                ok = value.get("ok")
                entry: dict[str, Any] = {
                    "ok": bool(ok) if ok is not None else None,
                    "latency_ms": _int_or_none(value.get("latency_ms")),
                }
                # `configured` / `detail_code` 是 DTK 对 browser_rpc 实际会给的两个字段
                # （见 DTK `routes/system.py::_browser_rpc_status`）：没有它们，面板只能说
                # "不可用"，说不出是"没连上"还是"连上了但状态不对"。`detail_code` 只收
                # 形如 `unreachable` 的短代码——它来自上游，不能让任意文本进快照。
                configured = value.get("configured")
                if isinstance(configured, bool):
                    entry["configured"] = configured
                code = _detail_code(value.get("detail_code"))
                if code:
                    entry["detail_code"] = code
                components[str(name)] = entry
            else:
                components[str(name)] = {"ok": None, "latency_ms": None}

    pool: dict[str, Any] = {}
    raw_pool = data.get("pool")
    if isinstance(raw_pool, Mapping):
        for name, value in raw_pool.items():
            if name == "total_active":
                pool["total_active"] = _int_or_none(value)
            elif isinstance(value, Mapping):
                pool[str(name)] = {
                    str(state): _int_or_none(count) or 0
                    for state, count in value.items()
                }

    storage: dict[str, Any] = {}
    raw_storage = data.get("storage")
    if isinstance(raw_storage, Mapping):
        storage = {
            "db_size_bytes": _int_or_none(raw_storage.get("db_size_bytes")),
            "identities": _int_or_none(raw_storage.get("identities")),
        }

    return {
        "ok": True,
        "checked_at": checked_at,
        "version": str(data.get("version") or "") or None,
        "uptime_seconds": _int_or_none(data.get("uptime_seconds")),
        "components": components,
        "pool": pool,
        "storage": storage,
    }


_DETAIL_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")


def _detail_code(value: Any) -> str | None:
    """上游给的原因代码（`unreachable` / `degraded`…）：只认短的 snake_case，其余丢弃。"""
    if isinstance(value, str) and _DETAIL_CODE_RE.fullmatch(value):
        return value
    return None


def _int_or_none(value: Any) -> int | None:
    """缺值是 `None`，不是 0 —— 与 `models.py` 的第一条契约同源。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


__all__ = ["MonitorLoop", "summarize_upstream_status"]
