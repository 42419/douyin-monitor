"""Web 面板与探针。

面板要回答两个问题：**每个账号现在怎么样**（状态页）与**最近到底发生了什么**（事件页）。

它不触发任何上游请求 —— 状态页读 `status.json`（每轮写一次的快照），事件页与详情读状态库
（SQLite，只 `SELECT`）。所以打开面板不消耗身份、不会碰到风控，也不会因为上游抖动而变慢。
唯一一次"顺手多读一次库"发生在 `/metrics`（见 `server.metrics_text` 的说明）。

路由清单在 `server` 的模块头。这里只做**门面**：把拆开的各部分重新导出一个名字，
于是 `from dywatch.webui import render_page` 这类既有写法一行都不用改。

模块地图（按依赖方向，上面的是下面的基础）：

| 模块 | 放什么 |
| --- | --- |
| `theme` | 全部 CSS、页面骨架、共用小组件模板 |
| `common` | 快照读取、类型归一化、HTML 转义、账号分级、LED / 数据条 / 横幅 |
| `charts` | Chart.js 静态资源、前端引导脚本、数据分桶（纯函数） |
| `queries` | 只读状态库查询（账号详情 / 事件 / 规模统计） |
| `page_status` | 状态页 `GET /` |
| `page_events` | 事件时间线 `GET /events` |
| `server` | HTTP 路由、探针、`/metrics`、服务器生命周期 |

拆分的原因很直接：面板原来是一个 1300 行的单文件，加图表与第二个页面之后就没法改了。
**视觉与交互仍然是原来那一套**（技术仪表盘：方括号读数、LED 阵列、等宽数字），
拆的时候 CSS 是整段搬过来的，一个字都没改。

默认只听 `127.0.0.1` 且**没有鉴权**（设计里是明确的决定）：要暴露出去就自己加反代鉴权。
"""

from __future__ import annotations

from . import charts, common, page_events, page_status, queries, server, theme, trend
from .charts import (
    BOOTSTRAP_JS,
    asset_bytes,
    asset_version,
    bucket_ticks,
    events_chart_payload,
)
from .common import (
    LED_SLOTS,
    STATUS_KEYS,
    STATUS_LEGEND,
    _as_int,
    _as_mapping,
    _as_number,
    _escape_html,
    _gate_html,
    _quantize_blocks,
    _render_ledarray,
    _render_stats,
    _selfcheck_html,
    classify_account,
    event_tone,
    read_status,
)
from .page_events import (
    GROUPS as EVENT_GROUPS,
    RANGES as EVENT_RANGES,
    build_view as build_events_view,
    delivery_state,
    kinds_for,
    parse_filters as parse_event_filters,
    query_string,
    render_fragment as render_events_fragment,
    render_page as render_events_page,
    summarize_payload as summarize_event,
)
from .page_status import PANEL_TITLE, REFRESH_SECONDS, build_health, render_page
from .queries import _parse_dt, user_detail
from .server import (
    PanelServer,
    _Handler,
    _PanelServer,
    _dtk_ok,
    _label,
    _metrics_label,
    _store_ok,
    access_urls,
    events_payload,
    guess_lan_ip,
    json_body,
    metrics_text,
)

__all__ = [
    "BOOTSTRAP_JS",
    "EVENT_GROUPS",
    "EVENT_RANGES",
    "LED_SLOTS",
    "PANEL_TITLE",
    "PanelServer",
    "REFRESH_SECONDS",
    "STATUS_KEYS",
    "STATUS_LEGEND",
    "_Handler",
    "_PanelServer",
    "_as_int",
    "_as_mapping",
    "_as_number",
    "_dtk_ok",
    "_escape_html",
    "_gate_html",
    "_label",
    "_metrics_label",
    "_parse_dt",
    "_quantize_blocks",
    "_render_ledarray",
    "_render_stats",
    "_selfcheck_html",
    "_store_ok",
    "access_urls",
    "asset_bytes",
    "asset_version",
    "bucket_ticks",
    "build_events_view",
    "build_health",
    "charts",
    "classify_account",
    "common",
    "delivery_state",
    "event_tone",
    "events_chart_payload",
    "events_payload",
    "guess_lan_ip",
    "json_body",
    "kinds_for",
    "metrics_text",
    "page_events",
    "page_status",
    "parse_event_filters",
    "queries",
    "query_string",
    "read_status",
    "render_events_fragment",
    "render_events_page",
    "render_page",
    "server",
    "summarize_event",
    "theme",
    "trend",
    "user_detail",
]
