"""状态页：`GET /`。

一页回答一个问题：**每个账号现在怎么样**。所以它的骨架是"总览读数 → 账号列表 → 逐账号详情"，
加上两条横幅（上游闸门关了 / 监控自己降级了）——那两件事必须先于任何账号被看见。

这里只有拼 HTML 的逻辑，数据来自 `common.read_status`（快照）与 `queries`（状态库，
只在详情接口里用）。**图上不发任何上游请求**：列表读每轮写一次的快照，详情读状态库；
所以打开面板不消耗身份、不碰风控、也不会因为上游抖动而变慢。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..settings import Settings
from . import charts
from .common import (
    STATUS_KEYS,
    _as_int,
    _escape_html,
    _gate_html,
    _overall_line,
    _render_ledarray,
    _render_row,
    _render_stats,
    _selfcheck_html,
    classify_account,
    read_status,
)
from .theme import masthead, page

#: 页面自动刷新的秒数（`<meta http-equiv="refresh">`，不需要 JS 参与）。
#:
#: 事件时间线页不用它（见 `theme.page` 的说明），但状态页用它是对的：这里的所有读数都是
#: "当前状态"，没有需要保留的交互状态（详情弹窗除外，它本来就会被重载关掉）。
REFRESH_SECONDS = 30

PANEL_TITLE = "dywatch · 抖音监控状态"


# =================== 上游健康 ===================

#: 组件名 → 展示名。DTK 的 `browser_rpc` 在页面上写成 `browser-rpc`，和它自己文档里一致。
_COMPONENT_LABELS: Mapping[str, str] = {
    "postgres": "postgres",
    "redis": "redis",
    "browser_rpc": "browser-rpc",
}
_POOL_LABELS: Mapping[str, str] = {"douyin": "抖音", "tiktok": "TikTok"}
#: 身份池各状态的展示顺序与文案。顺序照着"从可用到不可用"排。
_POOL_STATES: tuple[tuple[str, str], ...] = (
    ("active", "活跃"),
    ("minting", "铸币中"),
    ("cooling", "冷却"),
    ("degraded", "降级"),
    ("retired", "退役"),
)


def _human_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return "—"  # pragma: no cover - 循环必然 return


def _human_duration(value: Any) -> str:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return "—"
    days, rest = divmod(max(0, seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分钟"


#: 上游组件的原因代码 → 一句人话。代码来自 DTK 的 `system/status`（目前只有 browser_rpc 会给）；
#: 认不得的代码原样显示，不猜。措辞只陈述"DTK 报告了什么"：这是 DTK 那一侧的探测结果。
_COMPONENT_DETAIL: dict[str, str] = {
    "unreachable": "DTK 探测它时没连上（超时或网络不通）；这是 DTK 那一侧的探测结果",
    "degraded": "DTK 连上了它，但它的健康检查没有返回 ok",
}


def _kv(
    label: str,
    value: str,
    *,
    dot: str | None = None,
    note: str = "",
    mono: bool = False,
) -> str:
    """一格读数。`dot` 给颜色变量名；`mono` 给数字用等宽字体（表格式对齐）。"""
    dot_html = (
        f'<span class="kv-dot" style="background:var({dot})"></span>' if dot else ""
    )
    cls = "kv-value mono" if mono else "kv-value"
    note_html = f'<div class="kv-note">{_escape_html(note)}</div>' if note else ""
    return (
        '<div class="kv-item">'
        f'<div class="kv-label">{_escape_html(label)}</div>'
        f'<div class="{cls}">{dot_html}{_escape_html(value)}</div>'
        f"{note_html}</div>"
    )


def _upstream_html(upstream: Mapping[str, Any]) -> str:
    """上游（DTK）健康卡片。

    **数据全部来自快照，不在这里发请求**：主循环按 `UPSTREAM_STATUS_INTERVAL_SECONDS`
    取一次 `system/status` 写进 `status.json`，面板只读它。这是"打开面板不发上游请求"
    这个承诺的一部分——面板是随便刷的，上游的请求配额不是。

    取值失败时快照里留着上一次的读数 + `ok: false` + 错误码（见 `loop._refresh_upstream_status`），
    所以这里既能把卡片画出来，又要如实说出"这些数是旧的"。
    """
    base_url = str(upstream.get("base_url") or "—")
    checked_at = upstream.get("checked_at")
    if not checked_at:
        # 只有 base_url：说明还没取过（刚启动、或 UPSTREAM_STATUS_INTERVAL_SECONDS=0）
        return (
            '<div class="section-title">上游 DTK</div>'
            '<div class="kv"><div class="kv-item">'
            f'<div class="kv-label">地址</div><div class="kv-value mono">{_escape_html(base_url)}</div>'
            '<div class="kv-note">还没有取到读数：刚启动，或 UPSTREAM_STATUS_INTERVAL_SECONDS=0</div>'
            "</div></div>"
        )

    ok = bool(upstream.get("ok", True))
    items: list[str] = []
    items.append(
        _kv(
            "上游状态",
            "正常" if ok else f"不可用（{upstream.get('error') or '未知'}）",
            dot="--green" if ok else "--red",
            mono=False,
        )
    )
    items.append(_kv("DTK 版本", str(upstream.get("version") or "—"), mono=True))
    items.append(
        _kv("上游已运行", _human_duration(upstream.get("uptime_seconds")), mono=True)
    )

    components = upstream.get("components")
    if isinstance(components, Mapping):
        for name in ("postgres", "redis", "browser_rpc"):
            if name not in components:
                continue
            value = components.get(name)
            value = dict(value) if isinstance(value, Mapping) else {}
            state = value.get("ok")
            latency = value.get("latency_ms")
            # 上游给了原因代码就说出来：只写"不可用"，人分不出是没连上还是状态不对
            detail = value.get("detail_code")
            note = (
                _COMPONENT_DETAIL.get(str(detail), f"DTK 给出的原因代码：{detail}")
                if state is False and detail
                else ""
            )
            # `ok` 是 None == "不知道"（browser-rpc 没配时上游诚实地说不知道），
            # 那不是"坏了"，所以用灰色而不是红色——把"没配"画成"故障"会让人白跑一趟
            dot = "--green" if state else ("--off" if state is None else "--red")
            text = "未配置" if state is None else ("正常" if state else "不可用")
            items.append(
                _kv(
                    _COMPONENT_LABELS.get(name, name),
                    text + (f" · {latency}ms" if isinstance(latency, int) else ""),
                    dot=dot,
                    note=note,
                    mono=True,
                )
            )

    pool = upstream.get("pool")
    if isinstance(pool, Mapping):
        for name, value in pool.items():
            if name == "total_active" or not isinstance(value, Mapping):
                continue
            parts = [
                f"{label} {_as_int(value.get(state))}"
                for state, label in _POOL_STATES
                if _as_int(value.get(state))
            ]
            items.append(
                _kv(
                    _POOL_LABELS.get(name, name) + "身份池",
                    " · ".join(parts) or "空",
                    mono=True,
                )
            )
        total = _as_int(pool.get("total_active"))
        if total:
            items.append(_kv("全平台可用身份", str(total), mono=True))

    storage = upstream.get("storage")
    if isinstance(storage, Mapping) and storage:
        # 这是**两个互不相干的量**。以前用一个点号连成一行（"身份 20 个 · 177.7 MB"），读起来
        # 像"20 个身份占了 177.7 MB"，更像是在说内存占用——两样都不是：
        #   * `identities`：DTK 身份表的总行数，**不区分是否可用**（可用的数量是上面的
        #     「全平台可用身份」）；
        #   * `db_size_bytes`：DTK 整个 PostgreSQL 数据库在磁盘上的大小（请求日志、归档、
        #     身份等全在里面），不是内存，也不只是身份的。
        identities = storage.get("identities")
        if identities is not None:
            items.append(
                _kv(
                    "身份总数（含不可用）",
                    str(_as_int(identities)) + " 个",
                    note="身份表里的全部身份；可用的数量看「全平台可用身份」",
                    mono=True,
                )
            )
        if storage.get("db_size_bytes") is not None:
            items.append(
                _kv(
                    "上游数据库大小",
                    _human_bytes(storage.get("db_size_bytes")),
                    note="DTK 的 PostgreSQL 数据库在磁盘上的总大小，不是内存占用",
                    mono=True,
                )
            )

    items.append(
        _kv(
            "取值时间",
            str(checked_at),
            note="" if ok else "取不到上游状态，上面是上一次取到的读数",
            mono=True,
        )
    )
    items.append(_kv("接口地址", base_url, mono=True))
    return (
        '<div class="section-title">上游 DTK</div>'
        + '<div class="kv">'
        + "".join(items)
        + "</div>"
    )


# =================== 页面 ===================


def render_page(settings: Settings) -> str:
    stale_days = int(settings.get("STALE_FALLBACK_DAYS", 14))
    data = read_status(settings)
    users = [item for item in (data.get("users") or []) if isinstance(item, dict)]
    notify = data.get("notify") or {}
    channels = "、".join(notify.get("channels") or []) or "静默"
    if notify.get("silent"):
        channels += "（静默）"
    upstream = data.get("upstream") or {}
    upstream_url = str(upstream.get("base_url") or "—")

    buckets: dict[str, int] = dict.fromkeys(STATUS_KEYS, 0)
    for user in users:
        color, _ = classify_account(user, stale_days)
        buckets[color] += 1

    total = len(users)
    overall = _overall_line(
        total, buckets["green"], buckets["red"], buckets["amber"], buckets["blue"]
    )

    if total == 0:
        if not settings.status_path.exists() and not data:
            list_html = (
                '<div class="empty"><div class="headline">还没有数据</div>'
                "监控可能尚未跑完第一轮，跑完就会出现在这里。</div>"
            )
        elif not data:
            list_html = (
                '<div class="empty"><div class="headline">状态快照解析失败</div>'
                f"检查 {_escape_html(str(settings.status_path))} 是否损坏。</div>"
            )
        else:
            list_html = (
                '<div class="empty"><div class="headline">还没有监控账号</div>'
                "编辑工作目录下的 users.conf，一行一个「sec_user_id|昵称」。</div>"
            )
    else:
        list_html = (
            '<div class="section-title">账号列表</div>'
            '<div class="list">'
            + "".join(_render_row(user, stale_days) for user in users)
            + "</div>"
        )

    meta = "".join(
        [
            f"<span>检查于 {_escape_html(data.get('timestamp') or '—')}</span>",
            '<span class="sep">·</span>',
            f"<span>本次运行第 {_escape_html(str(data.get('rounds') if data.get('rounds') is not None else '—'))} 轮</span>",
            '<span class="sep">·</span>',
            f"<span>累计 {_escape_html(str(data.get('rounds_total') if data.get('rounds_total') is not None else '—'))} 轮</span>",
            '<span class="sep">·</span>',
            f"<span>渠道 {_escape_html(channels)}</span>",
            '<span class="sep">·</span>',
            f'<span class="hide-sm">上游 {_escape_html(upstream_url)}</span>',
            '<span class="sep hide-sm">·</span>',
            f'<span class="hide-sm">PID {_escape_html(str(data.get("pid") or "—"))}</span>',
            '<span class="sep hide-sm">·</span>',
            f"<span>{REFRESH_SECONDS} 秒自动刷新</span>",
        ]
    )

    body = (
        masthead(
            eyebrow="DYWATCH / STATUS",
            headline=overall,
            meta=meta,
            active="status",
        )
        + _gate_html(data.get("gate") or {})
        + _selfcheck_html(data.get("self_check") or {})
        + _render_ledarray(buckets)
        + _render_stats(total, buckets)
        + _upstream_html(upstream)
        + list_html
        + (
            '<div class="footer mono">'
            "<span>只读 · 无鉴权 · 数据来自 status.json 与状态库</span>"
            '<span><a href="/events">/events</a>&nbsp;&nbsp;'
            '<a href="/api/state">/api/state</a>&nbsp;&nbsp;'
            '<a href="/api/health">/api/health</a>&nbsp;&nbsp;<a href="/metrics">/metrics</a></span>'
            "</div>"
        )
    )

    after = (
        _DETAIL_PANEL
        + charts.script_tag()
        + f"<script>{charts.BOOTSTRAP_JS}</script>"
        + _PAGE_JS
    )
    return page(title=PANEL_TITLE, body=body, scripts=after, refresh=REFRESH_SECONDS)


def build_health(settings: Settings) -> dict[str, Any]:
    """机器可读的小结。格式与旧项目保持一致，另加新项目才有的轮次与闸门。"""
    data = read_status(settings)
    if not data:
        return {"status": "no_data", "users": 0, "failed_users": 0}
    users = [item for item in (data.get("users") or []) if isinstance(item, dict)]
    failing = sum(1 for user in users if _as_int(user.get("consecutive_fails")) > 0)
    self_check = data.get("self_check") or {}
    return {
        "status": "ok",
        "timestamp": data.get("timestamp"),
        "pid": data.get("pid"),
        "rounds": data.get("rounds"),
        "rounds_total": data.get("rounds_total"),
        "users": len(users),
        "active_users": len(users) - failing,
        "failed_users": failing,
        "gate_open": bool((data.get("gate") or {}).get("open", True)),
        # 与闸门同理：机器读的小结里也该有"服务自己好没好"，否则只能靠人看网页
        "self_ok": bool(self_check.get("ok", True)),
        "self_reasons": list(self_check.get("reasons") or []),
        "upstream_ok": bool((data.get("upstream") or {}).get("ok", True)),
    }


# =================== 详情弹窗 ===================

_DETAIL_PANEL = """
<!-- 详情面板 -->
<div class="detail-overlay" id="detailOverlay" onclick="closeDetail()">
  <div class="detail-panel" onclick="event.stopPropagation()">
    <div class="detail-head">
      <h2 id="detailName">-</h2>
      <button class="detail-close" onclick="closeDetail()">&times;</button>
    </div>
    <div class="detail-id mono" id="detailId"></div>
    <div id="detailContent"><div class="detail-empty">加载中...</div></div>
  </div>
</div>
"""


_PAGE_JS = r"""
<script>
// 点击整行打开详情：用 data-uid 属性 + 事件委托，而不是把 uid
// 拼进内联 onclick 的 JS 字符串里——HTML 实体转义没法防住内联事件处理器
// 里的 JS 字符串截断（浏览器解析属性值时会先做实体解码，解码结果才是
// 真正拿去当 JS 源码执行的内容，所以 &#39; 这类转义在这个上下文里防不住
// 单引号截断），用 dataset 读取就完全没有这个问题。
// 整行可点是给手指用的：手机上按名字那一行字太小了。
function esc(s) {
  var d = document.createElement('div');
  d.textContent = (s === null || s === undefined) ? '-' : s;
  // div.innerHTML 只转义内容上下文特殊字符（&<>），不转义引号；
  // esc() 的结果既会用在元素内容里，也会用在 title="..." 这种属性值里，
  // 补上引号转义让它在两种上下文里都安全，不依赖调用方自己判断用在哪
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function num(v) {
  var f = (window.dyChart && window.dyChart.fmtNum) || function (x) { return x; };
  return (v === null || v === undefined) ? '-' : f(v);
}

document.querySelectorAll('.row').forEach(function(row) {
  row.addEventListener('click', function(event) {
    // 频率气泡自己有交互（悬停/点按看均值），别让它顺带打开弹窗
    if (event.target.closest && event.target.closest('.freq-tag')) return;
    var uid = row.dataset.uid;
    if (uid) openDetail(uid);
  });
});
// 详情内容**唯一**的写入点。先销毁旧图、再换 DOM：顺序不能反——`innerHTML` 一换，旧 canvas
// 就脱离了面板，`clear()` 再按"是否在容器里"去找就找不到它们，Chart.js 实例会一直留着
// （实测：连续打开 5 次详情，实例数 1→2→3→4→5）。"加载中…"那一步也算一次替换，
// 所以所有写入都得走这里，而不只是最后渲染内容的那一处
function setDetail(html) {
  var holder = document.getElementById('detailContent');
  if (window.dyChart) window.dyChart.clear(holder);
  holder.innerHTML = html;
}
function openDetail(uid) {
  document.getElementById('detailOverlay').classList.add('open');
  // 弹窗自己滚动，所以背后那一页要停住，否则手机上滑到底会把列表也带着滚
  document.body.style.overflow = 'hidden';
  document.getElementById('detailName').textContent = '加载中...';
  document.getElementById('detailId').textContent = uid;
  setDetail('<div class="detail-empty">加载中...</div>');
  fetch('/api/user/' + encodeURIComponent(uid))
    .then(function(r) { return r.json(); })
    .then(function(d) { renderDetail(d); })
    .catch(function() {
      setDetail('<div class="detail-empty">加载失败</div>');
    });
}
function closeDetail() {
  document.getElementById('detailOverlay').classList.remove('open');
  document.body.style.overflow = '';
  document.getElementById('detailOverlay').scrollTop = 0;  // 下次打开从顶部开始
}
document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeDetail(); });

function metricLine(m) {
  if (!m) return '';
  var parts = [];
  if (m.digg !== null && m.digg !== undefined) parts.push('赞 ' + num(m.digg));
  if (m.comment !== null && m.comment !== undefined) parts.push('评 ' + num(m.comment));
  if (m.collect !== null && m.collect !== undefined) parts.push('藏 ' + num(m.collect));
  if (m.share !== null && m.share !== undefined) parts.push('转 ' + num(m.share));
  return parts.join(' · ');
}

function renderDetail(d) {
  trendStore = {};
  if (d.error) {
    setDetail('<div class="detail-empty">' + esc(d.error) + '</div>');
    return;
  }
  document.getElementById('detailName').textContent = d.nickname || d.sec_user_id;
  var h = '';
  h += '<div class="detail-grid">';
  h += di('状态', d.status_text);
  h += di('已知作品', d.known_posts + ' 条');
  h += di('已消失', d.tombstones + ' 条');
  h += di('对访客不可见', (d.hidden_posts || 0) + ' 条', d.hidden_posts ? '登录可见、未登录看不到' : '');
  h += di('连续失败', d.consecutive_fails + ' 次');
  h += di('最新作品发布', d.newest_post_at || '还没有作品');
  h += di('距最新作品', d.newest_post_ago);
  h += di('更新频率', d.update_frequency || '样本不足', d.freq_hint || '');
  h += di('累计轮次', d.runs);
  h += di('首次记录', d.initialized_at || '-');
  h += '</div>';
  if (d.last_error_code) {
    h += '<div class="detail-section">最近一次错误</div>';
    h += '<div class="detail-note mono">' + esc(d.last_error_code) + ' ' + esc(d.last_error || '') + '</div>';
  }
  h += metricsSection(d);
  if (d.posts && d.posts.length > 0) {
    h += '<div class="detail-section">已知作品（' + d.posts.length + ' 条，置顶在最前）</div>';
    h += '<ul class="video-list">';
    d.posts.forEach(function(v, i) {
      h += '<li>';
      if (d.metrics_enabled) {
        // 按钮而不是 span：键盘和读屏软件都认得；content_id 走 data 属性，不拼进内联脚本
        h += '<button type="button" class="vtitle vtoggle" aria-expanded="false" data-i="' + i
          + '" data-cid="' + esc(v.content_id) + '" title="点击展开这条作品的互动量趋势">'
          + esc(v.title) + '</button>';
      } else {
        h += '<span class="vtitle">' + esc(v.title) + '</span>';
      }
      if (v.is_top) h += '<span class="vtop">置顶</span>';
      if (v.absent_rounds > 0) h += '<span class="vabsent">缺席 ' + v.absent_rounds + ' 轮</span>';
      if (v.hidden) h += '<span class="vhidden" title="' + esc(v.hidden_since)
        + ' 起对访客不可见（登录视角一直看得到）">对访客不可见</span>';
      var line = metricLine(v.metrics);
      if (line) h += '<span class="vkind mono">' + esc(line) + '</span>';
      h += '<span class="vkind">' + esc(v.kind) + '</span>';
      h += '<span class="vdate">' + esc(v.date) + '</span>';
      h += '</li>';
      if (d.metrics_enabled) {
        h += '<li class="vtrend-row" id="postTrendRow' + i + '" data-uid="' + esc(d.sec_user_id)
          + '" hidden><div class="post-trend-body"></div></li>';
      }
    });
    h += '</ul>';
  } else {
    h += '<div class="detail-section">已知作品</div>';
    h += '<div class="detail-empty">还没有记录到任何作品</div>';
  }
  if (d.removed && d.removed.length > 0) {
    h += '<div class="detail-section">已消失作品（保留 ' + d.tombstones + ' 条）</div>';
    h += '<ul class="video-list">';
    d.removed.forEach(function(v) {
      h += '<li>';
      h += '<span class="vtitle mono">' + esc(v.content_id) + '</span>';
      h += '<span class="vkind">' + esc(v.reason) + '</span>';
      h += '<span class="vdate">' + esc(v.date) + '</span>';
      h += '</li>';
    });
    h += '</ul>';
  }
  if (d.events && d.events.length > 0) {
    h += '<div class="detail-section">最近事件</div>';
    h += '<ul class="video-list">';
    d.events.forEach(function(e) {
      h += '<li>';
      h += '<span class="vtitle">' + esc(e.label) + (e.content_id ? ' <span class="vkind mono">' + esc(e.content_id) + '</span>' : '') + '</span>';
      h += '<span class="vdate">' + esc(e.ts) + '</span>';
      h += '</li>';
    });
    h += '</ul>';
  }
  h += '<div class="detail-section"><a href="/events?author=' + encodeURIComponent(d.sec_user_id)
    + '" style="color:inherit">在事件时间线里看这个账号 &rsaquo;</a></div>';
  setDetail(h);
  // 图表要在 HTML 落进 DOM 之后才建：Chart.js 需要拿到真实的 canvas 元素尺寸，
  // 在字符串里拼的 canvas 没有尺寸，画出来是 0×0 的空白
  if (window.dyChart) window.dyChart.mount(document.getElementById('detailContent'));
  drawTrend('detailMetrics');
}

// ===== 互动量趋势 =====
// 画的是**新增量**，不是累计总数：总数 1500 里每小时只涨个位数，占比不到 1%，四条线共用
// 一根纵轴，画出来就是四条水平线。趋势数据由服务端算好（3 个范围 × 2 种口径），这里只负责
// 挑一份、套上图例和颜色。账号图是**全部作品的合计**，每条作品点标题可以单独展开自己的。
var TREND_RANGES = {'24h': '近 24 小时', '7d': '近 7 天', 'all': '总览'};
var TREND_MODES = {'delta': '新增', 'cumulative': '累计'};
var trendStore = {};   // 图表 id -> {trend, range, mode}

function trendCaption(st) {
  var view = st.trend.views[st.range];
  var span = st.range === 'all' ? '总览（' + view.labels.length + ' 天）' : TREND_RANGES[st.range];
  var what = st.mode === 'delta' ? view.unit + '新增' : '累计新增（从这段时间的起点算起）';
  return span + ' · ' + what;
}

function trendChip(id, kind, key, label, on) {
  return '<button type="button" class="trend-chip' + (on ? ' on' : '') + '" data-trend="' + id
    + '" data-' + kind + '="' + key + '">' + label + '</button>';
}

function trendControls(id, st) {
  var h = '<div class="trend-ctl mono">';
  Object.keys(TREND_RANGES).forEach(function (k) {
    h += trendChip(id, 'range', k, TREND_RANGES[k], k === st.range);
  });
  h += '<span class="trend-sep"></span>';
  Object.keys(TREND_MODES).forEach(function (k) {
    h += trendChip(id, 'mode', k, TREND_MODES[k], k === st.mode);
  });
  return h + '</div>';
}

// 一块趋势图的 HTML。载荷不在这里拼进去：`drawTrend` 画之前才写进载荷节点的 textContent，
// 所以没有"要转义脚本结束标记"这回事，切换范围 / 口径时也是同一条路径。
// （注意：这段 JS 本身就住在一个内联脚本里——注释里也**不能**出现脚本的结束标记，
// 浏览器在第一个遇到的位置就会把整段脚本截断，后面的代码会被当成页面文字）
function trendBlock(id, trend, title, scope, note) {
  trendStore[id] = {trend: trend, range: '24h', mode: 'delta'};
  var legend = '';
  trend.series.forEach(function (s) {
    legend += '<span><i style="background:var(' + esc(s.color) + ')"></i>' + esc(s.label) + '</span>';
  });
  return '<div class="chart-head"><div class="section-title">' + esc(title) + '</div>'
    + '<div class="chart-range mono" id="' + id + 'Caption"></div></div>'
    + (scope ? '<div class="trend-scope mono">' + esc(scope) + '</div>' : '')
    + trendControls(id, trendStore[id])
    + '<div class="chart-legend mono">' + legend + '</div>'
    + '<div class="chart-canvas" data-chart="' + id + '"><canvas id="' + id + 'Canvas"></canvas></div>'
    + '<script type="application/json" data-chart-data="' + id + '"><\/script>'
    + (note ? '<div class="trend-note mono">' + esc(note) + '</div>' : '');
}

function drawTrend(id) {
  var st = trendStore[id];
  var el = document.querySelector('[data-chart="' + id + '"]');
  var holder = document.querySelector('script[data-chart-data="' + id + '"]');
  if (!st || !el || !holder || !window.dyChart) return;
  var view = st.trend.views[st.range];
  var data = st.mode === 'delta' ? view.delta : view.cumulative;
  holder.textContent = JSON.stringify({
    type: 'line', straight: true, signed: st.mode === 'delta', labels: view.labels,
    datasets: st.trend.series.map(function (s) {
      return {label: s.label, color: s.color, data: data[s.key]};
    })
  });
  var caption = document.getElementById(id + 'Caption');
  if (caption) caption.textContent = trendCaption(st);
  document.querySelectorAll('[data-trend="' + id + '"]').forEach(function (b) {
    var on = (b.hasAttribute('data-range') && b.getAttribute('data-range') === st.range)
      || (b.hasAttribute('data-mode') && b.getAttribute('data-mode') === st.mode);
    b.classList.toggle('on', on);
  });
  window.dyChart.redraw(el);
}

// 账号级趋势：有数据就画，没数据就如实说原因（"没开记录"和"开了还没采到"是两件事，
// 画成同一张空图会让人以为采集坏了）
function metricsSection(d) {
  var head = '<div class="detail-section">互动量趋势</div>';
  if (!d.metrics_enabled) {
    return head
      + '<div class="chart-empty mono">未开启记录（METRICS_ENABLED=false）——数字本来随每轮抓取免费返回，'
      + '开启它不消耗任何额外的身份</div>';
  }
  var t = d.metrics_trend;
  if (!t) {
    return head
      + '<div class="chart-empty mono">还没有采到样本：下一轮抓到作品页时开始记录（按小时聚合，'
      + '保留 ' + d.metrics_keep_days + ' 天）</div>';
  }
  // 回答"这是哪个视频的"：不是任何一条，是这个账号作品的合计。N 要写**真正加进图里**的
  // 那几条（`t.posts`），不是已知作品数（`t.known`）：隐藏的 / 刚加的 / 开记录之前抓过的
  // 作品没有互动量记录，写成"N 条已知作品的合计"就把覆盖面说大了
  var tail = '，不是某一条；点下面的作品标题，单独看每一条';
  var scope = t.posts === t.known
    ? '该账号 ' + t.known + ' 条已知作品的合计' + tail
    : '这张图合计了 ' + t.posts + ' 条作品的互动量（该账号现在已知 ' + t.known
      + ' 条；没有足够记录的不参与）' + tail;
  if (!t.has_data) {
    // 一条都还没加进去：没有"合计"可言，只说账号有几条作品
    return '<div class="chart-block">' + head
      + '<div class="trend-scope mono">' + esc('该账号 ' + t.known + ' 条已知作品' + tail) + '</div>'
      + '<div class="chart-empty mono">样本还不够：新增量要同一作品相邻两个小时都采到，'
      + '刚开始记录时要等 1 到 2 小时</div></div>';
  }
  return '<div class="chart-block">'
    + trendBlock('detailMetrics', t, '互动量趋势', scope,
      '新增 = 这一格里各作品互动量的增加之和；作品第一次出现的那一刻只是起点，不算新增。')
    + '</div>';
}

// 点作品标题：就地展开 / 收起这条作品自己的趋势。数据点开才取（18 条作品 × 全部范围一次
// 带上，绝大多数人一条都不展开）。取回来之前账号可能已经换了——只往还连在页面上的行里写
function togglePost(btn) {
  var i = btn.getAttribute('data-i');
  var row = document.getElementById('postTrendRow' + i);
  if (!row) return;
  if (!row.hasAttribute('hidden')) {
    row.setAttribute('hidden', '');
    btn.setAttribute('aria-expanded', 'false');
    return;
  }
  row.removeAttribute('hidden');
  btn.setAttribute('aria-expanded', 'true');
  var id = 'postTrend' + i;
  if (trendStore[id]) { drawTrend(id); return; }   // 取过了：展开后按真实尺寸重画一次
  if (row.getAttribute('data-loading') === '1') return;
  var body = row.querySelector('.post-trend-body');
  row.setAttribute('data-loading', '1');
  body.innerHTML = '<div class="detail-empty">加载中...</div>';
  var url = '/api/user/' + encodeURIComponent(row.getAttribute('data-uid'))
    + '/post/' + encodeURIComponent(btn.getAttribute('data-cid')) + '/trend';
  fetch(url).then(function (r) { return r.json(); }).then(function (p) {
    row.removeAttribute('data-loading');
    if (!row.isConnected) return;
    if (p.error) { body.innerHTML = '<div class="detail-empty">' + esc(p.error) + '</div>'; return; }
    if (!p.metrics_enabled) {
      body.innerHTML = '<div class="chart-empty mono">未开启互动量记录（METRICS_ENABLED=false）</div>';
      return;
    }
    if (!p.trend.has_data) {
      body.innerHTML = '<div class="chart-empty mono">这条作品的样本还不够（目前 ' + esc(p.samples)
        + ' 个小时的采样）：新增量要相邻两个小时都采到，新出现的作品要等 1 到 2 小时</div>';
      return;
    }
    body.innerHTML = trendBlock(id, p.trend, '这条作品的互动量趋势', '', '');
    drawTrend(id);
  }).catch(function () {
    row.removeAttribute('data-loading');
    if (row.isConnected) body.innerHTML = '<div class="detail-empty">加载失败</div>';
  });
}

// 事件委托：详情内容会整块重写，监听不能挂在会被换掉的节点上
document.getElementById('detailContent').addEventListener('click', function (e) {
  var t = e.target.closest ? e.target.closest('.vtoggle, [data-trend]') : null;
  if (!t) return;
  if (t.classList.contains('vtoggle')) { togglePost(t); return; }
  var id = t.getAttribute('data-trend');
  var st = trendStore[id];
  if (!st) return;
  if (t.hasAttribute('data-range')) st.range = t.getAttribute('data-range');
  if (t.hasAttribute('data-mode')) st.mode = t.getAttribute('data-mode');
  drawTrend(id);
});

function di(label, value, tip) {
  var t = tip ? ' title="' + esc(tip) + '"' : '';
  return '<div class="detail-item"' + t + '><div class="dl">' + esc(label) + '</div><div class="dv">' + esc(value) + '</div></div>';
}

// 移动端 freq-tag 触摸切换 tooltip
document.querySelectorAll('.freq-tag').forEach(function(el) {
  el.addEventListener('touchstart', function(e) {
    e.stopPropagation();
    document.querySelectorAll('.freq-tag.active').forEach(function(x) { if (x !== el) x.classList.remove('active'); });
    el.classList.toggle('active');
  });
});
document.addEventListener('touchstart', function() {
  document.querySelectorAll('.freq-tag.active').forEach(function(x) { x.classList.remove('active'); });
});
</script>
"""


__all__ = ["PANEL_TITLE", "REFRESH_SECONDS", "build_health", "render_page"]
