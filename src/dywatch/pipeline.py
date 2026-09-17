"""单账号一轮的编排：取数 → 判定 → 落库 → 通知。

**它是唯一同时知道 `dtk` / `diff` / `state` / `notifiers` 的模块。** 这不是坏味道，
而是设计：把"这段流程涉及哪些部件"集中在一处，其它每个部件就都能只认识一两个概念。

顺序上有三条不能调换的：

1. **先落库，再通知。** 进程在发通知途中被杀，重启后不会重复推送同一条新作品；
   反过来（先发后存）就会。宁可少推一条，不可重复推——重复的告警会让人关掉通知，
   那才是真正的损失。
2. **失败不推进判定状态机。** 一次网络抖动如果让"疑似删除"的计数前进一格，
   就等于把确认建立在没看到数据的基础上，这正是旧项目踩过的坑。
3. **归档下载排在通知之后。** 它是旁路：可以慢、可以欠，但绝不能让主链路等它。
   详见 `ArchiveTrigger`。
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

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

#: 归档下载的内存队列上限。队列只在内存里：进程重启会丢，但重启前那几条本来也没有
#: 别的办法补（DTK 不知道"dywatch 想存哪几条"），为它加一张表不值得。
ARCHIVE_QUEUE_MAX = 200
#: 一条作品最多被试几次就放弃。没有这个上限，一条永远失败的作品会堵住队首，
#: 后面的条目一辈子排不上（它每轮只花掉一个预算）。
ARCHIVE_MAX_ATTEMPTS = 3
#: 配置类失败（缺 `media:write`、DTK 没配下载器）的退避窗口：这类问题重试没有意义。
ARCHIVE_CONFIG_MUTE_SECONDS = 3600
#: 容量类退避的兜底值——DTK 的 `QUEUE_FULL` 通常自带 `retry_after`，没给时用这个。
ARCHIVE_CAPACITY_MUTE_SECONDS = 300


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
    archive_trigger: ArchiveTrigger | None = None,
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

    # ---- 归档下载（**排在通知之后**） ---------------------------------------
    # 放在这里而不是通知之前，是因为"这一轮该推的消息"比"顺手存个档"重要：写请求慢
    # 下来只会推迟下一轮的覆盖（旁路本身有节奏器和每轮预算兜着），而通知被拖住，
    # 就是这条新作品真的没推出去。
    if archive_trigger is not None:
        await archive_trigger.trigger(events)

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


class ArchiveTrigger:
    """新作品 → DTK 媒体下载的旁路（`ARCHIVE_DOWNLOAD_ENABLED=true` 时才被创建）。

    它照抄 DTK 自己 `webhooks.py` 的原则——"触发动作绝不能影响主流程"——并把它落成
    三条不变量，按重要性排序：

    1. **不拖住主链路。** 调用点排在通知之后；**每次请求都过同一个 `RequestPacer`**
       （与抓取共用节奏，所以不会突发）；写请求用 `dtk.WRITE_TIMEOUT`（10 秒）；
       失败最多重试一次，不纠缠。
    2. **不丢档。** `NEW_POST` 只出现一次（`diff` 保证），所以"这一轮没发出去的请求"
       事后没有任何机会补。于是这里维护一个内存队列：失败、退避、超预算的条目都留在
       队列里，下一轮接着发。只有"这条作品确实没有可下载的媒体"（`INVALID_PARAM`）
       和"试满 `ARCHIVE_MAX_ATTEMPTS` 次还是失败"才会出队。
    3. **不刷屏。** 容量满（`QUEUE_FULL`，DTK 自带 `retry_after`）或配置不对
       （`NOT_CONFIGURED` / `FORBIDDEN_SCOPE`）时进入退避窗口，窗口内一条请求都不发，
       只在每次尝试时记一条 debug。

    它知道自己只是旁路：**所有失败都在这里终结**，绝不向 `run_author` 抛异常。
    """

    __slots__ = (
        "_client", "_pacer", "_pin_enabled", "_max_per_round", "_log",
        "_pending", "_in_queue", "_muted_until", "_muted_code", "_budget",
    )

    def __init__(
        self,
        *,
        client: Any,
        pacer: RequestPacer,
        pin: bool = False,
        max_per_round: int = 10,
        logger: Any = None,
    ) -> None:
        self._client = client
        self._pacer = pacer
        self._pin_enabled = bool(pin)
        self._max_per_round = max(1, int(max_per_round))
        self._log = logger
        #: 队列元素是可变的三元组：`[content_id, sec_user_id, attempts]`
        self._pending: deque[list[Any]] = deque()
        self._in_queue: set[str] = set()
        self._muted_until = 0.0
        self._muted_code: str | None = None
        self._budget = self._max_per_round

    # ------------------------------------------------------------------ 状态
    @property
    def pending(self) -> int:
        """队列里积压了多少条（面板/日志用）。"""
        return len(self._pending)

    @property
    def muted_code(self) -> str | None:
        return self._muted_code if time.monotonic() < self._muted_until else None

    def start_round(self) -> None:
        """每轮开头重置预算。由 `loop.run_round` 调用——预算按轮算，不按账号算。"""
        self._budget = self._max_per_round

    # ------------------------------------------------------------------ 主入口
    async def trigger(self, events: Sequence[Event]) -> None:
        """把本轮的新作品放进队列，然后尽预算往外发。"""
        self._enqueue(events)
        if not self._pending:
            return
        if time.monotonic() < self._muted_until:
            _log(
                self._log, "debug", "archive.muted",
                code=self._muted_code,
                remaining=round(self._muted_until - time.monotonic(), 1),
                pending=len(self._pending),
            )
            return
        await self._drain()

    def _enqueue(self, events: Iterable[Event]) -> None:
        for event in events:
            content_id = event.content_id
            if event.kind is not EventKind.NEW_POST or not content_id:
                continue
            if content_id in self._in_queue:
                continue
            while len(self._pending) >= ARCHIVE_QUEUE_MAX:
                dropped = self._pending.popleft()
                self._in_queue.discard(str(dropped[0]))
                _log(self._log, "warning", "archive.queue_overflow",
                     dropped=dropped[0], max=ARCHIVE_QUEUE_MAX)
            self._pending.append([content_id, event.sec_user_id, 0])
            self._in_queue.add(content_id)

    async def _drain(self) -> None:
        sent = 0
        while self._pending and self._budget > 0:
            content_id, sec_user_id, attempts = self._pending[0]
            self._budget -= 1
            await self._pacer.wait_for_turn()
            try:
                result = await self._client.start_download(str(content_id))
            except Exception as exc:  # noqa: BLE001 - 见 `_on_failure`：旁路必须兜住一切
                # 注意是 `Exception` 而不是 `BaseException`：`asyncio.CancelledError`
                # 继承自后者，停机时的取消必须照常往上走，不能被这条旁路吃掉。
                if not await self._on_failure(
                    exc, content_id=str(content_id), sec_user_id=str(sec_user_id),
                    attempts=int(attempts),
                ):
                    break
                continue
            self._dequeue()
            sent += 1
            # DTK 对"这条已经在存了/已经存过"返回 200 + reused，那不是一条新的下载，
            # 记成 started 会让人以为每次都真的重新下了一遍
            reused = result.get("reused")
            _log(
                self._log, "info",
                "archive.download_reused" if reused else "archive.download_started",
                sec_user_id=sec_user_id, content_id=content_id,
                download_id=result.get("download_id"), state=result.get("state"),
                archived=result.get("archived"), reused=reused,
            )
            if self._pin_enabled and result.get("download_id"):
                await self._pin(str(result["download_id"]), content_id=str(content_id))

        self._log_tail(sent)

    async def _pin(self, download_id: str, *, content_id: str) -> None:
        await self._pacer.wait_for_turn()
        try:
            await self._client.pin_download(download_id, True)
        except Exception as exc:  # noqa: BLE001 - 与 _drain 同一个理由：不外抛
            # 下载已经被受理、正在跑，所以这不是"下载失败"；但也不能沉默：
            # "以为 pin 上了其实没有"意味着这条档在容量满时会被静默淘汰
            code = exc.code if isinstance(exc, MonitorError) else type(exc).__name__
            message = (exc.message if isinstance(exc, MonitorError) else str(exc))[:120]
            _log(self._log, "warning", "archive.pin_failed", download_id=download_id,
                 content_id=content_id, code=code, message=message)
            return
        _log(self._log, "info", "archive.download_pinned",
             download_id=download_id, content_id=content_id)

    async def _on_failure(
        self, exc: BaseException, *, content_id: str, sec_user_id: str, attempts: int
    ) -> bool:
        """处理一次失败。返回 True = 还可以继续下一条，False = 本轮到此为止。

        DTK 的错误码在这里被分成三类，处置完全不同——把它们混成一句"失败了"是这段
        逻辑最容易写错的地方。非 `MonitorError` 的意外（客户端自己出 bug）走第三类：
        旁路连这个都要兜住，因为它唯一不可接受的行为就是"把主流程带崩"。
        """
        code = exc.code if isinstance(exc, MonitorError) else type(exc).__name__
        message = (exc.message if isinstance(exc, MonitorError) else str(exc))[:120]

        if code == "INVALID_PARAM":
            # 这条作品确实没有可下载的媒体（纯文字、已下架、只有一张封面），
            # 再试一百次也是一样的结果
            self._dequeue()
            _log(self._log, "info", "archive.download_skipped", sec_user_id=sec_user_id,
                 content_id=content_id, code=code, message=message)
            return True

        if isinstance(exc, MonitorError) and (exc.is_gate or exc.is_config):
            # 容量暂停（DTK 自带 retry_after）或配置问题（缺 scope、没配下载器）：
            # 这一轮剩下的请求再发也是白挨，进窗口等下轮
            seconds = int(
                exc.retry_after
                or (ARCHIVE_CONFIG_MUTE_SECONDS if exc.is_config else ARCHIVE_CAPACITY_MUTE_SECONDS)
            )
            self._muted_until = time.monotonic() + seconds
            self._muted_code = code
            _log(self._log, "warning", "archive.muted_raised", code=code, seconds=seconds,
                 pending=len(self._pending), message=message)
            return False

        # 余下三类都在这里：网络抖动（DTK_UNREACHABLE）、DTK 内部错误、以及非
        # MonitorError 的意外。留到下轮再试；试满次数就放弃并说清楚——不然一条永远
        # 失败的条目会一直占着队首（每轮只花掉一个预算，后面的条目一辈子排不上）。
        attempts += 1
        if attempts >= ARCHIVE_MAX_ATTEMPTS:
            self._dequeue()
            _log(self._log, "warning", "archive.download_given_up", sec_user_id=sec_user_id,
                 content_id=content_id, attempts=attempts, code=code, message=message)
            return True
        self._pending[0][2] = attempts
        _log(self._log, "warning", "archive.download_failed", sec_user_id=sec_user_id,
             content_id=content_id, attempts=attempts, code=code, message=message)
        return False

    def _dequeue(self) -> None:
        dropped = self._pending.popleft()
        self._in_queue.discard(str(dropped[0]))

    def _log_tail(self, sent: int) -> None:
        if not self._pending:
            return
        muted = max(0.0, self._muted_until - time.monotonic())
        _log(
            self._log, "info", "archive.pending", sent=sent, pending=len(self._pending),
            muted_seconds=round(muted, 1) if muted else 0,
            hint="积压的条目下一轮接着发；进程重启会丢队列",
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
