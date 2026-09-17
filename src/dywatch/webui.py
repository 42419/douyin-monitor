"""只读面板与探针。

面板要回答的问题很小：**每个账号现在怎么样，以及它最近发生了什么**。

它不触发任何上游请求 —— 列表读 `status.json`（每轮写一次的快照），详情读状态库
（SQLite，只 `SELECT`）。所以打开面板不消耗身份、不会碰到风控，也不会因为上游抖动而变慢。

路由：

```
GET /                      状态页：LED 阵列 + 数据条 + 账号列表 + 详情弹窗，30 秒自动刷新
GET /api/state             status.json 原文（给脚本/别的面板用）
GET /api/health            精简小结（机器可读：账号数 / 失败数 / 快照时间 / 闸门）
GET /api/user/<sec_user_id>  单账号详情：作品、已消失作品、最近事件（读状态库）
GET /healthz               进程活着（不碰任何依赖）
GET /readyz                依赖探针（状态库可读 + DTK 可达）
GET /metrics               Prometheus 文本
```

视觉与交互来自旧项目 `douyin-monitor-enhance` 的面板（技术仪表盘风格的 LED 状态阵列 /
数据条 / 账号列表 / 详情弹窗）。移植时改的是**数据来源**：旧项目每账号一个 JSON 状态文件，
这里换成 SQLite 的 `authors` / `posts` / `tombstones` / `events` 四张表 —— 于是详情里多出
"已消失作品"与"最近事件"两块，它们在新项目里本来就有落库，只是旧面板没有地方显示。

探针的取舍：

* `/healthz` 不碰任何依赖。数据库挂了、DTK 挂了，进程仍然应该报告"我活着"，
  否则编排层会把一个健康的进程反复重启。
* `/readyz` 才探依赖：DTK 能不能连上 + 状态库能不能读。
  这里探的是 DTK 的 `/healthz`（无需鉴权）而不是 `/auth/me`：两者都证明"连得上"，
  但前者不需要凭据、不消耗任何东西，而"凭据对不对"是启动自检该回答的问题——
  那是一个配置问题，不该在每次探针里重答一遍。

默认只听 `127.0.0.1` 且**没有鉴权**（设计里是明确的决定）：要暴露出去就自己加反代鉴权。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from typing import Any, Iterable, Mapping

from .messages import (
    event_label,
    fmt_time,
    freq_hint,
    frequency_stats,
    hours_since,
    kind_label,
    newest_post_at,
    strip_controls,
    tombstone_reason,
)
from .models import Kind, PostState
from .settings import Settings
from .users import is_safe_id, load_users_conf

#: 页面自动刷新的秒数（`<meta http-equiv="refresh">`，不需要 JS 参与）。
REFRESH_SECONDS = 30

#: LED 阵列的格子数。24 格是旧项目调出来的：再多读不出比例，再少小类别看不出存在。
LED_SLOTS = 24

#: 状态分类：(key, 颜色变量, 列表徽章文案)。顺序 == 图例顺序 == 数据条顺序。
STATUS_KEYS = ("green", "red", "amber", "blue", "off")
STATUS_LEGEND = {
    "green": ("正常", "var(--green)"),
    "red": ("失败", "var(--red)"),
    "amber": ("长期无更新", "var(--amber)"),
    "blue": ("从未有作品", "var(--blue)"),
    "off": ("已移除", "var(--off)"),
}


# =================== 设计 token 与页面 ===================
# 技术仪表盘风格：亮色为主（近白背景 + 细网格底纹），单一信号蓝作交互强调色，
# 状态用绿/红/黄区分；大量使用等宽字体和方括号标签模拟"读数"质感。
# 深色模式通过 prefers-color-scheme 覆盖同一套变量，自动跟随系统。

_PAGE = Template(r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dywatch · 抖音监控状态</title>
<meta http-equiv="refresh" content="$refresh">
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
  .wrap { max-width: 780px; margin: 0 auto; }
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
    color: var(--text3); font-size: 12px; margin-bottom: 28px;
    display: flex; flex-wrap: wrap; gap: 4px 10px;
  }
  .meta .sep { color: var(--line); }

  /* ---- 上游闸门关闭时的警示条 ---- */
  .gate {
    margin-bottom: 28px; padding: 10px 14px; border-radius: 8px;
    border: 1px solid var(--red); background: var(--red-soft); color: var(--red);
    font-size: 12.5px;
  }

  /* ---- LED 状态阵列：一格一格的读数条，不是平滑进度条 ---- */
  .led-row { display: flex; gap: 3px; margin-bottom: 12px; }
  .led {
    flex: 1 1 0; height: 20px; border-radius: 2px;
    background: var(--off-soft);
  }
  .led.on-green { background: var(--green); }
  .led.on-red   { background: var(--red); }
  .led.on-amber { background: var(--amber); }
  .led.on-blue  { background: var(--blue); }
  .led.on-off   { background: var(--line); }

  .led-legend {
    display: flex; flex-wrap: wrap; gap: 4px 18px;
    font-size: 12px; color: var(--text2); margin-bottom: 36px;
  }
  .led-legend span { display: inline-flex; align-items: center; gap: 6px; }
  .led-legend i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }

  /* ---- 数据条：方括号包裹的标签 + 大号等宽数字 ----
     用 grid 而不是 flex+wrap，也不给格子画竖分割线，原因有两个，都是"换行"引起的：
       1. 竖线只能靠 `+ .stat` 这种"除第一个以外"的写法加，换行之后每行的第一个格子
          也会匹配到，于是第二行整体右移一个内边距——一行装不下时立刻就能看出来；
       2. flex 换行按"本行有几个"分配宽度，每行数量不同时列与列对不上；
          grid 的列宽是全局算的，2 列还是 3 列都各列对齐。
     列间距代替竖线：少了点仪器感，但任何宽度下都不会再错位。 */
  .stats {
    display: grid;
    /* 104px 是照着"6 格 + 5 个间距刚好放得下默认的 780px 内容宽"定的：
       再宽一点第 6 格就会被挤到第二行，孤零零一个 */
    grid-template-columns: repeat(auto-fit, minmax(104px, 1fr));
    column-gap: 18px;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    margin-bottom: 40px;
  }
  .stat { padding: 16px 0; }
  .stat-label { font-size: 11px; color: var(--text3); margin-bottom: 6px; }
  .stat-label::before { content: "["; }
  .stat-label::after { content: "]"; }
  .stat-value { font-size: 26px; font-weight: 700; line-height: 1; }
  .stat-value.green { color: var(--green); }
  .stat-value.red   { color: var(--red); }
  .stat-value.amber { color: var(--amber); }
  .stat-value.blue  { color: var(--blue); }

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
    cursor: pointer;
  }
  .row:hover { background: var(--off-soft); }
  .row-off { opacity: .62; }
  .row-badge { flex: 0 0 auto; width: 8px; height: 8px; border-radius: 2px; }
  .row-name {
    flex: 1 1 auto; min-width: 0;
    font-weight: 600; font-size: 14.5px; color: var(--text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .freq-tag {
    flex: 0 0 auto; position: relative;
    font-size: 11px; font-weight: 500; color: var(--text2);
    background: var(--panel); border: 1px solid var(--line);
    padding: 1px 7px; border-radius: 3px; white-space: nowrap;
  }
  .row-status {
    flex: 0 0 auto; font-size: 12px; font-weight: 600;
    padding: 2px 8px; border-radius: 3px; min-width: 86px; text-align: center;
  }
  .row-status.green { background: var(--green-soft); color: var(--green); }
  .row-status.red   { background: var(--red-soft); color: var(--red); }
  .row-status.amber { background: var(--amber-soft); color: var(--amber); }
  .row-status.blue  { background: var(--blue-soft); color: var(--blue); }
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

  /* ---- 可点击名字 ---- */
  .row-name { cursor: pointer; transition: color .15s; }
  .row-name:hover { color: var(--blue); }
  .row-name::after { content: " ›"; color: var(--text3); font-weight: 400; }
  .row-name:hover::after { color: var(--blue); }

  /* ---- Frequency tooltip ---- */
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
  .freq-tag.active .tip { display: block; }

  /* ---- 详情面板（桌面端：居中弹窗） ---- */
  .detail-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,.35);
    z-index: 100; backdrop-filter: blur(3px);
    /* 内容长的时候由**这一层**滚动，弹窗自己不设 max-height/overflow——
       弹窗内滚动条会让人以为"就这么多内容"，而且手机上还会和外层滚动打架 */
    overflow-y: auto; overscroll-behavior: contain;
    padding: 30px 16px;
  }
  .detail-overlay.open { display: flex; }
  .detail-panel {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; padding: 28px 28px 24px;
    width: 90%; max-width: 600px;
    /* flex 里用 margin:auto 居中：内容比视口高时自动退化成"贴顶 + 外层滚动"，
       而 justify-content:center 在那种情况下会把顶部裁掉 */
    margin: auto;
    box-shadow: 0 8px 32px rgba(0,0,0,.12);
    opacity: 0; transform: scale(.95); transition: opacity .2s, transform .2s;
  }
  .detail-overlay.open .detail-panel { opacity: 1; transform: scale(1); }
  .detail-head { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .detail-head h2 { font-size: 18px; font-weight: 700; margin: 0; flex: 1; }
  .detail-id { font-size: 11px; color: var(--text3); margin: -10px 0 16px; word-break: break-all; }
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
  .detail-note {
    font-size: 12px; color: var(--red); background: var(--red-soft);
    border-radius: 6px; padding: 8px 10px; word-break: break-all;
  }
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
  .video-list .vkind { flex: 0 0 auto; font-size: 11px; color: var(--text3); }
  .video-list .vabsent { flex: 0 0 auto; font-size: 11px; color: var(--red); font-weight: 600; }
  .detail-empty { color: var(--text3); text-align: center; padding: 24px; }

  /* 窄桌面（窗口拖到 560~760px）：固定三列，免得 auto-fit 把最后一格挤成孤行。
     必须排在小屏那条前面——两条都命中时后写的赢。 */
  @media (max-width: 760px) {
    .stats { grid-template-columns: repeat(3, 1fr); }
  }

  /* ---- 小屏：手机上的一屏装得下多少信息，就只放多少 ---- */
  @media (max-width: 560px) {
    body { padding: 26px 14px 44px; -webkit-tap-highlight-color: transparent; }
    .eyebrow { margin-bottom: 12px; }
    h1 { font-size: 23px; margin-bottom: 8px; }
    .meta { font-size: 11.5px; margin-bottom: 20px; gap: 2px 8px; }
    .meta .hide-sm { display: none; }  /* 上游地址与 PID：手机上只是噪音 */
    .led { height: 14px; }
    .led-legend { gap: 2px 14px; font-size: 11.5px; margin-bottom: 24px; }
    /* 手机固定两列：数字大、看得清，也不用猜 auto-fit 会排成几列 */
    .stats { grid-template-columns: repeat(2, 1fr); column-gap: 14px; margin-bottom: 26px; }
    .stat { padding: 12px 0; }
    .stat-value { font-size: 22px; }

    /* 列表行改成"状态一行 / 名字整行 / 细节一行"：
       名字独占一行既不容易点错，也放得下长昵称 */
    .row { flex-wrap: wrap; gap: 4px 8px; padding: 13px 2px; cursor: pointer; }
    .row-badge, .row-status { order: 0; }
    .row-status { min-width: 0; font-size: 11.5px; }
    .row-name { order: 1; flex: 1 1 100%; font-size: 15.5px; padding: 1px 0; }
    .freq-tag, .row-count { order: 2; }
    .row-count { min-width: auto; text-align: left; font-size: 12px; }
    .row-time { order: 2; min-width: auto; margin-left: auto; text-align: right; font-size: 11.5px; }
    /* 气泡提示在窄屏居中会溢出屏幕，改成左对齐 + 自动换行 */
    .freq-tag .tip { left: 0; transform: none; white-space: normal; width: max-content; max-width: 72vw; }
    .freq-tag .tip::after { left: 18px; }

    /* 详情：底部抽屉。内容短时贴底，长时整层滚动——抽屉自己不出现滚动条 */
    .detail-overlay { padding: 0; }
    .detail-panel {
      width: 100%; max-width: none; border-radius: 16px 16px 0 0;
      border: none; border-top: 1px solid var(--line);
      margin: auto 0 0;
      padding: 18px 16px calc(24px + env(safe-area-inset-bottom));
      box-shadow: 0 -4px 16px rgba(0,0,0,.1);
      opacity: 1; transform: translateY(100%); transition: transform .25s ease-out;
    }
    .detail-overlay.open .detail-panel { transform: translateY(0); }
    .detail-close { width: 40px; height: 40px; font-size: 20px; }
    .detail-head h2 { font-size: 17px; }
    .detail-grid { grid-template-columns: repeat(auto-fit, minmax(122px, 1fr)); gap: 4px 12px; }
    .detail-id { margin: -6px 0 12px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .eyebrow .dot { animation: none; opacity: 1; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow mono"><span class="dot" style="background:$dot_color"></span>[ DYWATCH / STATUS ]</div>
  <h1>$overall_line</h1>
  <div class="meta mono">
    <span>检查于 $timestamp</span><span class="sep">·</span>
    <span>第 $rounds 轮</span><span class="sep">·</span>
    <span>渠道 $channels</span><span class="sep">·</span>
    <span class="hide-sm">上游 $upstream</span><span class="sep hide-sm">·</span>
    <span class="hide-sm">PID $pid</span><span class="sep hide-sm">·</span>
    <span>$refresh 秒自动刷新</span>
  </div>

  $gate_html

  $ledarray

  $stats

  $list

  <div class="footer mono">
    <span>只读 · 无鉴权 · 数据来自 status.json 与状态库</span>
    <span><a href="/api/state">/api/state</a>&nbsp;&nbsp;<a href="/api/health">/api/health</a>&nbsp;&nbsp;<a href="/metrics">/metrics</a></span>
  </div>
</div>

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

<script>
// 点击整行打开详情：用 data-uid 属性 + 事件委托，而不是把 uid
// 拼进内联 onclick 的 JS 字符串里——HTML 实体转义没法防住内联事件处理器
// 里的 JS 字符串截断（浏览器解析属性值时会先做实体解码，解码结果才是
// 真正拿去当 JS 源码执行的内容，所以 &#39; 这类转义在这个上下文里防不住
// 单引号截断），用 dataset 读取就完全没有这个问题。
// 整行可点是给手指用的：手机上按名字那一行字太小了。
document.querySelectorAll('.row').forEach(function(row) {
  row.addEventListener('click', function(event) {
    // 频率气泡自己有交互（悬停/点按看均值），别让它顺带打开弹窗
    if (event.target.closest && event.target.closest('.freq-tag')) return;
    var uid = row.dataset.uid;
    if (uid) openDetail(uid);
  });
});
function openDetail(uid) {
  document.getElementById('detailOverlay').classList.add('open');
  // 弹窗自己滚动，所以背后那一页要停住，否则手机上滑到底会把列表也带着滚
  document.body.style.overflow = 'hidden';
  document.getElementById('detailName').textContent = '加载中...';
  document.getElementById('detailId').textContent = uid;
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
  document.body.style.overflow = '';
  document.getElementById('detailOverlay').scrollTop = 0;  // 下次打开从顶部开始
}
document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeDetail(); });

function renderDetail(d) {
  if (d.error) {
    document.getElementById('detailContent').innerHTML = '<div class="detail-empty">' + esc(d.error) + '</div>';
    return;
  }
  document.getElementById('detailName').textContent = d.nickname || d.sec_user_id;
  var h = '';
  h += '<div class="detail-grid">';
  h += di('状态', d.status_text);
  h += di('已知作品', d.known_posts + ' 条');
  h += di('已消失', d.tombstones + ' 条');
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
  if (d.posts && d.posts.length > 0) {
    h += '<div class="detail-section">已知作品（' + d.posts.length + ' 条，置顶在最前）</div>';
    h += '<ul class="video-list">';
    d.posts.forEach(function(v) {
      h += '<li>';
      h += '<span class="vtitle">' + esc(v.title) + '</span>';
      if (v.is_top) h += '<span class="vtop">置顶</span>';
      if (v.absent_rounds > 0) h += '<span class="vabsent">缺席 ' + v.absent_rounds + ' 轮</span>';
      h += '<span class="vkind">' + esc(v.kind) + '</span>';
      h += '<span class="vdate">' + esc(v.date) + '</span>';
      h += '</li>';
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
  document.getElementById('detailContent').innerHTML = h;
}
function di(label, value, tip) {
  var t = tip ? ' title="' + esc(tip) + '"' : '';
  return '<div class="detail-item"' + t + '><div class="dl">' + esc(label) + '</div><div class="dv">' + esc(value) + '</div></div>';
}
function esc(s) {
  var d = document.createElement('div');
  d.textContent = (s === null || s === undefined) ? '-' : s;
  // div.innerHTML 只转义内容上下文特殊字符（&<>），不转义引号；
  // esc() 的结果既会用在元素内容里，也会用在 title="..." 这种属性值里，
  // 补上引号转义让它在两种上下文里都安全，不依赖调用方自己判断用在哪
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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
</body>
</html>""")

_STAT_TEMPLATE = Template("""<div class="stat">
  <div class="stat-label">$label</div>
  <div class="stat-value $color mono">$value</div>
</div>""")

_ROW_TEMPLATE = Template("""<div class="row$row_class" data-uid="$uid">
  <span class="row-badge" style="background:$badge_color"></span>
  <span class="row-name" title="$uid_tip">$nickname</span>
  $freq_tag
  <span class="row-status $status_color">$status_text</span>
  <span class="row-count mono">$known_posts 条</span>
  <span class="row-time mono">$post_age_text</span>
</div>""")

_FREQ_TAG = Template('<span class="freq-tag">$label<span class="tip">$tip</span></span>')

_LEGEND_ITEM = Template('<span><i style="background:$color"></i>$label $count</span>')


# =================== 状态分类 ===================

def classify_account(user: Mapping[str, Any], stale_days: int) -> tuple[str, str]:
    """一个账号的 `(颜色 key, 状态文案)`。

    判定顺序就是"该先看哪个问题"的顺序：已移出配置（不用管了）→ 请求失败（最急）→
    从未返回过作品（多半是 ID 写错了）→ 长期无更新 → 正常。

    `never_seen` 单独一色是新项目才有的状态：抖音对**形态合法但不存在**的
    `sec_user_id` 返回 `200 + items:[]`，上游永远不会报错，只有这里能把它标出来。
    """
    if not user.get("configured", True):
        return "off", "已移除"
    fails = int(user.get("consecutive_fails") or 0)
    if fails > 0:
        return "red", f"失败 {fails} 次"
    if not user.get("ever_had_posts"):
        return "blue", "从未有作品"
    hours = user.get("hours_since_newest_post")
    if hours is not None and hours >= stale_days * 24:
        return "amber", f"{hours // 24} 天无新作品"
    return "green", "正常"


def _format_post_age(hours: int | None) -> str:
    """距**最新一条作品发布**过了多久。

    注意不是"距上次检测到变化"：账号被删了一条作品、或改了标题，`last_update_at`
    就会刷新，于是"距上次更新"会显示"刚刚"，而它其实已经 20 天没发东西了。
    """
    if hours is None:
        return "还没有作品"
    if hours < 1:
        return "刚刚发布"
    if hours < 24:
        return f"{hours} 小时前发布"
    return f"{hours // 24} 天前发布"


def _overall_line(total: int, ok: int, failing: int, stale: int, never: int) -> str:
    if total == 0:
        return "还没有<b>监控账号</b>"
    if not (failing or stale or never):
        return f"{total} 个账号<b>全部正常</b>"
    parts: list[str] = []
    if failing:
        parts.append(f"<b>{failing}</b> 个请求失败")
    if stale:
        parts.append(f"<b>{stale}</b> 个长期无更新")
    if never:
        parts.append(f"<b>{never}</b> 个从未有作品")
    return "，".join(parts) + f"，{ok} 个正常"


def _quantize_blocks(counts: Iterable[int], slots: int = LED_SLOTS) -> list[int]:
    """把各类别的账号数按比例分配成正好 `slots` 个整数格子（最大余数法）。

    先给每个非零类别保底 1 格（只要格子够用），再把剩余格子按原始比例用最大余数法
    分配。不这样做的话，极端比例下（比如 100:1:1:1:1）小类别会被直接舍成 0 格，
    阵列里完全看不出这个状态存在——而恰恰是那 1 个失败账号最需要被看见。
    """
    buckets = list(counts)
    n = len(buckets)
    total = sum(buckets)
    if total == 0:
        return [0] * n

    nonzero = [index for index, count in enumerate(buckets) if count > 0]
    base = [0] * n
    if len(nonzero) <= slots:
        for index in nonzero:
            base[index] = 1
        remaining = slots - len(nonzero)
    else:  # pragma: no cover - 类别数（5）永远小于格子数（24）
        remaining = slots

    if remaining > 0:
        raw = [(buckets[index] / total) * remaining for index in range(n)]
        extra = [int(value) for value in raw]
        leftover = remaining - sum(extra)
        order = sorted(range(n), key=lambda i: raw[i] - extra[i], reverse=True)
        for index in range(leftover):
            extra[order[index % n]] += 1
        for index in range(n):
            base[index] += extra[index]
    return base


def _render_ledarray(buckets: Mapping[str, int]) -> str:
    counts = [buckets.get(key, 0) for key in STATUS_KEYS]
    if sum(counts) == 0:
        return ""
    blocks = _quantize_blocks(counts)
    cells: list[str] = []
    for count, key in zip(blocks, STATUS_KEYS):
        cells.extend([f'<span class="led on-{key}"></span>'] * count)
    legend = "".join(
        _LEGEND_ITEM.substitute(
            color=STATUS_LEGEND[key][1], label=STATUS_LEGEND[key][0], count=buckets.get(key, 0)
        )
        for key in STATUS_KEYS
        if buckets.get(key, 0)
    )
    return f'<div class="led-row">{"".join(cells)}</div><div class="led-legend mono">{legend}</div>'


def _render_stats(total: int, buckets: Mapping[str, int]) -> str:
    if total == 0:
        return ""
    cells = [_STAT_TEMPLATE.substitute(value=total, label="账号总数", color="")]
    for key in ("green", "red", "amber", "blue", "off"):
        count = buckets.get(key, 0)
        if count or key != "off":  # "已移除 0" 是噪音，其余四项固定成行
            cells.append(
                _STAT_TEMPLATE.substitute(value=count, label=STATUS_LEGEND[key][0], color=key)
            )
    return '<div class="stats">' + "".join(cells) + "</div>"


def _gate_html(gate: Mapping[str, Any]) -> str:
    if gate.get("open", True):
        return ""
    reason = str(gate.get("reason") or "未知")
    remaining = gate.get("remaining_seconds")
    tail = ""
    if isinstance(remaining, (int, float)) and remaining:
        tail = f"，约 {int(remaining)} 秒后自动重试"
    return (
        '<div class="gate mono">[ 闸门关闭 ] 上游不可用（'
        + _escape_html(reason)
        + "），本轮的请求已整体跳过，只记录不推送"
        + tail
        + "。这与某个账号无关，必要时去 DTK 控制台看身份池。</div>"
    )


# =================== 页面渲染 ===================

def read_status(settings: Settings) -> dict[str, Any]:
    """读状态快照。文件不存在/损坏都返回 `{}`——面板不该因此 500。"""
    try:
        data = json.loads(settings.status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def render_page(settings: Settings) -> str:
    stale_days = int(settings.get("STALE_FALLBACK_DAYS", 14))
    data = read_status(settings)
    users = [item for item in (data.get("users") or []) if isinstance(item, dict)]
    notify = data.get("notify") or {}
    channels = "、".join(notify.get("channels") or []) or "静默"
    if notify.get("silent"):
        channels += "（静默）"
    upstream = str((data.get("upstream") or {}).get("base_url") or "—")

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
            '<div class="list">' + "".join(_render_row(user, stale_days) for user in users) + "</div>"
        )

    return _PAGE.substitute(
        refresh=REFRESH_SECONDS,
        dot_color="var(--green)" if not (buckets["red"] or buckets["amber"] or buckets["blue"])
        else ("var(--red)" if buckets["red"] else "var(--amber)"),
        overall_line=overall,
        timestamp=_escape_html(data.get("timestamp") or "—"),
        rounds=_escape_html(str(data.get("rounds") if data.get("rounds") is not None else "—")),
        channels=_escape_html(channels),
        upstream=_escape_html(upstream),
        pid=_escape_html(str(data.get("pid") or "—")),
        gate_html=_gate_html(data.get("gate") or {}),
        ledarray=_render_ledarray(buckets),
        stats=_render_stats(total, buckets),
        list=list_html,
    )


def _render_row(user: Mapping[str, Any], stale_days: int) -> str:
    color, status_text = classify_account(user, stale_days)
    sec_user_id = str(user.get("sec_user_id") or "")
    freq_label = user.get("update_frequency")
    hint = user.get("freq_hint") or ""
    return _ROW_TEMPLATE.substitute(
        row_class="" if user.get("configured", True) else " row-off",
        uid=_escape_html(sec_user_id),
        uid_tip=_escape_html(sec_user_id),
        badge_color=f"var(--{color})",
        status_color=color,
        status_text=_escape_html(status_text),
        nickname=_escape_html(user.get("nickname") or "-"),
        freq_tag=(
            _FREQ_TAG.substitute(label=_escape_html(str(freq_label)), tip=_escape_html(str(hint)))
            if freq_label
            else ""
        ),
        known_posts=int(user.get("known_posts") or 0),
        post_age_text=_escape_html(_format_post_age(user.get("hours_since_newest_post"))),
    )


def _escape_html(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# =================== 单账号详情（读状态库） ===================

def build_health(settings: Settings) -> dict[str, Any]:
    """机器可读的小结。格式与旧项目保持一致，另加新项目才有的轮次与闸门。"""
    data = read_status(settings)
    if not data:
        return {"status": "no_data", "users": 0, "failed_users": 0}
    users = [item for item in (data.get("users") or []) if isinstance(item, dict)]
    failing = sum(1 for user in users if int(user.get("consecutive_fails") or 0) > 0)
    return {
        "status": "ok",
        "timestamp": data.get("timestamp"),
        "pid": data.get("pid"),
        "rounds": data.get("rounds"),
        "users": len(users),
        "active_users": len(users) - failing,
        "failed_users": failing,
        "gate_open": bool((data.get("gate") or {}).get("open", True)),
    }


def user_detail(settings: Settings, sec_user_id: str) -> dict[str, Any] | None:
    """一个账号的详情：作者行 + 作品 + 已消失作品 + 最近事件。

    数据来自状态库（只读）。`None` 表示库里没有这个账号；`sqlite3.Error`
    原样抛出，由调用方转成 503——"读不出来"和"没有这个账号"是两回事。
    """
    db_path = settings.db_path
    if not db_path.is_file():
        return None

    # 普通连接而不是 `?mode=ro`：库是 WAL 模式，只读连接要求 -shm 已存在且可写，
    # 监控进程没在跑的时候反而会打开失败。这里只执行 SELECT，不写任何东西。
    conn = sqlite3.connect(str(db_path), timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        author = conn.execute(
            "SELECT * FROM authors WHERE sec_user_id = ?", (sec_user_id,)
        ).fetchone()
        if author is None:
            return None
        posts = conn.execute(
            # 置顶排最前（作者自己摆在最上面的东西，看的人往往就是想知道那几条），
            # 其余按发布时间倒序；NULL 发布时间排最后：抖音偶尔不返回 created_at，
            # 它不该因此排到最新。
            "SELECT * FROM posts WHERE sec_user_id = ?"
            " ORDER BY is_top DESC, created_at IS NULL, created_at DESC",
            (sec_user_id,),
        ).fetchall()
        removed = conn.execute(
            "SELECT * FROM tombstones WHERE sec_user_id = ? ORDER BY removed_at DESC LIMIT 20",
            (sec_user_id,),
        ).fetchall()
        removed_total = int(
            conn.execute(
                "SELECT COUNT(*) FROM tombstones WHERE sec_user_id = ?", (sec_user_id,)
            ).fetchone()[0]
        )
        events = conn.execute(
            "SELECT ts, content_id, kind FROM events WHERE sec_user_id = ? ORDER BY id DESC LIMIT 12",
            (sec_user_id,),
        ).fetchall()
    finally:
        conn.close()

    # "还在不在 users.conf 里"要和列表页口径一致：列表读快照里的 configured，而快照就是
    # 拿 users.conf 比对出来的。所以这里不去猜"文件读不到就当它还在"——那会让同一个账号
    # 在列表上写"已移除"、点开却写"正常"。
    known_ids = {entry.sec_user_id for entry in load_users_conf(settings.users_conf)}
    configured = sec_user_id in known_ids
    post_states = [_post_state(post) for post in posts]
    newest = newest_post_at(post_states)
    hours = hours_since(newest)
    row = {
        "sec_user_id": sec_user_id,
        "nickname": author["nickname"] or sec_user_id,
        "configured": configured,
        "consecutive_fails": int(author["consecutive_fails"] or 0),
        "ever_had_posts": bool(author["ever_had_posts"]),
        "hours_since_newest_post": hours,
    }
    color, status_text = classify_account(row, int(settings.get("STALE_FALLBACK_DAYS", 14)))

    freq = frequency_stats(post_states)

    return {
        "sec_user_id": sec_user_id,
        "nickname": row["nickname"],
        "status_text": status_text,
        "status_color": color,
        "known_posts": len(posts),
        "tombstones": removed_total,
        "consecutive_fails": row["consecutive_fails"],
        "newest_post_ago": _format_post_age(hours),
        "newest_post_at": fmt_time(newest),
        "initialized_at": fmt_time(_parse_dt(author["initialized_at"])),
        "last_seen_at": fmt_time(_parse_dt(author["last_seen_at"])),
        "update_frequency": freq[0] if freq else None,
        "freq_hint": freq_hint(freq),
        "runs": int(author["runs"] or 0),
        "last_error": author["last_error"],
        "last_error_code": author["last_error_code"],
        "posts": [
            {
                "content_id": post["content_id"],
                "title": post["title"] or "(无标题)",
                "kind": kind_label(Kind.parse(post["kind"])),
                "is_top": bool(post["is_top"]),
                "date": fmt_time(_parse_dt(post["created_at"])),
                "first_seen": fmt_time(_parse_dt(post["first_seen_at"])),
                "absent_rounds": int(post["absent_rounds"] or 0),
            }
            for post in posts
        ],
        "removed": [
            {
                "content_id": row_["content_id"],
                "date": fmt_time(_parse_dt(row_["removed_at"])),
                "reason": tombstone_reason(row_["reason"]),
            }
            for row_ in removed
        ],
        "events": [
            {
                "ts": fmt_time(_parse_dt(event["ts"])),
                "label": event_label(event["kind"]),
                "content_id": event["content_id"],
            }
            for event in events
        ],
    }


def _post_state(post: sqlite3.Row) -> PostState:
    return PostState(
        content_id=post["content_id"],
        kind=Kind.parse(post["kind"]),
        title=post["title"] or "",
        created_at=_parse_dt(post["created_at"]),
        is_top=bool(post["is_top"]),
    )


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# =================== 探针用的小工具 ===================

def _store_ok(db_path: Path) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
        try:
            conn.execute("SELECT 1 FROM authors LIMIT 1").fetchone()
        finally:
            conn.close()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _dtk_ok(base_url: str, timeout: float = 3.0) -> dict[str, Any]:
    """An unauthenticated liveness probe of the upstream instance."""
    url = base_url.rstrip("/") + "/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read() or b"{}")
        return {"ok": response.status == 200, "status": body.get("status"),
                "uptime_seconds": body.get("uptime_seconds")}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _label(value: str) -> str:
    """Prometheus 的 label 值：`\\` `"` 换行回车按规范转义，其余控制字符换空格。

    少一个换行转义，昵称里带 `\\n` 的那一行就会把整个样本拆成两条、抓取端直接判坏——
    而这只是某个账号的昵称，不该波及整个 `/metrics`。

    **截断必须在转义之前**：先转义再截断，第 64 个字符正好可能落在 `\\"` 的反斜杠上，
    于是 label 以一个落单的转义符结尾，样本照样是坏的（等于没修）。截断原始值就没有这个问题——
    代价是转义后的长度可能超过 64，而 64 本来就只是我们自己定的显示长度，不是协议要求。
    """
    text = str(value)[:64]
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    for raw, escaped in (("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t")):
        text = text.replace(raw, escaped)
    return strip_controls(text)


# =================== HTTP ===================

class _Handler(BaseHTTPRequestHandler):
    server_version = "dywatch"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        del fmt, args

    # ------------------------------------------------------------------ helpers
    @property
    def settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ------------------------------------------------------------------ routes
    def do_GET(self) -> None:  # noqa: N802 - http.server 的接口
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, render_page(self.settings).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(200, read_status(self.settings))
        elif path == "/api/health":
            self._json(200, build_health(self.settings))
        elif path.startswith("/api/user/"):
            self._user(path)
        elif path == "/healthz":
            self._json(200, {"status": "ok"})
        elif path == "/readyz":
            self._readyz()
        elif path == "/metrics":
            self._send(200, self._metrics(), "text/plain; version=0.0.4; charset=utf-8")
        else:
            self._json(404, {"error": "not found"})

    def _user(self, path: str) -> None:
        """先解码再校验：前端 `encodeURIComponent` 会把 `=` `+` 编码掉，
        不解码就永远查不到；解码后的 `..` / `\\` 由 `is_safe_id` 拒绝。
        （这里查的是 SQLite 的参数化语句，本来也没有路径穿越，但没理由放宽这个检查。）
        """
        raw = urllib.parse.unquote(path[len("/api/user/"):].split("/", 1)[0])
        if not raw or not is_safe_id(raw):
            self._json(400, {"error": "invalid sec_user_id"})
            return
        try:
            detail = user_detail(self.settings, raw)
        except sqlite3.Error as exc:
            self._json(503, {"error": f"状态库暂不可读：{type(exc).__name__}: {exc}"})
            return
        if detail is None:
            self._json(404, {"error": "状态库里没有这个账号（还没跑过一轮？）"})
            return
        self._json(200, detail)

    def _readyz(self) -> None:
        checks: dict[str, Any] = {}
        checks["state_store"] = _store_ok(self.settings.db_path)
        checks["dtk"] = _dtk_ok(str(self.settings["DTK_BASE_URL"]))
        ready = all(bool(item.get("ok")) for item in checks.values())
        self._json(200 if ready else 503, {"status": "ok" if ready else "unavailable",
                                           "components": checks})

    def _metrics(self) -> bytes:
        data = read_status(self.settings)
        users = [item for item in (data.get("users") or []) if isinstance(item, dict)]
        lines = [
            "# HELP dywatch_users Configured or known accounts.",
            "# TYPE dywatch_users gauge",
            f"dywatch_users {len(users)}",
            "# HELP dywatch_rounds_total Rounds this process has completed.",
            "# TYPE dywatch_rounds_total counter",
            f"dywatch_rounds_total {int(data.get('rounds') or 0)}",
            "# HELP dywatch_gate_open 1 when the global gate is open.",
            "# TYPE dywatch_gate_open gauge",
            f"dywatch_gate_open {1 if (data.get('gate') or {}).get('open', True) else 0}",
            "# HELP dywatch_known_posts Posts currently tracked per account.",
            "# TYPE dywatch_known_posts gauge",
        ]
        for user in users:
            label = _label(str(user.get("nickname") or user.get("sec_user_id") or "?"))
            lines.append(f'dywatch_known_posts{{author="{label}"}} {int(user.get("known_posts") or 0)}')
            lines.append(
                f'dywatch_account_failures{{author="{label}"}} {int(user.get("consecutive_fails") or 0)}'
            )
        lines.append("# HELP dywatch_never_seen_accounts Accounts that never returned a post.")
        lines.append("# TYPE dywatch_never_seen_accounts gauge")
        lines.append(
            f"dywatch_never_seen_accounts {sum(1 for u in users if not u.get('ever_had_posts'))}"
        )
        return ("\n".join(lines) + "\n").encode("utf-8")


class _PanelServer(ThreadingHTTPServer):
    """静默公网端口扫描器/探测连接常见的连接重置异常。

    面板暴露到公网时，扫描器经常连上就立刻断开（RST），Python 3.12 的
    `http.server` 在读取请求行时抛 `ConnectionResetError`，默认 `handle_error`
    会把完整 traceback 打到 stderr 刷屏。这类异常只记 debug，其它异常照旧。
    """

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: A003
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            logging.getLogger("dywatch.webui").debug(
                "客户端 %s 连接异常断开: %s", client_address, exc
            )
            return
        super().handle_error(request, client_address)


def access_urls(host: str, port: int) -> list[str]:
    """可访问的地址列表。

    `WEB_HOST=0.0.0.0` 时直接把 `0.0.0.0` 打印成 URL 是打不开的（浏览器会把它当成
    "本机某个地址"去试，行为依实现而定），所以换成猜出来的局域网 IP；猜不到就只留回环。
    """
    if host not in ("0.0.0.0", "::", ""):
        return [f"http://{host}:{port}/"]
    urls = [f"http://127.0.0.1:{port}/"]
    lan_ip = guess_lan_ip()
    if lan_ip and lan_ip != "127.0.0.1":
        urls.append(f"http://{lan_ip}:{port}/")
    return urls


def guess_lan_ip() -> str | None:
    """猜本机在局域网里的 IP（`WEB_HOST=0.0.0.0` 时给出更有用的访问地址）。

    用 UDP "连接" 一个公网地址来确定路由走哪块网卡，不会真的发出数据包；
    拿不到就返回 None，调用方自行兜底。
    """
    import socket as _socket

    try:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return None


class PanelServer:
    """A tiny read-only HTTP server running in its own thread."""

    def __init__(self, settings: Settings) -> None:
        handler = type("BoundHandler", (_Handler,), {})
        self._server = _PanelServer((settings["WEB_HOST"], int(settings["WEB_PORT"])), handler)
        self._server.settings = settings  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="dywatch-webui",
                                       daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def urls(self, settings: Settings) -> list[str]:
        """可访问的地址列表（见 `access_urls`）。"""
        _, port = self.address
        return access_urls(str(settings["WEB_HOST"]), port)


__all__ = [
    "PanelServer",
    "access_urls",
    "build_health",
    "classify_account",
    "guess_lan_ip",
    "read_status",
    "render_page",
    "user_detail",
]
