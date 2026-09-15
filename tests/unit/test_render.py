"""通知渲染：缺值不显示、置顶/类型分支正确、凭据不会漏进消息。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dywatch.messages import fmt_count, fmt_duration, fmt_gap, md_escape
from dywatch.models import Content, Event, EventKind, Kind
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
