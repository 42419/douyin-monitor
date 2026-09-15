"""文案集中处：逻辑里不出现字符串拼接。

第一版只有中文；要加英文时改这一个文件即可，判定与投递代码一行都不用动
（DTK 的做法是同一个 catalogue 服务控制台与通知，这里只服务通知，但原则一样）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Content, Kind

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


__all__ = [name for name in dir() if not name.startswith("_")]
