"""Web 面板：渲染、转义、状态分级、详情接口。

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
from dywatch.models import (
    AuthorState,
    Event,
    EventKind,
    Kind,
    PostMetrics,
    PostState,
    Tombstone,
)
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
        "rounds_total": 3214,
        "gate": {
            "open": True,
            "reason": None,
            "remaining_seconds": 0,
            "times_closed": 0,
        },
        "upstream": {"base_url": "http://192.168.20.4:8000"},
        "notify": {"channels": ["dingtalk"], "silent": False},
        "users": [user_entry()],
    }
    snapshot.update(overrides)
    settings.status_path.parent.mkdir(parents=True, exist_ok=True)
    settings.status_path.write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )
    return settings.status_path


def seed_db(settings: Settings) -> None:
    store = StateStore(settings.db_path)
    store.migrate()
    store.ensure_author(ID_OK, "示例账号", NOW)
    store.ensure_author(ID_FAIL, "失败的账号", NOW)
    store.ensure_author(ID_WRONG, "疑似写错的 ID", NOW)
    settings.users_conf.write_text(
        f"{ID_OK}|示例账号\n{ID_FAIL}|失败的账号\n", encoding="utf-8"
    )

    posts = tuple(
        PostState(
            content_id=f"74{index:017d}",
            kind=Kind.VIDEO if index % 2 else Kind.IMAGE_ALBUM,
            title=f"第 {index} 条",
            # 第 3 条故意没有发布时间：抖音偶尔不给 created_at，它不该因此排到最新；
            # 置顶那条故意是最旧的（30 天前）——"置顶排最前"不能靠"它碰巧最新"来蒙对
            created_at=None
            if index == 3
            else NOW - timedelta(days=30 if index == 0 else index),
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
                Tombstone(
                    content_id="741",
                    removed_at=NOW - timedelta(days=2),
                    reason="confirmed",
                ),
                Tombstone(
                    content_id="742",
                    removed_at=NOW - timedelta(days=6),
                    reason="scrolled_out",
                ),
            ),
        ),
        [
            Event(
                kind=EventKind.NEW_POST,
                sec_user_id=ID_OK,
                nickname="示例账号",
                content_id="743",
            ),
            Event(
                kind=EventKind.POST_REMOVED,
                sec_user_id=ID_OK,
                nickname="示例账号",
                content_id="741",
            ),
        ],
        now=NOW,
    )
    store.save_round(
        ID_FAIL,
        AuthorState(
            sec_user_id=ID_FAIL,
            nickname="失败的账号",
            initialized_at=NOW,
            ever_had_posts=True,
            consecutive_fails=3,
            last_error="上游返回 403",
            last_error_code="403_FORBIDDEN_SCOPE",
            runs=88,
        ),
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


def test_panel_labels_the_two_round_counts_separately(tmp_path):
    """回归：面板上那个轮数曾被当成"累计轮数"读，实际只是本次进程的计数。

    `rounds`（进程内存，重启归零）与 `rounds_total`（状态库累计）必须同时出现、
    各自带口径，否则"库里几千轮、面板第 51 轮"看起来像丢了数据。
    """
    settings = make_settings(tmp_path)
    seed_db(settings)
    write_status(settings, rounds=51, rounds_total=3214)

    html = render_page(settings)

    assert "本次运行第 51 轮" in html
    assert "累计 3214 轮" in html


def test_panel_survives_snapshot_without_rounds_total(tmp_path):
    """旧快照（没有 `rounds_total` 键）不能让页面渲染失败。

    升级换代码后、还没跑完一轮时，快照就是上一版写的，缺这个键。
    """
    settings = make_settings(tmp_path)
    seed_db(settings)
    path = write_status(settings)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot.pop("rounds_total")
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    html = render_page(settings)

    assert "本次运行第 137 轮" in html
    assert "累计 — 轮" in html


def test_panel_and_metrics_survive_a_hand_edited_snapshot(tmp_path):
    """快照被手工改坏（类型不对）时，页面、`/metrics`、`/api/health` 都不能崩。

    回归：`rounds: "abc"` 曾让 `/metrics` 抛 `ValueError`，**整次抓取失败**——
    一个数字读不出来，比显示 0 严重得多。`status.json` 是文本文件，不能假设
    "它是我们写的就一定是干净的"。
    """
    settings = make_settings(tmp_path)
    seed_db(settings)
    write_status(
        settings,
        rounds="abc",
        rounds_total=["1"],
        users=[
            user_entry(
                known_posts={}, consecutive_fails="x", hours_since_newest_post="很久"
            )
        ],
    )

    html = render_page(settings)  # 页面：不崩，坏值原样显示
    assert "本次运行第 abc 轮" in html

    with panel(settings) as base:
        status, body = get(base + "/metrics")
        assert status == 200
        assert "dywatch_rounds_total 0" in body
        assert "dywatch_rounds_recorded_total 0" in body
        assert f'dywatch_known_posts{{author="{ID_OK}|示例账号"}} 0' in body
        assert f'dywatch_account_failures{{author="{ID_OK}|示例账号"}} 0' in body

        assert get(base + "/")[0] == 200
        assert get(base + "/api/health")[0] == 200


# --------------------------------------------------------------------- 状态分级


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"configured": False}, "已移除"),
        ({"configured": False, "consecutive_fails": 3}, "已移除"),  # 已移出配置的先说
        ({"consecutive_fails": 2}, "失败 2 次"),
        (
            {"consecutive_fails": 2, "ever_had_posts": False},
            "失败 2 次",
        ),  # 失败比无作品更急
        ({"ever_had_posts": False, "hours_since_newest_post": None}, "从未有作品"),
        ({"hours_since_newest_post": 24 * 20}, "20 天无新作品"),
        # 关键差异：即使"刚刚检测到变化"（比如删了一条作品），只要最新作品是 20 天前的，
        # 就该标成"长期无新作品"——旧口径（看 last_update_at）在这里会显示成正常
        (
            {"hours_since_newest_post": 24 * 20, "hours_since_update": 1},
            "20 天无新作品",
        ),
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
            user_entry(
                sec_user_id=ID_WRONG,
                nickname="写错的",
                ever_had_posts=False,
                hours_since_newest_post=None,
                update_frequency=None,
            ),
            user_entry(sec_user_id=ID_FAIL, nickname="失败的", consecutive_fails=3),
            user_entry(
                sec_user_id="MS4wLjABAAAAgone", nickname="老的", configured=False
            ),
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
    lines = [
        line for line in body.splitlines() if line.startswith("dywatch_known_posts{")
    ]
    assert len(lines) == 1, "换行没被转义，样本被拆成了多行"
    # label 现在是 `id|昵称`：id 在前，所以断言里面的昵称那一段
    assert 'nl\\nq\\"\\\\x"' in lines[0]
    assert lines[0].startswith(f'dywatch_known_posts{{author="{ID_OK}|')


def test_two_accounts_with_the_same_nickname_do_not_break_the_whole_scrape(tmp_path):
    """昵称允许重复，而重复样本会让 Prometheus **拒收整次抓取**。

    丢掉的不只是那两个同名账号的指标，是这个面板的全部指标——所以 label 里必须带 id。
    """
    settings = make_settings(tmp_path)
    write_status(
        settings,
        users=[
            user_entry(sec_user_id="MS4wLjABAAAAone", nickname="同名"),
            user_entry(sec_user_id="MS4wLjABAAAAtwo", nickname="同名"),
        ],
    )

    with panel(settings) as base:
        status, body = get(base + "/metrics")

    assert status == 200
    labels = [
        line.split('author="', 1)[1].split('"', 1)[0]
        for line in body.splitlines()
        if line.startswith("dywatch_known_posts{")
    ]
    assert len(labels) == len(set(labels)) == 2, "同名账号产出了重复样本"


def test_metrics_label_never_ends_with_a_half_escape():
    """截断点落在转义反斜杠上时，label 不能以落单的 `\\` 结尾（那等于换行那个 bug 的翻版）。

    `_label` 因此先截断再转义：只断言"结尾反斜杠成对"，因为落单的那一个会让
    Prometheus 认为转义没结束。
    """
    from dywatch.webui import _label

    for raw in (
        "a" * 95 + '"' + "b",
        "a" * 94 + '"' + "b",
        "长" * 95 + '"',
        "\\" * 60,
        '"' * 60,
    ):
        label = _label(raw)
        trailing = len(label) - len(label.rstrip("\\"))
        assert trailing % 2 == 0, f"{raw[-3:]!r} 截出了半个转义：{label[-4:]!r}"


def test_gate_closed_renders_warning_strip(tmp_path):
    settings = make_settings(tmp_path)
    write_status(
        settings,
        gate={
            "open": False,
            "reason": "IDENTITY_POOL_EXHAUSTED",
            "remaining_seconds": 240.0,
            "times_closed": 1,
        },
    )
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
    assert {row["reason"] for row in detail["removed"]} == {
        "已确认消失",
        "被新作品挤出窗口",
    }
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
            sec_user_id=ID_OK,
            nickname="示例账号",
            initialized_at=NOW,
            ever_had_posts=True,
            tombstones=tuple(
                Tombstone(
                    content_id=f"75{index:017d}", removed_at=NOW - timedelta(days=index)
                )
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


def test_user_detail_hidden_count_matches_the_per_post_badges(tmp_path):
    """「对访客不可见」的读数必须与逐条徽章**同源**。

    踩过的坑：统计格从 `_post_state()`（行→模型的转换层）取数、徽章读原始行，而转换层
    漏映射了 `hidden_from_guest_at` → 统计恒为 0、徽章却显示"对访客不可见"，同一个
    弹窗里自相矛盾。这条测试把两个读数钉在一起，掉字段就会红。
    """
    settings = make_settings(tmp_path)
    store = StateStore(settings.db_path)
    store.migrate()
    store.ensure_author(ID_OK, "示例账号", NOW)
    store.save_round(
        ID_OK,
        AuthorState(
            sec_user_id=ID_OK,
            nickname="示例账号",
            ever_had_posts=True,
            initialized_at=NOW - timedelta(days=30),
            last_update_at=NOW - timedelta(minutes=1),
            posts=(
                PostState(
                    content_id="visible",
                    title="看得见的",
                    created_at=NOW - timedelta(days=2),
                ),
                PostState(
                    content_id="hidden",
                    title="看不见的",
                    created_at=NOW - timedelta(days=1),
                    hidden_from_guest_at=NOW - timedelta(hours=1),
                ),
            ),
        ),
        [],
        now=NOW,
    )
    store.close()

    detail = user_detail(settings, ID_OK)
    badges = [post for post in detail["posts"] if post["hidden"]]

    assert len(badges) == 1
    assert detail["hidden_posts"] == len(badges), "统计格和徽章必须是同一个数"
    assert badges[0]["title"] == "看不见的"
    assert badges[0]["hidden_since"], "徽章上要能看出它是从什么时候起看不见的"
    assert detail["known_posts"] == 2, "被隐藏的作品仍在已知作品里（它没消失）"
    assert detail["tombstones"] == 0, "它不该出现在「已消失」里"


# --------------------------------------------------------------------- HTTP


def test_routes(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    seed_db(settings)
    write_status(settings)
    # 补丁打在**定义它的模块**上：`webui` 是门面，它上面那个名字只是一个副本，
    # 改它不会影响 handler 真正调用的那个函数（拆包时踩过一次）
    monkeypatch.setattr(webui.server, "_dtk_ok", lambda *a, **k: {"ok": True})

    with panel(settings) as base:
        status, body = get(base + "/")
        assert status == 200 and "DYWATCH / STATUS" in body
        # 两个轮次数都要出现，且各自带口径——只显示其中一个会被读成"累计轮数"
        assert "本次运行第 137 轮" in body
        assert "累计 3214 轮" in body

        status, body = get(base + "/api/state")
        assert status == 200 and json.loads(body)["rounds"] == 137

        status, body = get(base + "/api/health")
        health = json.loads(body)
        assert (status, health["users"], health["failed_users"]) == (200, 1, 0)
        assert health["rounds"] == 137 and health["rounds_total"] == 3214

        assert get(base + "/healthz") == (200, '{"status": "ok"}')
        assert get(base + "/readyz")[0] == 200

        status, body = get(base + "/metrics")
        assert status == 200 and "dywatch_gate_open 1" in body
        assert f'dywatch_known_posts{{author="{ID_OK}|示例账号"}} 8' in body
        # 进程级的那个保留 counter 语义（重启归零），累计的另起一个名字
        assert "dywatch_rounds_total 137" in body
        assert "dywatch_rounds_recorded_total 3214" in body
        # 上游 retry_after 被封顶的次数：>0 是"我们没完全听上游的"的唯一信号
        assert "dywatch_gate_retry_after_capped_total 0" in body

        status, body = get(base + "/events")
        assert status == 200 and "DYWATCH / EVENTS" in body
        # 图表库是本地打包的静态资源：面板常常跑在没有外网的机器上
        status, body = get(base + "/assets/chart.umd.min.js")
        assert status == 200 and "Chart" in body[:4000]
        status, events = get(base + "/api/events?range=24h")
        # seed_db 里那两条事件（new_post / post_removed）就落在 24 小时窗口内
        assert status == 200 and json.loads(events)["total"] == 2

        assert get(base + "/nope")[0] == 404


def test_metrics_report_how_often_an_upstream_retry_after_was_capped(tmp_path):
    """封顶次数要能从 `/metrics` 看到——否则"闸门为什么每十分钟开一次"只能靠读代码。"""
    settings = make_settings(tmp_path)
    write_status(
        settings,
        gate={
            "open": False,
            "reason": "RATE_LIMITED",
            "remaining_seconds": 600.0,
            "times_closed": 3,
            "retry_after_capped": 7,
        },
    )

    with panel(settings) as base:
        status, body = get(base + "/metrics")

    assert status == 200
    assert "dywatch_gate_retry_after_capped_total 7" in body
    assert "# TYPE dywatch_gate_retry_after_capped_total counter" in body


def test_a_snapshot_without_the_new_gate_field_still_scrapes(tmp_path):
    """旧版本写下的快照里没有 `retry_after_capped`：那一项按 0 处理，不能让整次抓取失败。"""
    settings = make_settings(tmp_path)
    write_status(
        settings,
        gate={"open": True, "reason": None, "remaining_seconds": 0, "times_closed": 0},
    )

    with panel(settings) as base:
        status, body = get(base + "/metrics")

    assert status == 200
    assert "dywatch_gate_retry_after_capped_total 0" in body


def test_readyz_reports_unreachable_upstream(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(
        webui.server,
        "_dtk_ok",
        lambda *a, **k: {"ok": False, "reason": "URLError: refused"},
    )
    with panel(settings) as base:
        status, body = get(base + "/readyz")
    assert status == 503
    assert json.loads(body)["components"]["dtk"]["ok"] is False


def test_readyz_does_not_create_the_state_store(tmp_path, monkeypatch):
    """回归：`/readyz` 是 systemd / 负载均衡周期性打的探针，它**不该**把状态库"建"出来。

    `sqlite3.connect` 会给不存在的路径建一个 0 字节文件；之后 `queries.has_store()` 就会
    说"库在"，所有查询转成 `no such table: authors` —— 排障的人盯着数据目录里那个空文件，
    会以为库被折腾坏了，而它只是被一个健康检查顺手创建的。
    """
    settings = make_settings(tmp_path)
    # 上游照样 patch 掉：这条用例只关心"库有没有被创建"，不该依赖网络
    monkeypatch.setattr(webui.server, "_dtk_ok", lambda *a, **k: {"ok": True})
    assert not settings.db_path.exists()

    with panel(settings) as base:
        status, body = get(base + "/readyz")

    assert status == 503
    assert (
        json.loads(body)["components"]["state_store"]["reason"]
        == "state store not found"
    )
    assert not settings.db_path.exists(), "探针在数据目录里留下了空库"
    assert (
        not settings.db_path.parent.exists()
        or list(settings.db_path.parent.iterdir()) == []
    )


def test_health_defaults_to_no_data(tmp_path):
    settings = make_settings(tmp_path)
    assert webui.build_health(settings) == {
        "status": "no_data",
        "users": 0,
        "failed_users": 0,
    }


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

    monkeypatch.setattr(webui.queries, "user_detail", boom)
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

    monkeypatch.setattr(webui.server, "guess_lan_ip", lambda: "192.168.20.4")
    assert webui.access_urls("0.0.0.0", 8787) == [
        "http://127.0.0.1:8787/",
        "http://192.168.20.4:8787/",
    ]

    monkeypatch.setattr(webui.server, "guess_lan_ip", lambda: None)
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
    assert (
        threading.active_count() <= before + 1
    )  # 后台是 daemon 线程，stop 会关 socket


def test_parse_dt_accepts_z_suffix_and_naive_values():
    assert webui._parse_dt("2026-09-16T12:00:00Z").tzinfo is not None
    assert webui._parse_dt("2026-09-16T12:00:00").tzinfo is not None
    assert webui._parse_dt("不是时间") is None
    assert webui._parse_dt(None) is None


def test_urllib_parse_roundtrip_for_ids():
    """前端 encodeURIComponent 的等价物：确保解码端拿得到原值。"""
    raw = "MS4wLjABAAAA+a=b/c"
    assert urllib.parse.unquote(urllib.parse.quote(raw, safe="")) == raw


# --------------------------------------------------- 上游健康卡片 / 自身降级横幅


def upstream_snapshot(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "base_url": "http://dtk.local:8000",
        "ok": True,
        "checked_at": "2026-09-16T20:00:00+08:00",
        "version": "5.1.2",
        "uptime_seconds": 9 * 86400 + 3600 * 7 + 1800,
        "components": {
            "postgres": {"ok": True, "latency_ms": 3},
            "redis": {"ok": True, "latency_ms": 1},
            "browser_rpc": {"ok": None, "latency_ms": None},
        },
        "pool": {
            "douyin": {
                "minting": 1,
                "active": 7,
                "cooling": 2,
                "degraded": 0,
                "retired": 4,
            },
            "total_active": 7,
        },
        "storage": {"db_size_bytes": 402_653_184, "identities": 14},
    }
    base.update(overrides)
    return base


def test_status_page_shows_the_upstream_card(tmp_path):
    settings = make_settings(tmp_path)
    write_status(settings, upstream=upstream_snapshot())
    html = render_page(settings)
    assert "上游 DTK" in html
    assert "5.1.2" in html
    assert "9 天 7 小时" in html  # uptime 要人话，不是秒数
    assert "384.0 MB" in html  # 存储用量要人话
    assert "身份池" in html and "活跃 7" in html
    assert "未配置" in html  # browser_rpc 的 ok 是 None == 不知道，不是坏了


def test_status_page_says_so_when_the_upstream_reading_is_missing(tmp_path):
    """老快照里只有 base_url：要说清"还没取到读数"，而不是画一张全是—的卡片。"""
    settings = make_settings(tmp_path)
    write_status(settings, upstream={"base_url": "http://dtk.local:8000"})
    html = render_page(settings)
    assert "还没有取到读数" in html
    assert "5.1.2" not in html


def test_status_page_marks_a_stale_upstream_reading(tmp_path):
    """取不到时保留上一次的值（比抹掉更有用），但必须说清它是旧的。"""
    settings = make_settings(tmp_path)
    write_status(
        settings,
        upstream=upstream_snapshot(
            ok=False,
            error="UPSTREAM_UNREACHABLE",
            version="5.1.2",
        ),
    )
    html = render_page(settings)
    assert "不可用（UPSTREAM_UNREACHABLE）" in html
    assert "上一次取到的读数" in html
    assert "5.1.2" in html


def test_self_check_banner_renders_reasons_and_readings(tmp_path):
    settings = make_settings(tmp_path)
    write_status(
        settings,
        self_check={
            "ok": False,
            "reasons": ["disk_low"],
            "free_mb": 138,
            "free_limit_mb": 200,
            "writable": True,
        },
    )
    html = render_page(settings)
    assert "[ 自身降级 ]" in html
    assert "磁盘剩余空间不足" in html
    assert "剩余 138MB / 阈值 200MB" in html


def test_self_check_banner_is_absent_when_everything_is_fine(tmp_path):
    settings = make_settings(tmp_path)
    write_status(settings, self_check={"ok": True, "reasons": [], "free_mb": 9000})
    # 用带方括号的横幅字样断言：CSS 注释里也有"自身降级"四个字，直接搜它会永远为真
    assert "[ 自身降级 ]" not in render_page(settings)


def test_health_report_includes_self_and_upstream_state(tmp_path):
    """机器读的小结里也该有"服务自己好没好"，否则只能靠人打开网页看。"""
    settings = make_settings(tmp_path)
    write_status(
        settings,
        self_check={"ok": False, "reasons": ["disk_low"]},
        upstream={"base_url": "x", "ok": False, "error": "E"},
    )
    health = webui.build_health(settings)
    assert health["self_ok"] is False and health["self_reasons"] == ["disk_low"]
    assert health["upstream_ok"] is False


# --------------------------------------------------- /metrics


def parse_exposition(text: str) -> tuple[dict[str, str], list[str]]:
    """极简 Prometheus 文本解析：只取 `# TYPE` 表和样本名。"""
    types: dict[str, str] = {}
    samples: list[str] = []
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            name, _, kind = line[len("# TYPE ") :].partition(" ")
            types[name] = kind
        elif line and not line.startswith("#"):
            samples.append(line.split("{")[0].split(" ")[0])
    return types, samples


def test_every_metric_is_declared_with_help_and_type(tmp_path):
    """**每个样本名都必须有 `# HELP` / `# TYPE`。**

    回归：`dywatch_account_failures` 只写了样本、没有声明，抓取端会把它当成 untyped——
    不报错，但在图表里既不能算速率也不能算均值，人只会觉得"这个数怪怪的"。
    """
    settings = make_settings(tmp_path)
    write_status(settings, upstream=upstream_snapshot())
    types, samples = parse_exposition(webui.metrics_text(settings))
    assert samples, "至少要有一个样本"
    undeclared = sorted({name for name in samples if name not in types})
    assert undeclared == [], f"这些指标没有 # HELP/# TYPE：{undeclared}"


def test_metrics_cover_upstream_pool_archive_and_features(tmp_path):
    settings = make_settings(tmp_path)
    write_status(
        settings,
        upstream=upstream_snapshot(),
        self_check={"ok": False, "reasons": ["disk_low"], "free_mb": 138},
        archive={"enabled": True, "pending": 3, "muted_code": None},
        features={"metrics": True, "hidden_check": False, "archive_download": True},
    )
    text = webui.metrics_text(settings)
    assert (
        'dywatch_upstream_pool_identities{platform="douyin",state="active"} 7' in text
    )
    assert "dywatch_upstream_pool_active 7" in text
    assert "dywatch_upstream_component_latency_ms" in text
    assert "dywatch_upstream_storage_db_bytes 402653184" in text
    assert "dywatch_archive_pending 3" in text
    assert "dywatch_archive_muted 0" in text
    assert (
        "dywatch_self_check_ok 0" in text and "dywatch_self_check_free_mb 138" in text
    )
    assert 'dywatch_feature_enabled{name="hidden_check"} 0' in text
    assert "dywatch_upstream_checked_timestamp_seconds" in text


def test_metrics_skip_unknown_upstream_components_instead_of_reporting_zero(tmp_path):
    """`ok: null` 是"上游不知道"，不是 0——报 0 就是替上游宣布一次故障。

    断言要**枚举全部 `component_ok` 行**，不能只看第一行：只检查 `[0]` 的话，
    多出来的那行未知组件（browser_rpc）永远不会被看到——这条用例曾经就是这个形状，
    把"跳过 `ok is None`"改坏它照样全绿。
    """
    settings = make_settings(tmp_path)
    write_status(settings, upstream=upstream_snapshot())
    text = webui.metrics_text(settings)

    ok_lines = [
        line
        for line in text.splitlines()
        if line.startswith("dywatch_upstream_component_ok")
    ]
    assert ok_lines == [
        'dywatch_upstream_component_ok{component="postgres"} 1',
        'dywatch_upstream_component_ok{component="redis"} 1',
    ], ok_lines

    # 延迟那一组同理：值为 None 的样本不该出现（不是写成 0）
    latency_lines = [
        line
        for line in text.splitlines()
        if line.startswith("dywatch_upstream_component_latency_ms")
    ]
    assert latency_lines == [
        'dywatch_upstream_component_latency_ms{component="postgres"} 3',
        'dywatch_upstream_component_latency_ms{component="redis"} 1',
    ], latency_lines


def test_metrics_count_events_from_the_store(tmp_path):
    settings = make_settings(tmp_path)
    seed_db(settings)
    text = webui.metrics_text(settings)
    assert "dywatch_state_readable 1" in text
    assert 'dywatch_events_recent{kind="new_post"} 1' in text
    assert 'dywatch_events_recent{kind="post_removed"} 1' in text
    assert 'dywatch_state_rows{table="authors"} 3' in text


def test_metrics_survive_a_database_that_cannot_be_read(tmp_path):
    """库读不出来时少几行 + `dywatch_state_readable 0`，**不能整次抓取 500**：
    那会把所有指标（包括与库无关的"上游挂了"）一起弄丢。"""
    settings = make_settings(tmp_path)
    write_status(settings, upstream=upstream_snapshot())
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.db_path.write_bytes("这不是一个 SQLite 文件".encode("utf-8"))
    text = webui.metrics_text(settings)
    assert "dywatch_state_readable 0" in text
    assert "dywatch_events_recent" not in text
    assert "dywatch_upstream_pool_active 7" in text


def test_metrics_omit_db_metrics_when_there_is_no_database_at_all(tmp_path):
    """库文件都不在时**不要顺手建一个空的**——那会让下一次查询报 `no such table`。"""
    settings = make_settings(tmp_path)
    write_status(settings)
    text = webui.metrics_text(settings)
    assert "dywatch_state_readable 0" in text
    assert not settings.db_path.exists()


# --------------------------------------------------- 图表资源


def test_chart_asset_is_served_with_a_content_hash_and_cached(tmp_path):
    settings = make_settings(tmp_path)
    write_status(settings)
    version = webui.asset_version()
    assert version and len(version) == 10
    with panel(settings) as base:
        status, body = get(base + f"/assets/chart.umd.min.js?v={version}")
        assert status == 200 and "Chart" in body[:4000]

        # 版本对不上就不缓存：老页面指向旧版本时宁可多下一次，也不要让浏览器
        # 一直用旧版本的图表库——那种错看起来像"图表代码有 bug"
        import urllib.request as _rq

        with _rq.urlopen(base + "/assets/chart.umd.min.js?v=stale") as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Content-Type"].startswith("text/javascript")


def test_status_page_links_the_chart_library_with_the_current_version(tmp_path):
    settings = make_settings(tmp_path)
    write_status(settings)
    html = render_page(settings)
    assert f"/assets/chart.umd.min.js?v={webui.asset_version()}" in html
    assert "data-chart" in html or 'id="detailMetrics"' in html


def test_unknown_asset_and_traversal_are_not_found(tmp_path):
    settings = make_settings(tmp_path)
    with panel(settings) as base:
        assert get(base + "/assets/../../etc/passwd")[0] == 404
        assert get(base + "/assets/nope.js")[0] == 404


# --------------------------------------------------- 互动量落库 → 面板


def seed_metrics(settings: Settings) -> None:
    """给 `seed_db` 的那个账号补两轮互动量（跨两个不同的小时桶）。"""
    store = StateStore(settings.db_path)
    store.migrate()
    posts = tuple(
        PostState(
            content_id=f"74{index:017d}",
            kind=Kind.VIDEO,
            title=f"第 {index} 条",
            created_at=NOW - timedelta(days=index),
        )
        for index in range(4)
    )
    for hour, factor in ((2, 100), (1, 180)):
        store.save_round(
            ID_OK,
            AuthorState(
                sec_user_id=ID_OK,
                nickname="示例账号",
                initialized_at=NOW,
                ever_had_posts=True,
                runs=120,
                posts=posts,
            ),
            [],
            now=NOW - timedelta(hours=hour),
            metrics=[
                PostMetrics(
                    content_id=f"74{index:017d}",
                    play_count=factor * 10,
                    digg_count=factor,
                    comment_count=index,
                    share_count=None,
                    collect_count=factor * 2,
                )
                for index in range(4)
            ],
        )
    store.close()


def test_user_detail_includes_the_engagement_series_and_trend(tmp_path):
    settings = make_settings(tmp_path)
    seed_db(settings)
    seed_metrics(settings)
    detail = user_detail(settings, ID_OK)

    trend = detail["metrics_trend"]
    assert trend["has_data"] is True
    assert trend["posts"] == 4, "真正参与合计的作品数"
    assert trend["known"] == len(detail["posts"]), "现在已知的作品数"
    assert [item["label"] for item in trend["series"]] == [
        "点赞",
        "评论",
        "收藏",
        "分享",
    ]
    delta = trend["views"]["all"]["delta"]

    def total(key: str) -> int:
        return sum(v for v in delta[key] if v is not None)

    # 4 条作品各从 100 涨到 180：新增量是 4 × 80，而不是累计合计 720
    assert total("digg") == 320
    assert total("collect") == 640
    # 评论没变：是 0（"这一格里没涨"）；分享从没采到：是 None（"不知道"）。这是两回事
    assert total("comment") == 0 and any(v == 0 for v in delta["comment"])
    assert all(v is None for v in delta["share"])
    # 逐条作品的"最新一行"要挂在作品上，而不是每个作品各查一次
    assert detail["posts"][0]["metrics"]["digg"] == 180
    assert detail["posts"][0]["metrics"]["share"] is None
    assert detail["metrics_enabled"] is True
    assert "metrics_chart" not in detail, "旧的累计总数图表载荷已被趋势取代"
    assert "metrics_series" not in detail, (
        "没人用的死载荷：每次打开详情白查一次、白传 336 行"
    )


def test_trend_counts_the_posts_that_actually_went_into_the_sum(tmp_path):
    """图上写「N 条作品的合计」，N 必须是真正加进去的那几条。

    真实账号里有些作品没有互动量记录（隐藏的、刚加的、开记录之前抓过的）：拿已知作品数当
    N，就把覆盖面说大了。这里让其中一条没有任何记录——已知 4 条，参与合计的只有 3 条。
    """
    settings = make_settings(tmp_path)
    seed_db(settings)
    seed_metrics(settings)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("DELETE FROM post_metrics WHERE content_id = ?", (f"74{0:017d}",))

    trend = user_detail(settings, ID_OK)["metrics_trend"]

    assert trend["known"] == 4
    assert trend["posts"] == 3


def test_a_single_sample_does_not_count_as_taking_part(tmp_path):
    """有记录 ≠ 参与了合计：只有一个采样的作品还没产生任何新增量。"""
    settings = make_settings(tmp_path)
    seed_db(settings)
    seed_metrics(settings)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            "DELETE FROM post_metrics WHERE content_id = ? AND hour = "
            "(SELECT MIN(hour) FROM post_metrics WHERE content_id = ?)",
            (f"74{0:017d}",) * 2,
        )

    trend = user_detail(settings, ID_OK)["metrics_trend"]

    assert trend["posts"] == 3, "那条作品只剩一个采样"
    assert trend["known"] == 4


def test_user_detail_says_when_metrics_are_switched_off(tmp_path):
    """没开记录 vs 开了还没采到——两件事，页面上的说法必须不同。"""
    settings = make_settings(tmp_path, METRICS_ENABLED="false")
    seed_db(settings)
    seed_metrics(settings)
    detail = user_detail(settings, ID_OK)
    assert detail["metrics_enabled"] is False
    assert detail["metrics_trend"] is None


def test_detail_page_shows_per_post_engagement_badges(tmp_path):
    settings = make_settings(tmp_path)
    seed_db(settings)
    seed_metrics(settings)
    html = render_page(settings)
    # 数字交给前端格式化（万/亿），所以这里只断言取值路径存在
    assert "metricLine" in html and "detailMetrics" in html


def test_user_endpoint_survives_engagement_metrics(tmp_path):
    """回归：`/api/user/<id>` 带互动量读数时，整条响应曾被掐断。

    上面那几条用例直接调 `user_detail`，所以发现不了这个 bug：`hour` 是 `datetime`，
    只有走到 `json.dumps` 那一步才抛 `TypeError`，而它抛在发响应头**之前**——客户端
    拿到的是连接被重置（不是 500，没有 body），浏览器只说"请求失败"。
    所以这一条必须走 HTTP，且要断言"能解析出 JSON"本身，而不只是某个字段的值。
    """
    settings = make_settings(tmp_path)
    seed_db(settings)
    seed_metrics(settings)
    with panel(settings) as base:
        status, body = get(f"{base}/api/user/{ID_OK}")
    assert status == 200, body
    detail = json.loads(body)
    # JSON 里没有 datetime：时间戳必须是**ISO** 字符串。只断言 `isinstance(..., str)` 不够——
    # `json.dumps` 的 `default=str` 兜底会把 `datetime` 降级成 `str(datetime)`（空格分隔），
    # 既不是 ISO、前端也解析不了，而"能解析出 JSON"照样成立
    hour = detail["posts"][0]["metrics"]["hour"]
    assert isinstance(hour, str) and "T" in hour
    assert datetime.fromisoformat(hour).minute == 0, "小时桶：整点"
    assert detail["posts"][0]["metrics"]["digg"] == 180
    # 趋势载荷是纯 JSON 类型，走的是同一次 json.dumps
    assert detail["metrics_trend"]["has_data"] is True


def test_metrics_json_normalises_only_the_timestamp():
    """`_metrics_json` 只把 `hour` 转成字符串，其余字段（含以后新加的）原样带走。"""
    raw = {"hour": NOW, "play": 7, "digg": None, "将来新增的字段": "x"}
    out = webui.queries._metrics_json(raw)
    assert out == {
        "hour": NOW.isoformat(),
        "play": 7,
        "digg": None,
        "将来新增的字段": "x",
    }
    # 复制一份再改：调用方手里那份不能被就地改掉
    assert raw["hour"] is NOW
    assert webui.queries._metrics_json(None) is None


def test_json_body_degrades_unknown_types_instead_of_dropping_the_response():
    """兜底：真出现没预料到的类型时，降级成字符串，而不是把整条响应丢掉。

    这是"失败方式"的选择——一个读数显示成 ISO 时间戳，好过一个点不动的详情弹窗。
    """
    payload = json.loads(
        webui.json_body({"t": NOW, "nested": {"d": timedelta(days=1)}})
    )
    assert isinstance(payload["t"], str)
    assert isinstance(payload["nested"]["d"], str)


# ------------------------------------------------- 安全响应头 / CSP


def get_with_headers(url: str) -> tuple[int, dict[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/events",
        "/api/state",
        "/api/events",
        "/healthz",
        "/metrics",
        "/assets/chart.umd.min.js",
        "/no-such-page",
    ],
)
def test_every_response_carries_the_security_headers(tmp_path, path):
    """面板无鉴权、监听可能是 0.0.0.0，页面里渲染的昵称/标题又来自平台：转义之外再加一层。

    **每条**响应都要带——包括 JSON、指标、静态资源和 404：漏掉任何一类，就是留了一条
    "只要换个路径就没有保护"的路。
    """
    settings = make_settings(tmp_path)
    seed_db(settings)
    write_status(settings)
    with panel(settings) as base:
        status, headers = get_with_headers(base + path)

    assert status in (200, 404)
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in headers["content-security-policy"]


def test_csp_allows_only_same_origin_and_nothing_foreign():
    csp = webui.server.CONTENT_SECURITY_POLICY
    directives = dict(part.split(" ", 1) for part in csp.split("; "))

    assert directives["default-src"] == "'none'"
    assert directives["connect-src"] == "'self'", (
        "跨源请求（数据外送的常见出路）要被拒绝"
    )
    assert directives["img-src"] == "'self' data:", "拼进外部图片 URL 的外送要被拒绝"
    assert directives["base-uri"] == "'none'"
    assert "http" not in csp, "策略里不应出现任何外部源"
    assert "'unsafe-eval'" not in csp


def test_csp_does_not_forbid_embedding_the_panel_in_an_iframe(tmp_path):
    """刻意的取舍：面板只读、没有可被劫持的操作，而不少人把它嵌在自己的 homepage /
    Home Assistant 的 iframe 里——禁掉就是白白弄坏他们。想改它的人先读这条测试。"""
    assert "frame-ancestors" not in webui.server.CONTENT_SECURITY_POLICY
    settings = make_settings(tmp_path)
    write_status(settings)
    with panel(settings) as base:
        _status, headers = get_with_headers(base + "/")
    assert "x-frame-options" not in headers


# ------------------------------------------------- 图表挂载契约（详情面板）


def test_every_chart_the_detail_script_builds_is_wired_for_the_bootstrap(tmp_path):
    """`mount()` 只处理带 `data-chart` 的节点，并靠 `data-chart-data` 找载荷。详情面板的图表
    容器曾经漏了前者，于是图例、标题都在，中间是一块**永远**不会被画出来的空白。

    这段 HTML 是前端 JS 里的字符串模板、不经过 Python 渲染，没有任何别的检查会碰到它。
    现在账号图和每条作品的图都从同一个 `trendBlock` 出来：canvas 与 `data-chart` 在同一个
    节点里、id 由同一个变量派生，没有第二个自己拼 canvas 的地方。
    """
    html = render_page(make_settings(tmp_path))

    assert html.count("<canvas") == 1, "只允许 trendBlock 里有一处 canvas 模板"
    assert (
        """data-chart="' + id + '"><canvas id="' + id + 'Canvas"></canvas></div>"""
        in html
    )
    assert """<script type="application/json" data-chart-data="' + id + '">""" in html
    assert "trendBlock('detailMetrics'" in html
    assert "trendBlock(id, p.trend" in html


def test_detail_panel_content_has_a_single_write_point_that_clears_charts_first(
    tmp_path,
):
    """详情内容的写入只能有一个出口，而且先销毁旧图、再换 DOM。

    顺序不能反：`innerHTML` 一换，旧 canvas 就脱离了面板，`clear()` 再按"是否在容器里"去找
    就找不到它们，Chart.js 实例会一直留着。实测过：连续打开 5 次详情，实例数 1→2→3→4→5。
    更隐蔽的是"加载中…"那一步——它也是一次替换，而且发生在最后渲染内容的那一处**之前**，
    所以只在渲染处调 `clear()` 看起来合理、实际上什么都没清。
    """
    import re

    html = render_page(make_settings(tmp_path))

    writes = re.findall(
        r"\.innerHTML\s*=[^=]", html[html.index("function setDetail") :]
    )
    detail_writes = re.findall(
        r"getElementById\('detailContent'\)\.innerHTML\s*=[^=]", html
    )
    assert detail_writes == [], "不能有绕过 setDetail 直接写 detailContent 的地方"
    body = html[html.index("function setDetail") : html.index("function openDetail")]
    assert body.index("dyChart.clear(holder)") < body.index("holder.innerHTML = html")
    assert writes, "前提：setDetail 里确实有那一次写入"
    # 加载中 / 加载失败 / 服务端报错 / 正式内容：四处都走它
    assert html.count("setDetail(") >= 5


def test_chart_bootstrap_prunes_charts_whose_container_is_gone():
    """兜底：任何页面忘了 `clear()` 就换掉容器，下一次 `mount()` 也会把脱离页面的实例销毁。"""
    js = webui.charts.BOOTSTRAP_JS

    assert "isConnected" in js
    assert js.index("function prune()") < js.index("function mount(")
    mount = js[js.index("function mount(") :]
    assert mount.index("prune();") < mount.index("querySelectorAll('[data-chart]')")


# ------------------------------------------------- 上游组件：把"为什么不可用"说出来


def components_with(browser_rpc: dict[str, Any]) -> dict[str, Any]:
    return upstream_snapshot(
        components={
            "postgres": {"ok": True, "latency_ms": 3},
            "redis": {"ok": True, "latency_ms": 1},
            "browser_rpc": browser_rpc,
        }
    )


def test_status_page_says_why_browser_rpc_is_unavailable(tmp_path):
    """只写"不可用"，人分不出是没连上还是状态不对，排查方向完全不同。"""
    settings = make_settings(tmp_path)
    write_status(
        settings,
        upstream=components_with(
            {
                "ok": False,
                "latency_ms": None,
                "configured": True,
                "detail_code": "unreachable",
            }
        ),
    )

    html = render_page(settings)

    assert "不可用" in html
    assert "DTK 探测它时没连上" in html
    assert "这是 DTK 那一侧的探测结果" in html, "不能让人以为是 dywatch 自己探测的"


def test_status_page_distinguishes_a_degraded_browser_rpc(tmp_path):
    settings = make_settings(tmp_path)
    write_status(
        settings,
        upstream=components_with(
            {
                "ok": False,
                "latency_ms": 12,
                "configured": True,
                "detail_code": "degraded",
            }
        ),
    )

    html = render_page(settings)

    assert "它的健康检查没有返回 ok" in html
    assert "没连上" not in html


def test_status_page_shows_an_unknown_reason_code_as_is(tmp_path):
    settings = make_settings(tmp_path)
    write_status(
        settings,
        upstream=components_with(
            {"ok": False, "configured": True, "detail_code": "brand_new_code"}
        ),
    )

    assert "DTK 给出的原因代码：brand_new_code" in render_page(settings)


def test_status_page_adds_no_explanation_when_nothing_is_wrong(tmp_path):
    """`ok` 是 None（没配）或 True（正常）时不该出现任何"为什么"——哪怕带着 detail_code。"""
    for browser_rpc in (
        {"ok": None, "configured": False},
        {"ok": None, "configured": False, "detail_code": "unreachable"},
        {"ok": True, "latency_ms": 9, "configured": True},
    ):
        settings = make_settings(tmp_path / str(len(str(browser_rpc))))
        write_status(settings, upstream=components_with(browser_rpc))
        html = render_page(settings)
        assert "DTK 探测它时没连上" not in html, browser_rpc
        assert "健康检查没有返回 ok" not in html, browser_rpc


# ------------------------------------------------- 单条作品的趋势接口


def post_trend_url(base: str, user: str, post: str) -> str:
    return f"{base}/api/user/{user}/post/{post}/trend"


def test_post_trend_route_returns_one_posts_own_increments(tmp_path):
    settings = make_settings(tmp_path)
    seed_db(settings)
    seed_metrics(settings)
    content_id = f"74{0:017d}"
    with panel(settings) as base:
        status, body = get(post_trend_url(base, ID_OK, content_id))

    payload = json.loads(body)
    assert status == 200
    assert payload["content_id"] == content_id and payload["samples"] == 2
    trend = payload["trend"]
    assert trend["has_data"] is True and trend["posts"] == 1
    delta = trend["views"]["all"]["delta"]
    # 这一条作品自己：100 → 180，新增 80（账号图里是 4 条作品之和 320）
    assert sum(v for v in delta["digg"] if v is not None) == 80


def test_post_trend_for_an_unknown_post_is_an_empty_trend_not_a_404(tmp_path):
    """ "没有这个作品"和"还没有足够的样本"是两回事：后者要由前端写明原因。"""
    settings = make_settings(tmp_path)
    seed_db(settings)
    with panel(settings) as base:
        status, body = get(post_trend_url(base, ID_OK, "999"))

    payload = json.loads(body)
    assert status == 200
    assert payload["samples"] == 0 and payload["trend"]["has_data"] is False


def test_post_trend_respects_the_metrics_switch(tmp_path):
    settings = make_settings(tmp_path, METRICS_ENABLED="false")
    seed_db(settings)
    seed_metrics(settings)
    with panel(settings) as base:
        _status, body = get(post_trend_url(base, ID_OK, f"74{0:017d}"))

    payload = json.loads(body)
    assert payload["metrics_enabled"] is False and payload["samples"] == 0


@pytest.mark.parametrize(
    ("user", "post", "which"),
    [
        ("bad%20id", "1", "sec_user_id"),
        ("a%2Fb", "1", "sec_user_id"),
        (ID_OK, "x%2Fy", "content_id"),
        (ID_OK, "..%2F..%2Fetc", "content_id"),
        (ID_OK, "a%20b", "content_id"),
        (ID_OK, "p%7Cq", "content_id"),
        (ID_OK, "a" * 300, "content_id"),
    ],
)
def test_post_trend_rejects_ids_that_could_smuggle_a_path(tmp_path, user, post, which):
    """两个 id 都是先切段、再解码、再校验：编码过的 `%2F` 解码后是 `/`，夹带不了路径。"""
    settings = make_settings(tmp_path)
    seed_db(settings)
    with panel(settings) as base:
        status, body = get(post_trend_url(base, user, post))

    assert status == 400
    assert which in json.loads(body)["error"]


def test_post_trend_without_a_state_store_is_a_404(tmp_path):
    with panel(make_settings(tmp_path)) as base:
        status, _body = get(post_trend_url(base, ID_OK, "1"))
    assert status == 404


def test_other_api_user_shapes_still_mean_the_account_detail(tmp_path):
    """新路由只认 `/api/user/<账号>/post/<作品>/trend` 这一种形状，其余行为不变。"""
    settings = make_settings(tmp_path)
    seed_db(settings)
    with panel(settings) as base:
        status, body = get(f"{base}/api/user/{ID_OK}/post/1")
    assert status == 200 and json.loads(body)["sec_user_id"] == ID_OK


# ------------------------------------------------- 趋势图的前端（字符串模板，只能在这里盯结构）


def test_post_titles_become_expand_buttons_with_data_attributes_only(tmp_path):
    """标题点开看这条作品自己的趋势。content_id 走 `data-*` 属性、点击走事件委托，
    不把任何值拼进内联 `onclick`——与详情面板里其余部分同一条安全规矩。"""
    html = render_page(make_settings(tmp_path))

    assert 'class="vtitle vtoggle" aria-expanded="false"' in html
    assert "esc(v.content_id)" in html, "content_id 要转义后放进 data-cid"
    assert 'id="postTrendRow' in html
    # 只看展开 / 收起这段 JS：页面其它地方（比如关闭按钮）有自己的内联 onclick，与它无关
    toggle = html[html.index("function togglePost") : html.index("// 事件委托")]
    assert "onclick" not in toggle
    assert "closest('.vtoggle, [data-trend]')" in html


def test_expanded_post_rows_never_write_into_a_replaced_account(tmp_path):
    """取回数据之前账号可能已经换了：只往还连在页面上的行里写，别写进别人的行。"""
    html = render_page(make_settings(tmp_path))
    body = html[html.index("function togglePost") : html.index("// 事件委托")]

    assert "row.isConnected" in body
    assert "encodeURIComponent" in body
    assert body.index("data-loading") < body.index("fetch(url)"), "加载中不重复发请求"


def test_account_trend_says_it_is_a_sum_not_one_video(tmp_path):
    """ "这条趋势是哪个视频的"——图上必须写明：不是任何一条，是该账号作品的合计。

    N 写真正加进图里的作品数；它和已知作品数不同时两个数都写，并说明没有足够记录的不参与。
    """
    html = render_page(make_settings(tmp_path))

    assert "，不是某一条；点下面的作品标题，单独看每一条" in html
    assert "' 条已知作品的合计' + tail" in html, "两个数相同：沿用简洁的说法"
    assert "这张图合计了 " in html and "没有足够记录的不参与" in html
    assert "t.posts === t.known" in html, "不能拿已知作品数冒充参与合计的作品数"
    assert "作品第一次出现的那一刻只是起点，不算新增" in html


def test_trend_has_the_three_ranges_and_two_modes(tmp_path):
    html = render_page(make_settings(tmp_path))

    for label in ("近 24 小时", "近 7 天", "总览", "'新增'", "'累计'"):
        assert label in html, label
    assert "累计新增（从这段时间的起点算起）" in html


def test_trend_payload_is_written_with_textcontent_not_innerhtml(tmp_path):
    """载荷里没有任何东西需要转义 `</script>`——因为根本不经过 HTML 解析。切换范围 / 口径
    和第一次绘制是同一条路径。"""
    html = render_page(make_settings(tmp_path))
    draw = html[html.index("function drawTrend") : html.index("// 账号级趋势")]

    assert "holder.textContent = JSON.stringify" in draw
    assert "innerHTML" not in draw
    assert "window.dyChart.redraw(el)" in draw
    assert "straight: true" in draw


# ------------------------------------------------- 上游存储：两个互不相干的量，分开写


def storage_card(tmp_path: Path, **storage: Any) -> str:
    settings = make_settings(tmp_path)
    write_status(settings, upstream=upstream_snapshot(storage=storage))
    return render_page(settings)


def test_storage_is_two_rows_not_one_dotted_line(tmp_path):
    """ "身份 20 个 · 177.7 MB"读起来像"20 个身份占了 177.7 MB"（更像内存占用）。两个数
    互不相干：前者是身份表的总行数（不区分是否可用），后者是 DTK 整个数据库的磁盘大小。"""
    html = storage_card(tmp_path, identities=20, db_size_bytes=186_319_872)

    assert "身份总数（含不可用）" in html and "20 个" in html
    assert "上游数据库大小" in html and "177.7 MB" in html
    assert "不是内存占用" in html
    assert "可用的数量看" in html
    assert "身份 20 个 ·" not in html
    assert ">上游存储<" not in html


def test_storage_rows_appear_only_for_what_the_upstream_reported(tmp_path):
    only_identities = storage_card(tmp_path / "a", identities=3)
    assert "身份总数（含不可用）" in only_identities
    assert "上游数据库大小" not in only_identities

    only_size = storage_card(tmp_path / "b", db_size_bytes=2048)
    assert "上游数据库大小" in only_size
    assert "身份总数（含不可用）" not in only_size


# ------------------------------------------------- 内联脚本不能被提前截断


@pytest.mark.parametrize(
    "name",
    ["webui.page_status._PAGE_JS", "webui.page_events._PAGE_JS"],
)
def test_an_inline_script_contains_exactly_one_script_end_tag_at_the_very_end(name):
    """这些常量自己带着 `<script>…</script>` 的外壳，而它们住在一个内联脚本里：浏览器在
    **第一个**脚本结束标记处就把脚本截断了——哪怕它写在 JS 注释里。后面的代码会被当成页面
    文字显示出来，整个页面的脚本都不工作。编译、单元测试都发现不了，只有页面真的被解析才暴露。

    `<!--` 也不行：它之后再出现 `<script` 会让解析器进入"双重转义"状态，脚本结束标记
    就不再结束脚本了。
    """
    text = eval(name, {"webui": webui})  # noqa: S307 - 只在测试里取模块常量

    assert text.lower().count("</script") == 1
    assert text.rstrip().lower().endswith("</script>")
    assert "<!--" not in text


def test_the_chart_bootstrap_is_free_of_script_terminators():
    js = webui.charts.BOOTSTRAP_JS.lower()

    assert "</script" not in js and "<!--" not in js


def test_the_rendered_status_page_has_no_javascript_leaking_into_the_document(tmp_path):
    """用符合浏览器规范的解析器看渲染结果：脚本里的代码不该出现在任何可见文字里。"""
    from html.parser import HTMLParser

    class VisibleText(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.in_script = False
            self.text: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.in_script = True

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.in_script = False

        def handle_data(self, data):
            if not self.in_script:
                self.text.append(data)

    parser = VisibleText()
    parser.feed(render_page(make_settings(tmp_path)))
    visible = "".join(parser.text)

    for marker in (
        "function ",
        "esc(",
        "trendBlock",
        "document.getElementById",
        "var ",
    ):
        assert marker not in visible, f"脚本漏进了页面文字：{marker!r}"
