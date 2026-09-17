"""通知渲染：缺值不显示、置顶/类型分支正确、凭据不会漏进消息。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    for fragment in ("标题", "类型", "发布", "时长", "数据", "话题", "封面", "链接"):
        assert fragment in message.markdown
    assert "1 天" in message.markdown or "3 天" in message.markdown
    assert "46.3万" in message.markdown  # 点赞数按中文习惯缩写


def test_play_count_is_never_rendered_for_douyin():
    """抖音的 `stats.play_count` 实测恒为 null —— 列出来只会每轮都是空的。"""
    content = make_content(play_count=None)
    message = render_event(
        Event(EventKind.NEW_POST, sec_user_id="u1", nickname="A", payload={"content": content}),
        now=NOW,
    )
    assert "播放" not in message.markdown


def test_missing_stats_omit_the_row_instead_of_printing_zero():
    content = make_content(digg_count=None, comment_count=None, share_count=None, collect_count=None)
    message = render_event(
        Event(EventKind.NEW_POST, sec_user_id="u1", nickname="A", payload={"content": content}),
        now=NOW,
    )
    assert "数据" not in message.markdown
    # 每一项都缺时，行直接消失；不存在"点赞 0"这种平台没说过的话
    assert "点赞" not in message.markdown
    assert "评论" not in message.markdown


def test_live_kind_has_no_duration_row():
    content = make_content(kind=Kind.LIVE, duration_ms=None)
    message = render_event(
        Event(EventKind.NEW_POST, sec_user_id="u1", nickname="A", payload={"content": content}),
        now=NOW,
    )
    assert "直播回放" in message.markdown
    assert "时长" not in message.markdown


def test_album_kind_reports_the_image_count():
    content = make_content(kind=Kind.IMAGE_ALBUM, image_count=9, duration_ms=None)
    message = render_event(
        Event(EventKind.NEW_POST, sec_user_id="u1", nickname="A", payload={"content": content}),
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
            "removed": [{"content_id": "1", "title": "走了", "created_at": NOW.isoformat(),
                         "is_top": False}],
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
        payload={"fails": 5, "code": "FORBIDDEN_SCOPE", "message": "no scope", "config": True},
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
        Event(EventKind.UPSTREAM_DEGRADED, sec_user_id="", payload={"code": "RATE_LIMITED"}),
    )
    payload = message.as_dict()
    assert set(payload) == {
        "source", "event", "severity", "subject", "text", "markdown", "sec_user_id", "content_id"
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


def test_revived_and_title_changed_render_with_context():
    """这两个事件现在会推送，因此必须有像样的文案（种类 + 链接来自 diff 带上的 payload）。"""
    revived = render_event(
        Event(
            EventKind.REVIVED, sec_user_id="u1", nickname="阿直", content_id="p1",
            payload={"title": "老作品", "kind": "video", "web_url": "https://www.douyin.com/video/1"},
        )
    )
    assert "作品回归" in revived.subject and "阿直" in revived.subject
    assert "老作品" in revived.markdown and "视频" in revived.markdown
    assert "https://www.douyin.com/video/1" in revived.markdown

    changed = render_event(
        Event(
            EventKind.TITLE_CHANGED, sec_user_id="u1", nickname="阿直", content_id="p1",
            payload={"old": "旧标题", "new": "新标题", "kind": "image_album", "web_url": ""},
        )
    )
    assert "标题变更" in changed.subject
    assert "旧标题" in changed.markdown and "新标题" in changed.markdown
    assert "图文" in changed.markdown
    # 没有链接就不该凭空造一个
    assert "链接" not in changed.markdown


def test_revived_without_context_still_renders():
    """payload 缺字段（老数据、上游没给）时也不能炸，只是少几行。"""
    message = render_event(
        Event(EventKind.REVIVED, sec_user_id="u1", nickname="阿直", content_id="p1", payload={})
    )
    assert "作品回归" in message.subject
    assert "(无标题)" in message.markdown


def test_revived_and_title_changed_render_with_context():
    """这两个事件现在会推送，因此必须有像样的文案（种类 + 链接来自 diff 带上的 payload）。"""
    revived = render_event(
        Event(
            EventKind.REVIVED, sec_user_id="u1", nickname="阿直", content_id="p1",
            payload={"title": "老作品", "kind": "video", "web_url": "https://www.douyin.com/video/1"},
        )
    )
    assert "作品回归" in revived.subject and "阿直" in revived.subject
    assert "老作品" in revived.markdown and "视频" in revived.markdown
    assert "https://www.douyin.com/video/1" in revived.markdown

    changed = render_event(
        Event(
            EventKind.TITLE_CHANGED, sec_user_id="u1", nickname="阿直", content_id="p1",
            payload={"old": "旧标题", "new": "新标题", "kind": "image_album", "web_url": ""},
        )
    )
    assert "标题变更" in changed.subject
    assert "旧标题" in changed.markdown and "新标题" in changed.markdown
    assert "图文" in changed.markdown
    # 没有链接就不该凭空造一个
    assert "链接" not in changed.markdown


def test_revived_without_context_still_renders():
    """payload 缺字段（老数据、上游没给）时也不能炸，只是少几行。"""
    message = render_event(
        Event(EventKind.REVIVED, sec_user_id="u1", nickname="阿直", content_id="p1", payload={})
    )
    assert "作品回归" in message.subject
    assert "(无标题)" in message.markdown


def test_removed_payload_of_the_wrong_shape_still_renders():
    """`removed` 不是列表或夹着非字典条目时也不能抛——渲染路径抛异常等于通知发不出去。"""
    for payload in ({"removed": None}, {"removed": "不是列表"}, {"removed": [None, 1, "x"]},
                    {"removed": [{"title": "正常一条"}]}):
        for kind in (EventKind.POST_REMOVED, EventKind.ALL_GONE):
            message = render_event(
                Event(kind, sec_user_id="u1", nickname="阿直", content_id="1", payload=payload)
            )
            assert message.subject and message.markdown
    # 正常条目照旧显示，畸形条目被跳过（计数也不含它们）
    message = render_event(
        Event(
            EventKind.POST_REMOVED, sec_user_id="u1", nickname="阿直", content_id="1",
            payload={"removed": [None, {"title": "正常一条"}]},
        )
    )
    assert "有 1 条作品已确认消失" in message.subject
    assert "正常一条" in message.markdown


def test_removed_payload_of_the_wrong_shape_still_renders():
    """`removed` 不是列表或夹着非字典条目时也不能抛——渲染路径抛异常等于通知发不出去。"""
    for payload in ({"removed": None}, {"removed": "不是列表"}, {"removed": [None, 1, "x"]},
                    {"removed": [{"title": "正常一条"}]}):
        for kind in (EventKind.POST_REMOVED, EventKind.ALL_GONE):
            message = render_event(
                Event(kind, sec_user_id="u1", nickname="阿直", content_id="1", payload=payload)
            )
            assert message.subject and message.markdown
    # 正常条目照旧显示，畸形条目被跳过（计数也不含它们）
    message = render_event(
        Event(
            EventKind.POST_REMOVED, sec_user_id="u1", nickname="阿直", content_id="1",
            payload={"removed": [None, {"title": "正常一条"}]},
        )
    )
    assert "有 1 条作品已确认消失" in message.subject
    assert "正常一条" in message.markdown
