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
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .alerts import Deduplicator
from .models import AuthorState, DiffConfig, RoundResult
from .pacer import RequestPacer, RoundWaiter
from .pipeline import run_author
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
        self.log = logger
        self.stop = stop or asyncio.Event()
        self.cfg = DiffConfig.from_settings(settings)
        self._users_mtime: float | None = None
        self._users: list[UserEntry] = []
        self._rounds = 0

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

        entries = load_users_conf(path)
        if entries:
            self.log.info("users.loaded", count=len(entries), path=str(path))
        else:
            self.log.warning("users.empty", path=str(path), hint="users.conf 为空或不存在")
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

        if not self._users:
            self.log.warning("round.skipped", reason="no users configured")
            return {"checked": 0, "initialized": 0, "new": 0, "deleted": 0,
                    "title_changed": 0, "failed": 0}

        if not self.gate.is_open():
            remaining = round(self.gate.remaining(), 1)
            self.log.warning(
                "round.skipped", reason="gate closed", code=self.gate.reason, remaining=remaining
            )
            self.store.record_round(
                now=started, results=[], gate_state=f"closed:{self.gate.reason}", duration_ms=0
            )
            return {"checked": 0, "initialized": 0, "new": 0, "deleted": 0,
                    "title_changed": 0, "failed": 0}

        states = self.store.load_authors()
        semaphore = asyncio.Semaphore(max(1, int(self.settings["MAX_CONCURRENT"])))
        tasks = [
            asyncio.create_task(self._one(entry, states.get(entry.sec_user_id), semaphore))
            for entry in self._users
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        collected: list[RoundResult] = []
        for entry, result in zip(self._users, results):
            if isinstance(result, BaseException):
                self.log.error("author.crashed", author=entry.sec_user_id, error=repr(result))
                collected.append(
                    RoundResult(sec_user_id=entry.sec_user_id, nickname=entry.nickname, status="fail",
                                error_code="INTERNAL")
                )
            else:
                collected.append(result)

        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        summary = self.store.record_round(
            now=started,
            results=collected,
            gate_state="open" if self.gate.is_open() else f"closed:{self.gate.reason}",
            duration_ms=duration_ms,
        )
        self._log_summary(summary, duration_ms)
        self.write_status_snapshot(collected)
        self.store.maintenance(
            now=started,
            events_days=int(self.settings["EVENTS_KEEP_DAYS"]),
            rounds_days=int(self.settings["ROUNDS_KEEP_DAYS"]),
        )
        return summary

    async def _one(
        self, entry: UserEntry, state: AuthorState | None, semaphore: asyncio.Semaphore
    ) -> RoundResult:
        async with semaphore:
            now = datetime.now(timezone.utc)
            author = state or AuthorState(sec_user_id=entry.sec_user_id, nickname=entry.nickname)
            if state is None:
                author = self.store.ensure_author(entry.sec_user_id, entry.nickname, now)
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
            summary="，".join(parts),
            duration_ms=duration_ms,
            gate="open" if self.gate.is_open() else self.gate.reason,
        )

    # ------------------------------------------------------------------ status
    def write_status_snapshot(self, results: Iterable[RoundResult]) -> Path:
        """A small JSON file the panel and `--status` both read."""
        by_id = {result.sec_user_id: result for result in results}
        states = self.store.load_authors()
        configured = {entry.sec_user_id for entry in self._users}
        entries: list[dict[str, Any]] = []

        for sec_user_id, state in states.items():
            result = by_id.get(sec_user_id)
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
                    "hours_since_update": _hours_since(state.last_update_at or state.initialized_at),
                    "update_frequency": _frequency_label(state.posts),
                    "runs": state.runs,
                    "round_status": result.status if result else None,
                    "round_new": result.new_count if result else 0,
                    "round_deleted": result.deleted_count if result else 0,
                }
            )

        snapshot = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "rounds": self._rounds,
            "gate": self.gate.snapshot(),
            "upstream": {"base_url": self.settings["DTK_BASE_URL"]},
            "notify": {
                "channels": self.notifier.names,
                "silent": bool(self.settings["SILENT_MODE"]),
            },
            "users": sorted(entries, key=lambda item: item["nickname"] or item["sec_user_id"]),
        }

        path = self.settings.status_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _hours_since(value: datetime | None) -> int | None:
    if value is None:
        return None
    delta = datetime.now(timezone.utc) - value
    return max(0, int(delta.total_seconds() // 3600))


def _frequency_label(posts: Any, *, exclude_top: bool = True) -> str | None:
    """更新频率分级，算法与旧项目一致：排除置顶后按相邻发布时间间隔取均值。"""
    times = sorted(
        post.created_at
        for post in posts
        if post.created_at is not None and (not exclude_top or not post.is_top)
    )
    if len(times) < 2:
        return None
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:]) if b > a]
    if not gaps:
        return None
    avg_days = (sum(gaps) / len(gaps)) / 86400
    if avg_days <= 1.5:
        return "日更"
    if avg_days <= 4:
        return "隔天更新"
    if avg_days <= 10:
        return "周更"
    if avg_days <= 20:
        return "半月更"
    if avg_days <= 45:
        return "月更"
    return "更新较少"


__all__ = ["MonitorLoop"]
