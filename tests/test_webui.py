import http.client
import json
import threading
import time

from douyin_monitor import webui as webui_module
from douyin_monitor.webui import _escape_html, _render_status_html, start_web_server


def test_escape_html_escapes_all_special_chars():
    raw = """<script>alert('x")</script>&"""
    escaped = _escape_html(raw)
    assert "<" not in escaped
    assert ">" not in escaped
    assert "'" not in escaped
    assert '"' not in escaped
    # & 本身要转义，但不能在转义别的字符时被重复转义出 &amp;amp; 这种双重转义
    assert "&amp;" in escaped
    assert "&amp;amp;" not in escaped


def test_render_status_html_escapes_malicious_nickname(tmp_path, monkeypatch):
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-12 10:00:00",
                "pid": 1,
                "users": [
                    {
                        "sec_user_id": "a",
                        "nickname": "<script>alert(1)</script>",
                        "known_videos": 1,
                        "hours_since_update": 0,
                        "consecutive_fails": 0,
                        "in_users_conf": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(webui_module, "STATUS_FILE", status_file)

    html = _render_status_html("dingtalk")

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def _start_test_server(tmp_path, monkeypatch):
    from douyin_monitor import config as config_module

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    status_file = tmp_path / "status.json"
    users_conf = tmp_path / "users.conf"

    monkeypatch.setattr(webui_module, "STATUS_FILE", status_file)
    monkeypatch.setattr(config_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(config_module, "USERS_CONF", users_conf)

    stop_event = threading.Event()
    server = start_web_server("127.0.0.1", 0, stop_event, ["dingtalk"])
    port = server.server_address[1]
    time.sleep(0.1)
    return state_dir, users_conf, port, stop_event, server


def test_serve_user_shows_removed_status_when_not_in_users_conf(tmp_path, monkeypatch):
    state_dir, users_conf, port, stop_event, server = _start_test_server(tmp_path, monkeypatch)
    try:
        (state_dir / "sec1.json").write_text(
            json.dumps(
                {
                    "sec_user_id": "sec1",
                    "nickname": "阿直",
                    "consecutive_fails": 0,
                    "videos": {},
                }
            ),
            encoding="utf-8",
        )
        users_conf.write_text("", encoding="utf-8")  # 空的 users.conf，sec1 已经不在里面了

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/user/sec1")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()

        assert resp.status == 200
        assert data["status_text"] == "已从配置移除"
    finally:
        stop_event.set()
        server.shutdown()


def test_serve_user_shows_normal_status_when_in_users_conf(tmp_path, monkeypatch):
    state_dir, users_conf, port, stop_event, server = _start_test_server(tmp_path, monkeypatch)
    try:
        (state_dir / "sec1.json").write_text(
            json.dumps(
                {
                    "sec_user_id": "sec1",
                    "nickname": "阿直",
                    "consecutive_fails": 0,
                    "videos": {},
                }
            ),
            encoding="utf-8",
        )
        users_conf.write_text("sec1|阿直\n", encoding="utf-8")

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/user/sec1")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()

        assert data["status_text"] == "正常"
    finally:
        stop_event.set()
        server.shutdown()


def test_serve_user_rejects_path_traversal():
    stop_event = threading.Event()
    server = start_web_server("127.0.0.1", 0, stop_event, ["dingtalk"])
    port = server.server_address[1]
    time.sleep(0.1)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/user/..%2f..%2f..%2fetc%2fpasswd")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 400
    finally:
        stop_event.set()
        server.shutdown()


def test_row_template_does_not_use_inline_onclick_for_uid():
    """回归测试：账号名的点击行为不应该再把 uid 拼进内联 onclick 的 JS 字符串里
    （HTML 实体转义防不住这个上下文的注入），应该用 data-uid + 事件委托。"""
    html = _render_status_html("dingtalk")
    assert "onclick=\"openDetail(" not in html
