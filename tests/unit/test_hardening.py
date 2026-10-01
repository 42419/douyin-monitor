"""对照这次修复的回归测试（不依赖 `tmp_path`，因此在任何环境都能跑）。

覆盖的每一处都是"安静地做错事"那类缺陷：配置被吃掉、假成功、全局停摆、
永久漏判、以及读一眼看不出来的类型问题。需要建目录的用例放在各自原有的测试文件里。
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any

import pytest

from dywatch.dtk import MonitorError, parse_page
from dywatch.models import Content, Event, EventKind, Kind
from dywatch.notifiers import (
    BarkChannel,
    DingTalkChannel,
    ServerChanChannel,
    TelegramChannel,
    WeComChannel,
    WebhookChannel,
    build_channels,
)
from dywatch.notifiers.base import HttpChannel
from dywatch.render import render_event
from dywatch.scheduler import GlobalGate
from dywatch.settings import load_settings
from dywatch.targets import parse_targets
from dywatch.users import INVISIBLE_CHARS, is_safe_id, parse_users
from dywatch.webui import _metrics_label

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------- 闸门真的会关
def test_backoff_for_alone_never_closes_the_gate():
    """这条钉住"算退避"与"关闸"是两件事——它们曾经被写成只做前者。

    现场特征就是那条 `gate.closed` 日志里的 `remaining=0.0`：自己说关了，余量却是零。
    """
    gate = GlobalGate()
    gate.backoff_for(MonitorError("RATE_LIMITED", retry_after=30))

    assert gate.is_open() is True, "只算不关 = 没关"
    gate.close(30, reason="RATE_LIMITED")
    assert gate.is_open() is False


# --------------------------------------------------------------- 通知目标注释
def test_a_comment_with_an_apostrophe_cannot_swallow_a_target():
    raw = "\ndingtalk token=a\n# can't use telegram\ntelegram bot_token=1:A chat_id=-1\n"
    parsed = parse_targets(raw)

    assert not parsed.errors, parsed.errors
    assert [t.kind for t in parsed.targets] == ["dingtalk", "telegram"]


def test_a_comment_with_a_semicolon_cannot_reactivate_a_target():
    raw = "\ndingtalk token=a\n# off;telegram bot_token=1:A chat_id=-1\n"
    parsed = parse_targets(raw)

    assert [t.kind for t in parsed.targets] == ["dingtalk"]


# --------------------------------------------------------------- 渠道业务错误
class _Reply:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, reply: _Reply) -> None:
        self.reply = reply
        self.calls = 0

    async def post(self, *args: Any, **kwargs: Any) -> _Reply:
        self.calls += 1
        return self.reply


def message() -> Any:
    return render_event(
        Event(
            EventKind.NEW_POST,
            sec_user_id="u",
            nickname="示例",
            content_id="1",
            payload={"content": Content(content_id="1", kind=Kind.VIDEO, title="标题")},
        ),
        now=NOW,
    )


@pytest.mark.parametrize(
    ("channel", "body"),
    [
        (WeComChannel(key="bad"), {"errcode": 93000}),
        (DingTalkChannel(token="t"), {"errcode": 310000}),
        (BarkChannel(device_key="d"), {"code": 400}),
        (ServerChanChannel(sendkey="s"), {"code": 40001}),
        # Telegram 的官方判据是响应体的 `ok`。文档只说"请求不成功时 ok 为 False"，
        # **没有规定 HTTP 状态码**（实测假 token 回 401），所以这里连 200 的形状一起钉住。
        (TelegramChannel(bot_token="1:A"), {"ok": False, "error_code": 400,
                                            "description": "chat not found"}),
        (TelegramChannel(bot_token="1:A"), {"ok": False, "error_code": 401,
                                            "description": "Unauthorized"}),
    ],
)
async def test_http_200_with_a_business_error_is_a_failure(channel: HttpChannel, body: dict) -> None:
    client = _Client(_Reply(200, body))

    with pytest.raises(RuntimeError):
        await channel.send(message(), client)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("channel", "body"),
    [
        (WeComChannel(key="k"), {"errcode": 0}),
        (DingTalkChannel(token="t"), {"errcode": 0}),
        (BarkChannel(device_key="d"), {"code": 200}),
        (ServerChanChannel(sendkey="s"), {"code": 0}),
        (WebhookChannel(url="https://x"), {"anything": 1}),
        (TelegramChannel(bot_token="1:A"), {"ok": True}),
        (WebhookChannel(url="https://x"), "not json"),
        (WeComChannel(key="k"), ValueError("no body")),
    ],
)
async def test_a_healthy_200_still_counts_as_sent(channel: HttpChannel, body: Any) -> None:
    client = _Client(_Reply(200, body))

    await channel.send(message(), client)  # type: ignore[arg-type]

    assert client.calls == 1


def test_serverchan_clips_the_title_by_bytes_not_characters():
    long_subject = "【新作品】" + "很长的中文昵称" * 3
    rendered = dataclasses.replace(message(), subject=long_subject)
    body = ServerChanChannel(sendkey="s").request(rendered)[1]

    assert len(body["title"].encode("utf-8")) <= 32
    assert "\ufffd" not in body["title"], "按字节切不能切出半个字符"


# --------------------------------------------------------------- 载荷形状
def test_a_malformed_removed_payload_does_not_claim_zero_posts():
    """`removed` 形状读不出来时不能渲染成"有 0 条作品已确认消失"——那是假信息。"""
    for bad in ("oops", 5, {"content_id": "1"}):
        rendered = render_event(
            Event(EventKind.POST_REMOVED, sec_user_id="u", nickname="示例", payload={"removed": bad}),
            now=NOW,
        )
        assert "0 条" not in rendered.subject, (bad, rendered.subject)
        assert "无法读取" in rendered.markdown


def test_a_well_formed_removed_payload_still_renders_the_count():
    rendered = render_event(
        Event(
            EventKind.POST_REMOVED,
            sec_user_id="u",
            nickname="示例",
            payload={"removed": [{"content_id": "1", "title": "T", "created_at": None}]},
        ),
        now=NOW,
    )

    assert "1 条" in rendered.subject


def test_parse_page_rejects_a_string_items_field():
    with pytest.raises(MonitorError) as caught:
        parse_page({"items": "abc"}, raw_included=False, task_id=None)

    assert caught.value.code == "CONTRACT_VIOLATION"


# --------------------------------------------------------------- 配置打码
def test_config_check_masks_every_credential_key():
    """`config-check` 的输出会被贴进 issue：凭据类键一个都不能明文出现。"""
    from dywatch.settings import MASKED_KEYS, SPECS

    assert {"PIN_DTK_API_KEY", "WEBHOOK_URL", "DTK_API_KEY"} <= MASKED_KEYS
    for key in MASKED_KEYS:
        assert key in SPECS, f"{key} 不在注册表里，说明这份清单过期了"


def test_describe_never_prints_the_identity_key_or_the_webhook_url():
    settings = load_settings(
        None,
        environ={
            "DTK_API_KEY": "dtk_aaaaaaaaaaaa_SECRETSECRETSECRETSECRETSECRETSECRET12",
            "PIN_DTK_API_KEY": "dtk_bbbbbbbbbbbb_VERYVERYVERYSECRETKEYKEYKEYKEYKEYKEY12",
            "WEBHOOK_URL": "https://hooks.example.com/T0KEN-IN-THE-PATH",
        },
    )
    printed = "\n".join(settings.describe())

    assert "VERYVERYVERYSECRET" not in printed
    assert "T0KEN-IN-THE-PATH" not in printed
    assert "PIN_DTK_API_KEY" in printed and "***" in printed


def test_masked_keys_covers_every_notifier_secret_field():
    """`TargetSpec.secrets` 里标了密的字段，在旧写法里也得有对应的打码键。"""
    from dywatch.settings import CHANNEL_REQUIRED

    settings = load_settings(None, environ={"DTK_API_KEY": "dtk_x"})
    printed = "\n".join(settings.describe())
    for channel, keys in CHANNEL_REQUIRED.items():
        for key in keys:
            assert key in printed, (channel, key)


# --------------------------------------------------------------- 账号 ID 校验
@pytest.mark.parametrize("char", sorted(INVISIBLE_CHARS))
def test_invisible_characters_are_refused_in_a_sec_user_id(char: str):
    assert is_safe_id(f"MS4wLjABAAAA{char}xyz") is False, repr(char)


def test_a_bom_prefixed_first_line_is_reported_instead_of_silently_kept():
    """BOM 只污染第一行：以前它会被当成 ID 的一部分，而 ID 看起来一模一样。"""
    entries = parse_users("\ufeffMS4wLjABAAAAfirst|一号\nMS4wLjABAAAAsecond|二号\n")

    assert [entry.sec_user_id for entry in entries] == ["MS4wLjABAAAAsecond"]


def test_inline_comment_is_not_part_of_the_nickname():
    entries = parse_users("MS4wLjABAAAAx|市场部  # 主账号\n")

    assert entries[0].nickname == "市场部"


def test_a_hash_inside_a_nickname_is_kept_when_it_is_not_a_comment():
    entries = parse_users("MS4wLjABAAAAx|#1 账号\n")

    assert entries[0].nickname == "#1 账号"


# --------------------------------------------------------------- /metrics label
def test_two_accounts_with_the_same_nickname_do_not_collide():
    """同名账号必须产出**不同**的 label（重复样本 = Prometheus 拒收整次抓取）。"""
    first = _metrics_label({"sec_user_id": "MS4wLjABAAAAa", "nickname": "示例账号"})
    second = _metrics_label({"sec_user_id": "MS4wLjABAAAAb", "nickname": "示例账号"})

    assert first != second
    assert "示例账号" in first, "昵称留着，人看图表时还认得出是谁"


def test_metrics_label_falls_back_when_there_is_no_id():
    assert _metrics_label({"nickname": "只有昵称"}) == "只有昵称"


# --------------------------------------------------------------- 状态快照类型
def test_snapshot_field_coercion_never_raises_on_hostile_shapes():
    """`read_status` 的归一化：把"当映射用"的字段统一成安全形状。

    手工改坏快照（或旧版本写下的另一种形状）时，`/`、`/metrics`、`/api/health`
    曾经会在**写出任何响应之前**抛异常——连接被直接关掉，比 500 更难查。
    """
    from dywatch.webui import _as_mapping

    assert _as_mapping("closed") == {}
    assert _as_mapping(["a"]) == {}
    assert _as_mapping({"open": False}) == {"open": False}
    assert _as_mapping(None) == {}

    # 真正被断言过的行为：坏形状下 `_gate_html` 不再抛
    from dywatch.webui import _gate_html

    assert _gate_html(_as_mapping("closed")) == ""
    assert "闸门关闭" not in _gate_html(_as_mapping("closed"))


# --------------------------------------------------------------- 旧写法多实例
def test_env_flag_works_before_and_after_the_subcommand():
    """文档承诺"所有命令都接受 `--env`"，那么两种位置都得能用。

    只在顶层注册时 `dywatch doctor --env X` 会被 argparse 拒掉；而给子解析器一个普通
    默认值又会把**顶层那份** `--env` 覆盖掉（`dywatch --env X doctor` 静默失败）。
    所以子解析器那边必须是 `SUPPRESS`——这条测试钉的就是这个细节。
    """
    from dywatch.cli import _env_path, _parser

    before = _parser().parse_args(["--env", "/tmp/before.env", "doctor"])
    after = _parser().parse_args(["doctor", "--env", "/tmp/after.env"])
    both = _parser().parse_args(["--env", "/tmp/before.env", "doctor", "--env", "/tmp/after.env"])
    neither = _parser().parse_args(["doctor"])

    assert str(_env_path(before)).replace("\\", "/").endswith("/tmp/before.env")
    assert str(_env_path(after)).replace("\\", "/").endswith("/tmp/after.env")
    assert str(_env_path(both)).replace("\\", "/").endswith("/tmp/after.env"), "两处都给时子命令的赢"
    assert "before.env" not in str(_env_path(neither)), "没写 --env 时走默认查找"


def test_legacy_duplicate_channel_entries_get_distinct_names():
    """`NOTIFY_CHANNELS=dingtalk,dingtalk` 会建出两个渠道；名字必须能分辨，
    否则 `Delivery.sent` / `failed` 会互相覆盖，"只有一个群收到"看不出来。"""
    settings = load_settings(
        None, environ={"DTK_API_KEY": "dtk_x", "NOTIFY_CHANNELS": "dingtalk,dingtalk",
                       "DINGTALK_TOKEN": "t", "DINGTALK_SECRET": "s"}
    )
    names = [channel.name for channel in build_channels(settings)]

    assert len(names) == len(set(names)) == 2


def test_legacy_single_channel_keeps_its_plain_name():
    settings = load_settings(
        None, environ={"DTK_API_KEY": "dtk_x", "NOTIFY_CHANNELS": "telegram",
                       "TELEGRAM_BOT_TOKEN": "1:A", "TELEGRAM_CHAT_ID": "-1"}
    )

    assert [channel.name for channel in build_channels(settings)] == ["telegram"]
