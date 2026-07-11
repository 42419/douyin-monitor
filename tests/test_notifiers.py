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


def test_build_notifier_skips_unknown_channel_and_raises_if_nothing_left():
    cfg = Config(notify_channels=["not_a_real_channel"])
    with pytest.raises(ValueError):
        build_notifier(cfg)
