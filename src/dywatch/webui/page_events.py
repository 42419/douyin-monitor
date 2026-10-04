"""事件时间线页：`GET /events`。

状态页回答"每个账号现在怎么样"，这一页回答**"最近到底发生了什么"**——两者的区别在
状态页看不见的东西上：窗口挪动（`scrolled_out` / `trimmed`）、平台限制（`hidden_from_guest`）、
上游与自身的问题（`upstream_degraded` / `self_degraded`）、以及**被抑制窗口压掉的重复**。
它们都落库了，但状态页只给每账号 12 条、且混在一起看不出形状。

页面三段：汇总一行 → 每格事件数的柱状图 → 逐条列表。过滤器（窗口 / 类别 / 单账号）
全在 URL 上：这样刷新、收藏、从详情弹窗跳过来都能带上同一组条件，服务端渲染也就不需要
在前端再实现一遍筛选逻辑。

**60 秒自动刷新走 `/events?...&frag=1` 的片段**，不是整页重载：整页重载会把滚动位置
和展开的载荷一起丢掉，而这页的全部价值就在"往下翻着看"。有人在看展开的载荷时
（`.tl-payload:not([hidden])`）连片段也不换——那正是他要读的东西。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ..messages import event_label, fmt_time, one_line
from ..models import EventKind, NOTIFY_KINDS
from ..settings import Settings
from . import charts
from .common import _as_int, _escape_html, event_tone
from .queries import author_name, event_ticks, events, has_store
from .theme import TL_ITEM, masthead, page

#: 时间窗口。`count` 是柱状图的格子数（每格一个柱子），`hourly` 决定标签是"13:00"还是"09-24"。
RANGES: Mapping[str, Mapping[str, Any]] = {
    "24h": {
        "label": "24 小时",
        "span": timedelta(hours=24),
        "count": 24,
        "hourly": True,
        "limit": 200,
    },
    "7d": {
        "label": "7 天",
        "span": timedelta(days=7),
        "count": 7,
        "hourly": False,
        "limit": 300,
    },
    "30d": {
        "label": "30 天",
        "span": timedelta(days=30),
        "count": 30,
        "hourly": False,
        "limit": 600,
    },
}
DEFAULT_RANGE = "24h"

#: 类别分组。**必须覆盖全部 16 种事件、且不重叠**——有测试盯着（`test_webui_events.py`）：
#: 少一个就会有一个事件永远筛不出来，而它在"全部"里又看得见，人会以为自己点错了。
GROUPS: Mapping[str, tuple[str, tuple[EventKind, ...]]] = {
    "all": ("全部", ()),
    "post": (
        "作品",
        (
            EventKind.NEW_POST,
            EventKind.POST_REMOVED,
            EventKind.ALL_GONE,
            EventKind.REVIVED,
            EventKind.TITLE_CHANGED,
            EventKind.HIDDEN_FROM_GUEST,
        ),
    ),
    "account": (
        "账号",
        (
            EventKind.ACCOUNT_FAILED,
            EventKind.ACCOUNT_RECOVERED,
            EventKind.NEVER_SEEN,
            EventKind.STALE_NO_UPDATE,
            EventKind.GAP_DETECTED,
        ),
    ),
    "system": ("系统", (EventKind.UPSTREAM_DEGRADED, EventKind.SELF_DEGRADED)),
    "mech": (
        "窗口挪动",
        (EventKind.SCROLLED_OUT, EventKind.TRIMMED, EventKind.INITIALIZED),
    ),
}
DEFAULT_GROUP = "all"

#: 载荷摘要里单条标题的字符上限。列表是一行一条，长标题会把右边的推送状态挤出去。
_CLIP = 46


# =================== URL 参数 ===================


def parse_filters(query: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """URL 查询串 → 过滤条件。**认不出来的值一律退回默认**，不报错。

    这一页的链接会被收藏、会被别的地方拼出来，一个过期的 `?range=90d` 应该是"给你看
    默认的 24 小时"，而不是 400 —— 面板是排查问题时打开的东西，它自己不该先出问题。
    """

    def first(key: str) -> str:
        values = query.get(key) or ()
        return str(values[0]).strip() if values else ""

    range_key = first("range")
    if range_key not in RANGES:
        range_key = DEFAULT_RANGE
    group_key = first("group")
    if group_key not in GROUPS:
        group_key = DEFAULT_GROUP

    kind = first("kind")
    try:
        EventKind(kind)
    except ValueError:
        kind = ""
    author = first("author")

    return {
        "range": range_key,
        "group": group_key,
        "kind": kind,
        "author": author,
        "frag": first("frag") in ("1", "true", "yes"),
    }


def kinds_for(filters: Mapping[str, Any]) -> tuple[str, ...]:
    """当前过滤条件对应的类型集合。空元组 == 不过滤（"全部"）。"""
    if filters.get("kind"):
        return (str(filters["kind"]),)
    group = str(filters.get("group") or DEFAULT_GROUP)
    return tuple(kind.value for kind in GROUPS.get(group, ("", ()))[1])


def query_string(filters: Mapping[str, Any], **overrides: Any) -> str:
    """由过滤条件拼查询串（只写非默认项，URL 短一点好读）。"""
    merged = {**filters, **overrides}
    parts: list[str] = []
    if merged.get("range") and merged["range"] != DEFAULT_RANGE:
        parts.append(f"range={merged['range']}")
    if merged.get("group") and merged["group"] != DEFAULT_GROUP:
        parts.append(f"group={merged['group']}")
    if merged.get("kind"):
        parts.append(f"kind={merged['kind']}")
    if merged.get("author"):
        parts.append("author=" + _quote(str(merged["author"])))
    return "&".join(parts)


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


# =================== 载荷 → 一行人话 ===================


def summarize_payload(kind: str, payload: Mapping[str, Any]) -> str:
    """事件的载荷 → 列表里那一行的正文。

    刻意**不复用通知的渲染**（`render.render_event`）：通知的文案是为"一条消息推给人看"
    写的（有标题、有链接、有多行细节），塞进一行会被 CSS 截成半句话。这里要的是
    "一眼看得出是哪条作品/哪个错误"。

    认不出的形状一律退回紧凑 JSON，而不是显示空白——空白会让人以为"这条事件没有载荷"。
    """
    data = payload if isinstance(payload, Mapping) else {}
    try:
        text = _summarize(EventKind(str(kind)), data)
    except ValueError:  # 库里可能有已下线的旧类型
        return _json_ish(data)
    # 认得类型但载荷不是那个形状（旧版本写下的、或字段被改过）时也要兜住：
    # 空白比 JSON 难查得多
    return text or _json_ish(data)


def _summarize(kind: EventKind, data: Mapping[str, Any]) -> str:
    if kind is EventKind.NEW_POST:
        content = data.get("content")
        content = content if isinstance(content, Mapping) else {}
        title = _clip(str(content.get("title") or content.get("content_id") or ""))
        gap = _as_int(data.get("gap_days"))
        return f"{title}（距上一条 {gap} 天）" if gap else title
    if kind in (EventKind.POST_REMOVED, EventKind.ALL_GONE):
        removed = data.get("removed")
        rows = (
            [row for row in removed if isinstance(row, Mapping)]
            if isinstance(removed, list)
            else []
        )
        titles = "、".join(
            _clip(str(row.get("title") or row.get("content_id") or ""))
            for row in rows[:3]
        )
        head = f"{len(rows)} 条" if len(rows) > 1 else ""
        if kind is EventKind.ALL_GONE:
            head = "全部消失" + (f"（{len(rows)} 条）" if rows else "")
        return (
            f"{head}：{titles}" if head and titles else (head or titles or "有作品消失")
        )
    if kind is EventKind.REVIVED:
        return _clip(str(data.get("title") or ""))
    if kind is EventKind.TITLE_CHANGED:
        return (
            f"{_clip(str(data.get('old') or ''))} → {_clip(str(data.get('new') or ''))}"
        )
    if kind is EventKind.HIDDEN_FROM_GUEST:
        rows = data.get("hidden")
        rows = (
            [row for row in rows if isinstance(row, Mapping)]
            if isinstance(rows, list)
            else []
        )
        titles = "、".join(
            _clip(str(row.get("title") or row.get("content_id") or ""))
            for row in rows[:3]
        )
        return (
            f"{len(rows)} 条登录可见、访客看不到：{titles}"
            if titles
            else f"{len(rows)} 条登录可见、访客看不到"
        )
    if kind is EventKind.GAP_DETECTED:
        return (
            f"上次最新 {_short_time(data.get('previous_newest'))}"
            f" → 本页最旧 {_short_time(data.get('oldest_in_page'))}"
            f"（一页 {_as_int(data.get('fetch_count'))} 条）"
        )
    if kind is EventKind.NEVER_SEEN:
        return f"连续 {_as_int(data.get('rounds'))} 轮没有返回任何作品"
    if kind is EventKind.STALE_NO_UPDATE:
        return f"{_as_int(data.get('days'))} 天没有新作品"
    if kind is EventKind.ACCOUNT_FAILED:
        fails = _as_int(data.get("fails"))
        return f"{data.get('code') or '未知错误'}（连续 {fails} 次）：{_clip(str(data.get('message') or ''))}"
    if kind is EventKind.ACCOUNT_RECOVERED:
        return f"连续失败 {_as_int(data.get('fails'))} 次后恢复"
    if kind is EventKind.INITIALIZED:
        return f"首次记录 {_as_int(data.get('count'))} 条作品"
    if kind is EventKind.UPSTREAM_DEGRADED:
        gate = _as_int(data.get("gate_seconds"))
        return (
            f"{data.get('code') or '未知错误'}：{_clip(str(data.get('message') or ''))}"
            + (f"（闸门 {gate} 秒）" if gate else "")
        )
    if kind is EventKind.SELF_DEGRADED:
        reason = str(data.get("reason") or "")
        labels = {
            "disk_low": "磁盘剩余空间不足",
            "data_dir_read_only": "数据目录不可写",
            "state_store_write_failed": "状态库写失败",
        }
        free = data.get("free_mb")
        tail = f"（剩余 {free}MB）" if isinstance(free, int) else ""
        return f"{labels.get(reason, reason)}{tail}"
    if kind in (EventKind.SCROLLED_OUT, EventKind.TRIMMED):
        return str(data.get("content_id") or "")
    return _json_ish(data)


def _short_time(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "—"
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return value[:16]


def _clip(text: str) -> str:
    """截断 + 压成一行。载荷里的标题来自平台，可能带换行（列表是一行一条）。"""
    cleaned = one_line(text)
    return cleaned if len(cleaned) <= _CLIP else cleaned[: _CLIP - 1] + "…"


def _json_ish(data: Mapping[str, Any]) -> str:
    if not data:
        return ""
    try:
        return _clip(json.dumps(data, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):  # pragma: no cover - 载荷来自 JSON，必然可序列化
        return ""


# =================== 视图 ===================


def build_view(settings: Settings, filters: Mapping[str, Any]) -> dict[str, Any]:
    """把过滤条件变成渲染要用的全部东西。纯读，不写任何状态。"""
    now = datetime.now(timezone.utc)
    spec = RANGES[str(filters["range"])]
    since = now - spec["span"]
    kinds = kinds_for(filters)
    limit = int(spec["limit"])
    author = str(filters.get("author") or "")

    # 多取一条只为知道"是不是被截断了"；截断必须说出来，否则"最近 200 条"看起来像"一共 200 条"
    rows = events(
        settings, since=since, until=now, kinds=kinds, author=author, limit=limit + 1
    )
    truncated = len(rows) > limit
    rows = rows[:limit]
    ticks = event_ticks(settings, since=since, until=now, kinds=kinds, author=author)

    payload = charts.events_chart_payload(
        ticks,
        since=since,
        until=now,
        count=int(spec["count"]),
        hourly=bool(spec["hourly"]),
    )
    total_ticks = sum(sum(item["data"]) for item in payload["datasets"])

    notified = sum(1 for row in rows if delivery_state(row) == "sent")
    silent = sum(1 for row in rows if delivery_state(row) == "silent")

    return {
        "filters": dict(filters),
        "spec": spec,
        "since": since,
        "until": now,
        "rows": rows,
        "payload": payload,
        "chart_total": total_ticks,
        "total_rows": len(rows),
        "truncated": truncated,
        "notified": notified,
        "silent": silent,
        "has_store": has_store(settings),
        "author": author,
        # 标题要显示"谁"的事件，而不是一串 sec_user_id
        "author_label": author_name(settings, author) if author else "",
    }


def delivery_state(row: Mapping[str, Any]) -> str:
    """一条事件的投递状态：`sent` / `failed` / `silent` / `none`。

    `silent`（静默落库）与 `none`（该推但没推成）必须分开：前者是**设计如此**
    （平台限制、窗口挪动这类），后者是"出了事但通知没出去"——把两者画成同一个灰点，
    就再也看不出通知链路到底有没有在工作了。
    """
    kind = str(row.get("kind") or "")
    delivery = row.get("delivery")
    delivery = delivery if isinstance(delivery, Mapping) else {}
    sent = delivery.get("sent")
    failed = delivery.get("failed")
    if sent:
        return "sent"
    if failed:
        return "failed"
    try:
        if EventKind(kind) not in NOTIFY_KINDS:
            return "silent"
    except ValueError:
        return "silent"
    return "none"


def _sent_html(row: Mapping[str, Any]) -> str:
    state = delivery_state(row)
    delivery = row.get("delivery")
    delivery = delivery if isinstance(delivery, Mapping) else {}
    sent = delivery.get("sent") if isinstance(delivery.get("sent"), list) else []
    failed = (
        delivery.get("failed") if isinstance(delivery.get("failed"), Mapping) else {}
    )
    tip = ""
    if sent:
        tip = "已送达：" + "、".join(str(x) for x in sent)
    if failed:
        tip += (
            ("；" if tip else "")
            + "失败："
            + "、".join(f"{name}（{reason}）" for name, reason in failed.items())
        )
    text = {
        "sent": "已推送",
        "failed": "推送失败",
        "silent": "静默",
        "none": "未推送",
    }[state]
    color = {
        "sent": "var(--green)",
        "failed": "var(--red)",
        "silent": "var(--text3)",
        "none": "var(--amber)",
    }[state]
    title = f' title="{_escape_html(tip)}"' if tip else ""
    return f'<span class="tl-sent mono" style="color:{color}"{title}>{text}</span>'


def render_row(row: Mapping[str, Any]) -> str:
    """一条事件 = 两个相邻元素：整行（可点）+ 默认折叠的载荷原文。"""
    kind = str(row.get("kind") or "")
    try:
        label = event_label(EventKind(kind))
    except ValueError:
        label = kind or "未知"
    sec_user_id = str(row.get("sec_user_id") or "")
    if sec_user_id:
        who = f"{str(row.get('nickname') or '') or sec_user_id} · {sec_user_id[:8]}…"
    else:
        who = "系统"  # 上游 / 自身降级这类不属于任何账号
    payload = row.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    text = summarize_payload(kind, payload)
    stamp = row.get("ts")
    item = TL_ITEM.substitute(
        time=_escape_html(fmt_time(stamp if isinstance(stamp, datetime) else None)),
        cls=f"k-{event_tone(EventKind(kind))}" if _is_kind(kind) else "k-quiet",
        kind=_escape_html(label),
        who=f'<span class="tl-who">{_escape_html(who)}</span>',
        text=_escape_html(text),
        sent=_sent_html(row),
    )
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return item + f'<div class="tl-payload mono" hidden>{_escape_html(raw)}</div>'


def _is_kind(kind: str) -> bool:
    try:
        EventKind(kind)
    except ValueError:
        return False
    return True


def render_fragment(view: Mapping[str, Any]) -> str:
    """汇总行 + 图表 + 列表。局部刷新换的就是这一块。"""
    spec = view["spec"]
    since, until = view["since"], view["until"]
    window = f"{since.astimezone().strftime('%m-%d %H:%M')} → {until.astimezone().strftime('%m-%d %H:%M')}"
    if not view["has_store"]:
        body = (
            '<div class="empty"><div class="headline">状态库还没有数据</div>'
            "监控跑完第一轮之后，这里会出现它记录的事件。</div>"
        )
        chart = ""
    else:
        if view["chart_total"]:
            chart = charts.chart_block(
                title="事件分布",
                range_text=f"每格 {'1 小时' if spec['hourly'] else '1 天'} · 共 {view['chart_total']} 条",
                legend=charts.tone_legend(),
                body=charts.canvas("eventsChart", view["payload"]),
            )
        else:
            chart = charts.chart_block(
                title="事件分布",
                range_text=window,
                legend=[],
                body=charts.empty_block("这个窗口里没有事件"),
            )
        if view["rows"]:
            rows = "".join(render_row(row) for row in view["rows"])
            body = f'<div class="tl">{rows}</div>'
        else:
            body = (
                '<div class="empty"><div class="headline">这个窗口里没有事件</div>'
                "换一个时间范围或类别看看。</div>"
            )

    parts = [
        f"窗口 {window}",
        f"{view['total_rows']} 条",
        f"已推送 {view['notified']} · 静默 {view['silent']}",
    ]
    if view["truncated"]:
        # "最近 200 条"不写出来的话，看起来像"这个窗口一共 200 条"——柱状图上的总数
        # 和列表条数就会对不上，而人对不上时会先怀疑采集漏了
        parts.append(f"只显示最近 {view['total_rows']} 条")
    parts.append("点击任意一行看载荷原文")
    meta = "".join(
        f'<span class="sep">·</span><span>{_escape_html(part)}</span>'
        if index
        else f"<span>{_escape_html(part)}</span>"
        for index, part in enumerate(parts)
    )
    return f'<div class="meta mono">{meta}</div>{chart}{body}'


def render_page(settings: Settings, filters: Mapping[str, Any]) -> str:
    view = build_view(settings, filters)
    active_range = str(filters["range"])
    active_group = str(filters["group"])
    active_kind = str(filters.get("kind") or "")
    author = str(filters.get("author") or "")

    chips = []
    for key, spec in RANGES.items():
        chips.append(_chip("range", key, spec["label"], key == active_range, filters))
    group_chips = []
    for key, (label, _kinds) in GROUPS.items():
        on = key == active_group and not active_kind
        group_chips.append(_chip("group", key, label, on, {**filters, "kind": ""}))

    reset = (
        '<div class="tl-filter mono"><a href="/events">清除过滤</a></div>'
        if (
            active_kind
            or author
            or active_range != DEFAULT_RANGE
            or active_group != DEFAULT_GROUP
        )
        else ""
    )

    filter_html = (
        '<div class="tl-filter mono">'
        + "".join(chips)
        + '</div><div class="tl-filter mono">'
        + "".join(group_chips)
        + f"</div>{reset}"
    )

    eyebrow = "DYWATCH / EVENTS"
    headline = "事件时间线"
    if author:
        label = view["author_label"] or (author[:12] + "…")
        headline = f"{_escape_html(label)} 的事件"
    elif active_kind:
        headline = f"只看「{_escape_html(event_label(EventKind(active_kind)))}」"

    meta = "".join(
        [
            "<span>数据来自状态库的 events 表</span>",
            '<span class="sep">·</span>',
            "<span>事件按时间倒序</span>",
            '<span class="sep">·</span>',
            "<span>60 秒自动刷新</span>",
        ]
    )

    body = (
        masthead(eyebrow=eyebrow, headline=headline, meta=meta, active="events")
        + filter_html
        + f'<div id="tlHolder">{render_fragment(view)}</div>'
        + (
            '<div class="footer mono">'
            "<span>只读 · 无鉴权 · 静默事件也在列表里（它们不推送但是落库）</span>"
            '<span><a href="/">/</a>&nbsp;&nbsp;<a href="/api/events">/api/events</a>'
            '&nbsp;&nbsp;<a href="/metrics">/metrics</a></span>'
            "</div>"
        )
    )
    after = charts.script_tag() + f"<script>{charts.BOOTSTRAP_JS}</script>" + _PAGE_JS
    return page(title="dywatch · 事件时间线", body=body, scripts=after, refresh=0)


def _chip(
    kind_key: str, value: str, label: str, on: bool, filters: Mapping[str, Any]
) -> str:
    query = query_string(filters, **{kind_key: value})
    href = f"/events?{query}" if query else "/events"
    cls = ' class="on"' if on else ""
    return f'<a href="{href}"{cls}>{_escape_html(label)}</a>'


_PAGE_JS = r"""
<script>
// 点一整行看载荷原文：整行可点（手指够得着），载荷块是它的下一个兄弟节点。
// 不用内联 onclick —— 载荷来自平台，把它拼进 JS 字符串就是一条注入路径。
document.addEventListener('click', function (event) {
  var row = event.target.closest && event.target.closest('.tl-item');
  if (!row) return;
  var box = row.nextElementSibling;
  if (!box || !box.classList.contains('tl-payload')) return;
  box.hidden = !box.hidden;
});

// 60 秒换一次片段，不整页重载：整页重载会丢滚动位置。有人在读展开的载荷时连片段都不换。
setInterval(function () {
  if (document.querySelector('.tl-payload:not([hidden])')) return;
  var holder = document.getElementById('tlHolder');
  if (!holder) return;
  var sep = location.search ? '&' : '?';
  fetch(location.pathname + location.search + sep + 'frag=1', {cache: 'no-store'})
    .then(function (r) { return r.ok ? r.text() : null; })
    .then(function (html) {
      if (html === null) return;
      if (window.dyChart) window.dyChart.clear(holder);
      holder.innerHTML = html;
      if (window.dyChart) window.dyChart.mount(holder);
    })
    .catch(function () { /* 拿不到就保持原样，下一轮再试 */ });
}, 60000);
</script>
"""


__all__ = [
    "DEFAULT_GROUP",
    "DEFAULT_RANGE",
    "GROUPS",
    "RANGES",
    "build_view",
    "delivery_state",
    "kinds_for",
    "parse_filters",
    "query_string",
    "render_fragment",
    "render_page",
    "render_row",
    "summarize_payload",
]
