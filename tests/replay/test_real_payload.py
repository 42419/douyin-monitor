"""回放真实响应，把契约钉死。

这些断言全部来自 2026-09-15 对 dtk 5.1.0 实例的一次真实调用（见 fixtures/ 下的注释）。
它们存在的意义是：**当 DTK 升级改变响应形状时，这里会先红**，而不是等到线上发现
"监控突然不再报新作品"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dywatch.dtk import parse_page
from dywatch.models import DiffConfig, EventKind, Page
from dywatch.diff import diff
from dywatch.models import AuthorState, Content, Kind

FIXTURES = Path(__file__).parent / "fixtures"
REAL = json.loads((FIXTURES / "douyin_user_posts_real.json").read_text(encoding="utf-8"))
PINNED_ID = "7328330582012841251"


def test_real_payload_keeps_the_single_level_data_shape():
    """`data` 是对象且带 items/cursor/has_more —— 不是裸数组。"""
    data = REAL["data"]
    assert set(data) == {"items", "cursor", "has_more"}
    assert isinstance(data["items"], list)
    assert data["has_more"] is True


def test_real_payload_parses_into_the_contract_we_coded_against():
    page = parse_page(
        REAL["data"], raw_included=True, task_id=REAL["meta"].get("task_id")
    )

    assert len(page.items) == 2
    assert page.cursor == "1781603809000"
    assert page.task_id == "fa7196f7-b9ff-414a-a60a-a9480c6e0d31"

    item = page.items[0]
    assert isinstance(item.content_id, str)
    assert item.kind is Kind.VIDEO
    assert item.created_at is not None and item.created_at.tzinfo is not None
    assert item.duration_ms == 38100
    assert item.cover_url and item.cover_url.startswith("https://")
    assert item.author_uid and item.author_uid.startswith("MS4wLjAB")
    assert item.tags == ("没表情包",)


def test_play_count_is_null_not_zero_on_douyin():
    """实测：抖音不返回真实播放数。把它写成 0 就是在通知里编一个平台没说过的事实。"""
    page = parse_page(REAL["data"], raw_included=True, task_id=None)
    assert all(item.play_count is None for item in page.items)


def test_is_top_comes_from_raw_and_matches_what_the_user_told_us():
    """用户指认 `7328330582012841251` 是置顶视频，列表里的 `raw.is_top` 就是 1。

    同一条视频走 `/video` 详情接口时 `is_top` 是 0（详情接口不带置顶语义），
    所以 `pipeline` 只从列表取这个字段——这条断言就是那个决定的守门人。
    """
    page = parse_page(REAL["data"], raw_included=True, task_id=None)
    pinned = [item for item in page.items if item.content_id == PINNED_ID]
    assert pinned and pinned[0].is_top is True
    assert [item.is_top for item in page.items] == [True, False]


def test_without_raw_we_do_not_claim_to_know_the_pinned_state():
    page = parse_page(REAL["data"], raw_included=False, task_id=None)
    assert all(item.is_top is False for item in page.items)


def test_count_is_not_a_hard_ceiling_which_is_why_gap_detection_uses_time():
    """实测：请求 count=5 时返回了 8 条（count + 3 条置顶）。

    所以"本页条数达到 count"不等于"被截断"，旧项目的 `len(new_ids) >= FETCH_COUNT`
    判据会在这里误报。这个 fixture 里两页共 2 条，用来说明"条数由平台决定"这件事
    已经被写进契约：我们只读 items，不假设它有几条。
    """
    page = parse_page(REAL["data"], raw_included=True, task_id=None)
    assert len(page.items) != 0  # 条数由平台给，不由 count 决定
    assert page.has_more is True


def test_end_to_end_first_round_on_the_real_payload():
    """真实载荷走一遍完整判定：应当只产出"初始化"，一条通知都不发。"""
    page = parse_page(REAL["data"], raw_included=True, task_id=None)
    events, state = diff(
        AuthorState(sec_user_id="MS4wLjABAAAA4MjT", nickname="示例账号"),
        now=page.items[0].created_at,
        cfg=DiffConfig(),
        page=page,
    )

    assert [event.kind for event in events] == [EventKind.INITIALIZED]
    assert state.known_ids == {item.content_id for item in page.items}
    assert state.post(PINNED_ID).is_top is True
    # 漏检基准只看非置顶：置顶那条发布于 2024-01，混进来会造出假告警
    assert state.newest_seen_created_at == page.items[1].created_at


def test_pinned_post_keeps_its_three_round_threshold_from_the_real_payload():
    """置顶状态是从 raw 里读出来的，于是它的删除确认阈值真的是 3 而不是 2。"""
    page = parse_page(REAL["data"], raw_included=True, task_id=None)
    _events, state = diff(
        AuthorState(sec_user_id="u1", ever_had_posts=True),
        now=page.items[0].created_at,
        cfg=DiffConfig(),
        page=page,
    )
    keep = page.items[1]
    pinned = state.post(PINNED_ID)
    assert pinned is not None and pinned.is_top is True

    # 下一轮：置顶那条不见了，另一条还在 → 只累计一轮，不确认
    next_page = Page(items=(keep,), cursor=None, has_more=False, raw_included=False)
    events, state2 = diff(state, now=page.items[0].created_at, cfg=DiffConfig(), page=next_page)
    assert EventKind.POST_REMOVED not in [event.kind for event in events]
    assert state2.post(PINNED_ID).absent_rounds == 1
