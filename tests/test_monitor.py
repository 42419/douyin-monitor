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
