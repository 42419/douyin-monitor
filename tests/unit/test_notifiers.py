"""渠道装配与载荷形状。

载荷形状是**对外契约**（钉钉/企业微信/Bark/Server 酱/Telegram 各要什么字段、URL 长什么样），
改坏了不会让测试变红、只会让推送静默失败，所以这里逐个钉住。

顺带覆盖这次新加的两件事：**同一类型多实例**、以及**一个渠道失败不连坐其它渠道**。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from dywatch.models import Event, EventKind
from dywatch.notifiers import (
    BarkChannel,
    DingTalkChannel,
    Notifier,
    ServerChanChannel,
    TelegramChannel,
    WebhookChannel,
    WeComChannel,
    build_channels,
)
from dywatch.render import Message, render_event
from dywatch.settings import load_settings

TWO_DINGTALK = """
dingtalk name=市场部 token=tok-A secret=SECa
dingtalk token=tok-B secret=SECb
telegram bot_token=1:AA chat_id=-100
"""


def make_message(kind: EventKind = EventKind.NEW_POST) -> Message:
    event = Event(
        kind,
        sec_user_id="u1",
        nickname="阿直",
        content_id="p1",
        payload={
            "title": "老标题",
            "new": "新标题",
            "kind": "video",
            "web_url": "https://www.douyin.com/video/1",
        },
    )
    return render_event(event, now=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc))


def settings_with(**env: Any) -> Any:
    base = {"DTK_API_KEY": "dtk_x"}
    base.update(env)
    return load_settings(None, environ=base)


# ------------------------------------------------------------------ 载荷形状
def test_dingtalk_payload_matches_the_official_shape():
    message = make_message()
    url, body = DingTalkChannel(token="tok", secret="").request(message)

    assert url == "https://oapi.dingtalk.com/robot/send?access_token=tok"
    assert body["msgtype"] == "markdown"
    assert body["markdown"] == {"title": message.subject, "text": message.markdown}
    assert "at" not in body, "没配 @人就别发空数组"


def test_dingtalk_signs_when_a_secret_is_given_and_mentions_people():
    message = make_message()
    url, body = DingTalkChannel(
        token="tok", secret="SECx", at_mobiles=("13800000000",)
    ).request(message)

    assert "timestamp=" in url and "sign=" in url, "加签是往 URL 上追加参数"
    assert body["at"] == {"atMobiles": ["13800000000"], "isAtAll": False}


def test_wecom_payload():
    message = make_message()
    url, body = WeComChannel(key="KEY-1").request(message)

    assert url == "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=KEY-1"
    assert body == {"msgtype": "markdown", "markdown": {"content": message.markdown}}


@pytest.mark.parametrize(
    ("kind", "level"),
    [
        (EventKind.ALL_GONE, "timeSensitive"),  # error
        (EventKind.POST_REMOVED, "active"),  # warning
        (EventKind.NEW_POST, "passive"),  # info
    ],
)
def test_bark_level_follows_severity(kind, level):
    """Bark 的 level 决定"会不会响"：error 穿透专注模式，info 连屏幕都不点亮。"""
    message = make_message(kind)
    url, body = BarkChannel(server="https://api.day.app/", device_key="dev").request(
        message
    )

    assert url == "https://api.day.app/dev", "服务器地址结尾的斜杠不该叠成两条"
    assert body["level"] == level
    assert body["title"] == message.subject and body["body"] == message.text


def test_serverchan_truncates_the_title_to_32_bytes():
    message = make_message()
    url, body = ServerChanChannel(sendkey="SCT123").request(message)

    assert url == "https://sctapi.ftqq.com/SCT123.send"
    assert len(body["title"].encode("utf-8")) <= 32, (
        "Server 酱的标题上限是 **32 字节**，不是 32 个字符"
    )
    assert body["desp"] == message.markdown


def test_serverchan_byte_truncation_never_splits_a_character():
    """按字节切时不能把一个多字节字符切成两半（那会变成替换字符，肉眼看着就是乱码）。"""
    import dataclasses

    message = make_message()
    long_subject = "【新作品】" + "很长的中文昵称" * 3 + " 发布了新视频"
    long_message = dataclasses.replace(message, subject=long_subject)
    body = ServerChanChannel(sendkey="S").request(long_message)[1]

    assert len(body["title"].encode("utf-8")) <= 32
    body["title"].encode("utf-8").decode("utf-8")  # 切坏了这里就会抛 UnicodeDecodeError
    assert "\ufffd" not in body["title"]


def test_telegram_keeps_the_bot_token_in_the_path():
    message = make_message()
    url, body = TelegramChannel(bot_token="1:AA", chat_id="-100").request(message)

    assert url == "https://api.telegram.org/bot1:AA/sendMessage"
    assert body["chat_id"] == "-100" and body["text"] == message.text
    assert body["disable_web_page_preview"] is True


def test_webhook_sends_our_own_contract():
    message = make_message()
    url, body = WebhookChannel(url="https://example.com/hook").request(message)

    assert url == "https://example.com/hook"
    assert body == message.as_dict()
    assert set(body) >= {"source", "event", "severity", "subject", "text", "markdown"}


# ------------------------------------------------------------------ 装配
def test_targets_path_builds_one_channel_per_instance_in_order():
    built = build_channels(settings_with(NOTIFY_TARGETS=TWO_DINGTALK))

    assert [c.name for c in built] == ["市场部", "dingtalk-2", "telegram"]
    assert [type(c).__name__ for c in built] == [
        "DingTalkChannel",
        "DingTalkChannel",
        "TelegramChannel",
    ]
    assert built[0].token == "tok-A" and built[1].token == "tok-B", (
        "两个实例各用各的凭据"
    )
    assert built[0].secret == "SECa" and built[1].secret == "SECb"


def test_targets_path_passes_the_global_at_mobiles_to_dingtalk():
    built = build_channels(
        settings_with(NOTIFY_TARGETS=TWO_DINGTALK, AT_MOBILES="138,139")
    )

    assert built[0].at_mobiles == ("138", "139")


def test_legacy_path_still_works_untouched():
    built = build_channels(
        settings_with(
            NOTIFY_CHANNELS="dingtalk,telegram",
            DINGTALK_TOKEN="old-tok",
            DINGTALK_SECRET="SECold",
            TELEGRAM_BOT_TOKEN="1:BB",
            TELEGRAM_CHAT_ID="42",
        )
    )

    assert [(c.name, type(c).__name__) for c in built] == [
        ("dingtalk", "DingTalkChannel"),
        ("telegram", "TelegramChannel"),
    ]


def test_targets_win_when_both_writings_are_configured():
    settings = settings_with(
        NOTIFY_TARGETS="wecom key=new-key",
        NOTIFY_CHANNELS="dingtalk",
        DINGTALK_TOKEN="old-tok",
        DINGTALK_SECRET="SECold",
    )

    assert [c.name for c in build_channels(settings)] == ["wecom"]
    assert any("新写法生效" in note for note in settings.warnings())


def test_broken_targets_never_fall_back_to_the_legacy_credentials():
    """新写法写错了就该**没有渠道**（进程也起不来），而不是偷偷用旧的凭据发出去。"""
    settings = settings_with(
        NOTIFY_TARGETS="dingtalk secret=SECx",  # 少了 token
        NOTIFY_CHANNELS="dingtalk",
        DINGTALK_TOKEN="old-tok",
        DINGTALK_SECRET="SECold",
    )

    assert build_channels(settings) == []
    assert any("NOTIFY_TARGETS" in error for error in settings.validate())


def test_malformed_legacy_entries_are_skipped_without_taking_others_down():
    built = build_channels(
        settings_with(NOTIFY_CHANNELS="dingtalk,wecom", DINGTALK_TOKEN="t")
    )
    # 钉钉缺 secret 也能发（不加签），wecom 没 key 就被跳过
    assert [c.name for c in built] == ["dingtalk"]


# ------------------------------------------------------------------ 投递
class _Channel:
    """假渠道：只记下"发过"，或者按要求失败。"""

    def __init__(self, name: str, *, boom: bool = False) -> None:
        self.name = name
        self.boom = boom
        self.sent: list[Message] = []

    async def send(self, message: Message, client: Any) -> None:
        if self.boom:
            raise RuntimeError("boom")
        self.sent.append(message)


async def test_delivery_isolates_a_broken_channel_and_names_the_instances():
    good_a, bad, good_b = (
        _Channel("市场部"),
        _Channel("坏的那个", boom=True),
        _Channel("dingtalk-2"),
    )
    notifier = Notifier([good_a, bad, good_b], gap_seconds=0)

    delivery = await notifier.send(make_message())

    assert delivery.sent == ["市场部", "dingtalk-2"], "好的两个都要收到"
    assert list(delivery.failed) == ["坏的那个"], "失败的按实例名记下来"
    assert "boom" in delivery.failed["坏的那个"]
    assert len(good_a.sent) == 1 and len(good_b.sent) == 1
    assert delivery.delivered and not delivery.ok


async def test_names_are_unique_so_failed_can_be_a_dict():
    """`failed` 是字典：同名实例会互相覆盖，所以解析阶段就要求名字唯一。"""
    settings = settings_with(
        NOTIFY_TARGETS="dingtalk token=a secret=x\ndingtalk token=b secret=y"
    )
    names = [c.name for c in build_channels(settings)]

    assert len(names) == len(set(names)) == 2


# ------------------------------------------------------------------ 200 但业务失败
class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls = 0

    async def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        self.calls += 1
        return self.response


@pytest.mark.parametrize(
    ("channel", "body"),
    [
        (WeComChannel(key="bad"), {"errcode": 93000, "errmsg": "invalid webhook key"}),
        (
            DingTalkChannel(token="t"),
            {"errcode": 310000, "errmsg": "keywords not in content"},
        ),
        (BarkChannel(device_key="d"), {"code": 400, "message": "bad device token"}),
        (ServerChanChannel(sendkey="s"), {"code": 40001, "message": "bad sendkey"}),
    ],
)
async def test_a_200_with_a_business_error_is_a_failure_not_a_success(channel, body):
    """这些渠道业务失败时**也回 200**，只看状态码会把"根本没发出去"记成已送达。

    `delivery_json`、`test-notify` 的 ✓ 和退出码都会跟着说谎——正是这个模块声称要防的事。
    """
    client = _FakeClient(_FakeResponse(200, body))

    with pytest.raises(RuntimeError) as caught:
        await channel.send(make_message(), client)  # type: ignore[arg-type]

    assert channel.error_field in str(caught.value)


@pytest.mark.parametrize(
    ("channel", "body"),
    [
        (WeComChannel(key="k"), {"errcode": 0, "errmsg": "ok"}),
        (DingTalkChannel(token="t"), {"errcode": 0, "errmsg": "ok"}),
        (BarkChannel(device_key="d"), {"code": 200, "message": "success"}),
        (ServerChanChannel(sendkey="s"), {"code": 0, "message": ""}),
        # 自建反代之类可能回一个没有这些字段的 200：**不算失败**，否则会误判成故障
        (WeComChannel(key="k"), {"ok": True}),
        (WebhookChannel(url="https://x"), "not json at all"),
        (TelegramChannel(bot_token="1:A"), {"ok": True, "result": {}}),
    ],
)
async def test_a_healthy_200_is_still_a_success(channel, body):
    client = _FakeClient(_FakeResponse(200, body))

    await channel.send(make_message(), client)  # type: ignore[arg-type]

    assert client.calls == 1


async def test_a_non_json_200_body_does_not_break_a_channel_that_has_no_error_field():
    """读不出 JSON 时不能把一条已经送达的消息判成失败。"""
    client = _FakeClient(_FakeResponse(200, ValueError("no json")))
    await DingTalkChannel(token="t").send(make_message(), client)  # type: ignore[arg-type]
