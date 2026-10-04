"""事件时间线页：过滤、摘要文案、图表分桶、片段刷新、注入防护。

这一页最容易出的两类错，测试就盯着这两类：

1. **枚举漏了**——事件类型有 16 种，分到 5 个类别里。漏一种的后果是"某个事件在'全部'里
   看得见、按类别却永远筛不出来"，而人只会以为自己点错了。
2. **图和列表用了不同的过滤条件**——柱子上 5 根、列表里 1 条，看起来像采集漏了数据。
   所以过滤条件只有一处（`build_view`），图和列表都从同一个 `view` 取。
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterator

import pytest

from dywatch import webui
from dywatch.models import Event, EventKind
from dywatch.settings import Settings, load_settings
from dywatch.state import StateStore

NOW = datetime.now(timezone.utc)
ID_A = "MS4wLjABAAAAaaaa"
ID_B = "MS4wLjABAAAAbbbb"


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


def seed(settings: Settings, rows: list[tuple[datetime, Event]]) -> None:
    store = StateStore(settings.db_path)
    store.migrate()
    store.ensure_author(ID_A, "老徐烤串", NOW - timedelta(days=30))
    store.ensure_author(ID_B, "夜跑团", NOW - timedelta(days=30))
    for stamp, event in rows:
        store.record_system_event(event, now=stamp)
    store.close()


def rows(*triples: tuple[float, str, str]) -> list[tuple[datetime, Event]]:
    """`(小时前, sec_user_id, 类型)` → 事件行。"""
    out = []
    for hours_ago, uid, kind in triples:
        out.append(
            (
                NOW - timedelta(hours=hours_ago),
                Event(
                    EventKind(kind),
                    sec_user_id=uid,
                    nickname="x",
                    payload={"code": "X"},
                ),
            )
        )
    return out


def filters(**overrides: Any) -> dict[str, Any]:
    base = webui.parse_event_filters({})
    base.update(overrides)
    return base


@contextlib.contextmanager
def panel(settings: Settings) -> Iterator[str]:
    server = webui.PanelServer(settings)
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


# --------------------------------------------------------------------- 枚举覆盖


def test_every_event_kind_belongs_to_exactly_one_group():
    """16 种事件分到 5 个类别里，**不漏不重**。

    漏一种 → 它在"全部"里看得见、按类别永远筛不出来；
    重一种 → 同一个事件出现在两个类别下，人无法确定自己看的是不是全集。
    """
    seen: list[EventKind] = []
    for key, (_label, kinds) in webui.EVENT_GROUPS.items():
        if key == "all":
            assert kinds == (), "「全部」不该显式列出类型——它是'不过滤'，不是一份清单"
            continue
        seen.extend(kinds)
    assert sorted(kind.value for kind in seen) == sorted(
        kind.value for kind in EventKind
    )
    assert len(seen) == len(set(seen)), "有事件被分到了两个类别里"


def test_every_range_is_reachable_and_ordered():
    spans = [spec["span"] for spec in webui.EVENT_RANGES.values()]
    assert spans == sorted(spans), "窗口必须递增，否则下拉的语义是乱的"
    for spec in webui.EVENT_RANGES.values():
        assert spec["count"] > 1 and spec["limit"] > 0


# --------------------------------------------------------------------- 过滤


@pytest.mark.parametrize(
    "query",
    [
        {"range": ["90d"], "group": ["nothing"]},
        {"range": [""], "group": [""]},
        {"range": ["1"], "group": ["post"]},
    ],
)
def test_unknown_filters_fall_back_to_defaults(query):
    """过期的收藏链接应当给默认视图，而不是 400——面板是排查问题时打开的东西。"""
    parsed = webui.parse_event_filters(query)
    assert parsed["range"] in webui.EVENT_RANGES
    assert parsed["group"] in webui.EVENT_GROUPS
    assert parsed["kind"] == ""


def test_unknown_kind_is_ignored_but_known_kind_wins_over_group():
    assert webui.parse_event_filters({"kind": ["no_such_kind"]})["kind"] == ""
    parsed = webui.parse_event_filters({"kind": ["new_post"], "group": ["system"]})
    assert parsed["kind"] == "new_post"
    assert webui.kinds_for(parsed) == ("new_post",)


def test_kinds_for_group_lists_every_member():
    parsed = webui.parse_event_filters({"group": ["system"]})
    assert webui.kinds_for(parsed) == ("upstream_degraded", "self_degraded")
    assert webui.kinds_for(webui.parse_event_filters({})) == ()  # 「全部」== 不过滤


def test_query_string_keeps_defaults_out_and_round_trips():
    parsed = webui.parse_event_filters({"author": ["MS4wLg=="], "group": ["post"]})
    query = webui.query_string(parsed)
    # `=` 必须被编码，否则再解析一次就被截断了
    assert "MS4wLg%3D%3D" in query and "range=" not in query
    again = webui.parse_event_filters({"author": ["MS4wLg=="], "group": ["post"]})
    assert again == parsed


# --------------------------------------------------------------------- 摘要文案


def test_summarize_covers_every_kind_without_raising():
    """每种事件都得给出非空的人话——空白会让人以为"这条事件没有载荷"。"""
    payloads = {
        EventKind.NEW_POST: {
            "content": {"title": "标题", "content_id": "1"},
            "gap_days": 3,
        },
        EventKind.POST_REMOVED: {"removed": [{"content_id": "1", "title": "没了"}]},
        EventKind.ALL_GONE: {
            "removed": [{"content_id": "1", "title": "没了"}],
            "all_gone": True,
        },
        EventKind.REVIVED: {"title": "回来了"},
        EventKind.TITLE_CHANGED: {"old": "旧", "new": "新"},
        EventKind.HIDDEN_FROM_GUEST: {"hidden": [{"content_id": "1", "title": "隐"}]},
        EventKind.GAP_DETECTED: {
            "oldest_in_page": "2026-09-01T00:00:00+00:00",
            "previous_newest": "2026-08-30T00:00:00+00:00",
            "fetch_count": 20,
        },
        EventKind.NEVER_SEEN: {"rounds": 90},
        EventKind.STALE_NO_UPDATE: {"days": 20},
        EventKind.ACCOUNT_FAILED: {"fails": 3, "code": "403", "message": "没权限"},
        EventKind.ACCOUNT_RECOVERED: {"fails": 3},
        EventKind.UPSTREAM_DEGRADED: {
            "code": "RATE_LIMITED",
            "message": "限流",
            "gate_seconds": 600,
        },
        EventKind.SELF_DEGRADED: {"reason": "disk_low", "free_mb": 10},
        EventKind.INITIALIZED: {"count": 20},
        EventKind.SCROLLED_OUT: {"content_id": "123"},
        EventKind.TRIMMED: {"content_id": "123"},
    }
    assert sorted(payloads) == sorted(EventKind), "新增事件类型时要在这里补一份载荷样本"
    for kind, payload in payloads.items():
        text = webui.summarize_event(kind.value, payload)
        assert text.strip(), kind
    # 几个具体的：错误码要出现（排障第一眼看的就是它）
    assert "403" in webui.summarize_event(
        "account_failed", payloads[EventKind.ACCOUNT_FAILED]
    )
    assert "磁盘" in webui.summarize_event(
        "self_degraded", payloads[EventKind.SELF_DEGRADED]
    )
    assert "→" in webui.summarize_event(
        "title_changed", payloads[EventKind.TITLE_CHANGED]
    )


def test_summarize_flattens_and_clips_hostile_titles():
    """载荷里的标题来自平台：换行会把"一行一条"的列表撑开，长标题会挤掉推送状态。"""
    text = webui.summarize_event(
        "new_post", {"content": {"title": "第一行\n第二行\r第三行"}}
    )
    assert "\n" not in text and "\r" not in text
    long = webui.summarize_event("new_post", {"content": {"title": "长" * 200}})
    assert len(long) <= 47 and long.endswith("…")


def test_summarize_unknown_shape_falls_back_to_json_not_blank():
    text = webui.summarize_event("new_post", {"something": "else"})
    assert "something" in text
    assert webui.summarize_event("new_post", {}) == ""


# --------------------------------------------------------------------- 分桶


def test_bucket_ticks_drops_points_outside_the_window():
    """夹到首尾格会在图上造出一个不存在的高峰，而这张图就是用来"看哪个小时出的问题"的。"""
    since, until = NOW - timedelta(hours=2), NOW
    ticks = [
        (since - timedelta(hours=5), "new_post"),
        (since + timedelta(minutes=1), "new_post"),
        (until + timedelta(hours=3), "new_post"),
    ]
    buckets = webui.bucket_ticks(ticks, since=since, until=until, count=4)
    assert sum(sum(values) for values in buckets.values()) == 1


def test_bucket_ticks_puts_the_last_edge_inside_the_last_bucket():
    since, until = NOW - timedelta(hours=1), NOW
    buckets = webui.bucket_ticks(
        [(until, "new_post")], since=since, until=until, count=4
    )
    assert buckets["good"] == [0, 0, 0, 1]


def test_events_chart_payload_sums_to_the_window_total():
    since, until = NOW - timedelta(hours=24), NOW
    ticks = [
        (since + timedelta(hours=1), "new_post"),
        (since + timedelta(hours=1, minutes=5), "account_failed"),
        (since + timedelta(hours=20), "hidden_from_guest"),
        (since + timedelta(hours=20), "self_degraded"),
    ]
    payload = webui.events_chart_payload(
        ticks, since=since, until=until, count=24, hourly=True
    )
    assert [item["label"] for item in payload["datasets"]] == list(
        webui.charts.TONES[key][0] for key in webui.charts.TONE_ORDER
    )
    assert sum(sum(item["data"]) for item in payload["datasets"]) == 4
    assert len(payload["labels"]) == 24
    # 静默事件（对访客不可见）不该和"失败"共用一档，否则图上只剩两种颜色
    tones = {item["label"]: sum(item["data"]) for item in payload["datasets"]}
    assert (
        tones["正向"] == 1
        and tones["失败"] == 1
        and tones["需注意"] == 1
        and tones["静默"] == 1
    )


def test_metrics_chart_payload_keeps_gaps_as_null():
    """缺值是断点、不是 0：写 0 会在曲线上造出一个平台从没说过的悬崖。"""
    series = [
        {
            "hour": NOW - timedelta(hours=1),
            "digg": 10,
            "comment": None,
            "share": 1,
            "collect": 2,
        },
        {"hour": NOW, "digg": None, "comment": 3, "share": 0, "collect": None},
    ]
    payload = webui.metrics_chart_payload(series)
    digg = next(item for item in payload["datasets"] if item["label"] == "点赞")
    assert digg["data"] == [10, None]


# --------------------------------------------------------------------- 投递状态


def test_delivery_state_separates_designed_silence_from_a_lost_push():
    """`静默`（设计如此）和`未推送`（该推没推成）必须分开：混成一个灰点，
    就再也看不出通知链路到底有没有在工作。"""
    silent = {"kind": "hidden_from_guest", "delivery": {}}
    lost = {"kind": "new_post", "delivery": {}}
    sent = {"kind": "new_post", "delivery": {"sent": ["dingtalk"], "failed": {}}}
    failed = {
        "kind": "new_post",
        "delivery": {"sent": [], "failed": {"dingtalk": "HTTP 500"}},
    }
    assert webui.delivery_state(silent) == "silent"
    assert webui.delivery_state(lost) == "none"
    assert webui.delivery_state(sent) == "sent"
    assert webui.delivery_state(failed) == "failed"


# --------------------------------------------------------------------- 页面


def test_events_page_renders_rows_chart_and_delivery_state(tmp_path):
    settings = make_settings(tmp_path)
    seed(
        settings,
        rows(
            (1, ID_A, "new_post"),
            (2, ID_B, "account_failed"),
            (30, ID_A, "post_removed"),  # 24 小时窗口外
        ),
    )

    with panel(settings) as base:
        status, body = get(base + "/events")
        assert status == 200
        assert "DYWATCH / EVENTS" in body
        assert "老徐烤串" in body and "夜跑团" in body
        assert "eventsChart" in body
        # 摘要与状态都要在页面上（而不是只在 JSON 接口里）
        assert "静默" in body or "未推送" in body

        status, body = get(base + "/events?range=24h")
        assert status == 200

        status, body = get(base + "/events?range=24h&group=post")
        assert status == 200 and "新作品" in body


def test_events_fragment_is_not_a_full_page(tmp_path):
    """片段接口只给"汇总 + 图 + 列表"：整页重载会丢掉滚动位置。"""
    settings = make_settings(tmp_path)
    seed(settings, rows((1, ID_A, "new_post")))
    with panel(settings) as base:
        status, body = get(base + "/events?range=24h&frag=1")
        assert status == 200
        assert "<html" not in body and "<!DOCTYPE" not in body
        assert "tl-item" in body and "eventsChart" in body


def test_author_filter_narrows_chart_and_list_together(tmp_path):
    """图和列表必须用同一组过滤条件：柱子上 5 根、列表里 1 条，看起来像采集漏了数据。"""
    settings = make_settings(tmp_path)
    seed(
        settings,
        rows(
            (1, ID_A, "new_post"),
            (2, ID_B, "account_failed"),
            (3, ID_B, "new_post"),
        ),
    )
    view = webui.build_events_view(settings, filters(author=ID_A))
    assert view["total_rows"] == 1
    assert view["chart_total"] == 1
    assert view["author_label"] == "老徐烤串"

    whole = webui.build_events_view(settings, filters())
    assert whole["chart_total"] == 3


def test_events_page_without_a_database_says_so(tmp_path):
    settings = make_settings(tmp_path)
    with panel(settings) as base:
        status, body = get(base + "/events")
    assert status == 200
    assert "状态库还没有数据" in body


def test_events_endpoint_matches_the_page(tmp_path):
    settings = make_settings(tmp_path)
    seed(settings, rows((1, ID_A, "new_post"), (2, ID_B, "account_failed")))
    with panel(settings) as base:
        status, body = get(base + "/api/events?range=24h")
    assert status == 200
    payload = json.loads(body)
    assert payload["total"] == 2
    assert payload["window"]["range"] == "24h"
    kinds = {item["kind"] for item in payload["events"]}
    assert kinds == {"new_post", "account_failed"}
    assert all(item["text"] for item in payload["events"])


def test_events_page_escapes_a_payload_that_tries_to_close_the_script_tag(tmp_path):
    """载荷来自平台：塞进内嵌 JSON 的那一段必须没法提前收尾 `<script>`。

    这一条是**回归**：第一版把载荷直接 `json.dumps` 进 `<script type="application/json">`，
    而 `json.dumps` 不转义 `/`——标题里放一个 `</script>` 就能把后面的页面结构当 HTML 解析。
    """
    settings = make_settings(tmp_path)
    store = StateStore(settings.db_path)
    store.migrate()
    store.ensure_author(ID_A, "老徐烤串", NOW)
    store.record_system_event(
        Event(
            EventKind.NEW_POST,
            sec_user_id=ID_A,
            nickname="老徐烤串",
            payload={"content": {"title": "</script><img src=x onerror=alert(1)>"}},
        ),
        now=NOW - timedelta(minutes=5),
    )
    store.close()

    view = webui.build_events_view(settings, filters())
    fragment = webui.page_events.render_fragment(view)
    # 载荷块必须刚好一个收尾标签（图表那一个），多出来的就意味着标题里的那个跑出来了
    assert fragment.count("</script>") == 1
    # 摘要进了 HTML 正文，所以它必须是转义过的
    assert "<img" not in fragment and "&lt;/script&gt;" in fragment


def test_events_page_hides_nothing_when_the_window_is_empty(tmp_path):
    settings = make_settings(tmp_path)
    seed(settings, rows((40, ID_A, "new_post")))  # 只在 7 天窗口里
    with panel(settings) as base:
        status, body = get(base + "/events?range=24h")
        assert status == 200 and "这个窗口里没有事件" in body
        status, body = get(base + "/events?range=7d")
        assert status == 200 and "这个窗口里没有事件" not in body


def test_events_query_string_keeps_the_filter_when_clicking_a_chip(tmp_path):
    settings = make_settings(tmp_path)
    seed(settings, rows((1, ID_A, "new_post")))
    page_html = webui.render_events_page(settings, filters(range="7d", group="post"))
    assert 'href="/events?range=7d&group=account"' in page_html
    # 当前项要标出来，否则点完看不出自己在哪个视图里
    assert 'class="on"' in page_html


def test_a_chart_payload_cannot_close_the_script_tag(tmp_path):
    """回归：图表载荷里的标题来自平台，谁都能往里放一个 `</script>`。

    `json.dumps` **不转义 `/`**，所以标签收尾串会原样进 HTML，浏览器在那里提前收掉
    脚本块、后面的内容就成了一小段可注入的 HTML。`canvas()` 里的 `</` → `<\\/` 是
    唯一挡住它的东西——而注入用例原先只覆盖了 `/events` 列表那一侧，图表这条路径是空的。
    """
    html = webui.charts.canvas("c1", {"title": "</script><img src=x onerror=alert(1)>"})

    assert html.count("</script>") == 1, "唯一的标签收尾只能是 canvas 自己那个"
    assert "<\\/script>" in html, "载荷里的收尾串必须是 JSON 等价的转义写法"
    assert html.count("<script") == 1, "不能再冒出一个真的 script 开始标签"

    # HTML 解析器对大小写不敏感：`</SCRIPT` 一样会收尾
    upper = webui.charts.canvas("c2", {"title": "</SCRIPT >"})
    assert "</SCRIPT" not in upper
    assert upper.count("</script>") == 1


class _CanvasAudit(HTMLParser):
    """收集页面里每个 `<canvas>` 的祖先里有没有 `data-chart`，以及有哪些图表载荷。"""

    def __init__(self) -> None:
        super().__init__()
        self._stack: list[dict[str, str | None]] = []
        self.canvases: list[
            tuple[str, str | None]
        ] = []  # (canvas id, 所在容器的 data-chart)
        self.payload_ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("data-chart-data"):
            self.payload_ids.add(str(attributes["data-chart-data"]))
        if tag == "canvas":
            owner = next(
                (a["data-chart"] for a in reversed(self._stack) if "data-chart" in a),
                None,
            )
            self.canvases.append((str(attributes.get("id")), owner))
        if tag not in ("canvas", "script", "br", "img", "input", "meta", "link"):
            self._stack.append(attributes)

    def handle_endtag(self, tag: str) -> None:
        if (
            tag not in ("canvas", "script", "br", "img", "input", "meta", "link")
            and self._stack
        ):
            self._stack.pop()


def test_events_page_charts_are_wired_for_the_bootstrap(tmp_path):
    """每个 `<canvas>` 都得在带 `data-chart` 的容器里、且有同名的载荷脚本——
    `mount()` 靠这两样才认得它。缺任何一样，图表区域就只剩一块空白。"""
    settings = make_settings(tmp_path)
    seed(settings, rows((5, ID_A, "new_post"), (30, ID_B, "post_removed")))

    with panel(settings) as base:
        status, html = get(f"{base}/events?range=30d")

    audit = _CanvasAudit()
    audit.feed(html)
    assert status == 200
    assert audit.canvases, "前提：页面里确实有图表"
    for canvas_id, owner in audit.canvases:
        assert owner is not None, f"{canvas_id} 不在任何 data-chart 容器里"
        assert owner in audit.payload_ids, f"{owner} 没有对应的 data-chart-data 载荷"


def test_chart_bootstrap_mounts_what_is_already_on_the_page_at_load():
    """页面加载完就要把已经在 DOM 里的图挂上。

    以前这一步靠每个页面自己记得调用：`/events` 没调，图表要等 60 秒一次的局部刷新才第一次
    出现——之前是一片空白，人只会以为面板坏了。`mount` 幂等（`data-chart-ready`），所以
    页面里已有的显式调用不受影响。
    """
    js = webui.charts.BOOTSTRAP_JS

    assert "DOMContentLoaded" in js and "mount(document)" in js
    assert "document.readyState" in js, (
        "脚本可能在 DOM 就绪之后才执行，两种情况都要覆盖"
    )


def test_chart_bootstrap_says_so_when_it_cannot_draw():
    """一块空白的画布看起来只会是"面板坏了"。库没加载、或绘制抛了异常，都要在原地说清楚。"""
    js = webui.charts.BOOTSTRAP_JS

    assert "typeof Chart === 'undefined'" in js
    assert "图表库没有加载" in js and "/assets/chart.umd.min.js" in js
    assert "图表绘制失败" in js
    assert "textContent" in js, "报错文本里可能带任何字符，不能走 innerHTML"
