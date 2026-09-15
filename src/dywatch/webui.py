"""只读面板与探针。

面板很小，因为它要回答的问题很小：**每个账号现在怎么样**。它不触发任何上游请求，
只读 `status.json` 和状态库——所以打开它不会消耗一个身份，也不会被风控。

探针的取舍：

* `/healthz` 不碰任何依赖。数据库挂了、DTK 挂了，进程仍然应该报告"我活着"，
  否则编排层会把一个健康的进程反复重启。
* `/readyz` 才探依赖：DTK 能不能连上 + 状态库能不能写。
  这里探的是 DTK 的 `/healthz`（无需鉴权）而不是 `/auth/me`：两者都证明"连得上"，
  但前者不需要凭据、不消耗任何东西，而"凭据对不对"是启动自检该回答的问题——
  那是一个配置问题，不该在每次探针里重答一遍。

默认只听 `127.0.0.1` 且**没有鉴权**（设计里是明确的决定）：要暴露出去就自己加反代鉴权。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .settings import Settings

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dywatch · 抖音监控状态</title>
<style>
 :root{--bg:#FAFAF8;--panel:#fff;--line:#E4E3DD;--text:#16171A;--muted:#6B6E76;
       --blue:#155EEF;--green:#17875A;--amber:#B4680A;--red:#D1352B;--mono:ui-monospace,Menlo,Consolas,monospace}
 @media (prefers-color-scheme:dark){:root{--bg:#0C0D10;--panel:#17181C;--line:rgba(255,255,255,.12);
       --text:#EDEEF0;--muted:#9A9EA6;--blue:#5B92FF;--green:#3FCE8E;--amber:#E4A63A;--red:#FF6B60}}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
   font:14px/1.6 system-ui,-apple-system,"Segoe UI","Noto Sans CJK SC",sans-serif}
 .wrap{max-width:1040px;margin:0 auto;padding:24px}
 h1{font-size:18px;font-weight:600;margin:0 0 4px}
 .sub{color:var(--muted);font-size:13px;margin-bottom:18px}
 .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
 .tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
 .tile .k{color:var(--muted);font-size:12px}.tile .v{font-size:22px;font-weight:600;font-family:var(--mono)}
 table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);
   border-radius:12px;overflow:hidden}
 th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);font-size:13px}
 th{color:var(--muted);font-weight:500;font-size:12px}
 tr:last-child td{border-bottom:0}
 code,.mono{font-family:var(--mono)}
 .pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;border:1px solid var(--line)}
 .ok{color:var(--green)}.warn{color:var(--amber)}.bad{color:var(--red)}.muted{color:var(--muted)}
 .empty{padding:28px;text-align:center;color:var(--muted)}
</style></head><body><div class="wrap">
<h1>dywatch · 抖音监控状态</h1>
<div class="sub" id="sub">加载中…</div>
<div class="tiles" id="tiles"></div>
<table><thead><tr><th>账号</th><th>状态</th><th>已知作品</th><th>更新频率</th>
<th>上次更新</th><th>连续失败</th><th>下次检查</th></tr></thead><tbody id="rows"></tbody></table>
<div class="sub" id="events" style="margin-top:18px"></div>
</div>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function pill(text, cls){return '<span class="pill '+cls+'">'+esc(text)+'</span>'}
function hours(v){return v==null?'—':(v<1?'<1 小时':v+' 小时')}
async function tick(){
  let d;
  try{ d = await (await fetch('/api/state',{cache:'no-store'})).json(); }
  catch(e){ document.getElementById('sub').textContent='读取状态失败：'+e; return; }
  document.getElementById('sub').textContent =
    '快照 '+ (d.timestamp||'—') +' · PID '+ (d.pid||'—')
    +' · 上游闸门 '+ ((d.gate&&d.gate.open)?'正常':'已关闭('+esc(d.gate&&d.gate.reason)+')')
    +' · 渠道 '+ esc((d.notify&&d.notify.channels||[]).join(',')||'静默')
    +' · 刷新于 '+ new Date().toLocaleTimeString();
  const users = d.users||[];
  const total = users.length;
  const failing = users.filter(u=>(u.consecutive_fails||0)>0).length;
  const never = users.filter(u=>!u.ever_had_posts).length;
  const posts = users.reduce((a,u)=>a+(u.known_posts||0),0);
  document.getElementById('tiles').innerHTML = [
    ['账号', total], ['已知作品', posts], ['失败中', failing], ['从未有作品', never],
    ['本进程轮次', d.rounds||0]
  ].map(([k,v])=>'<div class="tile"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>').join('');
  const rows = users.map(u=>{
    let cls='ok', text='正常';
    if((u.consecutive_fails||0)>0){cls='bad';text='失败 '+u.consecutive_fails}
    else if(!u.ever_had_posts){cls='warn';text='无作品'}
    else if(!u.configured){cls='muted';text='已移出配置'}
    return '<tr><td><div>'+esc(u.nickname)+'</div><div class="muted mono" style="font-size:11px">'
      +esc(u.sec_user_id.slice(0,18))+'…</div></td>'
      +'<td>'+pill(text,cls)+'</td><td class="mono">'+(u.known_posts||0)+'</td>'
      +'<td>'+esc(u.update_frequency||'—')+'</td><td class="mono">'+hours(u.hours_since_update)+'</td>'
      +'<td class="mono">'+(u.consecutive_fails||0)+'</td>'
      +'<td class="muted mono" style="font-size:12px">'+(u.last_error_code?esc(u.last_error_code):'—')+'</td></tr>';
  }).join('');
  document.getElementById('rows').innerHTML = rows || '<tr><td colspan="7" class="empty">还没有账号。把账号写进 users.conf 即可。</td></tr>';
  document.getElementById('events').textContent = '状态文件每轮更新；本页面每 15 秒自动刷新一次。';
}
tick(); setInterval(tick, 15000);
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "dywatch"
    settings: Settings
    store_path: Path

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        del fmt, args

    # ------------------------------------------------------------------ helpers
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

    def _read_status(self) -> dict[str, Any]:
        try:
            return json.loads(self.server.settings.status_path.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
        except (OSError, ValueError):
            return {"timestamp": None, "users": [], "gate": {}, "notify": {}}

    # ------------------------------------------------------------------ routes
    def do_GET(self) -> None:  # noqa: N802 - http.server 的接口
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(200, self._read_status())
        elif path == "/healthz":
            self._json(200, {"status": "ok"})
        elif path == "/readyz":
            self._readyz()
        elif path == "/metrics":
            self._send(200, self._metrics(), "text/plain; version=0.0.4; charset=utf-8")
        else:
            self._json(404, {"error": "not found"})

    def _readyz(self) -> None:
        checks: dict[str, Any] = {}
        checks["state_store"] = _store_ok(self.server.settings.db_path)  # type: ignore[attr-defined]
        checks["dtk"] = _dtk_ok(str(self.server.settings["DTK_BASE_URL"]))  # type: ignore[attr-defined]
        ready = all(bool(item.get("ok")) for item in checks.values())
        self._json(200 if ready else 503, {"status": "ok" if ready else "unavailable",
                                           "components": checks})

    def _metrics(self) -> bytes:
        data = self._read_status()
        users = data.get("users") or []
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
            label = _label(user.get("nickname") or user.get("sec_user_id") or "?")
            lines.append(f'dywatch_known_posts{{author="{label}"}} {int(user.get("known_posts") or 0)}')
            lines.append(
                f'dywatch_account_failures{{author="{label}"}} {int(user.get("consecutive_fails") or 0)}'
            )
        lines.append("# HELP dywatch_never_seen_accounts Accounts that never returned a post.")
        lines.append("# TYPE dywatch_never_seen_accounts gauge")
        lines.append(f"dywatch_never_seen_accounts {sum(1 for u in users if not u.get('ever_had_posts'))}")
        return ("\n".join(lines) + "\n").encode("utf-8")


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')[:64]


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


class PanelServer:
    """A tiny read-only HTTP server running in its own thread."""

    def __init__(self, settings: Settings) -> None:
        handler = type("BoundHandler", (_Handler,), {})
        self._server = ThreadingHTTPServer((settings["WEB_HOST"], int(settings["WEB_PORT"])), handler)
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
        host, port = self.address
        if str(settings["WEB_HOST"]) in ("0.0.0.0", "::"):
            return [f"http://127.0.0.1:{port}", f"http://<本机局域网 IP>:{port}"]
        return [f"http://{host}:{port}"]


__all__ = ["PanelServer"]
