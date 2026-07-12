"""极简只读 Web 状态面板，技术仪表盘视觉设计，亮色为主并自动跟随系统深色模式。

不引入 Flask/FastAPI 等额外依赖，用标准库 http.server 实现：
- GET /           自动刷新的状态页（LED 状态阵列 + 数据条 + 账号列表）
- GET /api/status 返回 status.json 原始内容
- GET /api/health 返回健康检查 JSON

通过 .env 中 WEB_ENABLED=true 开启，随主循环一起在后台线程启动，
只监听本机回环地址（默认 127.0.0.1），如需外部访问请自行改 WEB_HOST
并注意做好网络层面的访问控制（本面板不做鉴权）。

明暗配色完全由 CSS `prefers-color-scheme` 媒体查询驱动，跟随系统/浏览器
设置自动切换，没有额外的切换按钮或 JS 逻辑。
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template

from .config import STATUS_FILE

# =================== 设计 token ===================
# 技术仪表盘风格：亮色为主（近白背景 + 细网格底纹），单一信号蓝作交互强调色，
# 状态用红/绿/黄区分；大量使用等宽字体和方括号标签模拟"读数"质感。
# 深色模式通过 prefers-color-scheme 媒体查询覆盖同一套变量，自动跟随系统。

_PAGE = Template(r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>抖音监控 · 状态</title>
<meta http-equiv="refresh" content="30">
<style>
  :root {
    --bg:      #FAFAF8;
    --grid:    rgba(20,20,20,.045);
    --panel:   #FFFFFF;
    --line:    #E4E3DD;
    --line-2:  #EEEDE7;
    --text:    #16171A;
    --text2:   #6B6E76;
    --text3:   #A2A5AC;
    --blue:    #155EEF;
    --blue-soft: #EAF1FF;
    --green:   #17875A;
    --green-soft: #E6F5EE;
    --amber:   #B4680A;
    --amber-soft: #FBF0DF;
    --red:     #D1352B;
    --red-soft: #FBE9E7;
    --off:     #9CA1AA;
    --off-soft: #F1F1EF;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:      #0C0D10;
      --grid:    rgba(255,255,255,.05);
      --panel:   #17181C;
      --line:    rgba(255,255,255,.10);
      --line-2:  rgba(255,255,255,.06);
      --text:    #EDEEF0;
      --text2:   #9A9EA6;
      --text3:   #5C5F66;
      --blue:    #5B92FF;
      --blue-soft: rgba(91,146,255,.14);
      --green:   #3FCE8E;
      --green-soft: rgba(63,206,142,.14);
      --amber:   #E4A63A;
      --amber-soft: rgba(228,166,58,.14);
      --red:     #FF6B60;
      --red-soft: rgba(255,107,96,.14);
      --off:     #6A6E77;
      --off-soft: rgba(255,255,255,.06);
    }
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0;
    background:
      radial-gradient(circle, var(--grid) 1px, transparent 1px) 0 0/16px 16px,
      var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Microsoft YaHei", sans-serif;
    line-height: 1.55;
    padding: 52px 24px 72px;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 740px; margin: 0 auto; }
  .mono {
    font-family: "SF Mono", ui-monospace, SFMono-Regular, "IBM Plex Mono",
                 Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums;
  }

  /* ---- Masthead：方括号标签模拟"读数"质感 ---- */
  .eyebrow {
    font-size: 11px; letter-spacing: .1em;
    color: var(--blue); font-weight: 600; margin-bottom: 16px;
    display: flex; align-items: center; gap: 8px;
  }
  .eyebrow .dot {
    width: 6px; height: 6px; border-radius: 1px; background: var(--green);
    display: inline-block; animation: blink 2s steps(1) infinite;
  }
  @keyframes blink { 0%, 49% { opacity: 1; } 50%, 100% { opacity: .25; } }

  h1 {
    font-weight: 700;
    font-size: 30px;
    letter-spacing: -.01em;
    line-height: 1.3;
    margin: 0 0 10px;
    color: var(--text);
  }
  h1 b { color: var(--blue); font-weight: 700; }

  .meta {
    color: var(--text3); font-size: 12px; margin-bottom: 32px;
    display: flex; flex-wrap: wrap; gap: 4px 10px;
  }
  .meta .sep { color: var(--line); }

  /* ---- LED 状态阵列：一格一格的读数条，不是平滑进度条 ---- */
  .led-row { display: flex; gap: 3px; margin-bottom: 12px; }
  .led {
    flex: 1 1 0; height: 20px; border-radius: 2px;
    background: var(--off-soft);
  }
  .led.on-green { background: var(--green); }
  .led.on-red   { background: var(--red); }
  .led.on-amber { background: var(--amber); }
  .led.on-off   { background: var(--line); }

  .led-legend {
    display: flex; flex-wrap: wrap; gap: 4px 18px;
    font-size: 12px; color: var(--text2); margin-bottom: 36px;
  }
  .led-legend span { display: inline-flex; align-items: center; gap: 6px; }
  .led-legend i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }

  /* ---- 数据条：方括号包裹的标签 + 大号等宽数字 ---- */
  .stats {
    display: flex; flex-wrap: wrap;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    margin-bottom: 40px;
  }
  .stat { flex: 1 1 0; min-width: 110px; padding: 16px 20px 16px 0; }
  .stat + .stat { padding-left: 20px; border-left: 1px solid var(--line-2); }
  .stat-label {
    font-size: 11px; color: var(--text3); margin-bottom: 6px;
  }
  .stat-label::before { content: "["; }
  .stat-label::after { content: "]"; }
  .stat-value { font-size: 26px; font-weight: 700; line-height: 1; }
  .stat-value.green { color: var(--green); }
  .stat-value.red   { color: var(--red); }
  .stat-value.amber { color: var(--amber); }

  .section-title {
    font-size: 11px; letter-spacing: .08em;
    color: var(--text3); font-weight: 600; margin-bottom: 4px;
  }
  .section-title::before { content: "// "; color: var(--line); }

  /* ---- 账号列表 ---- */
  .list { border-top: 1px solid var(--line); margin-top: 14px; }
  .row {
    display: flex; align-items: center; gap: 12px;
    padding: 13px 4px;
    border-bottom: 1px solid var(--line-2);
  }
  .row-badge {
    flex: 0 0 auto; width: 8px; height: 8px; border-radius: 2px;
  }
  .row-name {
    flex: 1 1 auto; min-width: 0;
    font-weight: 600; font-size: 14.5px; color: var(--text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .freq-tag {
    flex: 0 0 auto;
    font-size: 11px; font-weight: 500; color: var(--text2);
    background: var(--panel); border: 1px solid var(--line);
    padding: 1px 7px; border-radius: 3px; white-space: nowrap;
  }
  .row-status {
    flex: 0 0 auto; font-size: 12px; font-weight: 600;
    padding: 2px 8px; border-radius: 3px; min-width: 76px; text-align: center;
  }
  .row-status.green { background: var(--green-soft); color: var(--green); }
  .row-status.red   { background: var(--red-soft); color: var(--red); }
  .row-status.amber { background: var(--amber-soft); color: var(--amber); }
  .row-status.off   { background: var(--off-soft); color: var(--text3); }
  .row-count { flex: 0 0 auto; font-size: 13px; color: var(--text2); min-width: 56px; text-align: right; }
  .row-time  { flex: 0 0 auto; font-size: 12px; color: var(--text3); min-width: 128px; text-align: right; }

  .empty {
    padding: 60px 24px; text-align: center; color: var(--text3);
    border-top: 1px solid var(--line);
  }
  .empty .headline { font-size: 18px; font-weight: 700; color: var(--text); margin-bottom: 8px; }

  .footer {
    margin-top: 50px; padding-top: 18px; border-top: 1px solid var(--line);
    font-size: 11.5px; color: var(--text3);
    display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  }
  .footer a { color: var(--text3); text-decoration: none; border-bottom: 1px solid var(--line); }
  .footer a:hover { color: var(--blue); border-color: var(--blue); }

  @media (max-width: 560px) {
    body { padding: 36px 16px 56px; }
    h1 { font-size: 24px; }
    .stat { min-width: 45%; padding: 12px 12px 12px 0; }
    .stat + .stat { padding-left: 12px; }
    .stat-value { font-size: 22px; }
    .led { height: 14px; }
    .row {
      flex-wrap: wrap; gap: 6px 8px;
      padding: 12px 4px;
    }
    .row-status { order: -1; }
    .row-count { min-width: auto; text-align: left; font-size: 12px; }
    .row-time { min-width: auto; text-align: left; font-size: 11px; flex-basis: 100%; }
    /* 移动端：底部抽屉样式 */
    .detail-overlay { align-items: flex-end; justify-content: stretch; }
    .detail-panel {
      width: 100%; max-width: none; border-radius: 16px 16px 0 0;
      border: none; border-top: 1px solid var(--line);
      max-height: 80vh; padding: 20px 16px 28px;
      box-shadow: 0 -4px 16px rgba(0,0,0,.1);
      opacity: 1; transform: translateY(100%); transition: transform .25s ease-out;
    }
    .detail-overlay.open .detail-panel { transform: translateY(0); }
  }
  @media (prefers-reduced-motion: reduce) {
    .eyebrow .dot { animation: none; opacity: 1; }
  }

  /* ---- 可点击名字 ---- */
  .row-name { cursor: pointer; transition: color .15s; }
  .row-name:hover { color: var(--blue); }
  .row-name::after { content: " ›"; color: var(--text3); font-weight: 400; }
  .row-name:hover::after { color: var(--blue); }

  /* ---- Frequency tooltip ---- */
  .freq-tag { position: relative; }
  .freq-tag .tip {
    display: none; position: absolute; bottom: calc(100% + 6px); left: 50%;
    transform: translateX(-50%); white-space: nowrap;
    background: var(--text); color: var(--bg); font-size: 11px; font-weight: 400;
    padding: 4px 8px; border-radius: 4px; z-index: 10; pointer-events: none;
    box-shadow: 0 2px 6px rgba(0,0,0,.15);
  }
  .freq-tag .tip::after {
    content: ""; position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
    border: 4px solid transparent; border-top-color: var(--text);
  }
  .freq-tag:hover .tip { display: block; }
  /* 移动端触摸显示 tooltip */
  .freq-tag.active .tip { display: block; }

  /* ---- 详情面板（桌面端：居中弹窗） ---- */
  .detail-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,.35);
    z-index: 100; backdrop-filter: blur(3px);
    align-items: center; justify-content: center;
  }
  .detail-overlay.open { display: flex; }
  .detail-panel {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; padding: 28px 28px 24px;
    width: 90%; max-width: 560px; max-height: 80vh; overflow-y: auto;
    box-shadow: 0 8px 32px rgba(0,0,0,.12);
    opacity: 0; transform: scale(.95); transition: opacity .2s, transform .2s;
  }
  .detail-overlay.open .detail-panel { opacity: 1; transform: scale(1); }
  .detail-head {
    display: flex; align-items: center; gap: 12px; margin-bottom: 16px;
  }
  .detail-head h2 { font-size: 18px; font-weight: 700; margin: 0; flex: 1; }
  .detail-close {
    width: 32px; height: 32px; border: none; border-radius: 8px;
    background: var(--off-soft); color: var(--text2); font-size: 18px;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
  }
  .detail-close:hover { background: var(--red-soft); color: var(--red); }
  .detail-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px; margin-bottom: 20px;
  }
  .detail-item { padding: 10px 0; }
  .detail-item .dl { font-size: 11px; color: var(--text3); margin-bottom: 2px; }
  .detail-item .dl::before { content: "[ "; }
  .detail-item .dl::after { content: " ]"; }
  .detail-item .dv { font-size: 15px; font-weight: 600; }
  .detail-section {
    font-size: 11px; color: var(--text3); font-weight: 600;
    letter-spacing: .06em; margin: 16px 0 8px;
  }
  .detail-section::before { content: "// "; color: var(--line); }
  .video-list { list-style: none; padding: 0; margin: 0; }
  .video-list li {
    display: flex; align-items: baseline; gap: 8px;
    padding: 6px 0; border-bottom: 1px solid var(--line-2);
    font-size: 13px;
  }
  .video-list li:last-child { border-bottom: none; }
  .video-list .vtitle { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .video-list .vdate { flex: 0 0 auto; color: var(--text3); font-size: 12px; }
  .video-list .vtop { flex: 0 0 auto; font-size: 11px; color: var(--amber); font-weight: 600; }
  .detail-empty { color: var(--text3); text-align: center; padding: 24px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow mono"><span class="dot"></span>[ DOUYIN-MONITOR / STATUS ]</div>
  <h1>$overall_line</h1>
  <div class="meta mono">
    <span>检查于 $timestamp</span><span class="sep">·</span>
    <span>渠道 $channels</span><span class="sep">·</span>
    <span>PID $pid</span><span class="sep">·</span>
    <span>30s 自动刷新</span>
  </div>

  $ledarray

  $stats

  $list

  <div class="footer mono">
    <span>只读 · 数据来自 status.json</span>
    <span><a href="/api/status">/api/status</a>&nbsp;&nbsp;<a href="/api/health">/api/health</a></span>
  </div>
</div>

<!-- 详情面板 -->
<div class="detail-overlay" id="detailOverlay" onclick="closeDetail()">
  <div class="detail-panel" onclick="event.stopPropagation()">
    <div class="detail-head">
      <h2 id="detailName">-</h2>
      <button class="detail-close" onclick="closeDetail()">&times;</button>
    </div>
    <div id="detailContent"><div class="detail-empty">加载中...</div></div>
  </div>
</div>

<script>
function openDetail(uid) {
  document.getElementById('detailOverlay').classList.add('open');
  document.getElementById('detailName').textContent = '加载中...';
  document.getElementById('detailContent').innerHTML = '<div class="detail-empty">加载中...</div>';
  fetch('/api/user/' + encodeURIComponent(uid))
    .then(function(r) { return r.json(); })
    .then(function(d) { renderDetail(d); })
    .catch(function() {
      document.getElementById('detailContent').innerHTML = '<div class="detail-empty">加载失败</div>';
    });
}
function closeDetail() {
  document.getElementById('detailOverlay').classList.remove('open');
}
document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeDetail(); });

function renderDetail(d) {
  document.getElementById('detailName').textContent = d.nickname || d.sec_user_id;
  var h = '';
  // 基本信息
  h += '<div class="detail-grid">';
  h += di('状态', d.status_text);
  h += di('已知视频', d.known_videos + ' 条');
  h += di('连续失败', d.consecutive_fails + ' 次');
  h += di('距上次更新', d.last_update_ago);
  h += di('首次记录', d.initialized_at || '-');
  h += di('频率', d.update_frequency || '-', d.freq_hint || '');
  h += '</div>';
  // 视频列表
  if (d.videos && d.videos.length > 0) {
    h += '<div class="detail-section">视频列表</div>';
    h += '<ul class="video-list">';
    d.videos.forEach(function(v) {
      h += '<li>';
      h += '<span class="vtitle">' + esc(v.title) + '</span>';
      if (v.is_top) h += '<span class="vtop">置顶</span>';
      h += '<span class="vdate">' + v.date + '</span>';
      h += '</li>';
    });
    h += '</ul>';
  }
  document.getElementById('detailContent').innerHTML = h;
}
function di(label, value, tip) {
  var t = tip ? ' title="' + esc(tip) + '"' : '';
  return '<div class="detail-item"' + t + '><div class="dl">' + esc(label) + '</div><div class="dv">' + esc(value) + '</div></div>';
}
function esc(s) { var d = document.createElement('div'); d.textContent = s || '-'; return d.innerHTML; }

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
</body>
</html>""")

_STAT_TEMPLATE = Template("""<div class="stat">
  <div class="stat-label">$label</div>
  <div class="stat-value $color mono">$value</div>
</div>""")

_ROW_TEMPLATE = Template("""<div class="row" data-uid="$uid">
  <span class="row-badge" style="background:$badge_color"></span>
  <span class="row-name" onclick="openDetail('$uid')">$nickname</span>
  $freq_tag
  <span class="row-status $status_color">$status_text</span>
  <span class="row-count mono">$known_videos 条</span>
  <span class="row-time mono">$last_update_text</span>
</div>""")

_FREQ_TAG = Template('<span class="freq-tag">$label<span class="tip">$tip</span></span>')

_LEGEND_ITEM = Template('<span><i style="background:$color"></i>$label $count</span>')

_LED_SLOTS = 24


# =================== 渲染逻辑 ===================

def _format_last_update(hours: "int | None") -> str:
    """按用户要求，明确展示"距上次更新 X 天"这样的具体天数，而不是模糊的相对时间。"""
    if hours is None:
        return "从未更新"
    if hours < 1:
        return "刚刚更新"
    if hours < 24:
        return f"距上次更新 {hours} 小时"
    days = hours // 24
    return f"距上次更新 {days} 天"


def _overall_line(total: int, active: int, failing: int, stale: int) -> str:
    if total == 0:
        return "还没有<b>监控账号</b>"
    if failing == 0 and stale == 0:
        return f"{total} 个账号<b>全部正常</b>"
    if failing:
        return f"<b>{failing}</b> 个账号请求失败，{active} 个正常"
    return f"<b>{stale}</b> 个账号长期无更新，{active} 个正常"


def _quantize_blocks(counts: list, slots: int = _LED_SLOTS) -> list:
    """把 counts（各状态的账号数）按比例分配成正好 slots 个整数格子（最大余数法），
    用于渲染离散的 LED 阵列，而不是一条平滑的百分比进度条。

    先给每个非零类别保底 1 格（只要格子数够用），再把剩余格子按原始比例用
    最大余数法分配。不这样做的话，极端比例下（比如 100:1:1:1）小类别会被
    直接舍入成 0 格，阵列里完全看不出这个状态存在。
    """
    n = len(counts)
    total = sum(counts)
    if total == 0:
        return [0] * n

    nonzero = [i for i, c in enumerate(counts) if c > 0]
    base = [0] * n
    if len(nonzero) <= slots:
        for i in nonzero:
            base[i] = 1
        remaining = slots - len(nonzero)
    else:
        # 极端情况：类别数比格子还多（本函数目前最多 4 类，正常不会发生）
        remaining = slots

    if remaining > 0:
        raw = [(counts[i] / total) * remaining for i in range(n)]
        extra = [int(x) for x in raw]
        rem = remaining - sum(extra)
        order = sorted(range(n), key=lambda i: raw[i] - extra[i], reverse=True)
        for i in range(rem):
            extra[order[i % n]] += 1
        for i in range(n):
            base[i] += extra[i]

    return base


def _render_ledarray(active: int, failing: int, stale: int, off: int) -> str:
    total = active + failing + stale + off
    if total == 0:
        return ""
    counts = [active, failing, stale, off]
    classes = ["on-green", "on-red", "on-amber", "on-off"]
    blocks = _quantize_blocks(counts)

    cells = []
    for count, cls in zip(blocks, classes):
        cells.extend([f'<span class="led {cls}"></span>'] * count)
    led_html = f'<div class="led-row">{"".join(cells)}</div>'

    legend_parts = []
    if active:
        legend_parts.append(_LEGEND_ITEM.substitute(color="var(--green)", label="正常", count=active))
    if failing:
        legend_parts.append(_LEGEND_ITEM.substitute(color="var(--red)", label="失败", count=failing))
    if stale:
        legend_parts.append(_LEGEND_ITEM.substitute(color="var(--amber)", label="无更新", count=stale))
    if off:
        legend_parts.append(_LEGEND_ITEM.substitute(color="var(--off)", label="已移除", count=off))

    return led_html + f'<div class="led-legend mono">{"".join(legend_parts)}</div>'


def _render_status_html(channels: str = "-") -> str:
    if not STATUS_FILE.exists():
        empty = (
            '<div class="empty"><div class="headline">还没有数据</div>'
            "监控可能尚未运行过一轮，稍等它跑完第一轮就会出现在这里。</div>"
        )
        return _PAGE.substitute(
            overall_line="还没有<b>数据</b>", timestamp="-", pid="-", channels=_escape_html(channels),
            ledarray="", stats="", list=empty,
        )

    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        empty = '<div class="empty"><div class="headline">状态文件解析失败</div>请检查 status.json 是否损坏。</div>'
        return _PAGE.substitute(
            overall_line="状态<b>解析失败</b>", timestamp="-", pid="-", channels=_escape_html(channels),
            ledarray="", stats="", list=empty,
        )

    users = data.get("users", [])
    total = len(users)
    active = failing = stale = off = 0
    for u in users:
        fails = u.get("consecutive_fails", 0)
        hours = u.get("hours_since_update")
        if not u.get("in_users_conf", True):
            off += 1
        elif fails > 0:
            failing += 1
        elif hours is not None and hours >= 14 * 24:
            stale += 1
        else:
            active += 1

    overall_line = _overall_line(total, active, failing, stale)
    ledarray_html = _render_ledarray(active, failing, stale, off)

    if total == 0:
        stats_html = ""
        list_html = (
            '<div class="empty"><div class="headline">还没有监控账号</div>'
            "编辑工作目录下的 users.conf，一行一个「sec_user_id|昵称」。</div>"
        )
    else:
        stats_html = (
            '<div class="stats">'
            + _STAT_TEMPLATE.substitute(value=total, label="账号总数", color="")
            + _STAT_TEMPLATE.substitute(value=active, label="正常", color="green")
            + _STAT_TEMPLATE.substitute(value=failing, label="请求失败", color="red")
            + _STAT_TEMPLATE.substitute(value=stale, label="长期无更新", color="amber")
            + "</div>"
        )

        rows = []
        for u in users:
            fails = u.get("consecutive_fails", 0)
            hours = u.get("hours_since_update")
            if not u.get("in_users_conf", True):
                badge_color, status_color, status_text = "var(--off)", "off", "已移除"
            elif fails > 0:
                badge_color, status_color, status_text = "var(--red)", "red", f"失败 {fails} 次"
            elif hours is not None and hours >= 14 * 24:
                badge_color, status_color, status_text = "var(--amber)", "amber", f"{hours // 24} 天无更新"
            else:
                badge_color, status_color, status_text = "var(--green)", "green", "正常"

            freq_avg = u.get("freq_avg_days")
            freq_n = u.get("freq_sample_count", 0)
            freq_tip = f"基于最近 {freq_n} 条非置顶视频，平均 {freq_avg} 天/条" if freq_avg is not None else ""

            rows.append(
                _ROW_TEMPLATE.substitute(
                    uid=_escape_html(u.get("sec_user_id") or ""),
                    badge_color=badge_color,
                    status_color=status_color,
                    status_text=status_text,
                    nickname=_escape_html(u.get("nickname") or "-"),
                    freq_tag=(
                        _FREQ_TAG.substitute(label=_escape_html(u["update_frequency"]), tip=_escape_html(freq_tip))
                        if u.get("update_frequency")
                        else ""
                    ),
                    known_videos=u.get("known_videos", 0),
                    last_update_text=_format_last_update(hours),
                )
            )
        list_html = (
            '<div class="section-title">账号列表</div>'
            '<div class="list">' + "".join(rows) + "</div>"
        )

    return _PAGE.substitute(
        overall_line=overall_line,
        timestamp=_escape_html(data.get("timestamp") or "-"),
        pid=data.get("pid", "-"),
        channels=_escape_html(channels),
        ledarray=ledarray_html,
        stats=stats_html,
        list=list_html,
    )


def _escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# =================== HTTP 处理 ===================

_CHANNELS_CACHE = ""


def _build_health_json() -> str:
    if not STATUS_FILE.exists():
        return json.dumps({"status": "no_data", "users": 0, "failed_users": 0})
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return json.dumps({"status": "error", "message": "status.json parse error"})
    users = data.get("users", [])
    total = len(users)
    failing = sum(1 for u in users if u.get("consecutive_fails", 0) > 0)
    return json.dumps({
        "status": "ok",
        "timestamp": data.get("timestamp"),
        "pid": data.get("pid"),
        "users": total,
        "active_users": total - failing,
        "failed_users": failing,
    })


class _StatusHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logging.debug("[web] " + fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/user/"):
            self._serve_user()
        elif self.path.startswith("/api/status"):
            self._serve_json()
        elif self.path.startswith("/api/health"):
            self._serve_health()
        elif self.path == "/" or self.path.startswith("/?"):
            self._serve_html()
        else:
            self.send_error(404, "Not Found")

    def _serve_html(self) -> None:
        html = _render_status_html(_CHANNELS_CACHE)
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_json(self) -> None:
        if STATUS_FILE.exists():
            try:
                raw = STATUS_FILE.read_text(encoding="utf-8")
            except OSError:
                raw = "{}"
        else:
            raw = "{}"
        payload = raw.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_health(self) -> None:
        payload = _build_health_json().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_user(self) -> None:
        from datetime import datetime
        from .config import STATE_DIR
        uid = self.path[len("/api/user/"):].split("?")[0].split("/")[0]
        # 防止路径穿越：sec_user_id 只含字母、数字、下划线、连字符
        import re
        if not re.fullmatch(r"[\w-]+", uid):
            self.send_error(400, "Invalid user ID")
            return
        state_path = STATE_DIR / f"{uid}.json"
        if not state_path.exists():
            self.send_error(404, "User not found")
            return
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.send_error(500, "State file error")
            return

        videos_raw = data.get("videos", {})
        # 按 create_time 倒序排列
        videos_sorted = sorted(
            videos_raw.items(), key=lambda kv: kv[1].get("create_time", 0), reverse=True
        )
        videos_list = []
        for vid, meta in videos_sorted:
            ct = meta.get("create_time", 0)
            date_str = datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M") if ct else "-"
            videos_list.append({
                "video_id": vid,
                "title": meta.get("title", "-"),
                "date": date_str,
                "is_top": meta.get("is_top", False),
            })

        last_update = data.get("last_update_at") or data.get("initialized_at")
        elapsed = None
        if last_update:
            try:
                dt = datetime.fromisoformat(last_update)
                elapsed = (datetime.now() - dt).total_seconds()
            except (ValueError, TypeError):
                pass
        hours = int(elapsed // 3600) if elapsed is not None else None

        from .monitor import _freq_stats
        freq_label, freq_avg, freq_n = _freq_stats(videos_raw)
        freq_hint = f"基于最近 {freq_n} 条非置顶视频，平均 {freq_avg} 天/条" if freq_avg is not None else ""

        status_text = "正常"
        if data.get("consecutive_fails", 0) > 0:
            status_text = f"失败 {data['consecutive_fails']} 次"
        elif hours is not None and hours >= 14 * 24:
            status_text = f"{hours // 24} 天无更新"

        result = {
            "sec_user_id": uid,
            "nickname": data.get("nickname", "-"),
            "status_text": status_text,
            "known_videos": len(videos_raw),
            "consecutive_fails": data.get("consecutive_fails", 0),
            "last_update_ago": _format_last_update(hours),
            "initialized_at": data.get("initialized_at", "-"),
            "update_frequency": freq_label,
            "freq_hint": freq_hint,
            "videos": videos_list,
        }
        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# =================== 启动 ===================

def _guess_lan_ip() -> "str | None":
    """猜测本机在局域网里的 IP（用于 host=0.0.0.0 时给出更有用的访问地址提示）。

    用 UDP "连接" 一个公网地址来确定路由会走哪块网卡，不会真的发出数据包，
    在大多数 Linux 环境（包括没有公网出口的内网机器）上都能拿到一个可用的
    局域网 IP；拿不到就返回 None，调用方自行兜底。
    """
    import socket as _socket

    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _describe_access_urls(host: str, port: int) -> list:
    """host 是具体地址时直接给出该地址；host=0.0.0.0（监听所有网卡）时，
    直接打印 http://0.0.0.0:port/ 是打不开的，改成猜测局域网 IP 并同时
    提醒云服务器场景下要用公网 IP + 放通安全组。
    """
    if host not in ("0.0.0.0", "::", ""):
        return [f"http://{host}:{port}/"]

    urls = [f"http://127.0.0.1:{port}/（本机）"]
    lan_ip = _guess_lan_ip()
    if lan_ip:
        urls.append(f"http://{lan_ip}:{port}/（局域网，或云服务器的内网 IP）")
    urls.append(
        f"如果是云服务器/VPS，公网访问请用「服务器公网 IP:{port}」，"
        "并确认安全组/防火墙已放通该端口"
    )
    return urls


def start_web_server(
    host: str, port: int, stop_event: threading.Event, channels: "list[str] | None" = None
) -> ThreadingHTTPServer:
    """启动后台 HTTP 服务器线程，返回 server 实例（daemon 线程，随主进程退出）。"""
    global _CHANNELS_CACHE
    _CHANNELS_CACHE = ", ".join(channels) if channels else "-"

    server = ThreadingHTTPServer((host, port), _StatusHandler)

    def _serve() -> None:
        logging.info("状态面板已启动，监听 %s:%s，可以这样访问：", host, port)
        for line in _describe_access_urls(host, port):
            logging.info(f"  - {line}")
        server.serve_forever(poll_interval=0.5)

    thread = threading.Thread(target=_serve, name="web-status", daemon=True)
    thread.start()

    def _watch_stop() -> None:
        stop_event.wait()
        server.shutdown()

    threading.Thread(target=_watch_stop, name="web-status-watchdog", daemon=True).start()
    return server
