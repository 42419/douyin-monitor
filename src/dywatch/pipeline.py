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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .alerts import Deduplicator, priority_of, should_send
from .diff import diff
from .dtk import MonitorError, include_raw_for_round
from .models import (
    ArchiveItem,
    AuthorState,
    DiffConfig,
    Event,
    EventKind,
    PostState,
    REVIVED_VIA_HIDDEN_CHECK,
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

#: 数字对不上、但定向核验没能解释缺口时，连续几轮之后放弃追踪、直接接受当下的
#: 实际值——见 `_check_hidden_posts`。防止一个解释不了的缺口（比如置顶项被删导致
#: 的偏差，或者上游数据本身的一次性异常）让核验无限期重试下去。
MAX_UNRESOLVED_DRIFT_ROUNDS = 5


@dataclass(frozen=True, slots=True)
class HiddenCheckConfig:
    """隐藏作品核验（`HIDDEN_POST_CHECK_ENABLED`）需要的三样东西。

    `HIDDEN_POST_CHECK_ENABLED=false` 时 `run_author` 根本不接这个参数（`None`），
    主链路上就是一次 `is None` 判断——跟归档下载旁路（`ArchiveTrigger`）同一个套路。
    """

    #: 定向核验用的身份 UUID（DTK 控制台 Identities 页面可查）
    pinned_identity: str
    #: 定向请求走哪个 client——可以是复用主 client，也可以是配了 identity:manage
    #: 的独立 Key 建的 client（见 `PIN_DTK_API_KEY`），两种都行，pipeline 不关心
    pin_client: Any


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
    hidden_check: HiddenCheckConfig | None = None,
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

    if hidden_check is not None and error is None and page is not None:
        next_state, events = await _check_hidden_posts(
            prev=author,
            next_state=next_state,
            events=events,
            client=client,
            pacer=pacer,
            cfg=cfg,
            now=now,
            hidden_check=hidden_check,
            raw_included=raw_included,
            logger=logger,
        )

    event_ids = store.save_round(author.sec_user_id, next_state, events, now=now)

    # ---- 通知（落库之后） ---------------------------------------------------
    # 投递顺序按优先级，不按 diff 的输出顺序：`new_post` 必须第一个发出去。
    # 一轮里的事件共用同一条投递通道（渠道间隔 + 单渠道超时重试），排在它前面的每一条
    # 都可能把它推到几十秒之后，甚至因为撞上渠道限流而变成"失败的那一条"。
    # `(row_id, event)` 成对排序，投递结果才能正确回写到对应的事件行。
    deliveries: dict[int, Mapping[str, Any]] = {}
    for row_id, event in sorted(zip(event_ids, events), key=lambda pair: priority_of(pair[1].kind)):
        if not event.should_notify:
            continue
        allowed, key = should_send(event, dedup)
        if not allowed:
            _log(logger, "debug", "notify.suppressed", kind=event.kind.value, key=key)
            continue
        try:
            result = await _deliver(notifier, event)
        except Exception as exc:  # noqa: BLE001 - 见下：一条消息炸了不能连坐后面的
            # 渲染或投递里出了意料之外的错（畸形载荷、渠道客户端 bug）：记一条，
            # 放下这条继续发下一条。**通知循环是唯一不能因单条失败而中断的地方**——
            # 中断意味着排在后面的（尤其 new_post）连尝试的机会都没有。
            # 也把抑制窗口还回去，下轮还能再试。
            _log(logger, "warning", "notify.crashed", kind=event.kind.value,
                 error=f"{type(exc).__name__}: {exc}"[:160])
            dedup.release(key)
            continue
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


async def _check_hidden_posts(
    *,
    prev: AuthorState,
    next_state: AuthorState,
    events: tuple[Event, ...],
    client: Any,
    pacer: RequestPacer,
    cfg: DiffConfig,
    now: datetime,
    hidden_check: HiddenCheckConfig,
    raw_included: bool,
    logger: Any,
) -> tuple[AuthorState, tuple[Event, ...]]:
    """隐藏作品核验：游客身份有时看不到作者主页最新发布的作品（抖音的访客限制），
    这里用"发布总数对不对得上"作为触发信号，只在真的对不上时才用登录身份重新核实一遍。

    返回的是**这一轮最终应该使用的完整 `events`**（不是"额外追加的"）——如果核验
    推翻了这一轮刚产出的删除确认，需要把矛盾的部分先摘掉，调用方不用再关心这些。

    在四种情况下会真的去调用 `/api/v1/douyin/user`（不是每轮都调）：

    1. 账号第一次被记录（`INITIALIZED`）——还没有基准值，先建立一个
    2. 本轮有新作品（`NEW_POST`）——游客视角已经看到了，不需要核验，但要把这些
       新增算进基准值，不然基准值会跟着漂移，等下次真的对不上时算出错误的缺口
    3. 本轮有作品被确认消失（`POST_REMOVED` / `ALL_GONE`）——核对数字对不对得上
    4. 长期无更新的一次性兜底提醒触发（`STALE_NO_UPDATE`）——同样核对一次

    其余轮次（既没有新作品、也没有作品消失、也没有触发兜底）直接原样返回，不产生
    任何额外请求。

    **已知盲区**：如果一条新作品从发布起就一直被游客身份隐藏，而账号后续也没有
    任何作品被删除、也没有碰到长期无更新的阈值，这条作品会一直悬空，要等到以后
    某次不相关的删除/兜底事件才会被连带翻出来——`STALE_FALLBACK_DAYS` 把这条尾巴
    的上限锁在 `STALE_FALLBACK_DAYS` 天之内，但不保证更快。这是权衡过的结果：
    改成不看事件、纯定期轮询能缩短这个上限，但意味着每个开启了这个功能的账号，
    不管有没有发生任何变化，都要按固定周期消耗一次请求——参见 DESIGN.md §4.10。
    """
    initialized = any(e.kind is EventKind.INITIALIZED for e in events)
    removed_count = 0
    for event in events:
        if event.kind in (EventKind.POST_REMOVED, EventKind.ALL_GONE):
            removed_count += len(event.payload.get("removed") or [])
    new_count = sum(1 for e in events if e.kind is EventKind.NEW_POST)
    stale_triggered = any(e.kind is EventKind.STALE_NO_UPDATE for e in events)

    if not (initialized or new_count > 0 or removed_count > 0 or stale_triggered):
        return next_state, events

    try:
        await pacer.wait_for_turn()
        profile = await client.author_profile(prev.sec_user_id)
    except MonitorError as exc:
        # 核验层，不是主判定：读失败不该影响这一轮已经算好的结果——下次再有触发
        # 时会重新算一遍基准值差，这次的空手不会造成永久性的漂移
        _log(logger, "debug", "hidden_check.profile_unavailable",
             sec_user_id=prev.sec_user_id, code=exc.code)
        return next_state, events

    actual = profile.content_count
    if actual is None:
        return next_state, events

    if prev.baseline_content_count is None:
        # 还没有基准值可比（通常是这个账号第一次被记录），这次先把它立起来，
        # 不做比较——没有"上一次"就无从谈起"对不对得上"
        return next_state.with_updates(
            baseline_content_count=actual, baseline_content_count_at=now,
            content_count_drift_rounds=0,
        ), events

    expected = prev.baseline_content_count + new_count - removed_count

    if actual <= expected:
        # 对得上，或者比预期还少（比如别处也发生了这轮没被确认到的删除，
        # 数字只会更小，不是"隐藏"要处理的问题）——都直接信任这次查到的实际值
        next_state = next_state.with_updates(
            baseline_content_count=actual, baseline_content_count_at=now,
            content_count_drift_rounds=0,
        )
        return next_state, events

    # actual > expected：有缺口，尝试定向核验
    try:
        await pacer.wait_for_turn()
        page = await hidden_check.pin_client.author_posts(
            prev.sec_user_id, cfg.fetch_count,
            include_raw=raw_included, identity=hidden_check.pinned_identity,
        )
    except MonitorError as exc:
        _log(logger, "warning", "hidden_check.verify_failed",
             sec_user_id=prev.sec_user_id, code=exc.code, message=exc.message[:120])
        page = None

    recovered_events: tuple[Event, ...] = ()
    if page is not None:
        recovered_events, next_state = _merge_hidden_verification(next_state, page, now)

    if recovered_events:
        # 缺口被解释清楚了：基准值推进，未解决计数清零。核验刚找回的作品如果
        # 恰好也在这一轮刚产出的 POST_REMOVED/ALL_GONE 里（同一轮先确认删除、
        # 核验又把它找回来了），要把它从那条事件里摘掉——不然"作品消失"和
        # "其实还在"会同批发出两条自相矛盾的通知
        recovered_ids = {e.content_id for e in recovered_events if e.kind is EventKind.REVIVED}
        events = _reconcile_recovered_removals(events, recovered_ids)
        events = events + recovered_events
        _log(logger, "info", "hidden_check.recovered",
             sec_user_id=prev.sec_user_id, count=len(recovered_events),
             expected=expected, actual=actual)
        next_state = next_state.with_updates(
            baseline_content_count=actual, baseline_content_count_at=now,
            content_count_drift_rounds=0,
        )
        return next_state, events

    # 数字对不上，但这次没能解释清楚（核验请求失败，或者核验拿到的页面里也
    # 没有本地不认识的 id）——**不推进基准值**，留到下一次触发时继续比对同一个
    # 缺口，而不是悄悄把它吃掉当作"已经解决"。但也不能无限期重试：连续太多轮
    # 都解释不了，大概率是别的原因（比如置顶项被删导致的偏差、上游数据的一次性
    # 异常），继续追踪没有意义，达到上限后放弃、直接接受当下的实际值。
    drift_rounds = prev.content_count_drift_rounds + 1
    if drift_rounds >= MAX_UNRESOLVED_DRIFT_ROUNDS:
        _log(logger, "warning", "hidden_check.drift_gave_up",
             sec_user_id=prev.sec_user_id, expected=expected, actual=actual,
             rounds=drift_rounds)
        next_state = next_state.with_updates(
            baseline_content_count=actual, baseline_content_count_at=now,
            content_count_drift_rounds=0,
        )
    else:
        _log(logger, "debug", "hidden_check.mismatch_unresolved",
             sec_user_id=prev.sec_user_id, expected=expected, actual=actual,
             rounds=drift_rounds)
        next_state = next_state.with_updates(content_count_drift_rounds=drift_rounds)
    return next_state, events


def _reconcile_recovered_removals(
    events: tuple[Event, ...], recovered_ids: set[str]
) -> tuple[Event, ...]:
    """核验刚发现某些 id 其实没删，把它们从这一轮已经产出的 `POST_REMOVED` /
    `ALL_GONE` 事件里摘掉——不然会跟核验产出的 `REVIVED` 同批发出两条自相矛盾
    的通知（"作品消失" + "其实还在"）。

    摘完如果一条 removed 事件的作品列表变空了，整条事件跟着撤销：这一批"消失"
    全部被核验推翻了，没有任何真实消失可报。
    """
    if not recovered_ids:
        return events
    result: list[Event] = []
    for event in events:
        if event.kind not in (EventKind.POST_REMOVED, EventKind.ALL_GONE):
            result.append(event)
            continue
        removed = event.payload.get("removed") or []
        kept = [item for item in removed if item.get("content_id") not in recovered_ids]
        if not kept:
            continue  # 这一批消失全部被核验推翻，整条事件撤销
        if len(kept) != len(removed):
            event = Event(
                event.kind,
                sec_user_id=event.sec_user_id,
                nickname=event.nickname,
                content_id=event.content_id,
                payload={**event.payload, "removed": kept},
            )
        result.append(event)
    return tuple(result)


def _merge_hidden_verification(
    state: AuthorState, page: Any, now: datetime
) -> tuple[tuple[Event, ...], AuthorState]:
    """把定向核验拿到的页面，合并进（这一轮已经算完的）状态里。

    刻意不复用 `diff()`：`diff()` 每次调用都会推进 `runs` / `raw_refresh_round` /
    `last_seen_at` 这些"一轮只应该走一次"的计数器，同一轮里调两次会把它们算错。
    这里只关心"核验页面里有没有本地不认识的 id"，产出的事件形状照抄 `diff()` 里
    `new_ids` / `reappeared` 那两段——分辨"全新"还是"曾经确认删除、现在核验回来了"
    的逻辑完全一致，只是重新出现的那一类会额外打上 `source` 标记：
    `alerts.should_send` 认这个标记不吃 `REVIVED` 的 6 小时抑制窗口，
    `render` 认它换一套"疑似此前一直被隐藏"的文案。
    """
    known = state.known_ids
    tomb_ids = state.tombstone_ids
    current = {item.content_id: item for item in page.items}

    posts = {p.content_id: p for p in state.posts}
    tombstones = {t.content_id: t for t in state.tombstones}
    events: list[Event] = []
    found_new = False

    for content_id, item in current.items():
        if content_id in known:
            continue
        posts[content_id] = PostState(
            content_id=content_id,
            kind=item.kind,
            title=item.title,
            created_at=item.created_at,
            is_top=item.is_top,
            first_seen_at=now,
            last_seen_at=now,
        )
        if content_id in tomb_ids:
            tombstones.pop(content_id, None)
            events.append(
                Event(
                    EventKind.REVIVED,
                    sec_user_id=state.sec_user_id,
                    nickname=state.nickname,
                    content_id=content_id,
                    payload={
                        "title": item.title,
                        "kind": item.kind.value,
                        "web_url": item.web_url,
                        "source": REVIVED_VIA_HIDDEN_CHECK,
                    },
                )
            )
        else:
            found_new = True
            events.append(
                Event(
                    EventKind.NEW_POST,
                    sec_user_id=state.sec_user_id,
                    nickname=state.nickname,
                    content_id=content_id,
                    payload={"content": item, "gap_days": None, "source": REVIVED_VIA_HIDDEN_CHECK},
                )
            )

    if not events:
        return (), state

    return tuple(events), state.with_updates(
        posts=tuple(posts.values()),
        tombstones=tuple(tombstones.values()),
        last_update_at=now,
        last_new_video_at=now if found_new else state.last_new_video_at,
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
