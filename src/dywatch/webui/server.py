"""HTTP 层：路由、探针、`/metrics`、以及服务器的生命周期。

路由：

```
GET /                           状态页（30 秒自动刷新）
GET /events                     事件时间线（过滤条件全在 URL 上）
GET /events?...&frag=1          时间线的片段，给页面自己的 60 秒局部刷新用
GET /assets/chart.umd.min.js    Chart.js（本地打包，按内容哈希做版本号）
GET /api/state                  status.json 原文（给脚本/别的面板用）
GET /api/health                 精简小结（机器可读：账号数 / 失败数 / 快照时间 / 闸门 / 自身）
GET /api/events                 事件列表（JSON，同样的过滤条件）
GET /api/user/<sec_user_id>     单账号详情：作品、已消失作品、最近事件、互动量曲线（读状态库）
GET /healthz                    进程活着（不碰任何依赖）
GET /readyz                     依赖探针（状态库可读 + DTK 可达）
GET /metrics                    Prometheus 文本
```

默认只听 `127.0.0.1` 且**没有鉴权**（设计里是明确的决定）：要暴露出去就自己加反代鉴权。

探针的取舍：

* `/healthz` 不碰任何依赖。数据库挂了、DTK 挂了，进程仍然应该报告"我活着"，
  否则编排层会把一个健康的进程反复重启。
* `/readyz` 才探依赖：DTK 能不能连上 + 状态库能不能读。
  这里探的是 DTK 的 `/healthz`（无需鉴权）而不是 `/auth/me`：两者都证明"连得上"，
  但前者不需要凭据、不消耗任何东西，而"凭据对不对"是启动自检该回答的问题——
  那是一个配置问题，不该在每次探针里重答一遍。
* `/metrics` 是**唯一**会顺手读一次状态库的端点（为了"最近各类事件有多少条"这类读数）。
  读失败就少几行 + `dywatch_state_readable 0`，绝不让整次抓取变成 500：那会把所有指标
  （包括"上游挂了"这种与库无关的）一起弄丢。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..messages import strip_controls
from ..settings import Settings
from ..users import is_safe_id
from . import charts, page_events, page_status, queries
from .common import STATUS_KEYS, _as_int, classify_account, read_status

# =================== 探针用的小工具 ===================


def _store_ok(db_path: Path) -> dict[str, Any]:
    """探针用的状态库检查。

    **先问文件在不在，再连。** `sqlite3.connect` 会给不存在的路径建一个 0 字节空库，
    而 `/readyz` 是被 systemd 与负载均衡周期性打的探针——让它把库"建"出来，
    之后 `queries.has_store()` 就会说"库在"，所有查询转成 `no such table: authors`，
    排障的人则会看着数据目录里那个空文件怀疑人生。这和 `queries.has_store` 的理由
    是同一条，只是发生在探针路径上。
    """
    if not db_path.is_file():
        return {"ok": False, "reason": "state store not found"}
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
        return {
            "ok": response.status == 200,
            "status": body.get("status"),
            "uptime_seconds": body.get("uptime_seconds"),
        }
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


# =================== Prometheus ===================


def _metrics_label(user: Mapping[str, Any]) -> str:
    """`/metrics` 的 `author=` label：**必须带上 `sec_user_id`**。

    只用昵称曾经是个真故障：昵称允许重复（`users.conf` 明说"可以与别的账号重复"），
    两个同名的账号会产出两行一模一样的样本，而 Prometheus 见到重复样本会把**整次抓取**
    判为失败——丢的不是那两个账号的指标，是这个面板的全部指标。
    label 里放 id 之后名称重复就不再冲突；昵称保留在后面，人看图表时还认得出是谁。
    """
    sec_uid = str(user.get("sec_user_id") or "").strip()
    nickname = str(user.get("nickname") or "").strip()
    if not sec_uid:
        return nickname or "?"
    return f"{sec_uid}|{nickname}" if nickname else sec_uid


def _label(value: str) -> str:
    """Prometheus 的 label 值：`\\` `"` 换行回车按规范转义，其余控制字符换空格。

    少一个换行转义，昵称里带 `\\n` 的那一行就会把整个样本拆成两条、抓取端直接判坏——
    而这只是某个账号的昵称，不该波及整个 `/metrics`。

    **截断必须在转义之前**：先转义再截断，第 64 个字符正好可能落在 `\\"` 的反斜杠上，
    于是 label 以一个落单的转义符结尾，样本照样是坏的（等于没修）。截断原始值就没有这个问题——
    代价是转义后的长度可能超过 64，而 64 本来就只是我们自己定的显示长度，不是协议要求。
    """
    # 96：`sec_user_id`（约 55 字符）加一个昵称要放得下，不然 id 会被截掉、
    # 同名账号又变回同一个 label（等于这条修复没生效）。
    text = str(value)[:96]
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    for raw, escaped in (("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t")):
        text = text.replace(raw, escaped)
    return strip_controls(text)


def _labels(pairs: Sequence[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{name}="{_label(value)}"' for name, value in pairs)
    return "{" + inner + "}"


class _Exposition:
    """按 Prometheus 文本格式攒样本。

    **每条指标都必须有 `# HELP` / `# TYPE` 且写在第一个样本之前。** 之前
    `dywatch_account_failures` 只写了样本、没有声明，抓取端会把它当成 untyped——
    不报错，但在图表里既不能算速率也不能算均值，人只会觉得"这个数怪怪的"。
    """

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._names: set[str] = set()

    def add(
        self,
        name: str,
        kind: str,
        help_: str,
        samples: Iterable[tuple[Sequence[tuple[str, str]], Any]],
    ) -> None:
        rows = [(labels, value) for labels, value in samples if value is not None]
        if not rows:
            return
        self._declare(name, kind, help_)
        for labels, value in rows:
            self._lines.append(f"{name}{_labels(labels)} {_num(value)}")

    def declare(self, name: str, kind: str, help_: str) -> None:
        """只声明不给样本。用于"值可能是 0 但必须存在"的指标。

        Prometheus 的计数器在归零时消失会让 `rate()` 出现断点；这里的取舍是
        **已经 emit 过就继续 emit**，所以空集合的 counter 也要声明一次。
        """
        self._declare(name, kind, help_)

    def _declare(self, name: str, kind: str, help_: str) -> None:
        if name in self._names:
            return
        self._names.add(name)
        self._lines.append(f"# HELP {name} {help_}")
        self._lines.append(f"# TYPE {name} {kind}")

    def text(self) -> str:
        return "\n".join(self._lines) + "\n"


def _num(value: Any) -> str:
    """样本值：布尔转 0/1，整数不带小数点。"""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    try:
        return repr(float(value))
    except (TypeError, ValueError):
        return "0"


def _epoch(text: Any) -> float | None:
    """`status.json` 里的 ISO 时间 → Unix 秒。认不出来就 `None`（那一行不输出）。"""
    if not isinstance(text, str) or not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def metrics_text(settings: Settings) -> str:
    """`/metrics` 的正文。**纯函数式**（只读快照 + 一次状态库统计），便于直接单测。"""
    data = read_status(settings)
    users = [item for item in (data.get("users") or []) if isinstance(item, dict)]
    gate = data.get("gate") or {}
    upstream = data.get("upstream") or {}
    self_check = data.get("self_check") or {}
    archive = data.get("archive") or {}
    notify = data.get("notify") or {}
    features = data.get("features") or {}

    stale_days = int(settings.get("STALE_FALLBACK_DAYS", 14))
    buckets: dict[str, int] = dict.fromkeys(STATUS_KEYS, 0)
    for user in users:
        color, _ = classify_account(user, stale_days)
        buckets[color] += 1

    keep_days = int(settings.get("EVENTS_KEEP_DAYS", 30))
    counts = queries.store_counts(
        settings, events_since=datetime.now(timezone.utc) - timedelta(days=keep_days)
    )

    out = _Exposition()
    out.add(
        "dywatch_users",
        "gauge",
        "Configured or known accounts.",
        [((), len(users))],
    )
    out.add(
        "dywatch_accounts",
        "gauge",
        "Accounts by status bucket (see the panel's legend).",
        [((("status", key),), buckets[key]) for key in STATUS_KEYS],
    )
    out.add(
        "dywatch_never_seen_accounts",
        "gauge",
        "Accounts that never returned a post.",
        [((), sum(1 for u in users if not u.get("ever_had_posts")))],
    )
    out.add(
        "dywatch_known_posts",
        "gauge",
        "Posts currently tracked per account.",
        [
            ((("author", _metrics_label(u)),), _as_int(u.get("known_posts")))
            for u in users
        ],
    )
    out.add(
        "dywatch_account_failures",
        "gauge",
        "Consecutive fetch failures per account.",
        [
            ((("author", _metrics_label(u)),), _as_int(u.get("consecutive_fails")))
            for u in users
        ],
    )

    out.add(
        "dywatch_rounds_total",
        "counter",
        "Rounds this process has completed (resets on restart).",
        [((), _as_int(data.get("rounds")))],
    )
    out.add(
        "dywatch_rounds_recorded_total",
        "counter",
        "Rounds ever recorded in the state DB (survives restarts, and is not"
        " truncated by ROUNDS_KEEP_DAYS).",
        [((), _as_int(data.get("rounds_total")))],
    )
    out.add(
        "dywatch_gate_open",
        "gauge",
        "1 when the global gate is open.",
        [((), 1 if gate.get("open", True) else 0)],
    )
    # 上游 retry_after 被封顶的次数：>0 说明"我们没完全听上游的"——闸门会比上游
    # 要求的更早放开，运维该看的是这个数（见 DESIGN 修正 #32）
    out.add(
        "dywatch_gate_retry_after_capped_total",
        "counter",
        "Times an upstream retry_after exceeded the configured ceiling and was capped.",
        [((), _as_int(gate.get("retry_after_capped")))],
    )

    out.add(
        "dywatch_self_check_ok",
        "gauge",
        "1 when the monitor's own resources are fine (see self_check reasons in"
        " status.json).",
        [((), 1 if self_check.get("ok", True) else 0)],
    )
    out.add(
        "dywatch_self_check_free_mb",
        "gauge",
        "Free megabytes on the filesystem holding the data directory.",
        [((), self_check.get("free_mb"))],
    )

    out.add(
        "dywatch_upstream_ok",
        "gauge",
        "1 when the last upstream system/status read succeeded (stale values are"
        " kept when it fails).",
        [((), 1 if upstream.get("ok", True) else 0)],
    )
    out.add(
        "dywatch_upstream_checked_timestamp_seconds",
        "gauge",
        "Unix time of the last upstream system/status read.",
        [((), _epoch(upstream.get("checked_at")))],
    )
    out.add(
        "dywatch_upstream_uptime_seconds",
        "gauge",
        "Uptime reported by the upstream.",
        [((), upstream.get("uptime_seconds"))],
    )
    components = upstream.get("components")
    if isinstance(components, Mapping):
        out.add(
            "dywatch_upstream_component_ok",
            "gauge",
            "1/0 per upstream component; absent when the upstream reports unknown.",
            [
                ((("component", str(name)),), 1 if dict(value).get("ok") else 0)
                for name, value in components.items()
                if isinstance(value, Mapping) and dict(value).get("ok") is not None
            ],
        )
        out.add(
            "dywatch_upstream_component_latency_ms",
            "gauge",
            "Reported latency per upstream component.",
            [
                ((("component", str(name)),), dict(value).get("latency_ms"))
                for name, value in components.items()
                if isinstance(value, Mapping)
            ],
        )
    pool = upstream.get("pool")
    if isinstance(pool, Mapping):
        out.add(
            "dywatch_upstream_pool_identities",
            "gauge",
            "Identity pool census, by platform and state.",
            [
                ((("platform", str(platform)), ("state", str(state))), count)
                for platform, value in pool.items()
                if platform != "total_active" and isinstance(value, Mapping)
                for state, count in value.items()
            ],
        )
        out.add(
            "dywatch_upstream_pool_active",
            "gauge",
            "Identities usable right now (upstream's pool.total_active).",
            [((), pool.get("total_active"))],
        )
    storage = upstream.get("storage")
    if isinstance(storage, Mapping):
        out.add(
            "dywatch_upstream_storage_db_bytes",
            "gauge",
            "Upstream database size.",
            [((), storage.get("db_size_bytes"))],
        )
        out.add(
            "dywatch_upstream_storage_identities",
            "gauge",
            "Identities stored by the upstream.",
            [((), storage.get("identities"))],
        )

    pending = _as_int(archive.get("pending")) if archive.get("enabled") else 0
    out.add(
        "dywatch_archive_pending",
        "gauge",
        "Archive downloads queued but not sent yet (0 when the side path is off).",
        [((), pending)],
    )
    out.add(
        "dywatch_archive_muted",
        "gauge",
        "1 when archive downloads are paused by capacity backoff.",
        [((), 1 if (archive.get("enabled") and archive.get("muted_code")) else 0)],
    )

    channels = notify.get("channels")
    out.add(
        "dywatch_notify_channels",
        "gauge",
        "Notification channels configured.",
        [((), len(channels)) if isinstance(channels, list) else ((), 0)],
    )
    out.add(
        "dywatch_notify_silent",
        "gauge",
        "1 when SILENT_MODE skips every push.",
        [((), 1 if notify.get("silent") else 0)],
    )

    out.add(
        "dywatch_feature_enabled",
        "gauge",
        "1 when a feature is switched on.",
        [
            ((("name", name),), 1 if features.get(key) else 0)
            for name, key in (
                ("metrics", "metrics"),
                ("hidden_check", "hidden_check"),
                ("archive_download", "archive_download"),
            )
        ],
    )

    # ---- 状态库规模：**读不出来时少几行，而不是报 0** ----
    # 报 0 会让人去查"为什么事件突然没了"，而真相是库读不出来（`dywatch_state_readable 0`
    # 就在同一份文本里，所以这个区分是有用的，不是啰嗦）
    out.add(
        "dywatch_state_readable",
        "gauge",
        "1 when the state DB answered a query.",
        [((), 1 if counts["readable"] else 0)],
    )
    if counts["readable"]:
        out.add(
            "dywatch_state_rows",
            "gauge",
            "Rows stored in the state DB, by table.",
            [
                ((("table", table),), counts[table])
                for table in ("authors", "posts", "tombstones", "events")
            ],
        )
        out.add(
            "dywatch_events_recent",
            "gauge",
            f"Events stored within EVENTS_KEEP_DAYS ({keep_days} days), by kind.",
            [
                ((("kind", kind),), count)
                for kind, count in sorted(counts["events_by_kind"].items())
            ],
        )
        out.add(
            "dywatch_post_metrics_rows",
            "gauge",
            "Engagement snapshot rows stored (hourly buckets).",
            [((), counts["metric_rows"])],
        )
        out.add(
            "dywatch_post_metrics_posts",
            "gauge",
            "Distinct posts with at least one engagement snapshot.",
            [((), counts["metric_posts"])],
        )
    return out.text()


def json_body(payload: Any) -> bytes:
    """响应体的序列化。**这里刻意不是裸 `json.dumps`。**

    面板的 JSON 是拼出来的（库里的读数值、Python 侧算出来的时间），只要有一个字段
    是 `datetime` 之类的非原生类型，`json.dumps` 就抛 `TypeError`——而它抛在
    `send_response` 之前，客户端拿到的是**连接被重置**：没有状态码、没有 body，
    浏览器只说"请求失败"，日志里才有一行 traceback。`/api/user/<id>` 就这样被一个
    `hour` 字段整条打不开过（`queries._metrics_json` 是它的正面修法，这里是兜底）。

    降级成字符串是**比整页打不开更好的失败方式**：一个读数显示成 ISO 时间戳，
    远好过一个点不动的详情弹窗。真要精确的类型，就在数据层归一化。
    """
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


# =================== HTTP ===================

#: 静态资源白名单。**只按名字给**，不从路径拼：面板可能被反代暴露到公网，
#: 一个 `../` 都别给它试的机会。
_ASSETS: Mapping[str, str] = {
    "chart.umd.min.js": "text/javascript; charset=utf-8",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "dywatch"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        del fmt, args

    # ------------------------------------------------------------------ helpers
    @property
    def settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    def _send(
        self, status: int, body: bytes, content_type: str, cache: str = "no-store"
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, html: str) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json_body(payload), "application/json; charset=utf-8")

    # ------------------------------------------------------------------ routes
    def do_GET(self) -> None:  # noqa: N802 - http.server 的接口
        split = urllib.parse.urlsplit(self.path)
        path = split.path
        query = urllib.parse.parse_qs(split.query)
        if path in ("/", "/index.html"):
            self._html(200, page_status.render_page(self.settings))
        elif path == "/events":
            self._events(query)
        elif path.startswith("/assets/"):
            self._asset(path[len("/assets/") :], query)
        elif path == "/api/state":
            self._json(200, read_status(self.settings))
        elif path == "/api/health":
            self._json(200, page_status.build_health(self.settings))
        elif path == "/api/events":
            self._json(200, events_payload(self.settings, query))
        elif path.startswith("/api/user/"):
            self._user(path)
        elif path == "/healthz":
            self._json(200, {"status": "ok"})
        elif path == "/readyz":
            self._readyz()
        elif path == "/metrics":
            self._send(
                200,
                metrics_text(self.settings).encode("utf-8"),
                "text/plain; version=0.0.4; charset=utf-8",
            )
        else:
            self._json(404, {"error": "not found"})

    def _events(self, query: Mapping[str, Sequence[str]]) -> None:
        filters = page_events.parse_filters(query)
        if filters["frag"]:
            # 片段接口：给页面自己的 60 秒局部刷新用。整页重载会丢滚动位置，
            # 而这一页的价值就在"往下翻着看"
            view = page_events.build_view(self.settings, filters)
            self._html(200, page_events.render_fragment(view))
            return
        self._html(200, page_events.render_page(self.settings, filters))

    def _asset(self, name: str, query: Mapping[str, Sequence[str]]) -> None:
        content_type = _ASSETS.get(name)
        if content_type is None:
            self._json(404, {"error": "not found"})
            return
        body = charts.asset_bytes()
        if not body:
            self._json(404, {"error": "asset missing from the installation"})
            return
        requested = (query.get("v") or [""])[0]
        # 版本号对不上就不缓存：老页面（或收藏的深链）指向旧版本时，宁可让它多下一次，
        # 也不要让浏览器把旧版本的图表库一直用下去——那种错看起来像"图表代码有 bug"
        cache = (
            "public, max-age=31536000, immutable"
            if requested == charts.asset_version()
            else "no-store"
        )
        self._send(200, body, content_type, cache=cache)

    def _user(self, path: str) -> None:
        """先解码再校验：前端 `encodeURIComponent` 会把 `=` `+` 编码掉，
        不解码就永远查不到；解码后的 `..` / `\\` 由 `is_safe_id` 拒绝。
        （这里查的是 SQLite 的参数化语句，本来也没有路径穿越，但没理由放宽这个检查。）
        """
        raw = urllib.parse.unquote(path[len("/api/user/") :].split("/", 1)[0])
        if not raw or not is_safe_id(raw):
            self._json(400, {"error": "invalid sec_user_id"})
            return
        try:
            detail = queries.user_detail(self.settings, raw)
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
        self._json(
            200 if ready else 503,
            {"status": "ok" if ready else "unavailable", "components": checks},
        )


def events_payload(
    settings: Settings, query: Mapping[str, Sequence[str]]
) -> dict[str, Any]:
    """`/api/events`：与网页同一组过滤条件、同一份摘要文字。

    **复用 `page_events` 的视图**而不是另写一套查询：两处各算一遍必然分叉，
    然后"网页上 12 条、API 9 条"会被当成面板的 bug 去查。
    """
    filters = page_events.parse_filters(query)
    view = page_events.build_view(settings, filters)
    return {
        "window": {
            "since": view["since"].isoformat(),
            "until": view["until"].isoformat(),
            "range": filters["range"],
        },
        "filters": {
            "range": filters["range"],
            "group": filters["group"],
            "kind": filters["kind"],
            "author": filters["author"],
        },
        "total": view["total_rows"],
        "truncated": view["truncated"],
        "notified": view["notified"],
        "silent": view["silent"],
        "chart": view["payload"],
        "events": [
            {
                "id": row.get("id"),
                "ts": row["ts"].isoformat()
                if isinstance(row.get("ts"), datetime)
                else None,
                "kind": row.get("kind"),
                "nickname": row.get("nickname") or "",
                "sec_user_id": row.get("sec_user_id") or "",
                "content_id": row.get("content_id"),
                "delivery": page_events.delivery_state(row),
                "text": page_events.summarize_payload(
                    str(row.get("kind") or ""), row.get("payload") or {}
                ),
                "payload": row.get("payload") or {},
            }
            for row in view["rows"]
        ],
    }


#: 内容安全策略。面板无鉴权、监听地址可能是 0.0.0.0，而页面里渲染的昵称/标题来自平台
#: （被监控账号的主人想改成什么就是什么）：转义是第一道防线，这是第二道。
#:
#: 诚实地说它挡住什么、挡不住什么：页面里有内联脚本，所以 `script-src` 必须带
#: `'unsafe-inline'`——**它拦不住一段被注入的内联脚本**。它拦得住的是后续动作：载入外部脚本
#: （`'self'` 以外一律拒绝）、把数据送出去（`connect-src` / `img-src` / `form-action` 都只
#: 允许同源）、改写 `<base>`。
#:
#: **刻意不加** `frame-ancestors` / `X-Frame-Options`：面板是只读的，没有可被劫持的操作，
#: 而不少人把它嵌在自己的 homepage / Home Assistant 的 iframe 里——禁掉就是白白弄坏他们。
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
    )
)

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
)


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
        self._server = _PanelServer(
            (settings["WEB_HOST"], int(settings["WEB_PORT"])), handler
        )
        self._server.settings = settings  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="dywatch-webui", daemon=True
        )

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
    "_Handler",
    "_PanelServer",
    "_dtk_ok",
    "_label",
    "_metrics_label",
    "_store_ok",
    "access_urls",
    "events_payload",
    "guess_lan_ip",
    "json_body",
    "metrics_text",
]
