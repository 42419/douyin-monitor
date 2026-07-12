import pytest

from douyin_monitor.config import Config
from douyin_monitor.notifiers import BarkNotifier, CompositeNotifier, DingTalkNotifier, build_notifier
from douyin_monitor.notifiers.base import BaseNotifier


class _FakeNotifier(BaseNotifier):
    name = "fake"

    def __init__(self, ok: bool):
        self._ok = ok
        self.calls = 0

    def send_text(self, title, content, at_mobiles=None):
        self.calls += 1
        return self._ok


class _RaisingNotifier(BaseNotifier):
    name = "raising"

    def send_text(self, title, content, at_mobiles=None):
        raise RuntimeError("boom")


def test_composite_notifier_succeeds_if_any_channel_succeeds():
    a = _FakeNotifier(ok=False)
    b = _FakeNotifier(ok=True)
    composite = CompositeNotifier([a, b])

    assert composite.send_text("title", "content") is True
    assert a.calls == 1
    assert b.calls == 1


def test_composite_notifier_survives_channel_exception():
    ok_notifier = _FakeNotifier(ok=True)
    composite = CompositeNotifier([_RaisingNotifier(), ok_notifier])

    # 一个渠道抛异常不应该影响其它渠道，也不应该向上抛出
    assert composite.send_text("title", "content") is True
    assert ok_notifier.calls == 1


def test_composite_notifier_requires_at_least_one_channel():
    with pytest.raises(ValueError):
        CompositeNotifier([])


def test_build_notifier_raises_when_no_channel_has_credentials():
    cfg = Config(notify_channels=["dingtalk"], dingtalk_token="", dingtalk_secret="")
    with pytest.raises(ValueError):
        build_notifier(cfg)


def test_build_notifier_single_channel_returns_bare_notifier():
    cfg = Config(notify_channels=["bark"], bark_device_key="devicekey123")
    notifier = build_notifier(cfg)
    assert isinstance(notifier, BarkNotifier)


def test_build_notifier_multiple_channels_returns_composite():
    cfg = Config(
        notify_channels=["dingtalk", "bark"],
        dingtalk_token="tok",
        dingtalk_secret="SECxxx",
        bark_device_key="devicekey123",
    )
    notifier = build_notifier(cfg)
    assert isinstance(notifier, CompositeNotifier)
    assert {n.name for n in notifier._notifiers} == {"dingtalk", "bark"}


class _RecordingFakeNotifier(BaseNotifier):
    """记录 send_video 收到的完整参数，用于验证 CompositeNotifier 正确转发。"""

    name = "recording_fake"

    def __init__(self):
        self.video_calls = []

    def send_text(self, title, content, at_mobiles=None):
        return True

    def send_video(self, *args, **kwargs):
        self.video_calls.append((args, kwargs))
        return True


def test_composite_notifier_forwards_all_send_video_kwargs():
    """回归测试：monitor.py 调用 notifier.send_video 时会传入
    digg_count/comment_count/share_count/collect_count/duration_ms/desc/gap_days
    等一整套关键字参数。之前 CompositeNotifier.send_video 的签名只接受
    (nickname, video_id, title, create_time, cover_url)，一旦启用了多个推送
    渠道（NOTIFY_CHANNELS 里配置了 2 个以上），新视频通知会直接抛
    TypeError，导致多渠道场景下新视频推送完全失效（Copilot code review 发现）。"""
    a = _RecordingFakeNotifier()
    b = _RecordingFakeNotifier()
    composite = CompositeNotifier([a, b])

    result = composite.send_video(
        "小明",
        "vid123",
        "标题",
        1752292800,
        "http://example.com/cover.jpg",
        digg_count=100,
        comment_count=20,
        share_count=5,
        collect_count=3,
        duration_ms=60000,
        desc="标题 #话题",
        gap_days=7,
    )

    assert result is True
    for fake in (a, b):
        assert len(fake.video_calls) == 1
        args, kwargs = fake.video_calls[0]
        assert args == ("小明", "vid123", "标题", 1752292800, "http://example.com/cover.jpg")
        assert kwargs == {
            "digg_count": 100,
            "comment_count": 20,
            "share_count": 5,
            "collect_count": 3,
            "duration_ms": 60000,
            "desc": "标题 #话题",
            "gap_days": 7,
        }


def test_build_notifier_skips_unknown_channel_and_raises_if_nothing_left():
    cfg = Config(notify_channels=["not_a_real_channel"])
    with pytest.raises(ValueError):
        build_notifier(cfg)
