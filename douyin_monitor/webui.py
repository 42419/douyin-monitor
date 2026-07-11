"""极简只读 Web 状态面板。

不引入 Flask/FastAPI 等额外依赖，用标准库 http.server 实现：
- GET /          返回一个自动刷新的 HTML 状态页
- GET /api/status 返回 status.json 原始内容（供自己写前端/脚本轮询用）

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

_PAGE_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>抖音监控状态面板</title>
<meta http-equiv="refresh" content="30">
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f5f5f7; color: #1d1d1f; margin: 0; padding: 24px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .meta { color: #86868b; font-size: 13px; margin-bottom: 20px; }
  table { width: 100%; border-collapse: collapse; background: #fff;
          border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  th, td { text-align: left; padding: 10px 14px; font-size: 14px; border-bottom: 1px solid #eee; }
  th { background: #fafafa; color: #6e6e73; font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  .ok { color: #2fa84f; font-weight: 600; }
  .warn { color: #d9822b; font-weight: 600; }
  .bad { color: #d93025; font-weight: 600; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
  .empty { color: #86868b; padding: 24px; text-align: center; }
</style>
</head>
<body>
<h1>抖音监控状态面板</h1>
<div class="meta">最近一轮检查时间：$timestamp　|　进程 PID：$pid　|　页面每 30 秒自动刷新</div>
$body
</body>
</html>
""")

_ROW_TEMPLATE = Template("""<tr>
  <td>$nickname</td>
  <td class="$status_class">$status_text</td>
  <td>$known_videos</td>
  <td>$last_update</td>
  <td>$fails</td>
</tr>""")


def _render_status_html() -> str:
    if not STATUS_FILE.exists():
        body = '<div class="empty">暂无状态数据（监控可能尚未运行过一轮）</div>'
        return _PAGE_TEMPLATE.substitute(timestamp="-", pid="-", body=body)

    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        body = '<div class="empty">状态文件解析失败</div>'
        return _PAGE_TEMPLATE.substitute(timestamp="-", pid="-", body=body)

    users = data.get("users", [])
    if not users:
        body = '<div class="empty">users.conf 为空或暂无用户数据</div>'
    else:
        rows = []
        for u in users:
            fails = u.get("consecutive_fails", 0)
            hours = u.get("hours_since_update")
            if not u.get("in_users_conf", True):
                status_class, status_text = "warn", "已从配置移除"
            elif fails > 0:
                status_class, status_text = "bad", f"连续失败 {fails} 次"
            elif hours is not None and hours >= 14 * 24:
                status_class, status_text = "warn", f"{hours // 24} 天无更新"
            else:
                status_class, status_text = "ok", "正常"

            rows.append(
                _ROW_TEMPLATE.substitute(
                    nickname=_escape_html(u.get("nickname") or "-"),
                    status_class=status_class,
                    status_text=status_text,
                    known_videos=u.get("known_videos", 0),
                    last_update=_escape_html(u.get("last_update") or "-"),
                    fails=fails,
                )
            )
        body = (
            "<table><thead><tr>"
            "<th>用户</th><th>状态</th><th>已知视频数</th><th>最近更新时间</th><th>连续失败次数</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )

    return _PAGE_TEMPLATE.substitute(
        timestamp=_escape_html(data.get("timestamp") or "-"),
        pid=data.get("pid", "-"),
        body=body,
    )


def _escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class _StatusHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logging.debug("[web] " + fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/status"):
            self._serve_json()
        elif self.path == "/" or self.path.startswith("/?"):
            self._serve_html()
        else:
            self.send_error(404, "Not Found")

    def _serve_html(self) -> None:
        html = _render_status_html()
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


def start_web_server(host: str, port: int, stop_event: threading.Event) -> ThreadingHTTPServer:
    """启动后台 HTTP 服务器线程，返回 server 实例（daemon 线程，随主进程退出）。"""
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
