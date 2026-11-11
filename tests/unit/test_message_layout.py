"""通知版式的硬约束（钉钉 ∩ 企业微信 的 markdown 子集）。

这些不是风格偏好，是**在真机上会坏**的东西：

- 钉钉把单个换行当软换行折叠（官方建议 `\\n` 前后各加两个空格才算硬换行）→ 靠单个换行拼出来的
  相邻行会把整条通知挤成一坨，第一版就是这样"太乱"的；所以**每个字段自成一段、段间空一行**
  （最早的 bash 版就是这样推的：没有列表圆点，清爽）；
- 钉钉不支持行内代码 / 表格 / 代码块 / 分隔线 → 用了就会原样显示反引号和竖线；
- 企业微信 markdown 正文上限 **4096 字节** → 超了整条消息发不出去；
- 转义与截断的顺序：先转义再截断会切出落单的反斜杠（`webui._label` 踩过同一个坑）。

所以这里对所有事件 × 畸形载荷做一遍"版式不变量"检查。改文案或版式时它会先红。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from dywatch.models import Content, Event, EventKind, Kind
from dywatch.render import MAX_BODY_BYTES, TITLE_CLIP, render_event

#: 每个事件都拿这些载荷跑一遍：正常值、缺值、类型不对、以及带 markdown 元字符的
HOSTILE: list[dict] = [
    {},
    {"title": None, "old": None, "new": None},
    {"removed": None},
    {"removed": [None, 1, "不是字典"]},
    {
        "title": "带 * 星号与 [方括号](x) 还有 `反引号`",
        "kind": "不存在的类型",
        "web_url": None,
    },
    {"web_url": "javascript:alert(1)", "kind": "video"},
    {"web_url": "https://www.douyin.com/video/1)", "kind": "video"},
    {
        "code": None,
        "message": None,
        "fails": None,
        "rounds": None,
        "days": None,
        "reason": None,
        "gate_seconds": None,
        "fetch_count": None,
    },
    {
        "code": "403_FORBIDDEN_SCOPE",
        "message": "上游返回的原始报错\n第二行",
        "config": True,
    },
    {"content": None},
    {
        "content": Content(
            content_id="1",
            kind=Kind.VIDEO,
            title="x" * 500,
            web_url="https://www.douyin.com/video/1",
            created_at=datetime.now(timezone.utc),
            cover_url="https://p3-pc.douyinpic.com/cover.jpg",
            tags=("话题",) * 12,
        )
    },
    {
        "content": Content(
            content_id="1",
            kind=Kind.IMAGE_ALBUM,
            title="。",
            image_count=9,
            web_url="https://www.douyin.com/video/1",
            created_at=None,
        )
    },
    {
        "removed": [
            {
                "content_id": str(i),
                "title": "条" * 300,
                "created_at": "2026-09-19T00:20:00+00:00",
                "is_top": i % 2 == 0,
            }
            for i in range(30)
        ]
    },
]

KINDS = list(EventKind)


def unescaped(markdown: str, char: str) -> bool:
    r"""正文里有没有**没被转义**的 `char`。

    `\`` 不算：标题里那个反引号被转义了，它开不了代码段。要区分"用了不支持的语法"和
    "把用户内容里的元字符转义了"，否则标题里出现一个反引号就会被误判成版式违规。
    """
    return re.search(rf"(?<!\\)(?:\\\\)*{re.escape(char)}", markdown) is not None


def make(kind: EventKind, payload: dict) -> str:
    event = Event(
        kind, sec_user_id="MS4wLjABAAAAxxxx", nickname="ζωευώ", payload=payload
    )
    return render_event(
        event, now=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    ).markdown


@pytest.mark.parametrize("kind", KINDS)
def test_every_row_is_its_own_paragraph(kind):
    """每个字段自成一段：任意两个非空行之间必须隔着空行，且不出现列表圆点。

    相邻两行只隔一个换行，钉钉会把它们折叠成一行——这正是第一版"推送内容挤成一坨"的根因；
    列表圆点则是后来"前面好几个点、看着乱"的来源（用户内容里的 `-` 已被转义，不会误伤）。
    """
    for payload in HOSTILE:
        previous_blank = True
        for line in make(kind, payload).split("\n"):
            blank = not line.strip()
            assert not (blank is False and previous_blank is False), (
                f"{kind.value} 有两行紧挨着（会被钉钉折叠）：{line[:60]!r}"
            )
            assert not line.startswith(("- ", "* ", "+ ")), (
                f"{kind.value} 出现列表圆点：{line[:60]!r}"
            )
            previous_blank = blank


@pytest.mark.parametrize("kind", KINDS)
def test_no_row_has_an_empty_value(kind):
    """字段行的值不能是空的——`已出现轮次：` 这种行对读的人毫无意义。

    踩过：早期 `_clip` 里写的是 `raw or ""`，于是 `rounds=0`、`fails=False` 这类**有效值**
    被悄悄吞成空串，渲染出一行只有标签的字段。
    """
    for payload in HOSTILE:
        for line in make(kind, payload).splitlines():
            assert not line.rstrip().endswith("："), (
                f"{kind.value} 出现空值字段行：{line!r}"
            )


def test_channel_probe_uses_the_same_layout():
    """`dywatch test-notify` 的自检消息也走同一套版式（它曾经是手写的裸段落行）。"""
    from dywatch.render import render_probe

    probe = render_probe()
    assert probe.markdown.startswith("### ")
    assert "\n- " not in probe.markdown, "自检消息也不该有列表圆点"
    assert "\n\n" in probe.markdown, "标题与正文之间要空一行"
    assert "**" not in probe.text and "](" not in probe.text


@pytest.mark.parametrize("kind", KINDS)
def test_only_constructs_both_channels_support(kind):
    """只用钉钉与企业微信都支持的语法；钉钉不支持的（行内代码）一律不出现。"""
    for payload in HOSTILE:
        md = make(kind, payload)
        assert not unescaped(md, "`"), f"{kind.value} 用了行内代码（钉钉不支持）"
        assert not unescaped(md, "|"), f"{kind.value} 用了表格（两个渠道都不支持）"
        assert "```" not in md, f"{kind.value} 用了代码块"
        assert not any(line.startswith("---") for line in md.split("\n")), (
            f"{kind.value} 用了分隔线"
        )
        # 斜体只在钉钉支持，企业微信 markdown 不支持 → 我们不产出单个未转义的星号
        assert not re.search(r"(?<!\\)(?<!\*)\*(?!\*)", md), f"{kind.value} 用了斜体"


@pytest.mark.parametrize("kind", KINDS)
def test_no_line_ends_with_a_dangling_escape(kind):
    """行尾不能是奇数个反斜杠（那是"转义写了一半"）。

    只要"先截断原始值、再转义"的顺序被破坏，标题被切在 `\\*` 中间就会留下这个形态。
    """
    for payload in HOSTILE:
        for line in make(kind, payload).split("\n"):
            trailing = len(line) - len(line.rstrip("\\"))
            assert trailing % 2 == 0, f"{kind.value} 行尾有落单的反斜杠：{line[-40:]!r}"


@pytest.mark.parametrize("kind", KINDS)
def test_body_stays_within_the_channel_byte_limit(kind):
    """正文必须 ≤ `MAX_BODY_BYTES`（企业微信 4096 字节是硬上限，超了发不出去）。"""
    for payload in HOSTILE:
        md = make(kind, payload)
        assert len(md.encode("utf-8")) <= MAX_BODY_BYTES, f"{kind.value} 正文超长"


def test_a_long_title_is_clipped_before_escaping():
    """超长标题按原始值截断，且截断点不会切坏转义序列。"""
    title = "あ" * TITLE_CLIP + "*"  # 截断点正好落在会被转义的字符附近
    event = Event(
        EventKind.NEW_POST,
        sec_user_id="u1",
        nickname="阿直",
        payload={
            "content": Content(
                content_id="1",
                kind=Kind.VIDEO,
                title=title,
                web_url="https://www.douyin.com/video/1",
                created_at=None,
            )
        },
    )
    md = render_event(event).markdown
    assert "…" in md, "超长标题要截断"
    assert title not in md, "不该原样塞进去"
    row = next(line for line in md.split("\n") if line.startswith("标题："))
    assert not row.rstrip().endswith("\\"), "截断点不能留下落单的反斜杠"


def test_subject_stays_plain_text():
    """`subject` 会直接进通知栏 / Bark 标题 / Server 酱标题——那边不做 markdown 解析。

    所以它要的是"纯文本"，而不是"转义过的文本"：`**`、`\\*` 这种残留会原样显示给用户
    （这一条正是改版时踩到的：subject 一度拿转义后的昵称拼，通知栏里出现了 `\\*`）。
    """
    for kind in KINDS:
        for payload in HOSTILE:
            event = Event(
                kind, sec_user_id="u1", nickname="阿直*直#话题", payload=payload
            )
            subject = render_event(event).subject
            assert subject.strip()
            assert "\\" not in subject, (
                f"{kind.value} 的 subject 里有转义残留：{subject!r}"
            )
            assert "\n" not in subject, "通知栏标题必须是单行"
            assert "\u200b" not in subject, "零宽空格只对 markdown 渠道有意义"
            assert len(subject) <= 120, f"{kind.value} 的 subject 过长：{len(subject)}"


@pytest.mark.parametrize("kind", KINDS)
def test_plain_text_variant_is_free_of_markdown(kind):
    """纯文本形态（Telegram/Bark/webhook）里不能再有 markdown 残渣。"""
    for payload in HOSTILE:
        event = Event(kind, sec_user_id="u1", nickname="阿直", payload=payload)
        text = render_event(event).text
        assert "**" not in text
        assert "# " not in text and not text.startswith("#")
        assert "](http" not in text, "链接要展开成 `文字 <url>`，不该留下 markdown 语法"
        assert "\u200b" not in text, "零宽空格只对 markdown 渠道有意义"
        for line in text.split("\n"):
            trailing = len(line) - len(line.rstrip("\\"))
            assert trailing % 2 == 0, f"{kind.value} 纯文本行尾有落单的反斜杠"


def test_new_post_card_survives_hostile_payloads():
    """钉钉 ActionCard 版：载荷再畸形也不抛异常；有卡片时一定带着合法的跳转地址。"""
    from dywatch.render import _URL_OK

    for payload in HOSTILE:
        event = Event(
            EventKind.NEW_POST, sec_user_id="u1", nickname="阿直*#", payload=payload
        )
        message = render_event(
            event, now=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        )
        if message.card_markdown is None:
            assert message.card_url is None
            continue
        assert message.card_url and _URL_OK.match(message.card_url)
        assert message.card_markdown.startswith("### 新")
        for line in message.card_markdown.split("\n"):
            trailing = len(line) - len(line.rstrip("\\"))
            assert trailing % 2 == 0, f"卡片行尾有落单的反斜杠：{line[-40:]!r}"
