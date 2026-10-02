"""设计 token、页面骨架与共用的小组件模板。

视觉与交互来自旧项目 `douyin-monitor-enhance` 的面板（技术仪表盘风格：LED 状态阵列 /
数据条 / 账号列表 / 详情弹窗）。这是**唯一**放 CSS 的地方——拆分之前它和路由、查询、渲染
混在同一个 1300 行的模块里；页面一多（状态页 / 事件时间线），再混下去就没法改了。

改这里之前先记住三条：

* **CSS 里不许出现 `$`。** 小组件用 `string.Template` 拼，多一个 `$` 就是运行时
  `KeyError`，而它在源码里长得像一条正常的样式。页面外壳因此用 `%s` 而不是 `Template`。
* **颜色只从这一组变量里取。** 深色模式靠 `prefers-color-scheme` 覆盖同一组变量，
  任何写死的颜色都会在深色下变成一块瞎的地方。图表同理——它们从 `getComputedStyle`
  读这同一组变量（见 `charts.py`），不另开一套色板。
* **断点只有两个：760px / 560px。** 基础样式在前、断点覆盖在后（两条都命中时后写的赢），
  再加一档就会出现"某些宽度下谁覆盖谁说不清"的问题。
"""

from __future__ import annotations

from string import Template

#: 全部样式。`page()` 会把它塞进 `<style>`。
CSS = """

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
  .video-list .vhidden { flex: 0 0 auto; font-size: 11px; color: var(--amber); font-weight: 600; }
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

  /* =================== 导航、图表与时间线 ===================
     下面的规则跟着上面的顺序原则走：基础样式在前，断点覆盖在后。
     新增的类名统一用 `nav` / `chart-` / `tl-` / `selfcheck` 前缀，免得和状态类混在一起。 */

  /* ---- 页面切换：只有两页，放在页头右上角，不做侧边栏 ---- */
  .masthead { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
  .nav { flex: 0 0 auto; display: flex; gap: 2px; padding-top: 2px; }
  .nav a {
    font-size: 12px; font-weight: 600; text-decoration: none;
    color: var(--text3); padding: 4px 10px; border-radius: 4px;
    border: 1px solid transparent; white-space: nowrap;
  }
  .nav a:hover { color: var(--blue); border-color: var(--line); }
  .nav a.on { color: var(--blue); background: var(--blue-soft); border-color: transparent; }

  /* ---- 自身降级横幅：形状照 .gate，颜色用 amber ----
     gate 说的是"上游挂了"，这条说的是"我自己快不行了"，两件事同时发生就两条都显示。 */
  .selfcheck {
    margin-bottom: 28px; padding: 10px 14px; border-radius: 8px;
    border: 1px solid var(--amber); background: var(--amber-soft); color: var(--amber);
    font-size: 12.5px;
  }

  /* ---- 上游健康读数（`.kv` = key/value 读数网格）----
     和数据条 `.stats` 是两种东西：`.stats` 是"一眼的总览"（26px 大号数字、上下两条线），
     这里是一屏多的明细（13~14px、每格一个小圆点表示好没好）。所以它复用 `.stats` 的
     grid 思路（列宽全局算，换行也不会错位）但不复用它的字号与边框。 */
  .kv {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(148px, 1fr));
    column-gap: 18px;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    padding: 4px 0;
    margin-bottom: 40px;
  }
  .kv-item { padding: 9px 0; min-width: 0; }
  .kv-label {
    font-size: 11px; color: var(--text3); margin-bottom: 3px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .kv-label::before { content: "["; }
  .kv-label::after { content: "]"; }
  .kv-value {
    font-size: 14px; font-weight: 600; line-height: 1.35;
    display: flex; align-items: center; gap: 6px;
  }
  .kv-value.mono { font-variant-numeric: tabular-nums; }
  .kv-dot { width: 7px; height: 7px; border-radius: 2px; flex: 0 0 auto; }
  .kv-note { font-size: 11px; color: var(--text3); margin-top: 3px; line-height: 1.4; }

  /* ---- 图表容器 ----
     高度写死并配 `maintainAspectRatio: false`：Chart.js 默认按宽度推高度，容器一宽就变得
     很高，会把下面的账号列表挤出首屏。 */
  .chart-block { border-top: 1px solid var(--line); padding-top: 14px; margin-bottom: 36px; }
  .chart-head {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; flex-wrap: wrap;
  }
  .chart-range { font-size: 11.5px; color: var(--text3); }
  .chart-legend {
    display: flex; flex-wrap: wrap; gap: 4px 16px;
    font-size: 11.5px; color: var(--text2); margin: 6px 0 12px;
  }
  .chart-legend span { display: inline-flex; align-items: center; gap: 6px; }
  .chart-legend i { width: 9px; height: 2px; border-radius: 1px; display: inline-block; }
  .chart-canvas { position: relative; height: 190px; }
  .chart-canvas canvas { display: block; }
  .chart-empty {
    font-size: 12px; color: var(--text3); padding: 26px 12px;
    text-align: center; border: 1px dashed var(--line); border-radius: 8px;
  }

  /* ---- 事件时间线 ---- */
  .tl-filter { display: flex; flex-wrap: wrap; gap: 4px 6px; margin: 12px 0 0; }
  .tl-filter a {
    font-size: 11.5px; text-decoration: none; color: var(--text2);
    border: 1px solid var(--line); border-radius: 3px; padding: 2px 8px;
  }
  .tl-filter a:hover { color: var(--blue); border-color: var(--blue); }
  .tl-filter a.on { color: var(--blue); background: var(--blue-soft); border-color: var(--blue); }
  .tl { border-top: 1px solid var(--line); margin-top: 14px; }
  .tl-item {
    display: flex; align-items: baseline; gap: 10px;
    padding: 9px 4px; border-bottom: 1px solid var(--line-2); font-size: 13px;
    cursor: pointer;   /* 整行可点：点开看载荷原文 */
  }
  .tl-item:hover { background: var(--off-soft); }
  /* 展开的载荷紧跟在它那一行后面（`+` 选择器），所以行本身的底边要在那种情况下收掉，
     否则会看到"一条淡淡的横线夹在行与载荷之间"，看起来像两条不同的记录 */
  .tl-item:has(+ .tl-payload:not([hidden])) { border-bottom-color: transparent; }
  .tl-time { flex: 0 0 auto; font-size: 11.5px; color: var(--text3); min-width: 116px; }
  .tl-kind {
    flex: 0 0 auto; font-size: 11px; font-weight: 600;
    padding: 1px 7px; border-radius: 3px; min-width: 84px; text-align: center;
  }
  .tl-kind.k-good  { background: var(--blue-soft);  color: var(--blue); }
  .tl-kind.k-bad   { background: var(--red-soft);   color: var(--red); }
  .tl-kind.k-warn  { background: var(--amber-soft); color: var(--amber); }
  .tl-kind.k-quiet { background: var(--off-soft);   color: var(--text3); }
  .tl-who {
    flex: 0 1 auto; min-width: 0; max-width: 168px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text2);
  }
  .tl-text {
    flex: 1 1 auto; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .tl-sent { flex: 0 0 auto; font-size: 11px; color: var(--text3); }

  /* 事件详情（点开一条看载荷原文） */
  .tl-payload {
    margin: 0 0 10px; padding: 8px 10px; border-radius: 6px;
    background: var(--off-soft); color: var(--text2);
    font-size: 11.5px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
  }

  @media (max-width: 560px) {
    .nav a { font-size: 11.5px; padding: 3px 8px; }
    /* 手机上两列：一列会让 8 格读数变成很长的一竖条，翻起来像在找东西 */
    .kv { grid-template-columns: repeat(2, 1fr); column-gap: 14px; margin-bottom: 26px; }
    .chart-canvas { height: 156px; }
    .chart-block { margin-bottom: 26px; }
    .tl-time { min-width: 0; }
    .tl-kind { min-width: 0; }
    /* 手机上挤不下四列，改成两行：时间与类型一行，内容整行 */
    .tl-item { flex-wrap: wrap; gap: 2px 8px; }
    .tl-text { flex: 1 1 100%; white-space: normal; }
    .tl-who { order: 2; }
    .tl-sent { order: 2; margin-left: auto; }
  }
"""


# =================== 页面骨架 ===================

#: 页面外壳。用 `%s` 而不是 `Template` 拼：CSS 里一个 `$` 就会让 `Template` 在渲染时抛
#: `KeyError`，而那时候你只会看到"少了一个占位符"，不会想到是样式里的符号。
_SHELL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
%s
<style>
%s
</style>
</head>
<body>
<div class="wrap">
%s
</div>
%s
</body>
</html>"""


def page(*, title: str, body: str, scripts: str = "", refresh: int = 0) -> str:
    """拼一页完整的 HTML。

    `refresh` 是 `<meta http-equiv="refresh">` 的秒数，0 表示不自动刷新——事件时间线页传 0：
    正在读列表的时候整页重载，会把滚动位置和刚点的筛选一起丢掉。
    """
    meta = (
        f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh > 0 else ""
    )
    return _SHELL % (title, meta, CSS, body, scripts)


def nav(active: str) -> str:
    """右上角的页面切换。只有两页，所以不做成组件，直接拼。"""
    items = (("/", "状态", "status"), ("/events", "事件", "events"))
    links = "".join(
        f'<a href="{href}" class="on">{label}</a>'
        if key == active
        else f'<a href="{href}">{label}</a>'
        for href, label, key in items
    )
    return f'<div class="nav mono">{links}</div>'


def masthead(*, eyebrow: str, headline: str, meta: str, active: str) -> str:
    """页头：左边方括号读数 + 大标题 + 一行元信息，右边页面切换。"""
    return (
        '<div class="masthead">'
        "<div>"
        f'<div class="eyebrow mono">[ {eyebrow} ]</div>'
        f"<h1>{headline}</h1>"
        f'<div class="meta mono">{meta}</div>'
        "</div>" + nav(active) + "</div>"
    )


# =================== 共用小组件 ===================

STAT_TEMPLATE = Template("""<div class="stat">
  <div class="stat-label">$label</div>
  <div class="stat-value $color mono">$value</div>
</div>""")

ROW_TEMPLATE = Template("""<div class="row$row_class" data-uid="$uid">
  <span class="row-badge" style="background:$badge_color"></span>
  <span class="row-name" title="$uid_tip">$nickname</span>
  $freq_tag
  <span class="row-status $status_color">$status_text</span>
  <span class="row-count mono">$known_posts 条</span>
  <span class="row-time mono">$post_age_text</span>
</div>""")

FREQ_TAG = Template('<span class="freq-tag">$label<span class="tip">$tip</span></span>')

LEGEND_ITEM = Template('<span><i style="background:$color"></i>$label $count</span>')

TL_ITEM = Template("""<div class="tl-item">
  <span class="tl-time mono">$time</span>
  <span class="tl-kind $cls">$kind</span>
  $who
  <span class="tl-text">$text</span>
  $sent
</div>""")

__all__ = [
    "CSS",
    "FREQ_TAG",
    "LEGEND_ITEM",
    "ROW_TEMPLATE",
    "STAT_TEMPLATE",
    "TL_ITEM",
    "masthead",
    "nav",
    "page",
]
