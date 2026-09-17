"""DTK v5 HTTP 客户端 —— 唯一知道 HTTP 的地方。

它只做四件事，其余一概不管：认证、解信封、`wait`→202→任务轮询、把失败归一化成
`MonitorError`。它**不认识**"新作品"这个概念，也不碰数据库。

归一化是这个模块最重要的职责。上游的五类失败在下游只应该表现为一个 `code`：

    DTK_UNREACHABLE      连不上 / DNS / 超时
    DTK_MALFORMED        HTTP 非 2xx 且响应不是 DTK 的信封
    CONTRACT_VIOLATION   信封成功，但载荷不符合我们读得懂的契约
    TASK_TIMEOUT         202 之后轮询到放弃也没等到终态
    其余                 直接采用上游的 error.code（RATE_LIMITED、NOT_FOUND …）

`CONTRACT_VIOLATION` 单独一类很重要：它意味着 **DTK 升级后响应形状变了**，
应当告警提醒升级本工具，而不是当成网络抖动反复重试。

实测钉死的三条（写代码时按这个来，别按直觉）：
  * `?wait=` 按时完成→200 且 `data` 是**单层**载荷；未完成→202 且 `data={task_id,state}`
  * 202 之后结果在 `GET /tasks/{id}` 的 `data.data`（**两层**）
  * **形态合法但不存在的 `sec_user_id` 返回 200 + `items:[]`**，不会报错
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Sequence

import httpx

from .models import ArchiveItem, Content, Kind, Page

#: 上游错误码里，值得整体闸门暂停的那些 —— 都是"现在别发请求"的意思
GATE_CODES: Final[frozenset[str]] = frozenset(
    {
        "RATE_LIMITED",
        "IDENTITY_POOL_EXHAUSTED",
        "ENDPOINT_CIRCUIT_OPEN",
        "QUEUE_FULL",
    }
)

#: 配置性问题，重试没有意义，应当直接告诉人
CONFIG_CODES: Final[frozenset[str]] = frozenset(
    {"UNAUTHENTICATED", "FORBIDDEN_SCOPE", "INVALID_PARAM", "NOT_CONFIGURED"}
)

#: 写请求（归档下载、pin）的超时。DTK 受理一个下载请求是本地操作，正常都在 1 秒内；
#: 10 秒还不回来就是它不对劲，再等下去只会把整轮的结束时间往后拖——写请求在旁路上，
#: 没有哪个人在等它。
WRITE_TIMEOUT: Final[float] = 10.0

#: 上游侧的临时问题，该账号记一次失败然后退避
TRANSIENT_CODES: Final[frozenset[str]] = frozenset(
    {
        "UPSTREAM_RISK_CONTROL",
        "UPSTREAM_CHANGED",
        "SIGNING_FAILED",
        "INTERNAL",
        "DTK_UNREACHABLE",
        "DTK_MALFORMED",
        "TASK_TIMEOUT",
    }
)

#: 我们自己的错误码 = 上游没有的那些
LOCAL_CODES: Final[frozenset[str]] = frozenset(
    {"DTK_UNREACHABLE", "DTK_MALFORMED", "CONTRACT_VIOLATION", "TASK_TIMEOUT"}
)


class MonitorError(RuntimeError):
    """Every failure this tool can produce, normalized to one code."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        details: Mapping[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retry_after = retry_after

    @property
    def is_gate(self) -> bool:
        return self.code in GATE_CODES

    @property
    def is_config(self) -> bool:
        return self.code in CONFIG_CODES

    @property
    def is_contract(self) -> bool:
        return self.code == "CONTRACT_VIOLATION"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}" if self.message else self.code


# ---------------------------------------------------------------------------
# 解析（纯函数，可脱网单测）
# ---------------------------------------------------------------------------


def _parse_dt(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _int_or_none(raw: Any) -> int | None:
    """Keep DTK's `None != 0` promise: absence stays `None`."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_content(node: Mapping[str, Any], *, raw_included: bool = False) -> Content:
    """DTK 的归一化作品对象 → 我们的 `Content`。

    `is_top` 只从 `raw` 里读，而且**只有这一轮带了 include_raw 才是真值**；
    没带时一律按 `False` 处理，绝不把已有值覆盖成 False 的判断留给调用方——
    调用方拿到的是"这一轮看到的事实"，合并规则在 `diff` 里。
    """
    raw = node.get("raw") if raw_included else None
    is_top = bool(raw.get("is_top")) if isinstance(raw, Mapping) else False

    media = node.get("media") or {}
    covers = media.get("covers") or []
    images = media.get("images") or []
    cover_url = None
    if isinstance(covers, Sequence) and covers:
        first = covers[0]
        if isinstance(first, Mapping):
            cover_url = first.get("url")

    author = node.get("author") or {}
    stats = node.get("stats") or {}

    return Content(
        content_id=str(node.get("content_id") or ""),
        kind=Kind.parse(node.get("kind")),
        title=str(node.get("title") or ""),
        description=str(node.get("description") or ""),
        web_url=str(node.get("web_url") or ""),
        created_at=_parse_dt(node.get("created_at")),
        duration_ms=_int_or_none(node.get("duration_ms")),
        is_top=is_top,
        is_deleted=bool(node.get("is_deleted")),
        is_private=bool(node.get("is_private")),
        cover_url=cover_url,
        image_count=len(images) if isinstance(images, Sequence) else 0,
        digg_count=_int_or_none(stats.get("digg_count")),
        comment_count=_int_or_none(stats.get("comment_count")),
        share_count=_int_or_none(stats.get("share_count")),
        collect_count=_int_or_none(stats.get("collect_count")),
        play_count=_int_or_none(stats.get("play_count")),
        tags=tuple(str(t) for t in (node.get("tags") or [])),
        author_uid=str(author.get("sec_uid") or author.get("uid") or "") or None,
        author_nickname=str(author.get("nickname") or "") or None,
    )


def parse_page(payload: Mapping[str, Any], *, raw_included: bool, task_id: str | None) -> Page:
    """The `{items, cursor, has_more}` shape, shared by content lists and archive."""
    raw_items = payload.get("items")
    if raw_items is None:
        raise MonitorError(
            "CONTRACT_VIOLATION",
            "载荷里没有 items 字段",
            details={"keys": sorted(payload)[:20]},
        )
    if not isinstance(raw_items, Sequence):
        raise MonitorError(
            "CONTRACT_VIOLATION",
            f"items 不是数组，而是 {type(raw_items).__name__}",
        )

    items: list[Content] = []
    for node in raw_items:
        if not isinstance(node, Mapping):
            continue
        parsed = parse_content(node, raw_included=raw_included and "raw" in node)
        if not parsed.content_id:
            # 一条没有 id 的条目无法跟踪。整页放弃，因为"少了哪几条"说不清楚。
            raise MonitorError(
                "CONTRACT_VIOLATION",
                "items 里有条目缺少 content_id",
                details={"kind": node.get("kind")},
            )
        items.append(parsed)

    cursor = payload.get("cursor")
    return Page(
        items=tuple(items),
        cursor=str(cursor) if cursor else None,
        has_more=bool(payload.get("has_more")),
        raw_included=raw_included,
        task_id=task_id,
    )


def parse_archive_item(node: Mapping[str, Any]) -> ArchiveItem:
    return ArchiveItem(
        content_id=str(node.get("content_id") or ""),
        availability=str(node.get("availability") or "unknown"),
        stored=bool(node.get("stored")),
        first_seen_at=_parse_dt(node.get("first_seen_at")),
        last_seen_at=_parse_dt(node.get("last_seen_at")),
    )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class DtkClient:
    """A thin, read-only client for one DTK v5 instance."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        wait: float = 25.0,
        timeout: float = 35.0,
        refresh: bool = True,
        user_agent: str = "dywatch/0.1",
        poll_interval: float = 2.0,
        max_polls: int = 15,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.wait = float(wait)
        self.refresh = bool(refresh)
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self._timeout = httpx.Timeout(timeout, connect=min(10.0, timeout))
        #: 认证头**每次请求都显式带上**，而不是只挂在默认 client 上：
        #: 注入一个 client（测试、或将来复用连接池）时，默认头不会跟着来，
        #: 而"少了一个头"的表现是 401，读起来像凭据错了，不像少了一行代码。
        self._headers = {
            "X-API-Key": api_key,
            "Accept": "application/json",
            "User-Agent": user_agent,
        }
        self._owns = client is None
        self._client = client or httpx.AsyncClient(
            timeout=self._timeout, headers=self._headers, follow_redirects=False
        )

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def __aenter__(self) -> "DtkClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------ 传输层
    async def _request(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        method: str = "GET",
        json_body: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        attempts: int = 2,
    ) -> tuple[int, dict[str, Any]]:
        """One HTTP call. Returns `(status, envelope)`; raises on transport failure.

        `timeout` / `attempts` 是给写操作留的口子：读可以慢慢等（`wait`→202→轮询本来就
        要等），而旁路上的写请求一慢就是整轮跟着慢。不传 `timeout` 时用客户端默认值
        （`DTK_TIMEOUT`）——注意不能传 `None` 给 httpx，那等于"永远不超时"。
        """
        url = f"{self.base_url}{path}"
        query = {k: v for k, v in (params or {}).items() if v is not None}
        last: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = await self._client.request(
                    method, url, params=query, json=json_body, headers=self._headers,
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
            except httpx.HTTPError as exc:
                last = exc
                if attempt < attempts:
                    await asyncio.sleep(0.5 + random.random() * 0.5)
                    continue
                raise MonitorError(
                    "DTK_UNREACHABLE",
                    f"{type(exc).__name__}: {exc}",
                    details={"url": url},
                ) from exc
            try:
                body = response.json()
            except ValueError:
                raise MonitorError(
                    "DTK_MALFORMED",
                    f"HTTP {response.status_code} 且响应不是 JSON",
                    details={"url": url, "status": response.status_code,
                             "body_head": response.text[:200]},
                ) from None
            if not isinstance(body, dict) or "success" not in body:
                raise MonitorError(
                    "DTK_MALFORMED",
                    f"HTTP {response.status_code} 且响应不是 DTK 信封",
                    details={"url": url, "keys": sorted(body)[:10] if isinstance(body, dict) else None},
                )
            return response.status_code, body
        raise MonitorError("DTK_UNREACHABLE", str(last))  # pragma: no cover

    @staticmethod
    def _raise_for_error(body: Mapping[str, Any]) -> None:
        error = body.get("error") or {}
        code = str(error.get("code") or "INTERNAL")
        raise MonitorError(
            code,
            str(error.get("message") or ""),
            details=error.get("details") or {},
            retry_after=error.get("retry_after"),
        )

    async def _poll_task(self, task_id: str) -> dict[str, Any]:
        """`GET /tasks/{id}` until terminal. Returns the *second* level `data`."""
        for _ in range(self.max_polls):
            await asyncio.sleep(self.poll_interval)
            _status, body = await self._request(f"/api/v1/tasks/{task_id}")
            if not body.get("success"):
                self._raise_for_error(body)
            view = body.get("data") or {}
            state = str(view.get("state") or "")
            if state == "failed":
                err = view.get("error") or {}
                raise MonitorError(
                    str(err.get("code") or "INTERNAL"),
                    str(err.get("message") or "任务失败"),
                    details=err.get("details") or {},
                    retry_after=err.get("retry_after"),
                )
            if state == "done":
                inner = view.get("data")
                if not isinstance(inner, dict):
                    raise MonitorError(
                        "CONTRACT_VIOLATION",
                        "任务已完成但 data.data 不是对象",
                        details={"type": type(inner).__name__},
                    )
                return inner
        raise MonitorError("TASK_TIMEOUT", f"轮询 {self.max_polls} 次仍未完成")

    async def _call(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int, str | None]:
        """One read that follows whichever of the two shapes comes back.

        Returns `(payload, http_status, task_id)`. The payload is the endpoint's own
        object in both cases — the 202 detour is absorbed here so no caller has to
        know the difference.
        """
        query = dict(params or {})
        if self.wait:
            query["wait"] = self.wait
        if self.refresh:
            query["refresh"] = "true"

        status, body = await self._request(path, query)
        if not body.get("success"):
            self._raise_for_error(body)

        data = body.get("data")
        meta = body.get("meta") or {}
        task_id = meta.get("task_id")

        if status == 202:
            if not isinstance(data, Mapping) or not data.get("task_id"):
                raise MonitorError("CONTRACT_VIOLATION", "202 但没有 data.task_id")
            inner = await self._poll_task(str(data["task_id"]))
            return inner, 200, str(data["task_id"])

        if not isinstance(data, Mapping):
            raise MonitorError(
                "CONTRACT_VIOLATION",
                f"200 但 data 是 {type(data).__name__} 而不是对象",
            )
        return data, status, str(task_id) if task_id else None

    # ------------------------------------------------------------ 业务读
    async def author_posts(
        self, sec_user_id: str, count: int, *, include_raw: bool = False
    ) -> Page:
        """★ 主抓手：作者最新一页作品。"""
        payload, _status, task_id = await self._call(
            "/api/v1/douyin/user/posts",
            {
                "sec_user_id": sec_user_id,
                "count": count,
                "include_raw": "true" if include_raw else None,
            },
        )
        return parse_page(payload, raw_included=include_raw, task_id=task_id)

    async def content_detail(self, content_id: str) -> Content:
        """One post's detail. Note: `raw.is_top` here is always 0 — never read it."""
        payload, _status, _task = await self._call(
            "/api/v1/douyin/video", {"aweme_id": content_id}
        )
        return parse_content(payload, raw_included=False)

    async def identify_url(self, url: str) -> dict[str, Any]:
        """Share link → `{platform, resource, resource_id, ...}`. Costs no identity."""
        status, body = await self._request("/api/v1/tools/parse-url", {"url": url})
        if not body.get("success"):
            self._raise_for_error(body)
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise MonitorError("CONTRACT_VIOLATION", "parse-url 返回的 data 不是对象")
        return dict(data)

    async def me(self) -> dict[str, Any]:
        """Who this key is and what it may do. Needs no scope, only a valid key."""
        status, body = await self._request("/api/v1/auth/me")
        if not body.get("success"):
            self._raise_for_error(body)
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise MonitorError("CONTRACT_VIOLATION", "auth/me 返回的 data 不是对象")
        return dict(data)

    async def system_status(self) -> dict[str, Any]:
        """Version, component health, identity pool census. No scope required."""
        status, body = await self._request("/api/v1/system/status")
        if not body.get("success"):
            self._raise_for_error(body)
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise MonitorError("CONTRACT_VIOLATION", "system/status 返回的 data 不是对象")
        return dict(data)

    async def archive_of_author(self, sec_user_id: str, *, limit: int = 50) -> dict[str, ArchiveItem]:
        """Every archived post of one author, keyed by content_id. Zero identity cost."""
        payload, _status, _task = await self._call(
            "/api/v1/archive",
            {"platform": "douyin", "author_uid": sec_user_id, "limit": limit},
        )
        raw_items = payload.get("items")
        if raw_items is None:
            raise MonitorError(
                "CONTRACT_VIOLATION",
                "archive 返回的载荷里没有 items",
                details={"keys": sorted(payload)[:20]},
            )
        out: dict[str, ArchiveItem] = {}
        for node in raw_items or []:
            if isinstance(node, Mapping):
                item = parse_archive_item(node)
                if item.content_id:
                    out[item.content_id] = item
        return out

    async def archive_item(self, content_id: str) -> ArchiveItem | None:
        """One archived post, or None when this instance never saw it."""
        try:
            status, body = await self._request(f"/api/v1/archive/douyin/{content_id}")
        except MonitorError:
            raise
        if not body.get("success"):
            error = body.get("error") or {}
            if str(error.get("code")) == "NOT_FOUND":
                return None
            self._raise_for_error(body)
        data = body.get("data")
        if not isinstance(data, Mapping):
            return None
        return parse_archive_item(data)

    # ------------------------------------------------------------ 归档下载（写操作）
    #
    # 这两个方法刻意不走 `_call`：`_call` 是为"读取内容、可以等、可以走 202 轮询"设计的，
    # 而下载是一次写请求——DTK 接受了就返回，真正的下载在它自己的磁盘上异步进行。
    # 要不要等下载完成、失败了怎么办，是调用方（loop.py 里触发归档的那段）的判断，
    # 客户端只负责把请求送到、把响应读回来，不替调用方做决定。

    async def start_download(
        self, content_id: str, *, skip_existing: bool = True
    ) -> dict[str, Any]:
        """请求 DTK 把一条作品的媒体存到它自己的磁盘上，返回 DTK 的受理结果。

        不轮询到下载完成——202 一收到就返回，`download_id`/`task_id` 留给调用方决定
        要不要跟进。真正的下载在 DTK 那边的磁盘上异步进行，这里等它没有任何意义。

        **pin 刻意不在这里做**：它是第二个写请求，调用方（`ArchiveTrigger`）要让它
        单独过一遍节奏器，也要能单独处理"下载受理了、但 pin 失败"——"以为 pin 上了
        其实没有"是比"没 pin"更危险的状态，不该被混在同一个返回值里悄悄吞掉。

        需要 API Key 带 `media:write` scope——比监控本身用的 `douyin:read`/`archive:read`
        高一级的权限，是否开这个功能应该是使用方主动做的决定（见 ARCHIVE_DOWNLOAD_ENABLED）。
        """
        _status, body = await self._request(
            "/api/v1/downloads",
            method="POST",
            json_body={
                "platform": "douyin",
                "content_id": content_id,
                "skip_existing": skip_existing,
            },
            timeout=WRITE_TIMEOUT,
        )
        if not body.get("success"):
            self._raise_for_error(body)
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise MonitorError("CONTRACT_VIOLATION", "downloads 返回的 data 不是对象")
        return dict(data)

    async def pin_download(self, download_id: str, pinned: bool) -> dict[str, Any]:
        """把一条已发起的下载标记为（不）豁免容量淘汰。同样需要 `media:write`。"""
        _status, body = await self._request(
            f"/api/v1/downloads/{download_id}/pin",
            method="POST",
            json_body={"pinned": pinned},
            timeout=WRITE_TIMEOUT,
        )
        if not body.get("success"):
            self._raise_for_error(body)
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise MonitorError("CONTRACT_VIOLATION", "pin 返回的 data 不是对象")
        return dict(data)

    async def download_storage(self) -> dict[str, Any]:
        """当前媒体存储用量与上限。只读，需要 `media:read`（比 `media:write` 低一级）。

        `doctor` 用它在真正下载之前先告诉你：2G 的默认上限还剩多少、下载器在不在线——
        比等到第一次下载失败才发现存储没配对要有用得多。
        """
        _status, body = await self._request("/api/v1/downloads/storage")
        if not body.get("success"):
            self._raise_for_error(body)
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise MonitorError("CONTRACT_VIOLATION", "downloads/storage 返回的 data 不是对象")
        return dict(data)


def include_raw_for_round(
    mode: str,
    *,
    raw_refresh_round: int,
    refresh_rounds: int,
    have_new_posts: bool,
    title_changed: bool,
) -> bool:
    """`INCLUDE_RAW=auto` 的策略决定：这一轮要不要带 raw。

    带 raw 的代价实测约 ×3 体积（36 KB/条 → 109 KB/条），而置顶变化极罕见，
    所以只在"确实可能有变化"或"隔得够久了"时才带。
    """
    if mode == "always":
        return True
    if mode == "never":
        return False
    if have_new_posts or title_changed:
        return True
    return raw_refresh_round >= refresh_rounds
