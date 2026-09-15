"""单账号一轮的编排：取数 → 判定 → 落库 → 通知。

**它是唯一同时知道 `dtk` / `diff` / `state` / `notifiers` 的模块。** 这不是坏味道，
而是设计：把"这段流程涉及哪些部件"集中在一处，其它每个部件就都能只认识一两个概念。

顺序上有两条不能调换的：

1. **先落库，再通知。** 进程在发通知途中被杀，重启后不会重复推送同一条新作品；
   反过来（先发后存）就会。宁可少推一条，不可重复推——重复的告警会让人关掉通知，
   那才是真正的损失。
2. **失败不推进判定状态机。** 一次网络抖动如果让"疑似删除"的计数前进一格，
   就等于把确认建立在没看到数据的基础上，这正是旧项目踩过的坑。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Mapping

from .alerts import Deduplicator, should_send
from .diff import diff
from .dtk import MonitorError, include_raw_for_round
from .models import (
    ArchiveItem,
    AuthorState,
    DiffConfig,
    Event,
    EventKind,
    RoundResult,
)
from .pacer import RequestPacer
from .render import render_event
from .scheduler import GlobalGate
from .state import StateStore

#: 配置类错误（Key 无效、缺 scope）会连坐所有账号，所以一次就把闸门关久一点，
#: 而不是让 N 个账号各失败一遍。
CONFIG_ERROR_GATE_SECONDS = 3600


async def run_author(
    *,
    author: AuthorState,
    nickname: str,
    client: Any,
    store: StateStore,
    notifier: Any,
    dedup: Deduplicator,
    pacer: RequestPacer,
    gate: GlobalGate,
    cfg: DiffConfig,
    now: datetime,
    archive_enabled: bool = True,
    logger: Any = None,
) -> RoundResult:
    started = time.monotonic()
    author = author.with_updates(nickname=nickname or author.nickname)

    if not gate.is_open():
        return RoundResult(
            sec_user_id=author.sec_user_id,
            nickname=nickname,
            status="skipped",
            duration_ms=0,
        )

    raw_included = _should_include_raw(author, cfg)

    page = None
    error: MonitorError | None = None
    archive: dict[str, ArchiveItem] | None = None

    try:
        await pacer.wait_for_turn()
        page = await client.author_posts(author.sec_user_id, cfg.fetch_count, include_raw=raw_included)
        gate.note_success()
    except MonitorError as exc:
        error = exc
        if exc.is_gate:
            seconds = gate.backoff_for(exc)
            _log(logger, "warn", "gate.closed", code=exc.code, seconds=seconds,
                 remaining=round(gate.remaining(), 1))
            await _notify_upstream(exc, seconds, notifier=notifier, dedup=dedup, logger=logger)
        elif exc.is_config:
            # 连坐所有账号的错误：关闸一小时并明确告警，而不是让每个账号各失败一遍
            gate.close(CONFIG_ERROR_GATE_SECONDS, reason=exc.code)
            await _notify_upstream(
                exc, CONFIG_ERROR_GATE_SECONDS, notifier=notifier, dedup=dedup, logger=logger
            )

    # 归档交叉确认：只在"确实有作品从本页消失"时才去读，且零身份成本
    if page is not None and archive_enabled and author.ever_had_posts and author.posts:
        missing = author.known_ids - page.ids()
        if missing:
            try:
                archive = await client.archive_of_author(author.sec_user_id, limit=200)
            except MonitorError as exc:
                # 归档读失败不该影响主判定：它是第二信源，不是必需项
                _log(logger, "debug", "archive.unavailable", code=exc.code)

    events, next_state = diff(author, now=now, cfg=cfg, page=page, error=error, archive=archive)

    event_ids = store.save_round(author.sec_user_id, next_state, events, now=now)

    # ---- 通知（落库之后） ---------------------------------------------------
    deliveries: dict[int, Mapping[str, Any]] = {}
    for row_id, event in zip(event_ids, events):
        if not event.should_notify:
            continue
        allowed, key = should_send(event, dedup)
        if not allowed:
            _log(logger, "debug", "notify.suppressed", kind=event.kind.value, key=key)
            continue
        result = await _deliver(notifier, event)
        deliveries[row_id] = result
        if result.get("failed") and not result.get("sent"):
            # 一条谁都没收到的消息不该占用抑制窗口
            dedup.release(key)
        _log(
            logger,
            "info" if result.get("sent") else "warning",
            "notify.sent",
            kind=event.kind.value,
            sent=result.get("sent"),
            failed=result.get("failed"),
        )

    if deliveries:
        store.record_deliveries(deliveries)

    return _summarize(author, next_state, events, error, started)


def _should_include_raw(author: AuthorState, cfg: DiffConfig) -> bool:
    """`INCLUDE_RAW=auto` 的策略决定。

    `diff` 在发现新作品时把 `last_new_video_at` 设为本轮时间，在有任何变化时把
    `last_update_at` 设为本轮时间，而 `last_seen_at` 每轮都更新——所以
    "上一轮有变化"等价于那两者之一等于 `last_seen_at`。用这个推断而不是再加一列，
    是因为它是同一个事实的另一种表达，加列只会多一处忘记同步的状态。
    """
    if not author.runs:
        return cfg.include_raw != "never"  # 首次记录：一定要拿到置顶标志
    have_new_posts = (
        author.last_new_video_at is not None and author.last_new_video_at == author.last_seen_at
    )
    changed = author.last_update_at is not None and author.last_update_at == author.last_seen_at
    return include_raw_for_round(
        cfg.include_raw,
        raw_refresh_round=author.raw_refresh_round,
        refresh_rounds=cfg.raw_refresh_rounds,
        have_new_posts=have_new_posts,
        title_changed=changed and not have_new_posts,
    )


async def _notify_upstream(
    error: MonitorError,
    seconds: int,
    *,
    notifier: Any,
    dedup: Deduplicator,
    logger: Any,
) -> None:
    """One UPSTREAM_DEGRADED alert per error code per hour.

    上游问题不属于任何一个账号，所以它不跟着某个账号的事务走，也不写进那一条轮次结果——
    直接在这里投递，去重窗口负责保证它不会变成噪音。
    """
    event = Event(
        EventKind.UPSTREAM_DEGRADED,
        sec_user_id="",
        payload={
            "code": error.code,
            "message": error.message,
            "gate_seconds": seconds,
            "rounds": 1,
        },
    )
    allowed, key = should_send(event, dedup)
    if not allowed:
        _log(logger, "debug", "notify.suppressed", kind=event.kind.value, key=key)
        return
    result = await _deliver(notifier, event)
    if result.get("failed") and not result.get("sent"):
        dedup.release(key)
    _log(logger, "warning", "notify.upstream", code=error.code, sent=result.get("sent"),
         failed=result.get("failed"))


async def _deliver(notifier: Any, event: Event) -> dict[str, Any]:
    message = render_event(event)
    delivery = await notifier.send(message)
    return delivery.as_dict()


def _summarize(
    author: AuthorState,
    next_state: AuthorState,
    events: tuple[Event, ...],
    error: MonitorError | None,
    started: float,
) -> RoundResult:
    new_count = sum(1 for e in events if e.kind is EventKind.NEW_POST)
    deleted_count = 0
    for event in events:
        if event.kind in (EventKind.POST_REMOVED, EventKind.ALL_GONE):
            deleted_count += len(event.payload.get("removed") or [])
    title_changed = sum(1 for e in events if e.kind is EventKind.TITLE_CHANGED)

    status = "ok"
    if error is not None:
        status = "fail"
    elif any(e.kind is EventKind.INITIALIZED for e in events):
        status = "init"

    return RoundResult(
        sec_user_id=author.sec_user_id,
        nickname=next_state.nickname,
        status=status,
        new_count=new_count,
        deleted_count=deleted_count,
        title_changed=title_changed,
        error_code=error.code if error else None,
        duration_ms=int((time.monotonic() - started) * 1000),
        events=events,
    )


def _include_raw_mode(cfg: DiffConfig) -> str:
    return getattr(cfg, "include_raw", "auto")


def _log(logger: Any, level: str, event: str, **fields: Any) -> None:
    if logger is None:
        return
    getattr(logger, level, logger.info)(event, **fields)


__all__ = ["CONFIG_ERROR_GATE_SECONDS", "run_author"]
