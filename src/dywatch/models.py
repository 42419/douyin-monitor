"""值对象：全是 frozen dataclass，没有方法、没有 I/O。

三条来自 DTK v5 的契约在这里被固化：

1. **缺值是 `None`，不是 `0`。** 抖音的 `stats.play_count` 实测恒为 `null`
   （平台不返回真实播放数），把 `None` 写成 0 会在通知里显示一个平台从未说过的事实。
2. **`content_id` 是字符串。** 19 位数字超出 JavaScript 安全整数范围，
   任何一次 `int()` 往返都可能悄悄改掉最后几位。
3. **时间统一用带时区的 `datetime`。** 通知里要显示本地时间，而比较要跨时区安全。

`AuthorState` 是 `diff.py` 的输入与输出，也是 `state.py` 落库的形状；
它刻意做成"一整个账号的全部状态"，这样判定函数没有隐藏的第二处状态可读。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    """DTK 的 `ContentKind` 实测有三种：video / image_album / live。"""

    VIDEO = "video"
    IMAGE_ALBUM = "image_album"
    LIVE = "live"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: Any) -> "Kind":
        try:
            return cls(str(raw))
        except ValueError:
            return cls.UNKNOWN


class EventKind(StrEnum):
    """Everything `diff` can decide. The value doubles as the notify catalog key."""

    NEW_POST = "new_post"
    POST_REMOVED = "post_removed"
    REVIVED = "revived"
    TITLE_CHANGED = "title_changed"
    SCROLLED_OUT = "scrolled_out"
    TRIMMED = "trimmed"
    GAP_DETECTED = "gap_detected"
    NEVER_SEEN = "never_seen"
    ALL_GONE = "all_gone"
    ACCOUNT_FAILED = "account_failed"
    ACCOUNT_RECOVERED = "account_recovered"
    STALE_NO_UPDATE = "stale_no_update"
    UPSTREAM_DEGRADED = "upstream_degraded"
    INITIALIZED = "initialized"
    SELF_DEGRADED = "self_degraded"
    #: 核验（登录身份定向查证）发现这条作品**对未登录访客不可见**：它在登录视角里存在，
    #: 但访客列表从来不给它。与 `NEW_POST` 分开报，是因为这两件事的"用户该做什么"不同：
    #: 新作品什么都不用做，而对访客不可见意味着曝光被平台限制住了。
    HIDDEN_FROM_GUEST = "hidden_from_guest"


#: 通知类事件（会真的推送）。其余只落库 / 只记日志。
NOTIFY_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.NEW_POST,
        EventKind.POST_REMOVED,
        EventKind.ALL_GONE,
        EventKind.REVIVED,
        EventKind.TITLE_CHANGED,
        EventKind.GAP_DETECTED,
        EventKind.NEVER_SEEN,
        EventKind.ACCOUNT_FAILED,
        EventKind.ACCOUNT_RECOVERED,
        EventKind.STALE_NO_UPDATE,
        EventKind.UPSTREAM_DEGRADED,
        EventKind.SELF_DEGRADED,
    }
)

#: 静默事件（只改状态，不推送）。两类：
#:
#: - `scrolled_out` / `trimmed` / `initialized` 是"窗口挪动"的机械后果；
#: - `hidden_from_guest` 是**平台对访客的展示限制**，不是账号出了什么事、也不用用户做任何
#:   动作。报出来只会淹掉真正该看的（新作品、作品消失），所以只落库 + 面板可见
#:   （详情里能查到是哪几条、从什么时候起），不推送。
SILENT_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.SCROLLED_OUT,
        EventKind.TRIMMED,
        EventKind.INITIALIZED,
        EventKind.HIDDEN_FROM_GUEST,
    }
)


@dataclass(frozen=True, slots=True)
class Content:
    """One post, as this monitor needs it (a subset of DTK's normalized model)."""

    content_id: str
    kind: Kind = Kind.UNKNOWN
    title: str = ""
    description: str = ""
    web_url: str = ""
    created_at: datetime | None = None
    duration_ms: int | None = None
    is_top: bool = False
    is_deleted: bool = False
    is_private: bool = False
    cover_url: str | None = None
    image_count: int = 0
    digg_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    collect_count: int | None = None
    play_count: int | None = None
    tags: tuple[str, ...] = ()
    author_uid: str | None = None
    author_nickname: str | None = None

    @property
    def has_video(self) -> bool:
        return self.kind is Kind.VIDEO


@dataclass(frozen=True, slots=True)
class PostMetrics:
    """One post's engagement counters, as the author's page already carries them.

    DTK 在 `user/posts` 的**每一条**里就带全了这些数字（实测载荷见
    `tests/replay/fixtures/douyin_user_posts_real.json`），所以采它们不花任何额外的
    身份——以前这里只是把它们解析进 `Content` 然后丢掉。落库按小时聚合，见
    `state.record_*` 那一节。
    """

    content_id: str
    play_count: int | None = None
    digg_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    collect_count: int | None = None

    @property
    def is_empty(self) -> bool:
        """五个数一个都没有 —— 这样的行不写。

        跟 DTK 自己 `services/snapshots.py` 的取舍一致：写一行全是 NULL 的记录只增加噪音，
        而写 0 会在增长曲线里造出一个平台从未说过的悬崖。
        """
        return all(
            value is None
            for value in (
                self.play_count,
                self.digg_count,
                self.comment_count,
                self.share_count,
                self.collect_count,
            )
        )

    @classmethod
    def from_content(cls, content: Content) -> "PostMetrics":
        return cls(
            content_id=content.content_id,
            play_count=content.play_count,
            digg_count=content.digg_count,
            comment_count=content.comment_count,
            share_count=content.share_count,
            collect_count=content.collect_count,
        )


@dataclass(frozen=True, slots=True)
class Page:
    """A page of `user/posts` results."""

    items: tuple[Content, ...] = ()
    cursor: str | None = None
    has_more: bool = False
    #: 本轮是否带了 include_raw —— `is_top` 只有带了才是真值
    raw_included: bool = False
    task_id: str | None = None

    def ids(self) -> frozenset[str]:
        return frozenset(item.content_id for item in self.items)

    def metrics(self) -> tuple[PostMetrics, ...]:
        """本页每条作品的互动量 —— 空值行直接丢掉（见 `PostMetrics.is_empty`）。"""
        return tuple(
            metrics
            for metrics in (PostMetrics.from_content(item) for item in self.items)
            if metrics.content_id and not metrics.is_empty
        )

    def non_top(self) -> tuple[Content, ...]:
        """置顶项排除在外的那些——窗口与漏检判定只看它们。

        置顶作品的发布时间是任意的（实测该账号三条置顶分别发布于 2025-04、2024-01），
        混进来做时间比较毫无意义。
        """
        return tuple(item for item in self.items if not item.is_top)


@dataclass(frozen=True, slots=True)
class PostState:
    """What we remember about one post of one author."""

    content_id: str
    kind: Kind = Kind.UNKNOWN
    title: str = ""
    created_at: datetime | None = None
    is_top: bool = False
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    #: 连续几轮不在本页（旧项目的 pending_deletes 计数）
    absent_rounds: int = 0
    #: 计数开始时的置顶状态，决定确认阈值是 2 还是 3
    absent_is_top: bool = False
    #: 带标记期间**连续**在访客列表里见到的轮数。清标记要"分级确认"：访客视角本身会抖
    #: （这次看不到、下次看得到），见一次就清会让它对上"缺席确认删除"来回横跳——
    #: 线上表现为同一条作品每几分钟被重新核验一次。中间有一轮看不到就归零。
    hidden_seen_streak: int = 0
    #: 核验确认过"这条对未登录访客不可见"的时间（`None` = 没有这个已知情况）。
    #:
    #: **这个标记不是给人看的标签，而是缺席判定的口径**：在访客视角下，"真的被删了"
    #: 和"只是访客看不到"这两种情况的表现完全一样（列表里都没有），所以必须把核验的
    #: 结论记下来，否则每一轮都会把它重新判成"确认消失"、再被核验填回来，来回横跳。
    #: 非空时 `diff` **不计算它的缺席**；它重新出现在访客列表里就清回 `None`。
    hidden_from_guest_at: datetime | None = None

    def with_updates(self, **changes: Any) -> "PostState":
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class Tombstone:
    """A post that left the window. Keeps it from being re-announced if it comes back."""

    content_id: str
    removed_at: datetime
    reason: str = "confirmed"  # confirmed | scrolled_out | trimmed


@dataclass(frozen=True, slots=True)
class AuthorProfile:
    """`GET /api/v1/douyin/user` 的裁剪版——目前只用得到发布总数这一个字段。

    `content_count` 对应抖音自己的 `aweme_count`：账号级的发布总数统计，实测**不**
    受访客身份限制（跟"关注/粉丝/获赞"这几个数字同一类，游客态也能看到真实值）——
    这条是使用方（见隐藏作品核验那一节）亲自用登录态账号核对过的，不是猜的。
    """

    content_count: int | None = None


@dataclass(frozen=True, slots=True)
class AuthorState:
    """Everything `diff` knows about one monitored account."""

    sec_user_id: str
    nickname: str = ""
    initialized_at: datetime | None = None
    #: 是否成功见到过作品。0 表示 never_seen —— 决定空列表怎么解释，见 diff 的 ②
    ever_had_posts: bool = False
    consecutive_fails: int = 0
    last_error: str | None = None
    last_error_code: str | None = None
    fail_alerted: bool = False
    last_fail_alert_at: datetime | None = None
    #: "全部作品同时消失"的连续轮数
    all_gone_rounds: int = 0
    #: 连续空列表轮数
    empty_rounds: int = 0
    never_seen_alerted: bool = False
    #: 上轮**非置顶**项里最新的发布时间，漏检判定的基准
    newest_seen_created_at: datetime | None = None
    #: 距上次带 include_raw 过了几轮
    raw_refresh_round: int = 0
    last_new_video_at: datetime | None = None
    last_update_at: datetime | None = None
    stale_alerted: bool = False
    last_seen_at: datetime | None = None
    runs: int = 0
    posts: tuple[PostState, ...] = ()
    tombstones: tuple[Tombstone, ...] = ()
    #: 上一次确认过的 `content_count`（见隐藏作品核验）。`None` = 还没确认过。
    baseline_content_count: int | None = None
    baseline_content_count_at: datetime | None = None
    #: 连续几轮"数字对不上、但核验没能解释"——只在这种未解决状态下才递增，
    #: 解决了（或者干脆没有缺口）就清零。到达上限会放弃这次追踪、直接接受
    #: 当下的实际值，避免一个解释不了的缺口让核验无限重试下去。
    content_count_drift_rounds: int = 0

    @property
    def known_ids(self) -> frozenset[str]:
        return frozenset(p.content_id for p in self.posts)

    @property
    def known_top_ids(self) -> frozenset[str]:
        """库里已知是置顶的那些 id。

        存在的理由：置顶标志只有带了 `include_raw` 的轮次才是真值，其余轮次本页
        `is_top` 恒为 False，而抖音会把置顶项**额外**塞进返回里。凡是需要"分辨置顶"
        的地方（漏检判据、隐藏作品核验的窗口底）都得靠它兜住那几轮。
        """
        return frozenset(p.content_id for p in self.posts if p.is_top)

    @property
    def tombstone_ids(self) -> frozenset[str]:
        return frozenset(t.content_id for t in self.tombstones)

    def post(self, content_id: str) -> PostState | None:
        for p in self.posts:
            if p.content_id == content_id:
                return p
        return None

    def with_updates(self, **changes: Any) -> "AuthorState":
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ArchiveItem:
    """What the local archive says about one post (zero identity cost)."""

    content_id: str
    availability: str = "unknown"  # live | deleted | private | unknown
    stored: bool = False
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Event:
    """One decision, ready to be rendered and delivered."""

    kind: EventKind
    sec_user_id: str
    nickname: str = ""
    content_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def should_notify(self) -> bool:
        return self.kind in NOTIFY_KINDS


@dataclass(frozen=True, slots=True)
class RoundResult:
    """One author, one round."""

    sec_user_id: str
    nickname: str
    status: str = "ok"  # ok | init | fail | skipped
    new_count: int = 0
    deleted_count: int = 0
    title_changed: int = 0
    error_code: str | None = None
    duration_ms: int = 0
    events: tuple[Event, ...] = ()


@dataclass(frozen=True, slots=True)
class DiffConfig:
    """The判定 knobs, lifted out of `Settings` so `diff` stays pure and testable."""

    fetch_count: int = 15
    delete_rounds: int = 2
    delete_rounds_top: int = 3
    delete_rounds_all: int = 3
    known_ids_max: int = 50
    removed_max: int = 200
    removed_ttl_days: int = 7
    max_consecutive_fails: int = 5
    fail_cooldown: int = 300
    empty_rounds_alert: int = 3
    stale_fallback_days: int = 14
    #: 置顶标志的获取策略与"最多隔多少轮带一次 raw"
    include_raw: str = "auto"
    raw_refresh_rounds: int = 20
    #: 隐藏作品核验的**低频保底**间隔（秒）：不管这一轮有没有事件，基准值超过它就重新
    #: 核对一次发布总数。0 = 关闭保底（只靠事件触发）。
    #:
    #: 这条保底不是优化，是**必需**：有两类变化在访客视角完全不留痕迹，事件驱动的触发
    #: 永远等不到它们——① 新作品从发布起就对访客不可见；② 已知"对访客不可见"的作品
    #: 被作者删掉（它本来就不在访客列表里）。没有它，这两件事都只能等 `STALE_FALLBACK_DAYS`
    #: 那 14 天的兜底（甚至更久）。
    hidden_check_interval_seconds: int = 1800

    @classmethod
    def from_settings(cls, settings: Any) -> "DiffConfig":
        return cls(
            fetch_count=int(settings["FETCH_COUNT"]),
            delete_rounds=int(settings["DELETE_CONFIRM_ROUNDS"]),
            delete_rounds_top=int(settings["DELETE_CONFIRM_ROUNDS_TOP"]),
            delete_rounds_all=int(settings["DELETE_CONFIRM_ROUNDS_ALL"]),
            known_ids_max=int(settings["KNOWN_IDS_MAX"]),
            removed_max=int(settings["REMOVED_MAX"]),
            removed_ttl_days=int(settings["REMOVED_TTL_DAYS"]),
            max_consecutive_fails=int(settings["MAX_CONSECUTIVE_FAILS"]),
            fail_cooldown=int(settings["FAIL_COOLDOWN"]),
            empty_rounds_alert=int(settings["EMPTY_ROUNDS_ALERT"]),
            stale_fallback_days=int(settings["STALE_FALLBACK_DAYS"]),
            include_raw=str(settings["INCLUDE_RAW"]),
            raw_refresh_rounds=int(settings["RAW_REFRESH_ROUNDS"]),
            hidden_check_interval_seconds=int(settings["HIDDEN_CHECK_INTERVAL_MINUTES"])
            * 60,
        )
