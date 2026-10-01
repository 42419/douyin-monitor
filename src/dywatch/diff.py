"""判定：`(上一轮状态, 本页) -> (事件, 新状态)`。**纯函数，零 I/O**。

这是整个工具最容易出错的一块，所以它被做成不依赖网络、不依赖时钟、不依赖数据库的形状：
`now` 由调用方传进来，测试里可以直接推进时间造出任意轮次序列。

规则全部沿用 `douyin-monitor-enhance` 已经跑过数月的逻辑，只在三处按 v5 的实测事实修正：

1. **漏检判据**。旧项目用 `len(new_ids) >= FETCH_COUNT`，但实测抖音返回的是
   `count + 本页置顶数`，命中"上限"不等于被截断，那条判据会误报。
   改为时间连续性：本页**非置顶**项中最旧的发布时间 > 上轮非置顶项中最新的发布时间
   → 两次观测之间出现了没见过的区间。
2. **空列表要分两种**。v5 实测：形态合法但不存在的 `sec_user_id` 返回 200 + `items:[]`，
   与"作者删光作品"无法区分。所以状态里区分 `ever_had_posts`：
   从未有过作品 → `never_seen`（提示核实 ID）；曾有过 → 走旧项目的"全部消失"三级确认。
3. **置顶标志只在带了 raw 的轮次更新**。不带 raw 时 `is_top` 恒为 `false`，
   若照抄就会把已知的置顶状态抹掉。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    AuthorState,
    Content,
    DiffConfig,
    Event,
    EventKind,
    Page,
    PostState,
    Tombstone,
)
from .dtk import MonitorError

#: tombstone 的三种来源。区分它们是为了让日志能回答"它为什么不在列表里了"。
REASON_CONFIRMED = "confirmed"
REASON_SCROLLED_OUT = "scrolled_out"
REASON_TRIMMED = "trimmed"


def diff(
    prev: AuthorState,
    *,
    now: datetime,
    cfg: DiffConfig,
    page: Page | None = None,
    error: MonitorError | None = None,
    archive: Mapping[str, Any] | None = None,
) -> tuple[tuple[Event, ...], AuthorState]:
    """One round for one author.

    Exactly one of `page` / `error` must be given. `archive` is optional and lets a
    locally-known "deleted" verdict save one confirmation round.
    """
    events: list[Event] = []

    if error is not None:
        return _on_failure(prev, error, now=now, cfg=cfg, events=events)

    if page is None:
        raise ValueError("diff 需要 page 或 error 之一")

    # 一次成功就顺手把"失败"状态收干净——旧项目也是在这里发恢复通知的
    if prev.consecutive_fails and prev.fail_alerted and prev.consecutive_fails >= cfg.max_consecutive_fails:
        events.append(
            Event(
                EventKind.ACCOUNT_RECOVERED,
                sec_user_id=prev.sec_user_id,
                nickname=prev.nickname,
                payload={"fails": prev.consecutive_fails},
            )
        )
        prev = prev.with_updates(fail_alerted=False)
    prev = prev.with_updates(consecutive_fails=0, last_error=None, last_error_code=None)

    items = page.items
    is_empty = not items

    empty_rounds = prev.empty_rounds + 1 if is_empty else 0

    # ---- never_seen：从未成功见到过作品，且一直返回空 ----------------------
    if is_empty and not prev.ever_had_posts:
        alerted = prev.never_seen_alerted
        if empty_rounds >= cfg.empty_rounds_alert and not alerted:
            events.append(
                Event(
                    EventKind.NEVER_SEEN,
                    sec_user_id=prev.sec_user_id,
                    nickname=prev.nickname,
                    payload={"rounds": empty_rounds},
                )
            )
            alerted = True
        return tuple(events), prev.with_updates(
            empty_rounds=empty_rounds,
            never_seen_alerted=alerted,
            initialized_at=prev.initialized_at or now,
            last_seen_at=now,
            runs=prev.runs + 1,
            raw_refresh_round=0 if page.raw_included else prev.raw_refresh_round + 1,
        )

    # ---- 首次记录：本页有内容，而我们从没见过这个账号 ----------------------
    if not prev.ever_had_posts and not is_empty:
        posts = tuple(
            PostState(
                content_id=item.content_id,
                kind=item.kind,
                title=item.title,
                created_at=item.created_at,
                is_top=item.is_top,
                first_seen_at=now,
                last_seen_at=now,
            )
            for item in items
        )
        newest = _newest_non_top(items)
        events.append(
            Event(
                EventKind.INITIALIZED,
                sec_user_id=prev.sec_user_id,
                nickname=prev.nickname,
                payload={"count": len(posts)},
            )
        )
        return tuple(events), prev.with_updates(
            initialized_at=prev.initialized_at or now,
            ever_had_posts=True,
            empty_rounds=0,
            all_gone_rounds=0,
            newest_seen_created_at=newest,
            last_new_video_at=now,
            last_update_at=now,
            last_seen_at=now,
            runs=prev.runs + 1,
            posts=posts,
            raw_refresh_round=0 if page.raw_included else prev.raw_refresh_round + 1,
        )

    current = {item.content_id: item for item in items}
    current_ids = set(current)
    known_ids = set(prev.known_ids)
    tomb_ids = set(prev.tombstone_ids)

    # ---- 回归：曾经消失、现在又出现在列表里（窗口回移）→ 静默恢复 ----------
    reappeared = (current_ids - known_ids) & tomb_ids

    posts: dict[str, PostState] = {p.content_id: p for p in prev.posts}
    tombstones: dict[str, Tombstone] = {t.content_id: t for t in prev.tombstones}

    for content_id in reappeared:
        tombstones.pop(content_id, None)
        item = current[content_id]
        posts[content_id] = PostState(
            content_id=content_id,
            kind=item.kind,
            title=item.title,
            created_at=item.created_at,
            is_top=item.is_top,
            first_seen_at=now,
            last_seen_at=now,
        )
        events.append(
            Event(
                EventKind.REVIVED,
                sec_user_id=prev.sec_user_id,
                nickname=prev.nickname,
                content_id=content_id,
                # 带上 kind 与 web_url：这两条现在会推送，通知里给得出类型和链接
                payload={"title": item.title, "kind": item.kind.value, "web_url": item.web_url},
            )
        )

    new_ids = current_ids - known_ids - reappeared
    disappeared_ids = known_ids - current_ids

    # ---- 标题/置顶同步（只对本页仍出现的作品） -----------------------------
    title_changed = 0
    for content_id in current_ids & known_ids:
        item = current[content_id]
        entry = posts[content_id]
        changes: dict[str, Any] = {"last_seen_at": now, "absent_rounds": 0}
        if entry.hidden_from_guest_at is not None:
            # 又能在访客列表里看到它了。**但清标记也要分级确认**：访客视角本身就会抖
            # （这次看不到、下次看得到），见一次就清会跟下面的"缺席确认删除"来回横跳——
            # 线上表现为同一条作品每几分钟被重新核验一次（标记清除 → 缺席 2 轮 → 确认删除
            # → 总数出现缺口 → 定向核验 → 重新标记，闭环）。要连续 `delete_rounds` 轮都
            # 看得到才认定"它真的又能被访客看到了"；中间有一轮看不到就归零（见下面的缺席段）。
            # 刻意不发事件：我们**从来没有报过它消失**，所以也没有"回归"可报。
            streak = entry.hidden_seen_streak + 1
            if streak >= cfg.delete_rounds:
                changes["hidden_from_guest_at"] = None
                changes["hidden_seen_streak"] = 0
            else:
                changes["hidden_seen_streak"] = streak
        if item.title != entry.title:
            title_changed += 1
            changes["title"] = item.title
            events.append(
                Event(
                    EventKind.TITLE_CHANGED,
                    sec_user_id=prev.sec_user_id,
                    nickname=prev.nickname,
                    content_id=content_id,
                    payload={
                        "old": entry.title,
                        "new": item.title,
                        "kind": item.kind.value,
                        "web_url": item.web_url,
                    },
                )
            )
        # 只有带了 raw 才相信 is_top，否则保留库里的值（见模块头注释第 3 条）
        if page.raw_included and item.is_top != entry.is_top:
            changes["is_top"] = item.is_top
        if entry.created_at is None and item.created_at is not None:
            changes["created_at"] = item.created_at
        posts[content_id] = entry.with_updates(**changes)

    for content_id in new_ids:
        item = current[content_id]
        posts[content_id] = PostState(
            content_id=content_id,
            kind=item.kind,
            title=item.title,
            created_at=item.created_at,
            is_top=item.is_top,
            first_seen_at=now,
            last_seen_at=now,
        )

    # "全部作品都不见了"只对**访客本来能看到**的作品才有意义：如果已知作品全部带
    # `hidden_from_guest_at` 标记，访客列表为空是预期内的常态，不是"删光"。不这么判的话，
    # 那个账号每一轮都会满足这个条件，`all_gone_rounds` 会一直往上涨（虽然此刻没有可确认
    # 的条目、发不出事件，但等真有作品进入确认流程时，它会绕过"全体消失等 N 轮"的等待）。
    all_gone = bool(prev.posts) and not current_ids and (
        any(p.hidden_from_guest_at is None for p in prev.posts)
    )
    all_gone_rounds = prev.all_gone_rounds + 1 if all_gone else 0

    # ---- 删除判定（挤出预算 + 分级确认） ----------------------------------
    confirmed: list[str] = []
    scrolled_out: list[str] = []

    budget = len(new_ids)  # 被新作品挤出窗口的条数不会超过本轮新增数
    ordered = sorted(
        disappeared_ids,
        key=lambda cid: (posts.get(cid).created_at if posts.get(cid) and posts[cid].created_at else _EPOCH),
    )

    for content_id in ordered:
        entry = posts.get(content_id)
        if entry is None:  # pragma: no cover - 防御：状态不一致时跳过
            continue
        if entry.hidden_from_guest_at is not None:
            # 核验确认过"这条对访客不可见"：它**缺席访客列表是预期内的**，不是可疑信号。
            # 既不计缺席、也不算被挤出窗口、更不判消失——否则每轮都会把它确认成
            # "已消失"，紧接着又被核验从登录视角填回来，两个视角来回横跳（曾经的真实故障）。
            # 标记要连续 `delete_rounds` 轮都看得到才清（见上面的标题同步段）：所以这一轮
            # 看不到就把"连续可见"归零——否则"看得见、看不到"交替的那种抖动会把计数慢慢
            # 攒够，标记被清掉，循环又回来了。
            if entry.hidden_seen_streak:
                posts[content_id] = entry.with_updates(hidden_seen_streak=0)
            continue
        is_top = entry.is_top

        if not is_top and budget > 0 and not all_gone and entry.absent_rounds == 0:
            # 挤出预算只发给**这一轮才消失**的作品。已经在确认计数里的那些
            # （`absent_rounds > 0`）在上一轮就已经在窗口之外了，**这一轮的新作品
            # 解释不了它们**——再拿预算去销案，等于把一个真删除静默埋掉：它既不会
            # 产出 `post_removed`，将来若因窗口回移重新出现，还会多一条假"作品回归"。
            budget -= 1
            scrolled_out.append(content_id)
            continue

        if all_gone:
            # 整批按 all_gone_rounds 统一确认，忽略各作品此前单独攒的进度，
            # 避免同一次"删光"被拆成多轮通知
            required = cfg.delete_rounds_all
            rounds = all_gone_rounds
        else:
            required = cfg.delete_rounds_top if is_top else cfg.delete_rounds
            rounds = entry.absent_rounds + 1

        if archive is not None:
            # 本地归档已判定为 deleted 时少等一轮交叉验证（零身份成本，值得用）
            if getattr(archive.get(content_id), "availability", None) == "deleted":
                required = max(1, required - 1)

        if rounds >= required:
            confirmed.append(content_id)
        else:
            # 尚未达到确认阈值：只累积计数，不产生任何事件（旧项目的"待后续确认"）
            posts[content_id] = entry.with_updates(
                absent_rounds=rounds,
                absent_is_top=entry.absent_is_top or is_top,
            )

    for content_id in scrolled_out:
        posts.pop(content_id, None)
        tombstones[content_id] = Tombstone(content_id=content_id, removed_at=now, reason=REASON_SCROLLED_OUT)
        events.append(
            Event(
                EventKind.SCROLLED_OUT,
                sec_user_id=prev.sec_user_id,
                nickname=prev.nickname,
                content_id=content_id,
            )
        )

    if confirmed:
        removed_payload = [
            {
                "content_id": cid,
                "title": (posts.get(cid).title if posts.get(cid) else ""),
                "created_at": (
                    posts[cid].created_at.isoformat() if posts.get(cid) and posts[cid].created_at else None
                ),
                "is_top": bool(posts.get(cid) and posts[cid].is_top),
            }
            for cid in confirmed
        ]
        events.append(
            Event(
                EventKind.ALL_GONE if all_gone else EventKind.POST_REMOVED,
                sec_user_id=prev.sec_user_id,
                nickname=prev.nickname,
                payload={"removed": removed_payload, "all_gone": all_gone},
            )
        )
        for content_id in confirmed:
            posts.pop(content_id, None)
            tombstones[content_id] = Tombstone(
                content_id=content_id, removed_at=now, reason=REASON_CONFIRMED
            )

    new_ids_ordered = sorted(
        new_ids,
        key=lambda cid: (current[cid].created_at or _EPOCH),
    )

    # ---- 漏检判定（时间连续性，非置顶项之间） ------------------------------
    #
    # 判据本身是"本页最旧的非置顶作品，比上轮最新还新"。它成立的前提是**真能分辨置顶项**，
    # 而 `INCLUDE_RAW=auto`（默认）只有少数几轮带 raw：其余轮次 `is_top` 全是 False，
    # 抖音额外塞进返回里的那几个置顶项（发布时间可能是几年前）就会混进"非置顶"里，
    # 把 `oldest_now` 拉到远古，判据于是**永不成立**——而且同一轮还会把
    # `newest_seen_created_at` 推到最新，后面带 raw 的那一轮也补不回来（永久漏判）。
    #
    # 所以这里用"库里的置顶状态"当补丁：库里已知是置顶的那些，不论本轮 raw 有没有带，
    # 一律排除在时间比较之外。库里的信息可能不全（新置顶项在首次带 raw 之前不知道），
    # 但**不会更差**——它只让"已知置顶"更准确，而不会把非置顶误排除。
    pinned_known = {pid for pid, entry in posts.items() if entry.is_top}
    oldest_now = _oldest_non_top(items, pinned=pinned_known)
    newest_now = _newest_non_top(items, pinned=pinned_known)
    if (
        prev.newest_seen_created_at is not None
        and oldest_now is not None
        and oldest_now > prev.newest_seen_created_at
    ):
        events.append(
            Event(
                EventKind.GAP_DETECTED,
                sec_user_id=prev.sec_user_id,
                nickname=prev.nickname,
                payload={
                    "oldest_in_page": oldest_now.isoformat(),
                    "previous_newest": prev.newest_seen_created_at.isoformat(),
                    "fetch_count": cfg.fetch_count,
                },
            )
        )
    newest_seen = _max_dt(newest_now, prev.newest_seen_created_at)

    # ---- 新作品通知 --------------------------------------------------------
    if new_ids_ordered:
        gap_days = _gap_days(prev, current, new_ids_ordered, now)
        for content_id in new_ids_ordered:
            item = current[content_id]
            events.append(
                Event(
                    EventKind.NEW_POST,
                    sec_user_id=prev.sec_user_id,
                    nickname=prev.nickname,
                    content_id=content_id,
                    payload={"content": item, "gap_days": gap_days},
                )
            )

    content_changed = bool(new_ids_ordered) or bool(confirmed) or bool(scrolled_out) or title_changed > 0

    # ---- 裁剪已知列表：优先淘汰最旧的非置顶 --------------------------------
    if len(posts) > cfg.known_ids_max:
        overflow = len(posts) - cfg.known_ids_max
        candidates = sorted(
            (p for p in posts.values() if not p.is_top),
            key=lambda p: (p.created_at or _EPOCH),
        )
        for entry in candidates[:overflow]:
            posts.pop(entry.content_id, None)
            tombstones[entry.content_id] = Tombstone(
                content_id=entry.content_id, removed_at=now, reason=REASON_TRIMMED
            )
            events.append(
                Event(
                    EventKind.TRIMMED,
                    sec_user_id=prev.sec_user_id,
                    nickname=prev.nickname,
                    content_id=entry.content_id,
                )
            )

    # ---- tombstone 回收（TTL + 条数上限） ----------------------------------
    cutoff = now - timedelta(days=cfg.removed_ttl_days)
    for content_id in [cid for cid, t in tombstones.items() if t.removed_at < cutoff]:
        tombstones.pop(content_id, None)
    if len(tombstones) > cfg.removed_max:
        drop = len(tombstones) - cfg.removed_max
        oldest = sorted(tombstones.values(), key=lambda t: t.removed_at)[:drop]
        for entry in oldest:
            tombstones.pop(entry.content_id, None)

    # ---- 长期无更新兜底（一次性） ------------------------------------------
    stale_alerted = prev.stale_alerted
    if new_ids_ordered:
        stale_alerted = False  # 又发了作品，重新计时
    else:
        since = prev.last_update_at or prev.initialized_at
        if (
            since is not None
            and prev.ever_had_posts  # never_seen 的账号由 NEVER_SEEN 告警负责，这里不重复
            and not stale_alerted
            and not prev.fail_alerted
            and (now - since) >= timedelta(days=cfg.stale_fallback_days)
        ):
            events.append(
                Event(
                    EventKind.STALE_NO_UPDATE,
                    sec_user_id=prev.sec_user_id,
                    nickname=prev.nickname,
                    payload={"days": int((now - since).total_seconds() // 86400)},
                )
            )
            stale_alerted = True

    next_state = prev.with_updates(
        ever_had_posts=True,
        empty_rounds=empty_rounds,
        all_gone_rounds=all_gone_rounds,
        newest_seen_created_at=newest_seen,
        raw_refresh_round=0 if page.raw_included else prev.raw_refresh_round + 1,
        last_seen_at=now,
        runs=prev.runs + 1,
        last_update_at=now if content_changed else prev.last_update_at,
        last_new_video_at=now if new_ids_ordered else prev.last_new_video_at,
        stale_alerted=stale_alerted,
        posts=tuple(sorted(posts.values(), key=lambda p: p.content_id)),
        tombstones=tuple(tombstones.values()),
    )
    return tuple(events), next_state


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _on_failure(
    prev: AuthorState,
    error: MonitorError,
    *,
    now: datetime,
    cfg: DiffConfig,
    events: list[Event],
) -> tuple[tuple[Event, ...], AuthorState]:
    """A failed round changes counters and nothing else.

    判定状态机不因为一次抓取失败而前进——旧项目同样是这个选择，理由是不这样做的话
    一次网络抖动就会把"疑似删除"的计数推一格，把确认建立在没看到数据的基础上。
    """
    fails = prev.consecutive_fails + 1
    alerted = prev.fail_alerted
    last_alert = prev.last_fail_alert_at

    if fails >= cfg.max_consecutive_fails and not error.is_gate:
        cooldown_ok = (
            last_alert is None or (now - last_alert).total_seconds() >= cfg.fail_cooldown
        )
        if cooldown_ok:
            events.append(
                Event(
                    EventKind.ACCOUNT_FAILED,
                    sec_user_id=prev.sec_user_id,
                    nickname=prev.nickname,
                    payload={
                        "fails": fails,
                        "code": error.code,
                        "message": error.message,
                        "config": error.is_config,
                    },
                )
            )
            last_alert = now
            alerted = True

    return tuple(events), prev.with_updates(
        consecutive_fails=fails,
        last_error=error.message or error.code,
        last_error_code=error.code,
        fail_alerted=alerted,
        last_fail_alert_at=last_alert,
        runs=prev.runs + 1,
        last_seen_at=now,
    )


def _max_dt(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _gap_days(
    prev: AuthorState,
    current: Mapping[str, Content],
    new_ids: Sequence[str],
    now: datetime,
) -> int | None:
    """距上一条作品发布多久（天）。

    **用发布时间算，而不是用我们上次看到它的时间算。** 后者在监控停机一段时间之后
    会给出一个偏小的数字（"距上次发布 1 天"，而实际隔了 10 天），而通知里写的是
    "距上次发布 N 天"——既然写的是发布，就该按发布算。

    只有拿不到发布时间（全新账号、或上游没给 `created_at`）时才退回观测时间。
    """
    newest = max(
        (current[cid].created_at for cid in new_ids if current[cid].created_at is not None),
        default=None,
    )
    if newest is not None and prev.newest_seen_created_at is not None:
        delta = newest - prev.newest_seen_created_at
        return max(0, int(delta.total_seconds() // 86400))

    baseline = prev.last_new_video_at or prev.last_update_at or prev.initialized_at
    if baseline is None:
        return None
    return max(0, int((now - baseline).total_seconds() // 86400))


def _non_top_times(
    items: Sequence[Content], *, pinned: Iterable[str] = frozenset()
) -> list[datetime]:
    """本页"非置顶"项的发布时间。

    `pinned` 是"库里已知的置顶 id"：本轮没带 raw 时 `item.is_top` 恒为 False，
    只有靠它才能把抖音额外塞进来的置顶项排除掉（见 `diff` 里漏检那一段的注释）。
    """
    return [
        c.created_at
        for c in items
        if not c.is_top and c.content_id not in pinned and c.created_at is not None
    ]


def _newest_non_top(
    items: Sequence[Content], *, pinned: Iterable[str] = frozenset()
) -> datetime | None:
    times = _non_top_times(items, pinned=pinned)
    return max(times) if times else None


def _oldest_non_top(
    items: Sequence[Content], *, pinned: Iterable[str] = frozenset()
) -> datetime | None:
    times = _non_top_times(items, pinned=pinned)
    return min(times) if times else None


def gap_suspect(prev: AuthorState, page: Page) -> bool:
    """Exposed for tests and for the startup doctor's sanity check."""
    pinned = prev.known_top_ids
    oldest = _oldest_non_top(page.items, pinned=pinned)
    return bool(
        prev.newest_seen_created_at is not None
        and oldest is not None
        and oldest > prev.newest_seen_created_at
    )


__all__ = ["diff", "gap_suspect", "REASON_CONFIRMED", "REASON_SCROLLED_OUT", "REASON_TRIMMED"]
