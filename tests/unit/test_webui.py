"""只读面板：渲染、转义、状态分级、详情接口。

这一版的面板是从旧项目 `douyin-monitor-enhance` 移植过来的，所以几个"踩过的坑"也一起
带了过来并继续盯着（转义、内联 onclick 注入、百分号编码的 uid、判断器静默）；
数据源换成了 SQLite，于是新增了针对作品 / 已消失作品 / 事件的用例。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from dywatch import webui
from dywatch.models import AuthorState, Event, EventKind, Kind, PostState, Tombstone
from dywatch.settings import Settings, load_settings
from dywatch.state import StateStore
from dywatch.webui import (
    LED_SLOTS,
    PanelServer,
    _escape_html,
    _quantize_blocks,
    classify_account,
    render_page,
    user_detail,
)

NOW = datetime.now(timezone.utc)
ID_OK = "MS4wLjABAAAAok"
ID_WRONG = "MS4wLjABAAAAwrong"
ID_FAIL = "MS4wLjABAAAAfail"


# --------------------------------------------------------------------- 脚手架

def make_settings(tmp_path: Path, **overrides: str) -> Settings:
    env = {
        "MONITOR_HOME": str(tmp_path),
        "DTK_API_KEY": "dtk_38c817704d0f_A5CMPbL1a7TowGb9LAfXYNAvYK6bjIwn",
        "DINGTALK_TOKEN": "t",
        "WEB_HOST": "127.0.0.1",
        "WEB_PORT": "0",
    }
    env.update(overrides)
    return load_settings(None, environ=env)


def user_entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sec_user_id": ID_OK,
        "nickname": "示例账号",
        "configured": True,
        "known_posts": 8,
        "tombstones": 2,
        "ever_had_posts": True,
        "consecutive_fails": 0,
        "hours_since_newest_post": 5,
        "update_frequency": "日更",
        "freq_avg_days": 1.0,
        "freq_sample_count": 7,
        "freq_hint": "基于最近 8 条非置顶作品，平均 1.0 天/条",
        "runs": 120,
    }
    base.update(overrides)
    return base


def write_status(settings: Settings, **overrides: Any) -> Path:
    snapshot: dict[str, Any] = {
        "timestamp": "2026-09-16T20:00:00+08:00",
        "pid": 4321,
        "rounds": 137,
        "gate": {"open": True, "reason": None, "remaining_seconds": 0, "times_closed": 0},
        "upstream": {"base_url": "http://192.168.20.4:8000"},
        "notify": {"channels": ["dingtalk"], "silent": False},
        "users": [user_entry()],
    }
    snapshot.update(overrides)
    settings.status_path.parent.mkdir(parents=True, exist_ok=True)
    settings.status_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return settings.status_path


def seed_db(settings: Settings) -> None:
    store = StateStore(settings.db_path)
    store.migrate()
    store.ensure_author(ID_OK, "示例账号", NOW)
    store.ensure_author(ID_FAIL, "失败的账号", NOW)
    store.ensure_author(ID_WRONG, "疑似写错的 ID", NOW)
    settings.users_conf.write_text(f"{ID_OK}|示例账号\n{ID_FAIL}|失败的账号\n", encoding="utf-8")

    posts = tuple(
        PostState(
            content_id=f"74{index:017d}",
            kind=Kind.VIDEO if index % 2 else Kind.IMAGE_ALBUM,
            title=f"第 {index} 条",
            # 第 3 条故意没有发布时间：抖音偶尔不给 created_at，它不该因此排到最新；
            # 置顶那条故意是最旧的（30 天前）——"置顶排最前"不能靠"它碰巧最新"来蒙对
            created_at=None if index == 3 else NOW - timedelta(days=30 if index == 0 else index),
            is_top=index == 0,
            first_seen_at=NOW - timedelta(days=index),
            absent_rounds=1 if index == 2 else 0,
        )
        for index in range(4)
    )
    store.save_round(
        ID_OK,
        AuthorState(
            sec_user_id=ID_OK,
            nickname="示例账号",
            initialized_at=NOW - timedelta(days=30),
            ever_had_posts=True,
            last_update_at=NOW - timedelta(hours=5),
            runs=120,
            posts=posts,
            tombstones=(
                Tombstone(content_id="741", removed_at=NOW - timedelta(days=2), reason="confirmed"),
                Tombstone(content_id="742", removed_at=NOW - timedelta(days=6),
                          reason="scrolled_out"),
            ),
        ),
        [
            Event(kind=EventKind.NEW_POST, sec_user_id=ID_OK, nickname="示例账号", content_id="743"),
            Event(kind=EventKind.POST_REMOVED, sec_user_id=ID_OK, nickname="示例账号",
                  content_id="741"),
        ],
        now=NOW,
    )
    store.save_round(
        ID_FAIL,
        AuthorState(sec_user_id=ID_FAIL, nickname="失败的账号", initialized_at=NOW,
                    ever_had_posts=True, consecutive_fails=3, last_error="上游返回 403",
                    last_error_code="403_FORBIDDEN_SCOPE", runs=88),
        [],
        now=NOW,
    )
    store.close()


@contextlib.contextmanager
def panel(settings: Settings) -> Iterator[str]:
    server = PanelServer(settings)
    server.start()
    try:
        yield f"http://127.0.0.1:{server.address[1]}"
    finally:
        server.stop()


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# --------------------------------------------------------------------- 转义

def test_escape_html_escapes_everything_once():
    escaped = _escape_html("""<script>alert('x")</script>&""")
    assert "<" not in escaped and ">" not in escaped
    assert "'" not in escaped and '"' not in escaped
    # & 要转义，但不能在转义别的字符时被重复转义出 &amp;amp;
    assert "&amp;" in escaped
    assert "&amp;amp;" not in escaped


def test_render_page_escapes_malicious_nickname(tmp_path):
    settings = make_settings(tmp_path)
    seed_db(settings)
    write_status(settings, users=[user_entry(nickname="<script>alert(1)</script>")])

    html = render_page(settings)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_row_template_does_not_use_inline_onclick_for_uid(tmp_path):
    """账号名的点击行为用 data-uid + 事件委托，不能把 uid 拼进内联 onclick。"""
    settings = make_settings(tmp_path)
    seed_db(settings)
    write_status(settings)
    assert 'onclick="openDetail(' not in render_page(settings)


# --------------------------------------------------------------------- 状态分级

@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"configured": False}, "已移除"),
        ({"configured": False, "consecutive_fails": 3}, "已移除"),  # 已移出配置的先说
        ({"consecutive_fails": 2}, "失败 2 次"),
        ({"consecutive_fails": 2, "ever_had_posts": False}, "失败 2 次"),  # 失败比无作品更急
        ({"ever_had_posts": False, "hours_since_newest_post": None}, "从未有作品"),
        ({"hours_since_newest_post": 24 * 20}, "20 天无新作品"),
        # 关键差异：即使"刚刚检测到变化"（比如删了一条作品），只要最新作品是 20 天前的，
        # 就该标成"长期无新作品"——旧口径（看 last_update_at）在这里会显示成正常
        ({"hours_since_newest_post": 24 * 20, "hours_since_update": 1}, "20 天无新作品"),
        ({"hours_since_newest_post": 24 * 20, "ever_had_posts": False}, "从未有作品"),
        ({}, "正常"),
    ],
)
def test_classify_account_priority(overrides, expected):
    color, text = classify_account(user_entry(**overrides), stale_days=14)
    assert text == expected
    assert color in webui.STATUS_LEGEND


def test_quantize_blocks_never_drops_a_nonzero_category():
    """极端比例下小类别也要有格子，否则"1 个失败账号"在阵列里完全看不见。"""
    blocks = _quantize_blocks([100, 1, 0, 1, 0], slots=LED_SLOTS)
    assert sum(blocks) == LED_SLOTS
    assert blocks[1] >= 1 and blocks[3] >= 1
    assert blocks[2] == 0 and blocks[4] == 0
    assert _quantize_blocks([0, 0, 0, 0, 0]) == [0, 0, 0, 0, 0]


def test_led_array_and_stats_render(tmp_path):
    settings = make_settings(tmp_path)
    seed_db(settings)
    write_status(
        settings,
        users=[
            user_entry(),
            user_entry(sec_user_id=ID_WRONG, nickname="写错的", ever_had_posts=False,
                       hours_since_newest_post=None, update_frequency=None),
            user_entry(sec_user_id=ID_FAIL, nickname="失败的", consecutive_fails=3),
            user_entry(sec_user_id="MS4wLjABAAAAgone", nickname="老的", configured=False),
        ],
    )

    html = render_page(settings)

    assert html.count('<span class="led on-') == LED_SLOTS
    for key in ("led on-green", "led on-red", "led on-blue", "led on-off"):
        assert key in html
    assert "从未有作品" in html and "已移除" in html
    assert "[ 闸门关闭 ]" not in html  # 闸门开着时不渲染警示条（CSS 注释里那个不算）
    # 四个账号四种状态，标题要能说清楚
    assert "全部正常" not in html


def test_row_shows_how_long_since_the_newest_post(tmp_path):
    """列表的时间列是"最新作品多久前发布"，不是"上次检测到变化"。"""
    settings = make_settings(tmp_path)
    seed_db(settings)
    write_status(settings, users=[user_entry(hours_since_newest_post=30)])

    html = render_page(settings)

    assert "1 天前发布" in html
    assert "距上次更新" not in html


def test_metrics_stay_parseable_with_hostile_nicknames(tmp_path):
    """昵称里带换行/引号/反斜杠时，/metrics 那一行仍必须是**一行**。

    少一个转义，这一个账号的昵称就能把整个 /metrics 抓取判坏——波及面远大于它自己。
    """
    settings = make_settings(tmp_path)
    write_status(settings, users=[user_entry(nickname='nl\nq"\\x')])

    with panel(settings) as base:
        status, body = get(base + "/metrics")

    assert status == 200
    lines = [line for line in body.splitlines() if line.startswith("dywatch_known_posts{")]
    assert len(lines) == 1, "换行没被转义，样本被拆成了多行"
    assert 'author="nl\\nq\\"\\\\x"' in lines[0]


def test_gate_closed_renders_warning_strip(tmp_path):
    settings = make_settings(tmp_path)
    write_status(settings, gate={"open": False, "reason": "IDENTITY_POOL_EXHAUSTED",
                                 "remaining_seconds": 240.0, "times_closed": 1})
    html = render_page(settings)
    assert "闸门关闭" in html
    assert "IDENTITY_POOL_EXHAUSTED" in html
    assert "240 秒后自动重试" in html


def test_render_page_without_snapshot_says_no_data(tmp_path):
    settings = make_settings(tmp_path)
    html = render_page(settings)
    assert "还没有数据" in html
    assert "还没有监控账号" not in html


def test_render_page_with_empty_users_conf(tmp_path):
    settings = make_settings(tmp_path)
    write_status(settings, users=[])
    html = render_page(settings)
    assert "还没有监控账号" in html


def test_silent_mode_is_visible_in_meta(tmp_path):
    settings = make_settings(tmp_path)
    write_status(settings, notify={"channels": ["dingtalk", "bark"], "silent": True})
    html = render_page(settings)
    assert "dingtalk、bark（静默）" in html


# --------------------------------------------------------------------- 详情

def test_user_detail_reports_posts_tombstones_and_events(tmp_path):
    settings = make_settings(tmp_path)
    seed_db(settings)

    detail = user_detail(settings, ID_OK)

    assert detail is not None
    assert detail["status_text"] == "正常"
    assert detail["known_posts"] == 4
    assert detail["tombstones"] == 2
    assert detail["runs"] == 120
    assert detail["update_frequency"] == "日更"
    assert "平均 1.0 天/条" in detail["freq_hint"]
    # 置顶的在最前（即使它是最旧的一条），其余按发布时间倒序，
    # 没有发布时间的那条排最后而不是被当成"最新"
    assert [post["is_top"] for post in detail["posts"]] == [True, False, False, False]
    assert detail["posts"][-1]["date"] == "未知"
    assert detail["posts"][-1]["title"] == "第 3 条"
    # "距最新作品"看的是最新那条作品的发布时间（第 1 条，1 天前），不是置顶那条 30 天前的
    assert detail["newest_post_ago"] == "1 天前发布"
    assert detail["newest_post_at"] != "未知"
    assert detail["posts"][0]["kind"] in ("视频", "图文")
    assert [p["absent_rounds"] for p in detail["posts"]].count(1) == 1
    assert {row["reason"] for row in detail["removed"]} == {"已确认消失", "被新作品挤出窗口"}
    assert {event["label"] for event in detail["events"]} == {"新作品", "作品消失"}


def test_user_detail_marks_account_removed_from_users_conf(tmp_path):
    settings = make_settings(tmp_path)
    seed_db(settings)
    settings.users_conf.write_text("", encoding="utf-8")  # 谁都不在配置里了
    detail = user_detail(settings, ID_OK)
    assert detail is not None and detail["status_text"] == "已移除"


def test_user_detail_returns_none_for_unknown_account(tmp_path):
    settings = make_settings(tmp_path)
    seed_db(settings)
    assert user_detail(settings, "MS4wLjABAAAAnope") is None


def test_user_detail_without_database(tmp_path):
    settings = make_settings(tmp_path)  # 没建过库
    assert user_detail(settings, ID_OK) is None


def test_user_detail_counts_all_tombstones_beyond_the_listing_limit(tmp_path):
    """详情只列前 20 条，但计数得是全量——"已消失 2 条"不能被列表长度截断。"""
    settings = make_settings(tmp_path)
    store = StateStore(settings.db_path)
    store.migrate()
    store.ensure_author(ID_OK, "示例账号", NOW)
    store.save_round(
        ID_OK,
        AuthorState(
            sec_user_id=ID_OK, nickname="示例账号", initialized_at=NOW, ever_had_posts=True,
            tombstones=tuple(
                Tombstone(content_id=f"75{index:017d}", removed_at=NOW - timedelta(days=index))
                for index in range(25)
            ),
        ),
        [],
        now=NOW,
    )
    store.close()
    detail = user_detail(settings, ID_OK)
    assert detail is not None
    assert detail["tombstones"] == 25
    assert len(detail["removed"]) == 20


# --------------------------------------------------------------------- HTTP

def test_routes(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    seed_db(settings)
    write_status(settings)
    monkeypatch.setattr(webui, "_dtk_ok", lambda *a, **k: {"ok": True})

    with panel(settings) as base:
        status, body = get(base + "/")
        assert status == 200 and "DYWATCH / STATUS" in body

        status, body = get(base + "/api/state")
        assert status == 200 and json.loads(body)["rounds"] == 137

        status, body = get(base + "/api/health")
        health = json.loads(body)
        assert (status, health["users"], health["failed_users"]) == (200, 1, 0)

        assert get(base + "/healthz") == (200, '{"status": "ok"}')
        assert get(base + "/readyz")[0] == 200

        status, body = get(base + "/metrics")
        assert status == 200 and "dywatch_gate_open 1" in body
        assert 'dywatch_known_posts{author="示例账号"} 8' in body

        assert get(base + "/nope")[0] == 404


def test_readyz_reports_unreachable_upstream(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(
        webui, "_dtk_ok", lambda *a, **k: {"ok": False, "reason": "URLError: refused"}
    )
    with panel(settings) as base:
        status, body = get(base + "/readyz")
    assert status == 503
    assert json.loads(body)["components"]["dtk"]["ok"] is False


def test_health_defaults_to_no_data(tmp_path):
    settings = make_settings(tmp_path)
    assert webui.build_health(settings) == {"status": "no_data", "users": 0, "failed_users": 0}


def test_user_endpoint_accepts_ids_with_equals_and_plus(tmp_path):
    """回归：真实 sec_user_id 可能带 `=`，前端还会把 `+` 编成 %2B，服务端必须先解码。"""
    settings = make_settings(tmp_path)
    store = StateStore(settings.db_path)
    store.migrate()
    for raw in ("sec=1", "sec+1"):
        store.ensure_author(raw, f"账号{raw}", NOW)
    store.close()

    with panel(settings) as base:
        for raw, encoded in (("sec=1", "sec=1"), ("sec+1", "sec%2B1")):
            status, body = get(f"{base}/api/user/{encoded}")
            assert status == 200, (raw, body)
            assert json.loads(body)["nickname"] == f"账号{raw}"


def test_user_endpoint_rejects_traversal_and_unknown(tmp_path):
    settings = make_settings(tmp_path)
    seed_db(settings)
    with panel(settings) as base:
        assert get(base + "/api/user/..%2f..%2f..%2fetc%2fpasswd")[0] == 400
        assert get(base + "/api/user/")[0] == 400  # 空 uid 直接拒
        assert get(base + "/api/user/" + urllib.parse.quote("有 空格"))[0] == 400
        assert get(base + "/api/user/MS4wLjABAAAAnope")[0] == 404


def test_user_endpoint_returns_503_when_store_is_unreadable(tmp_path, monkeypatch):
    """状态库读不出来（不是"没有这个账号"）应当是 503，别让前端显示成"查无此人"。"""
    settings = make_settings(tmp_path)
    seed_db(settings)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(webui, "user_detail", boom)
    with panel(settings) as base:
        status, body = get(base + f"/api/user/{ID_OK}")
    assert status == 503
    assert "database is locked" in json.loads(body)["error"]


def test_query_string_is_ignored(tmp_path):
    settings = make_settings(tmp_path)
    write_status(settings)
    with panel(settings) as base:
        assert get(base + "/?cache=bust")[0] == 200
        assert get(base + f"/api/state?t=1")[0] == 200


def test_status_server_silences_connection_reset_error(capsys):
    """公网扫描器连上就断开（ConnectionResetError）是常态噪音，不该打完整 traceback。"""
    server = webui._PanelServer(("127.0.0.1", 0), webui._Handler)
    try:
        try:
            raise ConnectionResetError("Connection reset by peer")
        except ConnectionResetError:
            server.handle_error(None, ("66.132.172.133", 7894))
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err

        try:
            raise ValueError("boom")
        except ValueError:
            server.handle_error(None, ("1.2.3.4", 1234))
        captured = capsys.readouterr()
        assert "Traceback" in captured.err and "boom" in captured.err
    finally:
        server.server_close()


def test_access_urls_never_print_the_wildcard_host(monkeypatch):
    """`WEB_HOST=0.0.0.0` 时不能把 0.0.0.0 当地址打印出来——那是打不开的。

    这里直接测纯函数：绑 0.0.0.0 在有些环境里会被策略拦掉，而这条断言与监听无关。
    """
    assert webui.access_urls("127.0.0.1", 8787) == ["http://127.0.0.1:8787/"]

    monkeypatch.setattr(webui, "guess_lan_ip", lambda: "192.168.20.4")
    assert webui.access_urls("0.0.0.0", 8787) == [
        "http://127.0.0.1:8787/",
        "http://192.168.20.4:8787/",
    ]

    monkeypatch.setattr(webui, "guess_lan_ip", lambda: None)
    assert webui.access_urls("0.0.0.0", 8787) == ["http://127.0.0.1:8787/"]


def test_guess_lan_ip_never_raises():
    """拿不到局域网 IP 时要返回 None，不能把启动流程带崩。"""
    value = webui.guess_lan_ip()
    assert value is None or isinstance(value, str)


def test_panel_server_starts_and_stops_cleanly(tmp_path):
    settings = make_settings(tmp_path)
    before = threading.active_count()
    server = PanelServer(settings)
    server.start()
    host, port = server.address
    assert (host, port)[1] > 0
    server.stop()
    assert threading.active_count() <= before + 1  # 后台是 daemon 线程，stop 会关 socket


def test_parse_dt_accepts_z_suffix_and_naive_values():
    assert webui._parse_dt("2026-09-16T12:00:00Z").tzinfo is not None
    assert webui._parse_dt("2026-09-16T12:00:00").tzinfo is not None
    assert webui._parse_dt("不是时间") is None
    assert webui._parse_dt(None) is None


def test_urllib_parse_roundtrip_for_ids():
    """前端 encodeURIComponent 的等价物：确保解码端拿得到原值。"""
    raw = "MS4wLjABAAAA+a=b/c"
    assert urllib.parse.unquote(urllib.parse.quote(raw, safe="")) == raw
