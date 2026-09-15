"""DTK 客户端：信封、错误码归一化、`wait`→202→任务轮询、载荷解析。

全部用 `httpx.MockTransport` 喂录制的响应，所以不打网络、不需要凭据，
但走的是真实的请求构造与解析路径。
"""

from __future__ import annotations

import httpx
import pytest

from dywatch.dtk import (
    DtkClient,
    MonitorError,
    include_raw_for_round,
    parse_content,
    parse_page,
)

BASE = "http://dtk.test"
KEY = "dtk_38c817704d0f_A5CMPbL1a7TowGb9LAfXYNAvYK6bjIwn"


def envelope(data, *, success=True, error=None, meta=None) -> dict:
    return {"success": success, "data": data, "error": error, "meta": meta or {"request_id": "r1"}}


def content_node(content_id="7496063824002403638", *, top=False, raw=False) -> dict:
    node = {
        "platform": "douyin",
        "content_id": content_id,
        "kind": "video",
        "web_url": f"https://www.douyin.com/video/{content_id}",
        "title": "有时候自己也乱套 #惊鸿一面",
        "description": "有时候自己也乱套 #惊鸿一面",
        "created_at": "2025-04-22T09:16:29Z",
        "duration_ms": 38634,
        "is_deleted": False,
        "is_private": False,
        "author": {
            "platform": "douyin",
            "uid": "MS4wLjABAAAA4MjT",
            "sec_uid": "MS4wLjABAAAA4MjT",
            "nickname": "示例账号",
            "avatar": {"url": "https://a/avatar.jpeg", "urls": [], "width": 720, "height": 720},
            "verified": False,
        },
        "stats": {
            "digg_count": 463098,
            "play_count": None,  # 抖音实测恒为 null
            "share_count": 274103,
            "collect_count": 42964,
            "comment_count": 16949,
        },
        "media": {
            "covers": [{"url": "https://p3/cover.jpeg", "urls": [], "width": 1080, "height": 1440}],
            "video": {"url": "https://v/video.mp4", "urls": [], "watermark": False},
            "images": [],
            "streams": [],
        },
        "music": {"title": "惊鸿一面", "author": "许嵩", "music_id": "1"},
        "tags": ["惊鸿一面"],
        "location": None,
        "fetched_at": "2026-09-15T07:42:49.335603Z",
    }
    if raw:
        node["raw"] = {"aweme_id": content_id, "is_top": 1 if top else 0, "desc": "raw 里的描述"}
    return node


def make_client(handler, **kwargs) -> DtkClient:
    transport = httpx.MockTransport(handler)
    return DtkClient(
        BASE,
        KEY,
        wait=0,
        client=httpx.AsyncClient(transport=transport),
        poll_interval=0,
        **kwargs,
    )


# --------------------------------------------------------------------------- 解析


def test_parse_page_reads_the_single_level_shape():
    page = parse_page(
        {"items": [content_node()], "cursor": "1779365942000", "has_more": True},
        raw_included=False,
        task_id="t1",
    )
    assert len(page.items) == 1
    assert page.cursor == "1779365942000"
    assert page.has_more is True
    item = page.items[0]
    assert item.content_id == "7496063824002403638"
    assert isinstance(item.content_id, str)
    assert item.play_count is None, "缺值必须是 None，不能是 0"
    assert item.is_top is False
    assert item.cover_url == "https://p3/cover.jpeg"
    assert item.author_uid == "MS4wLjABAAAA4MjT"
    assert item.created_at is not None and item.created_at.year == 2025


def test_parse_page_reads_is_top_only_when_raw_was_requested():
    payload = {"items": [content_node(top=True, raw=True)], "cursor": None, "has_more": False}

    with_raw = parse_page(payload, raw_included=True, task_id=None)
    assert with_raw.items[0].is_top is True

    without_raw = parse_page(payload, raw_included=False, task_id=None)
    assert without_raw.items[0].is_top is False, "没带 raw 时不能假装知道置顶状态"


def test_parse_page_refuses_a_payload_it_cannot_track():
    with pytest.raises(MonitorError) as missing_items:
        parse_page({"cursor": None}, raw_included=False, task_id=None)
    assert missing_items.value.code == "CONTRACT_VIOLATION"

    with pytest.raises(MonitorError) as no_id:
        parse_page({"items": [{"kind": "video"}], "cursor": None, "has_more": False},
                   raw_included=False, task_id=None)
    assert no_id.value.code == "CONTRACT_VIOLATION"


def test_non_top_helper_excludes_pinned_posts():
    payload = {
        "items": [content_node("a", top=True, raw=True), content_node("b", raw=True)],
        "cursor": None,
        "has_more": False,
    }
    page = parse_page(payload, raw_included=True, task_id=None)
    assert [item.content_id for item in page.non_top()] == ["b"]


def test_parse_content_on_a_detail_payload_without_raw():
    item = parse_content(content_node(), raw_included=False)
    assert item.is_top is False
    assert item.image_count == 0


# --------------------------------------------------------------------------- 请求


async def test_author_posts_sends_the_contract_it_was_designed_around():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=envelope(
            {"items": [content_node()], "cursor": "c", "has_more": False},
            meta={"task_id": "task-1"},
        ))

    async with make_client(handler) as client:
        page = await client.author_posts("MS4wLjABAAAA", 15, include_raw=False)

    request = seen[0]
    assert request.url.path == "/api/v1/douyin/user/posts"
    query = dict(request.url.params)
    assert query["sec_user_id"] == "MS4wLjABAAAA"
    assert query["count"] == "15"
    assert query.get("include_raw") is None
    assert request.headers["x-api-key"] == KEY
    assert page.task_id == "task-1"


async def test_refresh_true_is_always_sent():
    """不带 refresh 就会命中 DTK 的 300 秒列表缓存，监控粒度会静默变成 5 分钟。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=envelope(
            {"items": [], "cursor": None, "has_more": False}))

    async with make_client(handler) as client:
        await client.author_posts("u", 5)

    assert dict(seen[0].url.params)["refresh"] == "true"


async def test_include_raw_true_is_passed_through():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=envelope(
            {"items": [content_node(top=True, raw=True)], "cursor": None, "has_more": False}))

    async with make_client(handler) as client:
        page = await client.author_posts("u", 5, include_raw=True)

    assert dict(seen[0].url.params)["include_raw"] == "true"
    assert page.items[0].is_top is True


async def test_202_then_task_poll_lands_on_the_second_level_data():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/v1/douyin/user/posts":
            return httpx.Response(202, json=envelope(
                {"task_id": "task-9", "state": "queued"}))
        return httpx.Response(200, json=envelope({
            "task_id": "task-9",
            "state": "done",
            "endpoint": "douyin.author_posts",
            "data": {"items": [content_node()], "cursor": None, "has_more": False},
            "result_meta": {"cached": False},
        }))

    async with make_client(handler) as client:
        page = await client.author_posts("u", 5)

    assert calls == ["/api/v1/douyin/user/posts", "/api/v1/tasks/task-9"]
    assert len(page.items) == 1


async def test_task_failure_is_normalized_to_its_error_code():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/v1/tasks/"):
            return httpx.Response(200, json=envelope({
                "task_id": "t",
                "state": "failed",
                "error": {"code": "IDENTITY_POOL_EXHAUSTED", "message": "no identity",
                          "retry_after": 30},
            }))
        return httpx.Response(202, json=envelope({"task_id": "t", "state": "queued"}))

    async with make_client(handler) as client:
        with pytest.raises(MonitorError) as caught:
            await client.author_posts("u", 5)

    assert caught.value.code == "IDENTITY_POOL_EXHAUSTED"
    assert caught.value.retry_after == 30
    assert caught.value.is_gate is True


async def test_terminal_never_reached_becomes_task_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/v1/tasks/"):
            return httpx.Response(200, json=envelope({"task_id": "t", "state": "running"}))
        return httpx.Response(202, json=envelope({"task_id": "t", "state": "queued"}))

    transport = httpx.MockTransport(handler)
    client = DtkClient(
        BASE, KEY, wait=0, client=httpx.AsyncClient(transport=transport),
        poll_interval=0, max_polls=3,
    )
    async with client:
        with pytest.raises(MonitorError) as caught:
            await client.author_posts("u", 5)
    assert caught.value.code == "TASK_TIMEOUT"


# --------------------------------------------------------------------------- 失败


async def test_error_envelope_is_adopted_verbatim():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=envelope(
            None, success=False,
            error={"code": "RATE_LIMITED", "message": "slow down", "retry_after": 12},
        ))

    async with make_client(handler) as client:
        with pytest.raises(MonitorError) as caught:
            await client.author_posts("u", 5)

    error = caught.value
    assert error.code == "RATE_LIMITED"
    assert error.retry_after == 12
    assert error.is_gate is True
    assert error.is_config is False


async def test_401_is_classified_as_a_configuration_problem():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=envelope(
            None, success=False,
            error={"code": "UNAUTHENTICATED", "message": "Authentication failed."},
        ))

    async with make_client(handler) as client:
        with pytest.raises(MonitorError) as caught:
            await client.me()

    assert caught.value.is_config is True, "Key 不对不该被当成网络抖动反复重试"


async def test_a_non_envelope_response_is_malformed_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    async with make_client(handler) as client:
        with pytest.raises(MonitorError) as caught:
            await client.author_posts("u", 5)

    assert caught.value.code == "DTK_MALFORMED"
    assert "body_head" in caught.value.details


async def test_transport_failure_becomes_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    async with make_client(handler) as client:
        with pytest.raises(MonitorError) as caught:
            await client.author_posts("u", 5)

    assert caught.value.code == "DTK_UNREACHABLE"


async def test_success_but_wrong_payload_shape_is_a_contract_violation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(["not", "an", "object"]))

    async with make_client(handler) as client:
        with pytest.raises(MonitorError) as caught:
            await client.author_posts("u", 5)

    assert caught.value.code == "CONTRACT_VIOLATION"


# --------------------------------------------------------------------------- 其他读


async def test_identify_url_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/tools/parse-url"
        return httpx.Response(200, json=envelope({
            "allowed": True, "platform": "douyin", "resource": "user",
            "resource_id": "MS4wLjABAAAA4MjT", "handle": None, "content_kind": None,
            "url": "https://www.douyin.com/user/MS4wLjABAAAA4MjT", "needs_expansion": False,
        }))

    async with make_client(handler) as client:
        info = await client.identify_url("https://www.douyin.com/user/MS4wLjABAAAA4MjT")
    assert info["resource_id"] == "MS4wLjABAAAA4MjT"


async def test_archive_of_author_is_keyed_by_content_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params)["author_uid"] == "u1"
        return httpx.Response(200, json=envelope({
            "items": [
                {"content_id": "a", "availability": "live", "stored": False,
                 "first_seen_at": "2026-09-01T00:00:00Z", "last_seen_at": "2026-09-02T00:00:00Z"},
                {"content_id": "b", "availability": "deleted", "stored": True},
            ],
            "cursor": "c", "has_more": False,
        }))

    async with make_client(handler) as client:
        archive = await client.archive_of_author("u1")
    assert set(archive) == {"a", "b"}
    assert archive["b"].availability == "deleted"
    assert archive["b"].stored is True


async def test_archive_never_seen_returns_none_rather_than_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=envelope(
            None, success=False, error={"code": "NOT_FOUND", "message": "no such post"}))

    async with make_client(handler) as client:
        assert await client.archive_item("nope") is None


# --------------------------------------------------------------------------- 策略


def test_include_raw_policy():
    assert include_raw_for_round("always", raw_refresh_round=0, refresh_rounds=20,
                                 have_new_posts=False, title_changed=False) is True
    assert include_raw_for_round("never", raw_refresh_round=99, refresh_rounds=20,
                                 have_new_posts=True, title_changed=True) is False
    # auto：有新作品时带
    assert include_raw_for_round("auto", raw_refresh_round=0, refresh_rounds=20,
                                 have_new_posts=True, title_changed=False) is True
    # auto：隔得够久了也带
    assert include_raw_for_round("auto", raw_refresh_round=20, refresh_rounds=20,
                                 have_new_posts=False, title_changed=False) is True
    # auto：其它情况不带
    assert include_raw_for_round("auto", raw_refresh_round=3, refresh_rounds=20,
                                 have_new_posts=False, title_changed=False) is False
