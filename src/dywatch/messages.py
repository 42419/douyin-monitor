"""文案集中处：逻辑里不出现字符串拼接。

第一版只有中文；要加英文时改这一个文件即可，判定与投递代码一行都不用动
（DTK 的做法是同一个 catalogue 服务控制台与通知，这里只服务通知，但原则一样）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .models import Content, EventKind, Kind, PostState

# --- 标题 -------------------------------------------------------------------
T_NEW_POST = "【新作品】{nickname} 发布了新{kind_label}"
T_POST_REMOVED = "【作品消失】{nickname} 有 {count} 条作品已确认消失"
T_ALL_GONE = "【作品全部消失】{nickname} 的全部作品同时不见了"
T_GAP = "【可能漏检】{nickname} 的更新节奏超过单页窗口"
T_NEVER_SEEN = "【账号始终无作品】{nickname} 需要核实 ID"
T_ACCOUNT_FAILED = "【监控异常】{nickname} 连续失败 {fails} 次"
T_ACCOUNT_RECOVERED = "【已恢复】{nickname} 的监控恢复正常"
T_STALE = "【长期无更新】{nickname} 已 {days} 天没有新作品"
T_UPSTREAM = "【上游异常】DTK 实例需要处理"
T_SELF = "【自身降级】dywatch 已暂停推送"

KIND_VIDEO = "视频"
KIND_ALBUM = "图文"
KIND_LIVE = "直播回放"
KIND_UNKNOWN = "作品"

# --- 条目 -------------------------------------------------------------------
ROW_TITLE = "标题"
ROW_TYPE = "类型"
ROW_PUBLISHED = "发布"
ROW_DURATION = "时长"
ROW_STATS = "数据"
ROW_TAGS = "话题"
ROW_COVER = "封面"
ROW_LINK = "链接"
ROW_AUTHOR = "作者"
GAP_NOTE = "距上次发布 {gap}"
STATS_SEP = " · "

NOTE_ALL_GONE = (
    "\n> 注意：该账号的全部作品在同一时间段内消失。如果账号实际仍有作品，"
    "可能是上游返回了空列表（接口异常/风控）或作者把作品设为私密，请核实后再处理。"
)
NOTE_NEVER_SEEN = (
    "\n> 该账号从未返回过任何作品。最常见的原因是 **sec_user_id 写错了**"
    "（形态合法但不存在的 ID 会返回 200 + 空列表，上游不会报错）；"
    "也可能是账号确实没有作品、或作品全部设为私密。"
)
NOTE_GAP = (
    "\n> 两次观测之间出现了没见过的作品区间：该账号单次发布数超过了一页窗口，"
    "超出部分不会被采集到。建议调大 FETCH_COUNT（当前 {fetch_count}，上限 50）。"
)
NOTE_CONFIG = "\n> 这是配置问题而不是网络问题：{code} —— 修好之前每个账号都会一直失败。"
NOTE_GATE = "\n> 上游整体不可用，本轮已整体跳过，这与某个账号无关。"


def md_escape(text: str | None) -> str:
    """Escape the characters that would break a Markdown message or @-mention someone."""
    if not text:
        return ""
    out = text.replace("\\", "\\\\")
    for char in ("*", "_", "`", "[", "]", "(", ")", "~", ">", "#", "+", "-", "!", "|"):
        out = out.replace(char, "\\" + char)
    return out.replace("@", "@\u200b")


def kind_label(kind: Kind, image_count: int = 0) -> str:
    if kind is Kind.VIDEO:
        return KIND_VIDEO
    if kind is Kind.IMAGE_ALBUM:
        return f"{KIND_ALBUM}（{image_count} 张图）" if image_count else KIND_ALBUM
    if kind is Kind.LIVE:
        return KIND_LIVE
    return KIND_UNKNOWN


def fmt_time(value: datetime | None, *, local: bool = True) -> str:
    if value is None:
        return "未知"
    stamp = value.astimezone() if local and value.tzinfo else value
    return stamp.strftime("%Y-%m-%d %H:%M")


def fmt_gap(value: datetime | None, now: datetime | None = None) -> str:
    """距上次发布多久，用于"3 天 4 小时"这种展示。"""
    if value is None:
        return "未知"
    reference = now or datetime.now(timezone.utc)
    seconds = max(0, int((reference - value).total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def fmt_duration(ms: int | None) -> str | None:
    if not ms or ms <= 0:
        return None
    total = int(ms // 1000)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def fmt_count(value: int | None) -> str | None:
    """1.2万 / 3.4亿 / 892 —— 缺值返回 None，**绝不返回 0**。

    只用"万 / 亿"两级：中文场景下 `k` 不是阅读习惯，而 9999 显示成 "10k"
    既丢精度又自相矛盾（一个四位数被显示成五位数）。
    """
    if value is None:
        return None
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿".replace(".0亿", "亿")
    if value >= 10_000:
        return f"{value / 10_000:.1f}万".replace(".0万", "万")
    return str(value)


def fmt_stats(content: Content) -> str | None:
    """点赞/评论/收藏/分享。播放数不列：抖音的 `play_count` 实测恒为 null。"""
    pairs = (
        ("点赞", content.digg_count),
        ("评论", content.comment_count),
        ("收藏", content.collect_count),
        ("分享", content.share_count),
    )
    parts = [f"{label} {fmt_count(value)}" for label, value in pairs if value is not None]
    return STATS_SEP.join(parts) if parts else None


def fmt_tags(tags: tuple[str, ...], limit: int = 8) -> str | None:
    if not tags:
        return None
    shown = [f"#{md_escape(tag)}" for tag in tags[:limit]]
    if len(tags) > limit:
        shown.append(f"…(+{len(tags) - limit})")
    return " ".join(shown)


# --- 更新频率 ---------------------------------------------------------------
# 分级沿用旧项目：排除置顶后按相邻发布时间的间隔均值分类。阈值是实测调出来的，
# 不跟着感觉改——面板上"周更"这三个字要和通知里的口径一致。
FREQ_LEVELS: tuple[tuple[float, str], ...] = (
    (1.5, "日更"),
    (4.0, "隔天更新"),
    (10.0, "周更"),
    (20.0, "半月更"),
    (45.0, "月更"),
)
FREQ_LEVEL_LAST = "更新较少"


def hours_since(value: datetime | None, now: datetime | None = None) -> int | None:
    """距今多少小时（向下取整）。`None` 原样返回 `None`——"不知道"不是 0。"""
    if value is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return max(0, int((reference - value).total_seconds() // 3600))


def frequency_stats(
    posts: Iterable[PostState], *, exclude_top: bool = True
) -> tuple[str, float, int] | None:
    """更新频率 → `(分级文案, 平均间隔天数, 用到的间隔数)`；样本不足时 `None`。

    置顶作品的发布时间是任意的（实测一个账号的三条置顶分别发布于 2025-04 与 2024-01），
    混进来算间隔毫无意义，所以默认排除。快照与面板详情共用这一个实现。
    """
    times = sorted(
        post.created_at
        for post in posts
        if post.created_at is not None and (not exclude_top or not post.is_top)
    )
    if len(times) < 2:
        return None
    gaps = [
        (later - earlier).total_seconds()
        for earlier, later in zip(times, times[1:])
        if later > earlier
    ]
    if not gaps:
        return None
    avg_days = (sum(gaps) / len(gaps)) / 86400
    for limit, label in FREQ_LEVELS:
        if avg_days <= limit:
            return label, avg_days, len(gaps)
    return FREQ_LEVEL_LAST, avg_days, len(gaps)


def freq_hint(stats: tuple[str, float, int] | None) -> str:
    """频率的悬停提示（面板列表与详情弹窗共用）。"""
    if stats is None:
        return ""
    _, avg_days, gaps = stats
    return f"基于最近 {gaps + 1} 条非置顶作品，平均 {avg_days:.1f} 天/条"


# --- 事件与消失原因（面板与事后审计读同一份文案） -----------------------------
EVENT_LABELS: dict[EventKind, str] = {
    EventKind.NEW_POST: "新作品",
    EventKind.POST_REMOVED: "作品消失",
    EventKind.REVIVED: "作品回归",
    EventKind.TITLE_CHANGED: "标题变更",
    EventKind.SCROLLED_OUT: "挤出窗口",
    EventKind.TRIMMED: "超限裁剪",
    EventKind.GAP_DETECTED: "疑似漏检",
    EventKind.NEVER_SEEN: "账号始终无作品",
    EventKind.ALL_GONE: "作品全部消失",
    EventKind.ACCOUNT_FAILED: "抓取失败",
    EventKind.ACCOUNT_RECOVERED: "已恢复",
    EventKind.STALE_NO_UPDATE: "长期无更新",
    EventKind.UPSTREAM_DEGRADED: "上游异常",
    EventKind.INITIALIZED: "首次初始化",
    EventKind.SELF_DEGRADED: "自身降级",
}

#: tombstone 的 reason → 中文。三个取值来自 `diff.py` 的判定分支。
TOMBSTONE_REASONS: dict[str, str] = {
    "confirmed": "已确认消失",
    "scrolled_out": "被新作品挤出窗口",
    "trimmed": "超出跟踪上限被裁剪",
}


def event_label(kind: EventKind | str) -> str:
    try:
        return EVENT_LABELS[EventKind(str(kind))]
    except ValueError:
        return str(kind)


def tombstone_reason(reason: str) -> str:
    return TOMBSTONE_REASONS.get(reason, reason)


__all__ = [name for name in dir() if not name.startswith("_")]
