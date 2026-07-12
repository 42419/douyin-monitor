import json

from douyin_monitor import monitor as monitor_module
from douyin_monitor.monitor import Monitor

from .conftest import RecordingNotifier


class FakeConfig:
    api_url = "http://fake"
    fetch_count = 10
    stale_threshold = 7 * 86400
    at_mobiles = []


def _resp(items, code=200):
    return 200, json.dumps({"code": code, "data": {"aweme_list": items}})


def _item(vid, title="标题", create_time=1000, is_top=False, cover=None):
    entry = {
        "aweme_id": vid,
        "item_title": title,
        "create_time": create_time,
        "is_top": is_top,
    }
    if cover:
        entry["cover_original_scale"] = {"url_list": [cover]}
    return entry


def _make_monitor(monkeypatch, notifier=None, stop_event=None):
    import threading

    notifier = notifier or RecordingNotifier()
    stop_event = stop_event or threading.Event()
    monitor = Monitor(FakeConfig(), notifier, stop_event)
    return monitor, notifier


def test_first_round_initializes_without_notifications(state_dir, monkeypatch):
    monitor, notifier = _make_monitor(monkeypatch)
    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A"), _item("B")])
    )

    result = monitor.check_user("sec1", "小明")

    assert result["status"] == "init"
    assert result["new_count"] == 2
    assert notifier.videos == []
    assert notifier.deleted == []


def test_new_video_triggers_notification(state_dir, monkeypatch):
    monitor, notifier = _make_monitor(monkeypatch)
    monkeypatch.setattr(monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A")]))
    monitor.check_user("sec1", "小明")

    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A"), _item("B")])
    )
    result = monitor.check_user("sec1", "小明")

    assert result["new_count"] == 1
    assert [v[1] for v in notifier.videos] == ["B"]


def test_video_scrolled_out_is_silent(state_dir, monkeypatch):
    """非置顶视频消失时如果同轮有新视频顶入，视为被挤出窗口，静默清理，不发通知。"""
    monitor, notifier = _make_monitor(monkeypatch)
    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A"), _item("B")])
    )
    monitor.check_user("sec1", "小明")

    # B 消失，D 是新视频 -> B 应被判定为"挤出窗口"，不触发删除通知
    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A"), _item("D")])
    )
    result = monitor.check_user("sec1", "小明")

    assert result["deleted_count"] == 0
    assert notifier.deleted == []


def test_delete_requires_two_confirm_rounds_for_normal_video(state_dir, monkeypatch):
    """A、B 都非置顶。A 消失且当轮没有新视频顶入（关键：没有 new_ids，
    否则会被判定为"滚动出窗口"而静默清理），需要连续 2 轮确认才通知删除。"""
    monitor, notifier = _make_monitor(monkeypatch)
    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A"), _item("B")])
    )
    monitor.check_user("sec1", "小明")  # 初始化

    monkeypatch.setattr(monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("B")]))
    result = monitor.check_user("sec1", "小明")  # 第 1 轮消失，尚未确认
    assert result["deleted_count"] == 0
    assert notifier.deleted == []

    result = monitor.check_user("sec1", "小明")  # 第 2 轮仍消失 -> 达到确认阈值
    assert result["deleted_count"] == 1
    assert notifier.deleted == [("小明", ["A"])]


def test_top_video_requires_three_confirm_rounds(state_dir, monkeypatch):
    monitor, notifier = _make_monitor(monkeypatch)
    monkeypatch.setattr(
        monitor_module,
        "fetch_user_videos",
        lambda *a, **k: _resp([_item("A", is_top=True), _item("B")]),
    )
    monitor.check_user("sec1", "小明")  # 初始化，A 是置顶视频

    monkeypatch.setattr(monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("B")]))
    r1 = monitor.check_user("sec1", "小明")
    r2 = monitor.check_user("sec1", "小明")
    assert r1["deleted_count"] == 0
    assert r2["deleted_count"] == 0  # 置顶视频需要 3 轮才确认，2 轮还不够
    assert notifier.deleted == []

    r3 = monitor.check_user("sec1", "小明")
    assert r3["deleted_count"] == 1
    assert notifier.deleted == [("小明", ["A"])]


def test_title_change_detected_without_new_or_delete(state_dir, monkeypatch):
    monitor, notifier = _make_monitor(monkeypatch)
    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A", title="旧标题")])
    )
    monitor.check_user("sec1", "小明")

    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A", title="新标题")])
    )
    result = monitor.check_user("sec1", "小明")

    assert result["title_changed_count"] == 1
    assert result["new_count"] == 0
    assert result["deleted_count"] == 0


def test_malformed_response_missing_aweme_id_marks_fail(state_dir, monkeypatch):
    monitor, notifier = _make_monitor(monkeypatch)
    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A")])
    )
    monitor.check_user("sec1", "小明")

    bad_items = [{"item_title": "缺少 aweme_id 字段"}]
    monkeypatch.setattr(monitor_module, "fetch_user_videos", lambda *a, **k: _resp(bad_items))
    result = monitor.check_user("sec1", "小明")

    assert result["status"] == "fail"


def test_http_failure_increments_consecutive_fails_and_alerts(state_dir, monkeypatch):
    monitor, notifier = _make_monitor(monkeypatch)
    monkeypatch.setattr(monitor_module, "fetch_user_videos", lambda *a, **k: (500, "server error"))

    from douyin_monitor.config import MAX_CONSECUTIVE_FAILS

    for _ in range(MAX_CONSECUTIVE_FAILS):
        result = monitor.check_user("sec1", "小明")
        assert result["status"] == "fail"

    # 达到连续失败阈值应触发一次告警
    assert len(notifier.texts) == 1
    assert "连续" in notifier.texts[0][1]


def test_stale_fallback_alert_fires_once_then_stays_silent(state_dir, monkeypatch):
    """回归测试：14 天无更新的提醒之前和"响应哈希不变"共用 1 小时冷却，
    导致每小时都会重新推送一次（用户反馈的 bug）。现在改成一次性提醒：
    达到阈值只提醒一次，之后哪怕一直无更新，也不会再重复提醒。"""
    from datetime import timedelta

    from douyin_monitor.state import UserState
    from douyin_monitor.utils import now

    monitor, notifier = _make_monitor(monkeypatch)
    state = UserState(monitor_module.STATE_DIR / "sec1.json")
    state.data["last_update_at"] = (now() - timedelta(days=15)).isoformat()

    # 第一次检测：14 天无更新条件成立，且从未提醒过 -> 应该提醒一次
    monitor._check_stale(state, "阿直", aweme_list=None)
    assert len(notifier.texts) == 1
    assert "长期无更新提醒" in notifier.texts[0][0]

    # 之后不管过多久再跑检测，只要没有发布新视频，都不应该重复提醒
    for hours_ago in (1, 25, 200, 400):
        state.data["last_fallback_stale_alert_at"] = (
            now() - timedelta(hours=hours_ago)
        ).isoformat()
        monitor._check_stale(state, "阿直", aweme_list=None)
    assert len(notifier.texts) == 1


def test_stale_alert_resets_after_new_video_then_can_fire_again(state_dir, monkeypatch):
    """用户重新发布新视频后，一次性提醒的标记应该被重置；如果这个用户之后
    又进入了新一轮的长期无更新，应该能够重新收到一次提醒（而不是永远沉默）。"""
    monitor, notifier = _make_monitor(monkeypatch)
    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A")])
    )
    monitor.check_user("sec1", "阿直")  # 初始化

    from douyin_monitor.state import UserState

    state = UserState(monitor_module.STATE_DIR / "sec1.json")
    state.data["stale_alerted"] = True  # 模拟之前已经提醒过一次
    state.save()

    # 用户发布了新视频 B
    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A"), _item("B")])
    )
    monitor.check_user("sec1", "阿直")

    state = UserState(monitor_module.STATE_DIR / "sec1.json")
    assert state.data.get("stale_alerted") is False  # 已经被重置


def test_new_video_notification_includes_gap_days_when_previously_stale(state_dir, monkeypatch):
    """用户长期没更新后重新发布新视频，通知里应该带上距上次发布的间隔天数；
    间隔很短（比如几小时内连续发布）则不需要显示，避免信息噪音。"""
    from datetime import timedelta

    from douyin_monitor.state import UserState
    from douyin_monitor.utils import now

    monitor, notifier = _make_monitor(monkeypatch)
    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A")])
    )
    monitor.check_user("sec1", "阿直")  # 初始化

    # 把上次更新时间改到 20 天前，模拟长期无更新之后终于发新视频
    state = UserState(monitor_module.STATE_DIR / "sec1.json")
    state.data["last_update_at"] = (now() - timedelta(days=20)).isoformat()
    state.save()

    monkeypatch.setattr(
        monitor_module, "fetch_user_videos", lambda *a, **k: _resp([_item("A"), _item("B")])
    )
    monitor.check_user("sec1", "阿直")

    assert len(notifier.videos) == 1
    gap_days = notifier.videos[0][3]
    assert gap_days == 20

    # 紧接着又发了一条（间隔很短），不应该出现明显的间隔天数
    monkeypatch.setattr(
        monitor_module,
        "fetch_user_videos",
        lambda *a, **k: _resp([_item("A"), _item("B"), _item("C")]),
    )
    monitor.check_user("sec1", "阿直")
    assert notifier.videos[1][3] == 0
