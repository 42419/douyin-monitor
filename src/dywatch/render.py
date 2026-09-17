"""事件 → 消息。纯函数，不发送。

一条 `Message` 同时带三种形态，因为不同渠道能吃的东西不同：
Markdown（钉钉/企业微信）、纯文本（Telegram/邮件/webhook）、短标题（通知栏/Bark）。
渲染一次、各渠道各取所需，比每个渠道自己拼一遍要少一半不一致。

版式沿用旧项目已经验证过的高信息密度写法：一屏之内把"谁、发了什么、什么时候、
数据怎么样、去哪儿看"说全，且**缺值不显示该行**——抖音的 `stats.play_count`
实测恒为 `null`，所以"播放"这一项根本不出现。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Mapping

from . import messages as msg
from .models import Content, Event, EventKind, Kind

SEVERITY: Final[Mapping[EventKind, str]] = {
    EventKind.NEW_POST: "info",
    EventKind.POST_REMOVED: "warning",
    EventKind.ALL_GONE: "error",
    EventKind.GAP_DETECTED: "warning",
    EventKind.NEVER_SEEN: "warning",
    EventKind.ACCOUNT_FAILED: "error",
    EventKind.ACCOUNT_RECOVERED: "info",
    EventKind.STALE_NO_UPDATE: "info",
    EventKind.UPSTREAM_DEGRADED: "error",
    EventKind.SELF_DEGRADED: "warning",
    EventKind.REVIVED: "info",
    EventKind.TITLE_CHANGED: "info",
    EventKind.SCROLLED_OUT: "info",
    EventKind.TRIMMED: "info",
    EventKind.INITIALIZED: "info",
}


@dataclass(frozen=True, slots=True)
class Message:
    """One rendered notification, ready for any channel."""

    event: EventKind
    severity: str
    subject: str
    markdown: str
    text: str
    sec_user_id: str | None = None
    content_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "dywatch",
            "event": self.event.value,
            "severity": self.severity,
            "subject": self.subject,
            "text": self.text,
            "markdown": self.markdown,
            "sec_user_id": self.sec_user_id,
            "content_id": self.content_id,
        }


def _join(*lines: str | None) -> str:
    return "\n".join(line for line in lines if line)


def _title(value: Any) -> str:
    return msg.md_escape(str(value or "")) or "(无标题)"


def _context_rows(payload: Mapping[str, Any]) -> list[str]:
    """`revived` / `title_changed` 共用的补充行：类型与链接（`diff` 顺手带上的）。"""
    rows: list[str] = []
    raw_kind = payload.get("kind")
    if raw_kind:
        rows.append(f"**{msg.ROW_TYPE}**：{msg.kind_label(Kind.parse(raw_kind))}")
    if payload.get("web_url"):
        rows.append(f"**{msg.ROW_LINK}**：{payload['web_url']}")
    return rows


def render_event(event: Event, *, now: datetime | None = None) -> Message:
    payload = event.payload or {}
    nickname = event.nickname or event.sec_user_id
    severity = SEVERITY.get(event.kind, "info")

    subject, markdown = _build(event, payload, nickname, now)
    # 纯文本版：去掉 Markdown 加粗与引用符号，保留结构
    text = markdown.replace("\u200b", "").replace("**", "").replace("\n> ", "\n  ")
    return Message(
        event=event.kind,
        severity=severity,
        subject=subject,
        markdown=markdown,
        text=text,
        sec_user_id=event.sec_user_id or None,
        content_id=event.content_id,
    )


def _build(
    event: Event, payload: Mapping[str, Any], nickname: str, now: datetime | None
) -> tuple[str, str]:
    kind = event.kind
    safe_name = msg.md_escape(nickname)

    if kind is EventKind.NEW_POST:
        content: Content | None = payload.get("content")
        if content is None:  # pragma: no cover - 防御
            return msg.T_NEW_POST.format(nickname=safe_name, kind_label=msg.KIND_UNKNOWN), safe_name
        kind_label = msg.kind_label(content.kind, content.image_count)
        subject = msg.T_NEW_POST.format(nickname=safe_name, kind_label=kind_label)

        gap_days = payload.get("gap_days")
        published = msg.fmt_time(content.created_at)
        if gap_days is not None:
            published = f"{published}（{msg.GAP_NOTE.format(gap=msg.fmt_gap(content.created_at, now))}）"

        rows = [
            f"**{msg.ROW_TITLE}**：{msg.md_escape(content.title) or '(无标题)'}",
            f"**{msg.ROW_TYPE}**：{kind_label}",
            f"**{msg.ROW_PUBLISHED}**：{published}",
        ]
        duration = msg.fmt_duration(content.duration_ms)
        if duration and content.has_video:
            rows.append(f"**{msg.ROW_DURATION}**：{duration}")
        stats = msg.fmt_stats(content)
        if stats:
            rows.append(f"**{msg.ROW_STATS}**：{stats}")
        tags = msg.fmt_tags(content.tags)
        if tags:
            rows.append(f"**{msg.ROW_TAGS}**：{tags}")
        if content.cover_url:
            rows.append(f"**{msg.ROW_COVER}**：{content.cover_url}")
        if content.web_url:
            rows.append(f"**{msg.ROW_LINK}**：{content.web_url}")
        return subject, _join(f"### {subject}", "", *rows)

    if kind in (EventKind.POST_REMOVED, EventKind.ALL_GONE):
        removed = payload.get("removed") or []
        all_gone = bool(payload.get("all_gone")) or kind is EventKind.ALL_GONE
        template = msg.T_ALL_GONE if all_gone else msg.T_POST_REMOVED
        subject = template.format(nickname=safe_name, count=len(removed))
        rows = []
        for item in removed[:10]:
            when = msg.fmt_time(_parse(item.get("created_at")))
            mark = "（置顶）" if item.get("is_top") else ""
            rows.append(f"- {msg.md_escape(str(item.get('title') or '(无标题)'))}{mark} · {when}")
        if len(removed) > 10:
            rows.append(f"- …另有 {len(removed) - 10} 条")
        note = msg.NOTE_ALL_GONE if all_gone else ""
        return subject, _join(f"### {subject}", "", *rows, note)

    if kind is EventKind.REVIVED:
        subject = msg.T_REVIVED.format(nickname=safe_name)
        rows = [f"**{msg.ROW_TITLE}**：{_title(payload.get('title'))}"]
        rows.extend(_context_rows(payload))
        return subject, _join(f"### {subject}", "", *rows)

    if kind is EventKind.TITLE_CHANGED:
        subject = msg.T_TITLE_CHANGED.format(nickname=safe_name)
        rows = [
            f"**{msg.ROW_TITLE_OLD}**：{_title(payload.get('old'))}",
            f"**{msg.ROW_TITLE_NEW}**：{_title(payload.get('new'))}",
        ]
        rows.extend(_context_rows(payload))
        return subject, _join(f"### {subject}", "", *rows)

    if kind is EventKind.GAP_DETECTED:
        subject = msg.T_GAP.format(nickname=safe_name)
        note = msg.NOTE_GAP.format(fetch_count=payload.get("fetch_count"))
        rows = [
            f"- 本页最旧作品的发布时间：{msg.fmt_time(_parse(payload.get('oldest_in_page')))}",
            f"- 上一轮见到的最新作品：{msg.fmt_time(_parse(payload.get('previous_newest')))}",
        ]
        return subject, _join(f"### {subject}", "", *rows, note)

    if kind is EventKind.NEVER_SEEN:
        subject = msg.T_NEVER_SEEN.format(nickname=safe_name)
        rows = [
            f"- 账号：`{event.sec_user_id}`",
            f"- 已连续 {payload.get('rounds', '?')} 轮返回空列表",
        ]
        return subject, _join(f"### {subject}", "", *rows, msg.NOTE_NEVER_SEEN)

    if kind is EventKind.ACCOUNT_FAILED:
        fails = payload.get("fails")
        subject = msg.T_ACCOUNT_FAILED.format(nickname=safe_name, fails=fails)
        rows = [
            f"- 错误码：`{payload.get('code')}`",
            f"- 详情：{msg.md_escape(str(payload.get('message') or ''))}",
        ]
        note = msg.NOTE_CONFIG.format(code=payload.get("code")) if payload.get("config") else ""
        return subject, _join(f"### {subject}", "", *rows, note)

    if kind is EventKind.ACCOUNT_RECOVERED:
        subject = msg.T_ACCOUNT_RECOVERED.format(nickname=safe_name)
        rows = [f"- 此前连续失败 {payload.get('fails')} 次，本轮已成功读取"]
        return subject, _join(f"### {subject}", "", *rows)

    if kind is EventKind.STALE_NO_UPDATE:
        days = payload.get("days")
        subject = msg.T_STALE.format(nickname=safe_name, days=days)
        rows = [
            f"- 已 {days} 天没有新作品",
            "- 这条提醒只会发一次，该账号发布新作品后会重新计时",
        ]
        return subject, _join(f"### {subject}", "", *rows)

    if kind is EventKind.UPSTREAM_DEGRADED:
        subject = msg.T_UPSTREAM
        rows = [
            f"- 错误码：`{payload.get('code')}`",
            f"- 已出现的轮次：{payload.get('rounds', 1)}",
            f"- 影响：全局闸门已关闭 {payload.get('gate_seconds', '?')} 秒，本轮整体跳过",
        ]
        return subject, _join(f"### {subject}", "", *rows, msg.NOTE_GATE)

    if kind is EventKind.SELF_DEGRADED:
        subject = msg.T_SELF
        rows = [f"- 原因：{msg.md_escape(str(payload.get('reason') or '未知'))}"]
        return subject, _join(f"### {subject}", "", *rows)

    # 静默事件（不推送）也留一个可读形态，便于写日志
    subject = f"[{kind.value}] {safe_name}"
    return subject, _join(f"### {subject}", "", f"- {payload}")


def _parse(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


__all__ = ["Message", "SEVERITY", "render_event"]
