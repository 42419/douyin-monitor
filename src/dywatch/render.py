"""事件 → 消息。纯函数，不发送。

一条 `Message` 同时带三种形态，因为不同渠道能吃的东西不同：
Markdown（钉钉/企业微信/Server 酱）、纯文本（Telegram/Bark/webhook）、短标题（通知栏/Bark）。
渲染一次、各渠道各取所需，比每个渠道自己拼一遍要少一半不一致。

## 版式：每个字段写成 `**标签**：值`，自成一段，不用列表圆点

钉钉与企业微信**共用同一个 `markdown` 字段**，所以版式取两者的交集
（钉钉：[消息发送与接收类型]；企业微信：[群机器人消息推送配置]）：

| 元素 | 钉钉 | 企业微信 markdown |
| ---------------- | -- | ------------------------------- |
| `###` 标题 | ✅ | ✅（`#` 与文字之间要有空格） |
| `**加粗**` | ✅ | ✅ |
| `[文字](链接)` | ✅ | ✅ |
| `>` 引用 | ✅ | ✅ |
| `- ` 无序列表 | ✅ | ✗（原样显示 `- `）——**不用** |
| 表格 / 代码块 / 行内代码 / 分隔线 / 斜体 | ✗ | ✗ |

标签加粗是唯一的强调手段：交集里只有 `**` 两边都认，值本身一律不加装饰。
分行靠**段落之间的空行**：钉钉把**单个**换行当软换行折叠（官方建议 `\\n` 前后各加
两个空格才有硬换行），相邻两行只隔一个换行会挤成一整行——那正是第一版"太乱"的根因。
空行隔开的段落两边都能正确分行，且不依赖行尾那些不可见的空格。

新作品在钉钉里另走**独立跳转 ActionCard**（`Message.card_markdown` / `card_url`，
见 `_new_post_card`）：那份正文只有钉钉读，所以里面可以用 `- ` 列表；列表项本身就是
块级元素，**单换行**分隔即可，不会被"软换行折叠"那条规则吃掉。

## 两个长度约束

- **正文**：企业微信 markdown 上限 **4096 字节**（UTF-8，中文 3 字节/字），钉钉 5000 字符。
  取更严的那个再留余量 → `MAX_BODY_BYTES`，超了在**行边界**截断并附一句说明。
- **单条作品标题**：抖音标题可以很长，整段塞进通知会淹掉其它字段 → `_title()` 截断。
  截断发生在**转义之前**：先转义再截断会把 `\\*` 切成落单的反斜杠，通知里就留下一个坏掉的
  转义序列（`webui.server._label` 踩过同一个坑）。

[消息发送与接收类型]: https://open.dingtalk.com/document/development/robot-message-type
[群机器人消息推送配置]: https://developer.work.weixin.qq.com/document/path/91770
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Sequence

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

#: 正文上限（UTF-8 字节）。企业微信 4096 是硬上限，留 500+ 字节余量给各家客户端
#: 对换行/表情的差异；超了按行边界截断（见 `_cap`）。
MAX_BODY_BYTES: Final[int] = 3500
#: 单条作品标题的展示上限（字符）。面板与事件表里仍是全文。
TITLE_CLIP: Final[int] = 80
#: 列表类事件一次最多列几条（其余折成一句"另有 N 条"）
MAX_LIST_ITEMS: Final[int] = 10

_URL_OK = re.compile(r"^https?://[^\s()<>\"']+$")


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
    #: 钉钉 ActionCard 版正文与按钮跳转地址（目前只有"新作品"有）。**不进 `as_dict`**：
    #: 这是给钉钉渠道挑版式用的，不是 webhook 载荷契约的一部分。
    card_markdown: str | None = None
    card_url: str | None = None

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


# --------------------------------------------------------------- 版式基础件
def _clip(raw: Any, limit: int = TITLE_CLIP) -> str:
    """把**原始值**截断（转义之前）。见模块头注释：顺序反了会切坏转义序列。"""
    # 只有 None 算"没有值"：0 / False / {} 都是有效值，`raw or ""` 会把它们吞成空串
    text = "" if raw is None else str(raw)
    return text[: limit - 1] + "…" if len(text) > limit else text


def _val(raw: Any, limit: int = 120) -> str:
    """字段值：**压成一行**（控制字符与换行换空格）**再转义**。

    外部字符串（标题、上游报错、账号 ID）都可能带换行；带进 `**标签**：值` 这种一行式
    字段里，换行就会撑出一个裸段落行——而钉钉会把裸行折叠进上一行，整段就花了。
    """
    return msg.md_escape(msg.one_line(_clip(raw, limit)))


def _title(value: Any, limit: int = TITLE_CLIP) -> str:
    return _val(value, limit) or msg.PLACEHOLDER_NO_TITLE


def _field(label: str, value: Any) -> str:
    """一个字段段落：`**标签**：值`。

    标签加粗是唯一的强调手段——钉钉与企业微信都支持 `**`；列表、表格、行内代码至少有一边
    会原样显示，所以不用（见模块头注释与 DESIGN D20）。加粗落在**转义之后**：`label` 是常量
    不含 `*`，而值里的 `*` 已被 `_val` 转成 `\\*`，不会被误当成加粗标记。
    """
    return f"**{label}**：{value}"


def _item(text: str) -> str:
    """一条自成段落的文字（多条目事件里的一条作品、一句陈述）。"""
    return text


def _link(label: str, url: Any) -> str | None:
    """可点链接。URL 不合法（含空格/括号/引号，或不是 http(s)）就**不放**这一行——
    放进去只会把整行的 markdown 链接语法弄坏，不如不给。"""
    target = str(url or "").strip()
    return _item(f"[{label}]({target})") if _URL_OK.match(target) else None


def _titles(
    template: str, plain_name: str, md_name: str, **fields: Any
) -> tuple[str, str]:
    """同一句话的两种形态：

    - **subject**：进通知栏、Bark 标题、Server 酱标题，必须是**纯文本**（不能有 `\\*` 这种
      转义残留，那边不做 markdown 解析，会原样显示）；
    - **heading**：放进 `### ` 的那一份，昵称要转义（否则昵称里的 `*` / `#` 会改版式）。
    """
    return (
        template.format(nickname=plain_name, **fields),
        template.format(nickname=md_name, **fields),
    )


def _card(heading: str, rows: Sequence[str], note: str = "") -> str:
    """统一版式：`### 标题`，每一行各自成段（段间一个空行），最后可选一段引用。

    **没有列表圆点**：最早的 bash 版就是这样推的，看着最清爽。分行靠的是"段落之间的空行"
    ——钉钉与企业微信都会把空行隔开的两段渲染成两行；挤成一坨的只是"单个换行"。
    """
    blocks = [f"### {heading}", *rows]
    if note:
        blocks.append(f"> {note}")
    return "\n\n".join(blocks)


def _as_list(value: Any) -> list[Any]:
    """载荷里的"列表"字段：不是列表就当空——渲染路径绝不允许因为载荷畸形而抛异常。"""
    return list(value) if isinstance(value, (list, tuple)) else []


def _entries(payload: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    """取列表里的**字典**条目：这条路径上抛异常等于"这条通知永远发不出去"。"""
    return [item for item in _as_list(payload.get(key)) if isinstance(item, Mapping)]


def _list_rows(items: Sequence[Mapping[str, Any]]) -> list[str]:
    """多条目事件的列表：一条一段，`标题（置顶） · 时间`。"""
    rows = []
    for item in items[:MAX_LIST_ITEMS]:
        when = msg.fmt_time(_parse(item.get("created_at")))
        mark = f"（{msg.MARK_TOP}）" if item.get("is_top") else ""
        rows.append(_item(f"{_title(item.get('title'))}{mark} · {when}"))
    if len(items) > MAX_LIST_ITEMS:
        rows.append(_item(msg.MORE_ITEMS.format(count=len(items) - MAX_LIST_ITEMS)))
    return rows


def _context_rows(payload: Mapping[str, Any]) -> list[str]:
    """`revived` / `title_changed` 共用的补充行：类型与作品页链接（`diff` 顺手带上的）。"""
    rows: list[str] = []
    raw_kind = payload.get("kind")
    if raw_kind:
        rows.append(_field(msg.ROW_TYPE, msg.kind_label(Kind.parse(raw_kind))))
    link = _link(msg.LINK_POST, payload.get("web_url"))
    if link:
        rows.append(link)
    return rows


def _cap(markdown: str, limit: int = MAX_BODY_BYTES) -> str:
    """超长正文在**行边界**截断，并附一句说明。

    只按整行保留：切在行中间会切坏 `**`、`](` 这些成对语法，通知看起来就是坏的。
    """
    if len(markdown.encode("utf-8")) <= limit:
        return markdown
    marker = f"> {msg.NOTE_TRUNCATED}"
    budget = limit - len((marker + "\n\n").encode("utf-8"))
    kept: list[str] = []
    used = 0
    for line in markdown.split("\n"):
        size = len(line.encode("utf-8")) + 1
        if used + size > budget:
            break
        kept.append(line)
        used += size
    return "\n".join(kept).rstrip() + "\n\n" + marker


_ESCAPE_INVERSE = re.compile(r"\\(.)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
#: 行首的字段标签加粗：`**标题**：值`。**只剥这一种形状**——全局 `replace("**", "")`
#: 会把值里的 `a**b` 和链接 URL 里的 `**` 一起吃掉（实测：封面地址变成另一个地址，点开 404）。
_TAG_BOLD = re.compile(r"(?m)^\*\*([^*\n]+)\*\*：")


def _to_text(markdown: str) -> str:
    """纯文本形态（Telegram/Bark/webhook）：去掉只对 markdown 有意义的东西。

    三件事：`### ` 标题前缀、`**加粗**`、`[文字](url)` → `文字 <url>`（纯文本里点不开，
    所以把地址显式写出来），以及 `md_escape` 加的那些反斜杠转义（单遍替换正好是它的逆）。
    """
    # 版面里每个字段都自成一段（段间空行）；纯文本里字段该紧挨着，所以把相邻的字段段落并回
    # 一组，只在 标题 / 字段组 / 引用 之间留空行
    groups: list[str] = []
    prev_is_row = False
    for block in markdown.split("\n\n"):
        is_row = not block.startswith(("### ", "> "))
        if is_row and prev_is_row:
            groups[-1] += "\n" + block
        else:
            groups.append(block)
        prev_is_row = is_row
    lines = []
    for line in "\n\n".join(groups).split("\n"):
        if line.startswith("### "):
            lines.append(line[4:])
        elif line.startswith("> "):
            lines.append("  " + line[2:])
        else:
            lines.append(line)
    text = _MD_LINK.sub(lambda m: f"{m.group(1)} <{m.group(2)}>", "\n".join(lines))
    # 去掉标签加粗要**在反转义之前**：此时正文里的 `*` 还是 `\*`，只有版面自己加的 `**`
    # 是裸的。而且只认**行首**的 `**标签**：`——值里的 `a**b` 与 URL 里的 `**` 是用户数据，
    # 删掉就换了一个字符串（链接会 404），它们既不在行首、也不成对出现在冒号前。
    text = _TAG_BOLD.sub(r"\1：", text)
    text = _ESCAPE_INVERSE.sub(r"\1", text)
    return text.replace("\u200b", "")


def render_event(event: Event, *, now: datetime | None = None) -> Message:
    payload = event.payload or {}
    nickname = event.nickname or event.sec_user_id
    severity = SEVERITY.get(event.kind, "info")

    subject, markdown = _build(event, payload, nickname, now)
    markdown = _cap(markdown)
    card = None
    if event.kind is EventKind.NEW_POST:
        card = _new_post_card(
            payload, msg.md_escape(msg.one_line(_clip(nickname, 40))), now
        )
    return Message(
        event=event.kind,
        severity=severity,
        subject=subject,
        markdown=markdown,
        text=_to_text(markdown),
        sec_user_id=event.sec_user_id or None,
        content_id=event.content_id,
        card_markdown=card[0] if card else None,
        card_url=card[1] if card else None,
    )


def _new_post_card(
    payload: Mapping[str, Any], md_name: str, now: datetime | None
) -> tuple[str, str] | None:
    """钉钉 ActionCard 版"新作品"：`(正文, 跳转地址)`。

    ```
    ### 阿Z想开了 发布了新视频

    - **标题**：xxx
    - **发布时间**：2026-11-10 18:24:00
    - **检测时间**：2026-11-10 18:25:03

    ![封面](https://…)
    ```

    **标题带昵称**：群里同时有多条卡片，只写"新视频发布"看不出是谁发的；正文因此
    不再重复一行"用户"，三个字段就够了。

    **这里可以用列表**：卡片正文只有钉钉会读（`Message.card_markdown` 不被任何其它渠道取用），
    不受"钉钉 ∩ 企业微信"那条约束。字段挤在同一个列表块里，比每行之间空一行紧凑得多；
    封面单独成块，视觉上和字段分开。标题、昵称仍然先压成一行再转义。

    没有可用的作品链接（既没有合法的 `web_url`，也无法给视频拼出地址）就返回 None，
    钉钉渠道会退回通用的 markdown 版——没有地址就没有按钮可放，不如不用。
    """
    content = payload.get("content")
    if content is None:
        return None
    url = str(content.web_url or "").strip()
    if not _URL_OK.match(url):
        url = f"https://www.douyin.com/video/{content.content_id}"
        if content.kind is not Kind.VIDEO or not _URL_OK.match(url):
            return None

    heading = msg.CARD_HEADING.format(
        nickname=md_name, kind=msg.kind_label(content.kind)
    )
    detected = now or datetime.now(timezone.utc)
    rows = [
        (msg.ROW_TITLE, _title(content.title)),
        (msg.CARD_PUBLISHED, msg.fmt_time(content.created_at, seconds=True)),
        (msg.CARD_DETECTED, msg.fmt_time(detected, seconds=True)),
    ]
    blocks = [
        f"### {heading}",
        "\n".join(f"- **{label}**：{value}" for label, value in rows),
    ]
    cover = str(content.cover_url or "").strip()
    if _URL_OK.match(cover):
        blocks.append(f"![{msg.CARD_COVER_ALT}]({cover})")
    return "\n\n".join(blocks), url


# ------------------------------------------------------------------- 各事件
def _build(
    event: Event, payload: Mapping[str, Any], nickname: str, now: datetime | None
) -> tuple[str, str]:
    kind = event.kind
    # 昵称两态：纯文本版进通知栏，转义版进 `### ` 标题（见 `_titles`）
    plain_name = msg.one_line(_clip(nickname, 40))
    md_name = msg.md_escape(plain_name)

    if kind is EventKind.NEW_POST:
        content: Content | None = payload.get("content")
        if content is None:
            # 防御：没有 content 也要给一条**结构完整**的通知（曾经这里直接返回昵称，
            # 那是个裸段落行——钉钉会把它折叠掉，出来的通知等于没有正文）
            subject, heading = _titles(
                msg.T_NEW_POST, plain_name, md_name, kind_label=msg.KIND_UNKNOWN
            )
            return subject, _card(
                heading, [_item("上游没有返回作品详情，这一轮只有事件记录")]
            )

        kind_label = msg.kind_label(content.kind, content.image_count)
        subject, heading = _titles(
            msg.T_NEW_POST, plain_name, md_name, kind_label=kind_label
        )

        published = msg.fmt_time(content.created_at)
        if payload.get("gap_days") is not None:
            published = f"{published}（{msg.GAP_NOTE.format(gap=msg.fmt_gap(content.created_at, now))}）"

        rows = [
            _field(msg.ROW_TITLE, _title(content.title)),
            _field(msg.ROW_TYPE, kind_label),
            _field(msg.ROW_PUBLISHED, published),
        ]
        duration = msg.fmt_duration(content.duration_ms)
        if duration and content.has_video:
            rows.append(_field(msg.ROW_DURATION, duration))
        stats = msg.fmt_stats(content)
        if stats:
            rows.append(_field(msg.ROW_STATS, stats))
        tags = msg.fmt_tags(content.tags)
        if tags:
            rows.append(_field(msg.ROW_TAGS, tags))
        for link in (
            _link(msg.LINK_COVER, content.cover_url),
            _link(msg.LINK_POST, content.web_url),
        ):
            if link:
                rows.append(link)
        return subject, _card(heading, rows)

    if kind in (EventKind.POST_REMOVED, EventKind.ALL_GONE):
        removed = _entries(payload, "removed")
        all_gone = bool(payload.get("all_gone")) or kind is EventKind.ALL_GONE
        if not removed and payload.get("removed"):
            # 载荷形状不对（不是列表/里面没有字典）。不能继续按"0 条"渲染：
            # 那会推出一条"有 0 条作品已确认消失"的假信息——比不推更糟，
            # 看的人会以为"什么事都没有"。所以**不走 `_titles` 的计数模板**，
            # 标题直接说明形状读不出来（`_titles` 会把 count 写进标题栏）。
            shape = type(payload.get("removed")).__name__
            subject = f"【作品消失·载荷异常】{plain_name}"
            heading = f"【作品消失·载荷异常】{md_name}"
            note = msg.NOTE_PAYLOAD_UNREADABLE.format(shape=shape)
            return subject, _card(heading, [_item(note)])
        template = msg.T_ALL_GONE if all_gone else msg.T_POST_REMOVED
        subject, heading = _titles(template, plain_name, md_name, count=len(removed))
        note = msg.NOTE_ALL_GONE if all_gone else ""
        return subject, _card(heading, _list_rows(removed), note)

    if kind is EventKind.REVIVED:
        subject, heading = _titles(msg.T_REVIVED, plain_name, md_name)
        rows = [
            _field(msg.ROW_TITLE, _title(payload.get("title"))),
            *_context_rows(payload),
        ]
        return subject, _card(heading, rows)

    if kind is EventKind.TITLE_CHANGED:
        subject, heading = _titles(msg.T_TITLE_CHANGED, plain_name, md_name)
        rows = [
            _field(msg.ROW_TITLE_OLD, _title(payload.get("old"))),
            _field(msg.ROW_TITLE_NEW, _title(payload.get("new"))),
            *_context_rows(payload),
        ]
        return subject, _card(heading, rows)

    if kind is EventKind.GAP_DETECTED:
        subject, heading = _titles(msg.T_GAP, plain_name, md_name)
        rows = [
            _field(msg.ROW_OLDEST, msg.fmt_time(_parse(payload.get("oldest_in_page")))),
            _field(
                msg.ROW_PREVIOUS, msg.fmt_time(_parse(payload.get("previous_newest")))
            ),
        ]
        note = msg.NOTE_GAP.format(fetch_count=payload.get("fetch_count"))
        return subject, _card(heading, rows, note)

    if kind is EventKind.NEVER_SEEN:
        subject, heading = _titles(msg.T_NEVER_SEEN, plain_name, md_name)
        rows = [
            _field(msg.ROW_ACCOUNT, _val(event.sec_user_id)),
            _field(msg.ROW_ROUNDS, _val(payload.get("rounds") or "?", 20)),
        ]
        return subject, _card(heading, rows, msg.NOTE_NEVER_SEEN)

    if kind is EventKind.ACCOUNT_FAILED:
        subject, heading = _titles(
            msg.T_ACCOUNT_FAILED,
            plain_name,
            md_name,
            fails=_val(payload.get("fails") or "?", 10),
        )
        # 失败次数已经写在标题里（"连续失败 N 次"），正文不再重复一遍
        rows = [
            _field(msg.ROW_CODE, _val(payload.get("code") or "?", 40)),
            _field(
                msg.ROW_DETAIL, _val(payload.get("message") or "（上游没有给出详情）")
            ),
        ]
        note = (
            msg.NOTE_CONFIG.format(code=_val(payload.get("code"), 40))
            if payload.get("config")
            else ""
        )
        return subject, _card(heading, rows, note)

    if kind is EventKind.ACCOUNT_RECOVERED:
        subject, heading = _titles(msg.T_ACCOUNT_RECOVERED, plain_name, md_name)
        rows = [
            _item(
                f"此前连续失败 {_val(payload.get('fails') or '?', 10)} 次，本轮已成功读取"
            )
        ]
        return subject, _card(heading, rows)

    if kind is EventKind.STALE_NO_UPDATE:
        days = _val(payload.get("days") or "?", 10)
        subject, heading = _titles(msg.T_STALE, plain_name, md_name, days=days)
        # "已 N 天没有新作品"就是标题本身，正文只留"这件事需要你做什么"
        rows = [_item("只提醒一次，该账号发布新作品后重新计时")]
        return subject, _card(heading, rows)

    if kind is EventKind.UPSTREAM_DEGRADED:
        subject = heading = msg.T_UPSTREAM
        rows = [
            _field(msg.ROW_CODE, _val(payload.get("code") or "?", 40)),
            _field(msg.ROW_ROUNDS, _val(payload.get("rounds") or "?", 10)),
            _field(
                msg.ROW_IMPACT,
                f"全局闸门关闭 {_val(payload.get('gate_seconds') or '?', 10)} 秒，本轮整体跳过",
            ),
        ]
        return subject, _card(heading, rows, msg.NOTE_GATE)

    if kind is EventKind.SELF_DEGRADED:
        subject = heading = msg.T_SELF
        reason = str(payload.get("reason") or "")
        rows = [
            _field(
                msg.ROW_REASON, msg.SELF_REASONS.get(reason) or _val(reason or "未知")
            )
        ]
        free, limit = payload.get("free_mb"), payload.get("free_limit_mb")
        if (
            reason == "disk_low"
            and isinstance(free, int)
            and isinstance(limit, int)
            and not isinstance(free, bool)
            and not isinstance(limit, bool)
        ):
            rows.append(
                _field(
                    msg.ROW_DETAIL,
                    msg.SELF_DETAIL_DISK.format(free_mb=free, limit_mb=limit),
                )
            )
        return subject, _card(heading, rows, msg.NOTE_SELF)

    # 其余静默事件（不推送）也留一个可读形态，便于写日志；载荷是外部数据，同样要压成
    # 一行并转义，否则标题里的反引号/竖线会直接改写这条日志的 markdown
    subject = f"[{kind.value}] {plain_name}"
    heading = f"[{kind.value}] {md_name}"
    return subject, _card(
        heading, [_item(f"载荷：{_val(payload, 200) if payload else '（无额外字段）'}")]
    )


def render_probe() -> Message:
    """渠道自测消息（`dywatch test-notify`）。

    走**同一套版式**：以前这段 markdown 是写在 `notifiers/base.py` 里的裸段落行，既不满足
    "每行都是块级元素"，也让版式有了第二个出处。
    """
    subject = msg.T_PROBE
    markdown = _card(subject, [_item(msg.PROBE_NOTE)])
    return Message(
        event=EventKind.INITIALIZED,
        severity="info",
        subject=subject,
        markdown=markdown,
        text=_to_text(markdown),
    )


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


__all__ = [
    "MAX_BODY_BYTES",
    "Message",
    "SEVERITY",
    "TITLE_CLIP",
    "render_event",
    "render_probe",
]
