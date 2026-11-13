"""通知渲染：缺值不显示、置顶/类型分支正确、凭据不会漏进消息。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from dywatch.messages import (
    fmt_count,
    fmt_duration,
    fmt_gap,
    md_escape,
    newest_post_at,
    one_line,
    strip_controls,
)
from dywatch.models import Content, Event, EventKind, Kind, PostState
from dywatch.render import render_event

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def make_content(**overrides) -> Content:
    base = dict(
        content_id="7496063824002403638",
        kind=Kind.VIDEO,
        title="有时候自己也乱套",
        web_url="https://www.douyin.com/video/7496063824002403638",
        created_at=NOW - timedelta(days=3, hours=4),
        duration_ms=38_634,
        digg_count=463_098,
        comment_count=16_949,
        share_count=274_103,
        collect_count=42_964,
        cover_url="https://p3-pc-sign.douyinpic.com/cover.jpeg",
        tags=("惊鸿一面",),
        author_uid="MS4wLjABAAAA4MjT",
    )
    base.update(overrides)
    return Content(**base)


def test_new_post_message_carries_the_useful_fields():
    event = Event(
        EventKind.NEW_POST,
        sec_user_id="u1",
        nickname="示例账号",
        content_id="7496063824002403638",
        payload={"content": make_content(), "gap_days": 3},
    )
    message = render_event(event, now=NOW)

    assert "示例账号" in message.subject
    assert "视频" in message.subject
    for fragment in ("标题", "类型", "发布", "时长", "数据", "话题"):
        assert fragment in message.markdown
    # 两处链接要写成可点的 markdown 链接（纯 URL 在钉钉里点不开）
    assert "[查看封面图](https://" in message.markdown
    assert (
        "[打开作品](https://www.douyin.com/video/7496063824002403638)"
        in message.markdown
    )
    assert "1 天" in message.markdown or "3 天" in message.markdown
    assert "46.3万" in message.markdown  # 点赞数按中文习惯缩写


def test_play_count_is_never_rendered_for_douyin():
    """抖音的 `stats.play_count` 实测恒为 null —— 列出来只会每轮都是空的。"""
    content = make_content(play_count=None)
    message = render_event(
        Event(
            EventKind.NEW_POST,
            sec_user_id="u1",
            nickname="A",
            payload={"content": content},
        ),
        now=NOW,
    )
    assert "播放" not in message.markdown


def test_missing_stats_omit_the_row_instead_of_printing_zero():
    content = make_content(
        digg_count=None, comment_count=None, share_count=None, collect_count=None
    )
    message = render_event(
        Event(
            EventKind.NEW_POST,
            sec_user_id="u1",
            nickname="A",
            payload={"content": content},
        ),
        now=NOW,
    )
    assert "数据" not in message.markdown
    # 每一项都缺时，行直接消失；不存在"点赞 0"这种平台没说过的话
    assert "点赞" not in message.markdown
    assert "评论" not in message.markdown


def test_live_kind_has_no_duration_row():
    content = make_content(kind=Kind.LIVE, duration_ms=None)
    message = render_event(
        Event(
            EventKind.NEW_POST,
            sec_user_id="u1",
            nickname="A",
            payload={"content": content},
        ),
        now=NOW,
    )
    assert "直播回放" in message.markdown
    assert "时长" not in message.markdown


def test_album_kind_reports_the_image_count():
    content = make_content(kind=Kind.IMAGE_ALBUM, image_count=9, duration_ms=None)
    message = render_event(
        Event(
            EventKind.NEW_POST,
            sec_user_id="u1",
            nickname="A",
            payload={"content": content},
        ),
        now=NOW,
    )
    assert "图文（9 张图）" in message.markdown


def test_all_gone_message_carries_the_verification_note():
    event = Event(
        EventKind.ALL_GONE,
        sec_user_id="u1",
        nickname="A",
        payload={
            "all_gone": True,
            "removed": [
                {
                    "content_id": "1",
                    "title": "走了",
                    "created_at": NOW.isoformat(),
                    "is_top": False,
                }
            ],
        },
    )
    message = render_event(event)
    assert "全部作品" in message.subject
    assert "核实" in message.markdown
    assert "走了" in message.markdown


def test_never_seen_message_points_at_the_id():
    event = Event(
        EventKind.NEVER_SEEN,
        sec_user_id="MS4wLjABAAAAnonexistent_xyz",
        nickname="A",
        payload={"rounds": 3},
    )
    message = render_event(event)
    assert "sec_user_id" in message.markdown
    assert "空列表" in message.markdown


def test_config_failure_message_says_it_is_not_a_network_problem():
    event = Event(
        EventKind.ACCOUNT_FAILED,
        sec_user_id="u1",
        nickname="A",
        payload={
            "fails": 5,
            "code": "FORBIDDEN_SCOPE",
            "message": "no scope",
            "config": True,
        },
    )
    message = render_event(event)
    assert "配置问题" in message.markdown


def test_plain_text_variant_drops_markdown_noise():
    message = render_event(
        Event(
            EventKind.NEW_POST,
            sec_user_id="u1",
            nickname="A",
            payload={"content": make_content()},
        ),
        now=NOW,
    )
    assert "**" not in message.text
    assert "\u200b" not in message.text


def test_webhook_payload_shape_is_stable():
    message = render_event(
        Event(
            EventKind.UPSTREAM_DEGRADED,
            sec_user_id="",
            payload={"code": "RATE_LIMITED"},
        ),
    )
    payload = message.as_dict()
    assert set(payload) == {
        "source",
        "event",
        "severity",
        "subject",
        "text",
        "markdown",
        "sec_user_id",
        "content_id",
    }
    assert payload["source"] == "dywatch"
    assert payload["severity"] == "error"


def test_md_escape_neutralises_mentions_and_markup():
    # @ 后面插一个零宽空格：字面上还看得见 @，但对方再也拼不出一个可点击的提及
    assert "@everyone" not in md_escape("@everyone")
    escaped = md_escape("*bold* [x](y)")
    assert "\\*" in escaped and "\\[" in escaped


def test_formatters():
    assert fmt_count(None) is None
    assert fmt_count(0) == "0"
    assert fmt_count(9_999) == "9999"  # 不到一万就如实显示，不做 k 级四舍五入
    assert fmt_count(12_345) == "1.2万"
    assert fmt_count(123_456_789) == "1.2亿"
    assert fmt_duration(None) is None
    assert fmt_duration(15_300) == "00:15"
    assert fmt_duration(3_930_000) == "1:05:30"
    assert fmt_gap(NOW - timedelta(days=3, hours=4), NOW) == "3 天 4 小时"
    assert fmt_gap(NOW - timedelta(hours=2, minutes=5), NOW) == "2 小时 5 分钟"


def test_newest_post_at_takes_the_latest_and_ignores_missing_dates():
    """置顶也算：它的发布时间是真的，只是被作者置顶了；缺时间的那条不参与比较。"""
    posts = (
        PostState(content_id="a", created_at=NOW - timedelta(days=30), is_top=True),
        PostState(content_id="b", created_at=NOW - timedelta(days=1)),
        PostState(content_id="c"),  # 抖音偶尔不返回 created_at
    )
    assert newest_post_at(posts) == NOW - timedelta(days=1)
    assert newest_post_at((PostState(content_id="c"),)) is None
    assert newest_post_at(()) is None


def test_one_line_strips_control_characters_and_truncates():
    """昵称/标题来自上游，进日志这类"一行一条"的出口前必须压平。

    回车与 ANSI 转义不会让程序崩，但能让终端里的日志显示成另一副样子——
    日志一旦可以被输入伪造，排障时就不能信它。
    """
    assert one_line("阿\r直\u001b[31m红\u001b[0m") == "阿 直 [31m红 [0m"
    assert one_line("第一行\n第二行") == "第一行 第二行"
    assert one_line("长" * 10, limit=5) == "长长长长…"
    assert one_line("普通昵称") == "普通昵称"
    assert strip_controls("x\x07y") == "x y"
    # 三个"行分隔符"（NEL / LS / PS）不是 C0 控制字符，但在渲染器里就是一次真实换行：
    # 卡片正文是 `- ` 列表，标题里混进一个 U+2028 就能把一条作品撑成两行。
    assert one_line("a\u2028b\u2029c\u0085d") == "a b c d"


def test_revived_and_title_changed_render_with_context():
    """这两个事件现在会推送，因此必须有像样的文案（种类 + 链接来自 diff 带上的 payload）。"""
    revived = render_event(
        Event(
            EventKind.REVIVED,
            sec_user_id="u1",
            nickname="阿直",
            content_id="p1",
            payload={
                "title": "老作品",
                "kind": "video",
                "web_url": "https://www.douyin.com/video/1",
            },
        )
    )
    assert "作品回归" in revived.subject and "阿直" in revived.subject
    assert "老作品" in revived.markdown and "视频" in revived.markdown
    assert "https://www.douyin.com/video/1" in revived.markdown

    changed = render_event(
        Event(
            EventKind.TITLE_CHANGED,
            sec_user_id="u1",
            nickname="阿直",
            content_id="p1",
            payload={
                "old": "旧标题",
                "new": "新标题",
                "kind": "image_album",
                "web_url": "",
            },
        )
    )
    assert "标题变更" in changed.subject
    assert "旧标题" in changed.markdown and "新标题" in changed.markdown
    assert "图文" in changed.markdown
    # 没有链接就不该凭空造一个
    assert "[打开作品]" not in changed.markdown


def test_revived_without_context_still_renders():
    """payload 缺字段（老数据、上游没给）时也不能炸，只是少几行。"""
    message = render_event(
        Event(
            EventKind.REVIVED,
            sec_user_id="u1",
            nickname="阿直",
            content_id="p1",
            payload={},
        )
    )
    assert "作品回归" in message.subject
    assert "(无标题)" in message.markdown


def test_removed_payload_of_the_wrong_shape_still_renders():
    """`removed` 不是列表或夹着非字典条目时也不能抛——渲染路径抛异常等于通知发不出去。"""
    for payload in (
        {"removed": None},
        {"removed": "不是列表"},
        {"removed": [None, 1, "x"]},
        {"removed": [{"title": "正常一条"}]},
    ):
        for kind in (EventKind.POST_REMOVED, EventKind.ALL_GONE):
            message = render_event(
                Event(
                    kind,
                    sec_user_id="u1",
                    nickname="阿直",
                    content_id="1",
                    payload=payload,
                )
            )
            assert message.subject and message.markdown
    # 正常条目照旧显示，畸形条目被跳过（计数也不含它们）
    message = render_event(
        Event(
            EventKind.POST_REMOVED,
            sec_user_id="u1",
            nickname="阿直",
            content_id="1",
            payload={"removed": [None, {"title": "正常一条"}]},
        )
    )
    assert "有 1 条作品已确认消失" in message.subject
    assert "正常一条" in message.markdown


def self_degraded(**payload):
    return render_event(
        Event(EventKind.SELF_DEGRADED, sec_user_id="", payload=payload), now=NOW
    )


def test_self_degraded_does_not_claim_that_pushing_stopped():
    """这条通知是 dywatch 自己发出来的——"已暂停推送"这句话本身就是自相矛盾的。

    那句文案是这个事件类型在还没有任何产生点时预留的；磁盘不足 / 目录不可写 / 状态库
    写不进去这几种情况下推送都没有停。
    """
    for reason in ("disk_low", "data_dir_read_only", "state_store_write_failed"):
        message = self_degraded(reason=reason)
        assert "已暂停推送" not in message.subject
        assert "已暂停推送" not in message.markdown


def test_self_degraded_says_what_is_wrong_in_words_not_in_codes():
    expected = {
        "disk_low": "状态库所在磁盘空间不足",
        "data_dir_read_only": "数据目录不可写",
        "state_store_write_failed": "状态库写入失败",
    }
    for reason, words in expected.items():
        markdown = self_degraded(reason=reason).markdown
        assert words in markdown
        assert reason not in markdown, "内部代码不该直接念给人听"


def test_self_degraded_disk_low_carries_the_numbers():
    markdown = self_degraded(reason="disk_low", free_mb=120, free_limit_mb=200).markdown

    assert "剩余 120 MB" in markdown
    assert "阈值 200 MB" in markdown
    assert "SELF_CHECK_FREE_MB" in markdown, "告诉人去调哪个配置项"


def test_self_degraded_survives_missing_or_malformed_readings():
    for payload in (
        {"reason": "disk_low"},
        {"reason": "disk_low", "free_mb": "lots", "free_limit_mb": None},
        {"reason": "disk_low", "free_mb": True, "free_limit_mb": True},
        {"reason": "something_new"},
        {},
    ):
        message = self_degraded(**payload)
        assert message.markdown
        assert "剩余" not in message.markdown or payload.get("free_mb") == 120


def test_self_degraded_shows_an_unknown_reason_as_is():
    """认不得的代码原样显示、不猜：将来加了新原因，这里不会悄悄变成错的说明。"""
    message = self_degraded(reason="something_new")
    # markdown 里下划线会被转义（`something\\_new`），所以拿纯文本版本比
    assert "原因：something_new" in message.text


def _new_post(content: Content, **extra) -> "Message":
    return render_event(
        Event(
            EventKind.NEW_POST,
            sec_user_id="u1",
            nickname="阿Z想开了",
            content_id=content.content_id,
            payload={"content": content, **extra},
        ),
        now=NOW,
    )


def test_new_post_card_is_a_compact_field_list():
    """钉钉 ActionCard 版：标题带昵称，字段收在一个列表里（卡片只有钉钉读，可以用 `- `），封面单独一块。"""
    message = _new_post(make_content(title="好Q弹的猪鼻子"))

    assert message.card_url == "https://www.douyin.com/video/7496063824002403638"
    blocks = message.card_markdown.split("\n\n")
    assert blocks[0] == "### 阿Z想开了 发布了新视频", (
        "标题必须带昵称，否则群里看不出是谁发的"
    )
    rows = blocks[1].split("\n")
    assert rows[0] == "- **标题**：好Q弹的猪鼻子"
    assert rows[1].startswith("- **发布时间**：") and rows[1].count(":") == 2
    assert rows[2].startswith("- **检测时间**：")
    assert len(rows) == 3, (
        "三个字段必须在同一个列表块里，不能每行一段；昵称已在标题里，正文不再重复"
    )
    assert blocks[2] == "![封面](https://p3-pc-sign.douyinpic.com/cover.jpeg)"
    assert len(blocks) == 3


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (EventKind.NEW_POST, {"content": make_content()}),
        (EventKind.REVIVED, {"title": "旧作", "kind": "video"}),
        (EventKind.TITLE_CHANGED, {"old": "旧", "new": "新", "kind": "video"}),
        (
            EventKind.GAP_DETECTED,
            {
                "oldest_in_page": "2026-01-01T00:00:00+00:00",
                "previous_newest": "2026-01-01T00:00:00+00:00",
                "fetch_count": 15,
            },
        ),
        (EventKind.NEVER_SEEN, {"rounds": 3}),
        (EventKind.ACCOUNT_FAILED, {"fails": 5, "code": "X", "message": "m"}),
        (EventKind.UPSTREAM_DEGRADED, {"code": "x", "rounds": 1, "gate_seconds": 60}),
        (
            EventKind.SELF_DEGRADED,
            {"reason": "disk_low", "free_mb": 1, "free_limit_mb": 2},
        ),
    ],
)
def test_field_labels_are_bold(kind, payload):
    """每个字段行都是 `**标签**：值`，**逐个事件**都要守。

    `**` 是钉钉与企业微信 markdown 的**交集**里唯一的强调手段（列表、表格、行内代码都
    至少有一边不支持），也是这张通知看着有没有层次的唯一来源。去掉它不会报错、不会丢字段，
    只是整条消息退回一坨平文——所以要有断言守着，别让哪次重构悄悄改回 `标签：值`。

    只测一条事件是不够的：`new_post` 的字段最多、用户最先看到，它的一条行若退回平文，
    别的用例全绿也发现不了。
    """
    message = render_event(
        Event(kind, sec_user_id="u1", nickname="阿Z想开了", payload=payload), now=NOW
    )

    # 引用块（`> `）里的说明文字含全角冒号但不是字段；标题行也不是
    rows = [
        line
        for line in message.markdown.split("\n")
        if "：" in line and not line.startswith(("### ", "> "))
    ]
    assert rows, f"{kind.value} 应该有字段行"
    for line in rows:
        assert re.fullmatch(r"\*\*[^*\n]+\*\*：.*", line), (
            f"{kind.value} 的字段行没加粗：{line!r}"
        )


def test_card_list_survives_a_hostile_title():
    """标题里的 `-`、`**`、换行都不能把卡片的列表结构撑坏。

    抖音标题真的会带这些字符（`- **加粗**` 是最常见的组合）。换行被 `one_line` 压平、
    `-` 与 `*` 被 `md_escape` 转义之后，四行仍然必须是四个列表项，不能多出一行。
    """
    message = _new_post(make_content(title="- **加粗**\n第二行"))

    list_rows = [r for r in message.card_markdown.split("\n") if r.startswith("- ")]
    assert len(list_rows) == 3, f"列表被标题撑成了 {len(list_rows)} 行"
    assert all(r.startswith("- **") for r in list_rows)
    assert "\\- \\*\\*加粗\\*\\* 第二行" in message.card_markdown


def test_plain_text_keeps_literal_asterisks():
    """纯文本版只去掉**版面自己加**的 `**`，标题里真的写了 `a**b` 要原样留着。

    这条守的是 `_to_text` 的顺序：先去 `**` 再反转义。反过来做会把 `a\\*\\*b` 反转义成
    `a**b` 之后一并删掉，用户看到的标题就变成 `ab`。
    """
    message = _new_post(make_content(title="a**b"))

    assert "a**b" in message.text
    assert "**标题**" not in message.text, "版面的加粗标记仍然要清掉"
    assert "**" not in message.text.replace("a**b", "")


def test_plain_text_keeps_asterisks_inside_urls():
    """URL 里的 `**` 是地址的一部分，纯文本版删掉它就换成了另一个地址（点开 404）。

    `_link` 把 URL 原样写进正文，不过 `md_escape`（否则 `?a=1&b=2` 里的字符会被转义成
    别的东西）。所以"全局删掉 `**`"会连地址一起改：`a**b.jpeg` → `ab.jpeg`。
    只剥行首的字段标签才安全。
    """
    message = _new_post(make_content(cover_url="https://p3.douyinpic.com/a**b.jpeg"))

    assert "a**b.jpeg" in message.text
    assert "查看封面图 <https://p3.douyinpic.com/a**b.jpeg>" in message.text


def test_new_post_card_falls_back_to_a_video_url_and_otherwise_to_markdown():
    # 没有作品链接：视频可以按 id 拼出地址（旧脚本就是这么跳的）
    video = _new_post(make_content(web_url=""))
    assert video.card_url == "https://www.douyin.com/video/7496063824002403638"
    # 图文没有可靠的拼法 → 不出卡片，钉钉渠道会退回 markdown
    album = _new_post(make_content(web_url="", kind=Kind.IMAGE_ALBUM, image_count=3))
    assert album.card_markdown is None and album.card_url is None


def test_card_fields_do_not_leak_into_the_webhook_payload():
    """卡片只是钉钉渠道挑版式用的，webhook 载荷契约不变。"""
    payload = _new_post(make_content()).as_dict()
    assert "card_markdown" not in payload and "card_url" not in payload


def test_other_events_have_no_card_and_no_bullets():
    message = render_event(
        Event(
            EventKind.POST_REMOVED,
            sec_user_id="u1",
            nickname="阿Z想开了",
            payload={
                "removed": [{"title": "甲", "created_at": "2026-11-03T12:44:00+00:00"}]
            },
        ),
        now=NOW,
    )
    assert message.card_markdown is None
    assert "- " not in message.markdown.replace("\\-", "")
