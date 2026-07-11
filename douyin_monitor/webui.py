"""极简只读 Web 状态面板。

不引入 Flask/FastAPI 等额外依赖，用标准库 http.server 实现：
- GET /           返回一个自动刷新的 HTML 状态页（摘要卡片 + 用户表格）
- GET /api/status 返回 status.json 原始内容
- GET /api/health 返回健康检查 JSON

通过 .env 中 WEB_ENABLED=true 开启，随主循环一起在后台线程启动，
只监听本机回环地址（默认 127.0.0.1），如需外部访问请自行改 WEB_HOST
并注意做好网络层面的访问控制（本面板不做鉴权）。
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template

from .config import STATUS_FILE

# =================== HTML 模板 ===================

_PAGE = Template(r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>抖音监控状态面板</title>
<meta http-equiv="refresh" content="30">
<style>
  :root {
    --bg: #f0f2f5;
    --card: #ffffff;
    --border: #e8eaed;
    --text: #1f2937;
    --text2: #6b7280;
    --text3: #9ca3af;
    --green: #059669;
    --green-bg: #ecfdf5;
    --yellow: #d97706;
    --yellow-bg: #fffbeb;
    --red: #dc2626;
    --red-bg: #fef2f2;
    --blue: #2563eb;
    --blue-bg: #eff6ff;
    --radius: 12px;
    --shadow: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Helvetica Neue", sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5;
    min-height: 100vh; padding: 24px;
  }
  .container { max-width: 1100px; margin: 0 auto; }

  /* Header */
  .header { margin-bottom: 24px; }
  .header h1 {
    font-size: 22px; font-weight: 700; color: var(--text);
    display: flex; align-items: center; gap: 10px;
  }
  .header h1 .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--green); display: inline-block;
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .4; } }
  .meta {
    color: var(--text2); font-size: 13px; margin-top: 6px;
    display: flex; flex-wrap: wrap; gap: 16px;
  }
  .meta span { display: flex; align-items: center; gap: 4px; }

  /* Summary cards */
  .cards {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px; margin-bottom: 24px;
  }
  .card {
    background: var(--card); border-radius: var(--radius);
    padding: 16px 18px; box-shadow: var(--shadow);
    border: 1px solid var(--border);
  }
  .card-label { font-size: 12px; color: var(--text3); font-weight: 500; text-transform: uppercase; letter-spacing: .3px; }
  .card-value { font-size: 28px; font-weight: 700; margin-top: 4px; line-height: 1.2; }
  .card-value.green { color: var(--green); }
  .card-value.yellow { color: var(--yellow); }
  .card-value.red { color: var(--red); }
  .card-value.blue { color: var(--blue); }

  /* Table */
  .table-wrap {
    background: var(--card); border-radius: var(--radius);
    box-shadow: var(--shadow); border: 1px solid var(--border);
    overflow: hidden;
  }
  .table-title {
    padding: 16px 18px 12px; font-size: 15px; font-weight: 600;
    border-bottom: 1px solid var(--border);
  }
  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left; padding: 10px 16px; font-size: 12px;
    color: var(--text3); font-weight: 600; text-transform: uppercase;
    letter-spacing: .3px; background: #fafbfc; border-bottom: 1px solid var(--border);
  }
  td {
    padding: 12px 16px; font-size: 14px; border-bottom: 1px solid #f3f4f6;
    vertical-align: middle;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #f9fafb; }

  /* Status badges */
  .badge {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
    white-space: nowrap;
  }
  .badge-ok { background: var(--green-bg); color: var(--green); }
  .badge-warn { background: var(--yellow-bg); color: var(--yellow); }
  .badge-bad { background: var(--red-bg); color: var(--red); }
  .badge-info { background: var(--blue-bg); color: var(--blue); }
  .badge-dot {
    width: 6px; height: 6px; border-radius: 50%; display: inline-block;
  }
  .badge-ok .badge-dot { background: var(--green); }
  .badge-warn .badge-dot { background: var(--yellow); }
  .badge-bad .badge-dot { background: var(--red); }

  .nickname { font-weight: 600; color: var(--text); }
  .text-muted { color: var(--text2); font-size: 13px; }
  .text-mono { font-family: "SF Mono", "Cascadia Code", Consolas, monospace; font-size: 13px; }

  .empty {
    color: var(--text3); padding: 48px 24px; text-align: center; font-size: 14px;
  }
  .empty-icon { font-size: 32px; margin-bottom: 8px; opacity: .5; }

  @media (max-width: 640px) {
    body { padding: 12px; }
    .cards { grid-template-columns: repeat(2, 1fr); }
    th, td { padding: 8px 10px; font-size: 13px; }
    .hide-sm { display: none; }
  }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1><span class="dot"></span>抖音监控状态面板</h1>
    <div class="meta">
      <span>最近检查：$timestamp</span>
      <span>PID：$pid</span>
      <span>推送渠道：$channels</span>
      <span>每 30 秒自动刷新</span>
    </div>
  </div>
  $cards
  $table
</div>
</body>
</html>""")

_CARD_TEMPLATE = Template("""<div class="card">
  <div class="card-label">$label</div>
  <div class="card-value $color">$value</div>
</div>""")

_ROW_TEMPLATE = Template("""<tr>
  <td class="nickname">$nickname</td>
  <td><span class="badge $badge_class"><span class="badge-dot"></span>$badge_text</span></td>
  <td>$known_videos</td>
  <td class="text-muted">$last_update_ago</td>
  <td class="text-muted hide-sm">$initialized</td>
  <td class="text-mono">$fails</td>
</tr>""")


# =================== 渲染逻辑 ===================

def _format_ago(hours: int | None) -> str:
    if hours is None:
        return "-"
    if hours < 1:
        return "不到 1 小时"
    if hours < 24:
        return f"{hours} 小时前"
    days = hours // 24
    h = hours % 24
    return f"{days} 天 {h} 小时前" if h else f"{days} 天前"


def _format_datetime(iso_str: str | None) -> str:
    if not iso_str:
        return "-"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16] if len(iso_str) >= 16 else iso_str


def _render_status_html(channels: str = "-") -> str:
    if not STATUS_FILE.exists():
        cards = _render_cards(0, 0, 0, 0)
        table = '<div class="table-wrap"><div class="empty"><div class="empty-icon">&#128203;</div>暂无状态数据<br>监控可能尚未运行过一轮</div></div>'
        return _PAGE.substitute(timestamp="-", pid="-", channels=channels, cards=cards, table=table)

    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cards = _render_cards(0, 0, 0, 0)
        table = '<div class="table-wrap"><div class="empty"><div class="empty-icon">&#9888;&#65039;</div>状态文件解析失败</div></div>'
        return _PAGE.substitute(timestamp="-", pid="-", channels=channels, cards=cards, table=table)

    users = data.get("users", [])

    # 统计
    total = len(users)
    active = 0
    failing = 0
    stale = 0
    for u in users:
        fails = u.get("consecutive_fails", 0)
        hours = u.get("hours_since_update")
        if fails > 0:
            failing += 1
        elif hours is not None and hours >= 14 * 24:
            stale += 1
        else:
            active += 1

    cards = _render_cards(total, active, failing, stale)

    if not users:
        table = '<div class="table-wrap"><div class="empty"><div class="empty-icon">&#128100;</div>users.conf 为空或暂无用户数据</div></div>'
    else:
        rows = []
        for u in users:
            fails = u.get("consecutive_fails", 0)
            hours = u.get("hours_since_update")
            if not u.get("in_users_conf", True):
                badge_class, badge_text = "badge-info", "已移除"
            elif fails > 0:
                badge_class, badge_text = "badge-bad", f"失败 {fails} 次"
            elif hours is not None and hours >= 14 * 24:
                badge_class, badge_text = "badge-warn", f"{hours // 24} 天无更新"
            else:
                badge_class, badge_text = "badge-ok", "正常"

            rows.append(
                _ROW_TEMPLATE.substitute(
                    nickname=_escape_html(u.get("nickname") or "-"),
                    badge_class=badge_class,
                    badge_text=badge_text,
                    known_videos=u.get("known_videos", 0),
                    last_update_ago=_format_ago(hours),
                    initialized=_format_datetime(u.get("initialized_at")),
                    fails=fails,
                )
            )
        table = (
            '<div class="table-wrap">'
            '<div class="table-title">监控用户</div>'
            "<table><thead><tr>"
            "<th>用户</th><th>状态</th><th>视频数</th><th>最近更新</th>"
            "<th class=\"hide-sm\">首次记录</th><th>失败</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        )

    return _PAGE.substitute(
        timestamp=_escape_html(data.get("timestamp") or "-"),
        pid=data.get("pid", "-"),
        channels=channels,
        cards=cards,
        table=table,
    )


def _render_cards(total: int, active: int, failing: int, stale: int) -> str:
    return (
        _CARD_TEMPLATE.substitute(label="总用户", value=total, color="blue")
        + _CARD_TEMPLATE.substitute(label="正常", value=active, color="green")
        + _CARD_TEMPLATE.substitute(label="失败", value=failing, color="red")
        + _CARD_TEMPLATE.substitute(label="长期无更新", value=stale, color="yellow")
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
        if self.path.startswith("/api/status"):
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


# =================== 启动 ===================

def start_web_server(host: str, port: int, stop_event: threading.Event, channels: list[str] | None = None) -> ThreadingHTTPServer:
    """启动后台 HTTP 服务器线程，返回 server 实例（daemon 线程，随主进程退出）。"""
    global _CHANNELS_CACHE
    _CHANNELS_CACHE = ", ".join(channels) if channels else "-"

    server = ThreadingHTTPServer((host, port), _StatusHandler)

    def _serve() -> None:
        logging.info(f"状态面板已启动: http://{host}:{port}/")
        server.serve_forever(poll_interval=0.5)

    thread = threading.Thread(target=_serve, name="web-status", daemon=True)
    thread.start()

    def _watch_stop() -> None:
        stop_event.wait()
        server.shutdown()

    threading.Thread(target=_watch_stop, name="web-status-watchdog", daemon=True).start()
    return server
