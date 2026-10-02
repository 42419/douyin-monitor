"""图表：静态资源、Chart.js 引导脚本、以及"数据 → 图表载荷"的纯函数。

三条设计约束，都是为了让图表**看起来像这个面板的一部分**而不是贴上去的：

1. **颜色从 CSS 变量里取，不另开色板。** 载荷里传的是变量名（`--blue`），
   前端用 `getComputedStyle` 解析。这样深色模式切换、以后调色，图表自动跟着变，
   不会出现"页面换了配色、曲线还是上一版颜色"。
2. **图例是 HTML 的，不是 Chart.js 自己画的。** 面板已有 `.chart-legend` 与
   `theme.LEGEND_ITEM`（方括号/等宽字那一套），用同一个模板，视觉上才是同一层东西。
3. **不给图表加动画。** 状态页每 30 秒整页重载一次（`REFRESH_SECONDS`），曲线每 30 秒
   从头长一遍会非常晃眼；`animation: false` 让它是"读数"而不是"演示"。

Chart.js 是**本地打包**的静态资源（`assets/chart.umd.min.js`），不走 CDN：面板常常跑在
没有外网的内网机器上，一个拉不到的 CDN 会让整页图表变成空白，而排障的人只会看到
"面板坏了"。它按内容哈希做版本号 + 长缓存，不会被 `no-store` 拖着每次重下 200KB。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from ..models import EventKind
from .common import event_tone
from .theme import LEGEND_ITEM

#: 静态资源在包内的相对路径。
ASSET_PATH = "assets/chart.umd.min.js"
#: 浏览器取它的 URL（`?v=` 是内容哈希，见 `asset_version`）。
ASSET_URL = "/assets/chart.umd.min.js"

#: 四条曲线/柱子的颜色，全部是 CSS 变量名。
TONES: Mapping[str, tuple[str, str]] = {
    "good": ("正向", "--blue"),
    "bad": ("失败", "--red"),
    "warn": ("需注意", "--amber"),
    "quiet": ("静默", "--text3"),
}
TONE_ORDER = ("good", "bad", "warn", "quiet")

#: 互动量四条曲线的顺序与配色。
METRIC_SERIES: tuple[tuple[str, str], ...] = (
    ("digg", "点赞"),
    ("comment", "评论"),
    ("collect", "收藏"),
    ("share", "分享"),
)

_asset_cache: bytes | None = None
_version_cache: str | None = None


def asset_bytes() -> bytes:
    """静态资源的字节。给 HTTP 层用（见 `server._asset`）。"""
    return _asset_bytes()


def _asset_bytes() -> bytes:
    """读打包进来的 Chart.js。读不到返回空 bytes（页面会退化成"没有图表"而不是崩）。

    用 `importlib.resources` 而不是按 `__file__` 拼路径：安装方式是 wheel 复制，
    但也不排除有人以 zipapp / 直接调用 zip 里的包来跑；`resources` 两种都能读。
    """
    global _asset_cache
    if _asset_cache is None:
        try:
            from importlib import resources

            _asset_cache = (
                resources.files(__package__).joinpath(ASSET_PATH).read_bytes()
            )
        except (OSError, ModuleNotFoundError, AttributeError):
            _asset_cache = b""
    return _asset_cache


def asset_version() -> str:
    """静态资源的内容哈希前 10 位。**由内容算出来**，所以换了 Chart.js 版本
    忘了改常量这件事不存在（那种情况下浏览器会一直用缓存里的旧版本，而页面看起来正常）。
    """
    global _version_cache
    if _version_cache is None:
        data = _asset_bytes()
        _version_cache = hashlib.sha256(data).hexdigest()[:10] if data else ""
    return _version_cache


def script_tag() -> str:
    """`<script>` 标签；资源缺失时返回空串（图块自己会显示"未安装图表库"）。"""
    version = asset_version()
    if not version:
        return ""
    return f'<script src="{ASSET_URL}?v={version}"></script>'


def chart_block(
    *, title: str, range_text: str, legend: Sequence[tuple[str, str]], body: str
) -> str:
    """一块图表的统一外壳：标题 + 范围 + HTML 图例 + 画布（或空状态）。

    `legend` 是 `(文案, CSS 变量名)` 的序列；`body` 由调用方给（画布或空状态）。
    """
    items = "".join(
        LEGEND_ITEM.substitute(color=f"var({color})", label=label, count="")
        for label, color in legend
    )
    legend_html = f'<div class="chart-legend mono">{items}</div>' if items else ""
    return (
        '<div class="chart-block">'
        f'<div class="chart-head"><div class="section-title">{title}</div>'
        f'<div class="chart-range mono">{range_text}</div></div>'
        + legend_html
        + body
        + "</div>"
    )


def canvas(chart_id: str, payload: Mapping[str, Any]) -> str:
    """画布 + 内嵌载荷。

    载荷走 `<script type="application/json">` 而不是拼进 JS 源码：这样载荷里带引号、
    换行、标签收尾串都不会变成语法错误或提前收尾。`json.dumps` 不转义 `/`，
    所以标签收尾串要**再替换一次**成 JSON 里等价的转义写法（反斜杠加斜杠）——
    作品的标题来自平台，谁都能往里放一个 `</script>`。
    """
    encoded = json.dumps(payload, ensure_ascii=False)
    # `</` → `<\/`：JSON 里这是合法转义（`\/` == `/`），但浏览器不再把它当成标签结束。
    # 载荷里的标题来自平台，谁都能放一个 `</script>` 进去。
    encoded = encoded.replace("</", "<\\/")
    return (
        f'<div class="chart-canvas" data-chart="{chart_id}">'
        f'<canvas id="{chart_id}"></canvas></div>'
        f'<script type="application/json" data-chart-data="{chart_id}">{encoded}</script>'
    )


def empty_block(message: str) -> str:
    return f'<div class="chart-empty mono">{message}</div>'


def metric_legend() -> list[tuple[str, str]]:
    colors = {"digg": "--blue", "comment": "--green", "collect": "--amber", "share": "--text2"}
    return [(label, colors[key]) for key, label in METRIC_SERIES]


def tone_legend() -> list[tuple[str, str]]:
    return [(TONES[tone][0], TONES[tone][1]) for tone in TONE_ORDER]


# =================== 数据 → 载荷（纯函数） ===================

def _batch_labels(since: datetime, until: datetime, count: int, *, hourly: bool) -> list[str]:
    """把一段时间切成 `count` 段，给每段一个短标签。

    标签取每段的**起点**（"13:00" 表示 13:00~14:00 这一格），而不是中点：
    中点会让人把 12:30 读成"12 点这一格"，而排障时"是哪个小时出的问题"要能一眼对上。
    """
    if count <= 0:
        return []
    span = (until - since).total_seconds()
    if span <= 0:
        return []
    step = span / count
    out: list[str] = []
    for index in range(count):
        stamp = (since + timedelta(seconds=step * index)).astimezone()
        out.append(stamp.strftime("%H:%M" if hourly else "%m-%d"))
    return out


def bucket_ticks(
    ticks: Iterable[tuple[datetime, str]],
    *,
    since: datetime,
    until: datetime,
    count: int,
) -> dict[str, list[int]]:
    """`(时间, 类型)` 序列 → 每格各类色调的事件数（给堆叠柱状图用）。

    **落在窗口外的点被丢掉而不是夹到首尾格**：夹进去会在图上造出一个不存在的高峰，
    而这张图的用途恰恰是"哪个小时出了问题"。
    """
    out = {tone: [0] * max(0, count) for tone in TONE_ORDER}
    span = (until - since).total_seconds()
    if span <= 0 or count <= 0:
        return out
    for stamp, kind in ticks:
        offset = (stamp - since).total_seconds()
        if offset < 0 or offset > span:
            continue
        index = min(int(offset / span * count), count - 1)
        out[_tone_of(kind)][index] += 1
    return out


def _tone_of(kind: Any) -> str:
    try:
        return event_tone(EventKind(str(kind)))
    except ValueError:  # 库里可能有已下线的旧类型
        return "quiet"


def events_chart_payload(
    ticks: Iterable[tuple[datetime, str]],
    *,
    since: datetime,
    until: datetime,
    count: int,
    hourly: bool,
) -> dict[str, Any]:
    buckets = bucket_ticks(ticks, since=since, until=until, count=count)
    return {
        "type": "bar",
        "labels": _batch_labels(since, until, count, hourly=hourly),
        "datasets": [
            {"label": TONES[tone][0], "color": TONES[tone][1], "data": buckets[tone]}
            for tone in TONE_ORDER
        ],
    }


def metrics_chart_payload(
    series: Sequence[Mapping[str, Any]], *, hourly: bool = True
) -> dict[str, Any]:
    """一个账号逐小时的互动量 → 折线图载荷。

    缺值是 `null`（断点）而不是 0：平台的载荷里没有这个数时，写 0 会在曲线上造出一个
    "数据掉到零"的悬崖，而平台从没这么说过（`models.py` 的第一条契约）。
    """
    labels: list[str] = []
    for row in series:
        stamp = row.get("hour")
        labels.append(stamp.astimezone().strftime("%H:%M" if hourly else "%m-%d") if stamp else "")
    return {
        "type": "line",
        "labels": labels,
        "datasets": [
            {"label": label, "color": _series_color(key), "data": [row.get(key) for row in series]}
            for key, label in METRIC_SERIES
        ],
    }


def _series_color(key: str) -> str:
    return {"digg": "--blue", "comment": "--green", "collect": "--amber", "share": "--text2"}[key]


# =================== 前端引导 ===================

#: 一次注入所有页面：主题变量解析、数字缩写、折线/柱状两种图、跟随系统配色重建。
#: 写法刻意是 ES5（`var` + `function`）——面板要在手机上、在内网的老浏览器里也能开。
BOOTSTRAP_JS = r"""
(function () {
  var charts = [];   // {canvas, payload, chart}

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement)
      .getPropertyValue(name).trim();
    return value || fallback || '#888';
  }
  function color(spec) {
    // 载荷里存的是 CSS 变量名（'--blue'），也接受直接的颜色值
    return spec && spec.indexOf('--') === 0 ? cssVar(spec) : (spec || '#888');
  }
  // 纵轴与提示里的数字：上万折成"万"，上亿折成"亿"——这个面板是给人扫一眼的，
  // "1234567" 读起来要数位数，而 "123.5万" 不用
  function fmtNum(value) {
    if (value === null || value === undefined) return '-';
    var n = Number(value);
    if (!isFinite(n)) return '-';
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(1) + ' 亿';
    if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(1) + ' 万';
    return String(n);
  }

  function baseOptions() {
    var line = cssVar('--line'), line2 = cssVar('--line-2');
    var text3 = cssVar('--text3'), text2 = cssVar('--text2'), text = cssVar('--text');
    var panel = cssVar('--panel');
    return {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: {mode: 'index', intersect: false},
      layout: {padding: {top: 4, right: 2, bottom: 0, left: 0}},
      plugins: {
        legend: {display: false},   // 图例是页面上的 HTML
        tooltip: {
          backgroundColor: panel,
          titleColor: text3,
          bodyColor: text,
          borderColor: line,
          borderWidth: 1,
          titleFont: {size: 11},
          bodyFont: {size: 12},
          padding: 8,
          displayColors: true,
          boxWidth: 8, boxHeight: 8, boxPadding: 4,
          callbacks: {
            label: function (item) {
              return ' ' + item.dataset.label + ' ' + fmtNum(item.parsed.y);
            }
          }
        }
      },
      scales: {
        x: {
          grid: {display: false},
          border: {color: line},
          ticks: {
            color: text3, font: {size: 10},
            maxRotation: 0, autoSkipPadding: 12
          }
        },
        y: {
          beginAtZero: true,
          grid: {color: line2, drawTicks: false},
          border: {display: false},
          ticks: {color: text3, font: {size: 10}, padding: 6,
                  callback: function (v) { return fmtNum(v); }}
        }
      }
    };
  }

  function build(canvas, payload) {
    var opts = baseOptions();
    if (payload.type === 'bar') {
      opts.scales.x.stacked = true;
      opts.scales.y.stacked = true;
    }
    var datasets = [];
    for (var i = 0; i < payload.datasets.length; i++) {
      var d = payload.datasets[i];
      var c = color(d.color);
      var flags = {
        label: d.label, data: d.data, borderColor: c,
        /* 曲线上的点密度是"每小时一个"，画点会变成一串珠子 */
        pointRadius: 0, pointHoverRadius: 3,
        pointHoverBackgroundColor: c, borderWidth: 1.5
      };
      if (payload.type === 'bar') {
        flags.backgroundColor = c;
        flags.borderRadius = 2;
        flags.borderSkipped = false;
        flags.barPercentage = 0.86;
        flags.categoryPercentage = 0.9;
      } else {
        flags.tension = 0.25;
        flags.spanGaps = false;   // 缺值就是断点，不连线
      }
      datasets.push(flags);
    }
    return new Chart(canvas.getContext('2d'), {
      type: payload.type,
      data: {labels: payload.labels, datasets: datasets},
      options: opts
    });
  }

  function readPayload(el) {
    var holder = document.querySelector(
      'script[data-chart-data="' + el.getAttribute('data-chart') + '"]');
    if (!holder) return null;
    try { return JSON.parse(holder.textContent); } catch (e) { return null; }
  }

  function mount(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll('[data-chart]');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var canvas = el.querySelector('canvas');
      var payload = readPayload(el);
      if (!canvas || !payload || el.getAttribute('data-chart-ready') === '1') continue;
      el.setAttribute('data-chart-ready', '1');
      charts.push({el: el, canvas: canvas, payload: payload, chart: build(canvas, payload)});
    }
  }

  function rebuild() {
    for (var i = 0; i < charts.length; i++) {
      var item = charts[i];
      if (item.chart) item.chart.destroy();
      item.chart = build(item.canvas, item.payload);
    }
  }

  // 局部刷新前必须先把旧图上交的实例销毁：`innerHTML = html` 只删 DOM，
  // Chart.js 仍握着那个 canvas 与它的监听器，反复刷新就是一路泄漏
  function clear(root) {
    var scope = root || document;
    var kept = [];
    for (var i = 0; i < charts.length; i++) {
      var item = charts[i];
      if (scope.contains(item.el)) {
        if (item.chart) item.chart.destroy();
      } else {
        kept.push(item);
      }
    }
    charts = kept;
  }

  // 系统配色一变（用户在系统里切了深色模式）就得重建：canvas 上的颜色是画上去的，
  // CSS 变量变了它不会自己重画，会留下"浅色页面 + 深色曲线"的半截状态
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    var onTheme = function () { rebuild(); };
    if (mq.addEventListener) mq.addEventListener('change', onTheme);
    else if (mq.addListener) mq.addListener(onTheme);
  }

  window.dyChart = {
    mount: mount, clear: clear, rebuild: rebuild, fmtNum: fmtNum, cssVar: cssVar
  };
})();
"""


__all__ = [
    "ASSET_PATH",
    "ASSET_URL",
    "BOOTSTRAP_JS",
    "METRIC_SERIES",
    "TONE_ORDER",
    "TONES",
    "asset_version",
    "bucket_ticks",
    "canvas",
    "chart_block",
    "empty_block",
    "events_chart_payload",
    "metric_legend",
    "metrics_chart_payload",
    "script_tag",
    "tone_legend",
]
