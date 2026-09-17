"""`diff` 的行为规格。

这个文件是整个测试套件的重点，因为它守着的规则全部来自真实误报：
挤出预算、分级确认、窗口回移静默恢复、"全部消失"三级确认、never_seen 与 all_gone 的区别、
以及按实测修正过的漏检判据。

写法上有一条纪律：**时间由参数注入**。想造出"连续两轮不在本页"就直接把 `now` 往前推，
不需要 sleep、不需要 monkeypatch 时钟。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dywatch.diff import REASON_CONFIRMED, REASON_SCROLLED_OUT, REASON_TRIMMED, diff
from dywatch.models import (
    AuthorState,
    Content,
    DiffConfig,
    EventKind,
    Kind,
    Page,
    PostState,
    Tombstone,
)

T0 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
CFG = DiffConfig(fetch_count=15)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def post(content_id: str, *, minutes_ago: int = 0, top: bool = False, title: str = "") -> Content:
    return Content(
        content_id=content_id,
        kind=Kind.VIDEO,
        title=title or f"标题{content_id}",
        web_url=f"https://www.douyin.com/video/{content_id}",
        created_at=T0 - timedelta(minutes=minutes_ago),
        is_top=top,
        duration_ms=15_000,
        digg_count=100,
        comment_count=10,
    )


def page(*items: Content, raw_included: bool = False) -> Page:
    return Page(items=tuple(items), cursor="c1", has_more=True, raw_included=raw_included)


def kinds(events) -> list[EventKind]:
    return [event.kind for event in events]


def ids_of(events, kind: EventKind) -> list[str]:
    return [event.content_id for event in events if event.kind is kind]


# ---------------------------------------------------------------- 初始化


def test_first_sighting_records_everything_and_notifies_nothing():
    """首次见到一个账号时，把已有作品全部记下但**一条都不推送**。

    否则给一个新账号上线时，历史作品会一次性刷屏。
    """
    events, state = diff(
        AuthorState(sec_user_id="u1", nickname="A"),
        page=page(post("1"), post("2", minutes_ago=10)),
        now=T0,
        cfg=CFG,
    )

    assert kinds(events) == [EventKind.INITIALIZED]
    assert state.ever_had_posts is True
    assert state.known_ids == {"1", "2"}
    assert state.initialized_at == T0
    assert state.last_new_video_at == T0


def test_new_post_after_init_is_announced_with_gap_days():
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        initialized_at=T0,
        last_update_at=T0,
        last_new_video_at=T0,
        newest_seen_created_at=T0 - timedelta(days=3),
        posts=(PostState(content_id="1", title="标题1", created_at=T0 - timedelta(days=3)),),
    )
    events, state = diff(
        prev,
        page=page(post("1", minutes_ago=4320), post("2")),
        now=at(60),
        cfg=CFG,
    )

    assert kinds(events) == [EventKind.NEW_POST]
    assert ids_of(events, EventKind.NEW_POST) == ["2"]
    # 距上一条作品发布 3 天（按发布时间算，不是按"我们上次看到它的时间"）
    assert events[0].payload["gap_days"] == 3
    assert state.last_new_video_at == at(60)


# ---------------------------------------------------------------- 删除确认


def test_post_absent_once_is_not_a_deletion():
    """第一轮消失只累计计数，不发任何通知。

    注意这个场景里本页**没有新作品**：有新增时窗口整体前移，最旧的那条会被
    "挤出预算"解释掉（见下一个测试），那是另一条规则。
    """
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        last_update_at=T0,
        posts=(
            PostState(content_id="keep", title="留", created_at=T0 - timedelta(days=1)),
            PostState(content_id="gone", title="走", created_at=T0 - timedelta(days=2)),
        ),
    )
    events, state = diff(prev, page=page(post("keep", minutes_ago=1440)), now=at(1), cfg=CFG)

    assert EventKind.POST_REMOVED not in kinds(events)
    assert state.post("gone").absent_rounds == 1


def test_post_absent_twice_is_confirmed_and_tombstoned():
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        posts=(
            PostState(content_id="keep", title="留", created_at=T0 - timedelta(days=1)),
            PostState(
                content_id="gone",
                title="走",
                created_at=T0 - timedelta(days=2),
                absent_rounds=1,
            ),
        ),
    )
    events, state = diff(prev, page=page(post("keep", minutes_ago=1440)), now=at(2), cfg=CFG)

    assert EventKind.POST_REMOVED in kinds(events)
    removed = [e for e in events if e.kind is EventKind.POST_REMOVED][0]
    assert [item["content_id"] for item in removed.payload["removed"]] == ["gone"]
    assert state.post("gone") is None
    assert state.tombstone_ids == {"gone"}
    assert state.tombstones[0].reason == REASON_CONFIRMED


def test_top_post_needs_three_rounds():
    """置顶作品多给一轮：它的位置变化（被别人顶下去/顶上来）比普通作品更容易发生。"""
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        posts=(
            PostState(content_id="keep", title="留", created_at=T0 - timedelta(days=1)),
            PostState(
                content_id="top1",
                title="顶",
                created_at=T0 - timedelta(days=9),
                is_top=True,
                absent_rounds=1,
            ),
        ),
    )
    events, state = diff(prev, page=page(post("keep", minutes_ago=1440)), now=at(2), cfg=CFG)
    assert EventKind.POST_REMOVED not in kinds(events)
    assert state.post("top1").absent_rounds == 2

    events2, state2 = diff(state, page=page(post("keep", minutes_ago=1440)), now=at(3), cfg=CFG)
    assert EventKind.POST_REMOVED in kinds(events2)
    assert state2.post("top1") is None


def test_scrolled_out_budget_absorbs_the_oldest_non_top():
    """作者发了 1 条新作品，最旧的那条被挤出窗口——静默清理，不算删除。

    这正是 `budget = len(new_ids)` 存在的理由：窗口整体前移了多少，就最多有多少条
    能用"挤出"解释，多出来的才可能是真的删了。
    """
    old = PostState(content_id="old", title="旧", created_at=T0 - timedelta(days=30))
    keep = PostState(content_id="keep", title="留", created_at=T0 - timedelta(days=1))
    prev = AuthorState(sec_user_id="u1", ever_had_posts=True, posts=(old, keep))

    events, state = diff(
        prev,
        page=page(post("keep", minutes_ago=60), post("new")),
        now=at(1),
        cfg=CFG,
    )

    assert EventKind.SCROLLED_OUT in kinds(events)
    assert ids_of(events, EventKind.SCROLLED_OUT) == ["old"]
    assert EventKind.POST_REMOVED not in kinds(events)
    assert state.tombstones[0].reason == REASON_SCROLLED_OUT


def test_deletion_is_not_hidden_by_scroll_out_budget():
    """一轮里同时"删旧 + 发新"时，真实删除不能被挤出预算吞掉。

    旧项目专门为这个场景写的注释：预算只够一条，那一条给了最旧的；第二条（同轮也消失）
    要么进确认计数，要么在下一轮被确认——总之不能静默。
    """
    a = PostState(content_id="a", title="A", created_at=T0 - timedelta(days=30))
    b = PostState(content_id="b", title="B", created_at=T0 - timedelta(days=20))
    prev = AuthorState(sec_user_id="u1", ever_had_posts=True, posts=(a, b))

    events, state = diff(prev, page=page(post("new")), now=at(1), cfg=CFG)

    assert ids_of(events, EventKind.SCROLLED_OUT) == ["a"]
    assert state.post("b") is not None
    assert state.post("b").absent_rounds == 1


# ---------------------------------------------------------------- 窗口回移


def test_reappearing_post_is_restored_silently():
    """作者删掉一批较新的作品会导致窗口回移，老作品"重新出现"。

    如果把它当新作品推送，就是一次彻头彻尾的误报——`tombstone` 表就为这一件事存在。
    """
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        last_update_at=T0,
        posts=(PostState(content_id="keep", title="k", created_at=T0 - timedelta(days=1)),),
        tombstones=(Tombstone(content_id="back", removed_at=T0 - timedelta(days=2)),),
    )

    events, state = diff(
        prev,
        page=page(post("keep", minutes_ago=60), post("back", minutes_ago=200)),
        now=at(1),
        cfg=CFG,
    )

    assert EventKind.REVIVED in kinds(events)
    assert EventKind.NEW_POST not in kinds(events)  # 关键：不是新作品
    assert state.post("back") is not None
    assert state.tombstone_ids == set()


# ---------------------------------------------------------------- 全部消失


def test_all_gone_needs_three_rounds_then_warns():
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        posts=(PostState(content_id="1", title="t", created_at=T0 - timedelta(days=1)),),
        all_gone_rounds=2,
    )
    events, state = diff(prev, page=page(), now=at(3), cfg=CFG)

    assert EventKind.ALL_GONE in kinds(events)
    gone = [e for e in events if e.kind is EventKind.ALL_GONE][0]
    assert gone.payload["all_gone"] is True
    assert state.post("1") is None


def test_all_gone_resets_when_a_post_comes_back():
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        posts=(PostState(content_id="1", title="t", created_at=T0 - timedelta(days=1)),),
        all_gone_rounds=2,
    )
    _events, state = diff(prev, page=page(post("1", minutes_ago=60)), now=at(3), cfg=CFG)

    assert state.all_gone_rounds == 0
    assert state.post("1") is not None


# ---------------------------------------------------------------- never_seen


def test_empty_page_for_unknown_author_is_never_seen_not_all_gone():
    """v5 实测：形态合法但不存在的 sec_user_id 返回 200 + 空列表。

    和"作者把作品删光了"完全无法区分，所以只能靠"我们到底有没有见过作品"来分。
    """
    prev = AuthorState(sec_user_id="u1", nickname="A")

    events, state = diff(prev, page=page(), now=at(1), cfg=CFG)
    assert kinds(events) == []
    assert state.empty_rounds == 1
    assert state.ever_had_posts is False

    events2, state2 = diff(state, page=page(), now=at(2), cfg=CFG)
    events3, state3 = diff(state2, page=page(), now=at(3), cfg=CFG)

    assert kinds(events3) == [EventKind.NEVER_SEEN]
    assert state3.never_seen_alerted is True

    # 一次性：再来一轮空空如也，不再重复告警
    events4, _state4 = diff(state3, page=page(), now=at(4), cfg=CFG)
    assert EventKind.NEVER_SEEN not in kinds(events4)


def test_empty_page_for_known_author_is_not_never_seen():
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        posts=(PostState(content_id="1", title="t", created_at=T0 - timedelta(days=1)),),
    )
    events, state = diff(prev, page=page(), now=at(1), cfg=CFG)

    assert EventKind.NEVER_SEEN not in kinds(events)
    assert state.all_gone_rounds == 1


# ---------------------------------------------------------------- 漏检


def test_gap_detected_when_page_skips_a_window():
    """本页最旧的非置顶作品比上轮最新的还新 → 中间必然有没采集到的作品。"""
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        newest_seen_created_at=T0 - timedelta(days=10),
        posts=(PostState(content_id="old", title="o", created_at=T0 - timedelta(days=10)),),
    )
    events, _state = diff(
        prev,
        page=page(post("a", minutes_ago=1), post("b", minutes_ago=5)),
        now=at(1),
        cfg=CFG,
    )
    assert EventKind.GAP_DETECTED in kinds(events)
    gap = [e for e in events if e.kind is EventKind.GAP_DETECTED][0]
    assert gap.payload["fetch_count"] == CFG.fetch_count


def test_gap_not_detected_for_contiguous_page():
    """连续的两页不该报漏检——旧的 `len(new_ids) >= FETCH_COUNT` 判据会在这里误报。"""
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        newest_seen_created_at=T0 - timedelta(minutes=30),
        posts=(PostState(content_id="p", title="p", created_at=T0 - timedelta(minutes=30)),),
    )
    events, _state = diff(
        prev,
        page=page(post("a", minutes_ago=30), post("b", minutes_ago=60)),
        now=at(1),
        cfg=CFG,
    )
    assert EventKind.GAP_DETECTED not in kinds(events)


def test_gap_judgement_ignores_pinned_posts():
    """置顶作品的发布时间是任意的（实测三条置顶分别是 2025-04、2024-01），
    混进时间比较会凭空造出漏检。"""
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        newest_seen_created_at=T0 - timedelta(minutes=30),
        posts=(
            PostState(content_id="p", title="p", created_at=T0 - timedelta(minutes=30)),
            PostState(content_id="t", title="t", created_at=T0 - timedelta(minutes=30), is_top=True),
        ),
    )
    events, _state = diff(
        prev,
        page=page(
            post("t", minutes_ago=999_999, top=True),  # 一条很早的置顶
            post("a", minutes_ago=30),
        ),
        now=at(1),
        cfg=CFG,
    )
    assert EventKind.GAP_DETECTED not in kinds(events)


# ---------------------------------------------------------------- 静默更新


def test_title_change_is_recorded_and_notified():
    """标题变更从"只落库"改成"也推送"（`NOTIFY_KINDS`）。

    它是作者真实做过的动作，而通知里同时给出原文、新文与链接；担心刷屏的部分交给
    `alerts.TRIGGERS` 的窗口（同一账号 1 小时一次），不是靠不推送。
    """
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        posts=(PostState(content_id="1", title="旧标题", created_at=T0 - timedelta(days=1)),),
    )
    events, state = diff(
        prev,
        page=page(post("1", minutes_ago=1440, title="新标题")),
        now=at(1),
        cfg=CFG,
    )

    changed = [e for e in events if e.kind is EventKind.TITLE_CHANGED]
    assert len(changed) == 1
    assert changed[0].should_notify
    assert changed[0].payload["old"] == "旧标题" and changed[0].payload["new"] == "新标题"
    assert state.post("1").title == "新标题"


def test_is_top_is_preserved_when_raw_was_not_requested():
    """没带 raw 的轮次 `is_top` 恒为 false，绝不能因此把已知的置顶状态抹掉。

    这是本设计里最容易写错的一处：错误在这行会让一个置顶作品在下一轮"变成非置顶"，
    于是它的删除确认阈值从 3 掉到 2。
    """
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        posts=(PostState(content_id="t", title="t", is_top=True, created_at=T0 - timedelta(days=1)),),
    )
    # 不带 raw：item.is_top 为 False（因为 raw 里没有 is_top 可读）→ 不更新
    _events, state = diff(prev, page=page(post("t", minutes_ago=1440)), now=at(1), cfg=CFG)
    assert state.post("t").is_top is True

    # 带了 raw 且说它不是置顶：这次要信，把库里的值改过来
    _events2, state2 = diff(
        state,
        page=page(post("t", minutes_ago=1440), raw_included=True),
        now=at(2),
        cfg=CFG,
    )
    assert state2.post("t").is_top is False

    # 再回到不带 raw 的轮次：降级后的值要被保留，不能"又变回置顶"
    _events3, state3 = diff(state2, page=page(post("t", minutes_ago=1440)), now=at(3), cfg=CFG)
    assert state3.post("t").is_top is False


def test_raw_refresh_counter_advances_only_without_raw():
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        raw_refresh_round=5,
        posts=(PostState(content_id="1", title="t", created_at=T0 - timedelta(days=1)),),
    )
    _e1, s1 = diff(prev, page=page(post("1", minutes_ago=1440)), now=at(1), cfg=CFG)
    assert s1.raw_refresh_round == 6

    _e2, s2 = diff(
        prev, page=page(post("1", minutes_ago=1440), raw_included=True), now=at(1), cfg=CFG
    )
    assert s2.raw_refresh_round == 0


# ---------------------------------------------------------------- 失败


def test_failure_does_not_advance_the_state_machine():
    """一次抓取失败不能把"疑似删除"的计数推一格。"""
    from dywatch.dtk import MonitorError

    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        posts=(
            PostState(content_id="1", title="t", created_at=T0 - timedelta(days=1), absent_rounds=1),
        ),
    )
    events, state = diff(
        prev, error=MonitorError("UPSTREAM_RISK_CONTROL", "风控"), now=at(1), cfg=CFG
    )

    assert state.post("1").absent_rounds == 1  # 没动
    assert state.consecutive_fails == 1
    assert EventKind.POST_REMOVED not in kinds(events)


def test_fail_alert_fires_at_threshold_then_respects_cooldown():
    from dywatch.dtk import MonitorError

    state = AuthorState(sec_user_id="u1", nickname="A")
    events = []
    for offset in range(1, 6):
        batch, state = diff(
            state, error=MonitorError("UPSTREAM_RISK_CONTROL", "x"), now=at(offset), cfg=CFG
        )
        events.extend(batch)

    assert kinds(events).count(EventKind.ACCOUNT_FAILED) == 1
    assert state.fail_alerted is True

    # 冷却期内不再重复
    batch, state = diff(state, error=MonitorError("UPSTREAM_RISK_CONTROL", "x"), now=at(6), cfg=CFG)
    assert EventKind.ACCOUNT_FAILED not in kinds(batch)


def test_recovery_after_alert_is_announced_once():
    from dywatch.dtk import MonitorError

    state = AuthorState(sec_user_id="u1", nickname="A")
    for offset in range(1, 6):
        _batch, state = diff(
            state, error=MonitorError("UPSTREAM_RISK_CONTROL", "x"), now=at(offset), cfg=CFG
        )
    assert state.fail_alerted is True

    events, state2 = diff(state, page=page(post("1")), now=at(7), cfg=CFG)
    assert EventKind.ACCOUNT_RECOVERED in kinds(events)
    assert state2.fail_alerted is False
    assert state2.consecutive_fails == 0


# ---------------------------------------------------------------- 裁剪与兜底


def test_trimming_prefers_the_oldest_non_top():
    """已知列表超过上限时，先淘汰最旧的非置顶——置顶是作者的选择，不该被我们裁掉。"""
    cfg = DiffConfig(fetch_count=15, known_ids_max=3)
    prev = AuthorState(
        sec_user_id="u1",
        ever_had_posts=True,
        posts=(
            PostState(content_id="t", title="顶", is_top=True, created_at=T0 - timedelta(days=900)),
            PostState(content_id="old", title="旧", created_at=T0 - timedelta(days=300)),
            PostState(content_id="mid", title="中", created_at=T0 - timedelta(days=100)),
        ),
    )
    events, state = diff(
        prev,
        page=page(
            post("t", minutes_ago=999_999, top=True),
            post("old", minutes_ago=432_000),
            post("mid", minutes_ago=144_000),
            post("n1", minutes_ago=5),
            post("n2", minutes_ago=3),
        ),
        now=at(1),
        cfg=cfg,
    )

    assert EventKind.TRIMMED in kinds(events)
    assert ids_of(events, EventKind.TRIMMED) == ["old", "mid"]
    assert state.post("t") is not None  # 置顶不动
    assert state.post("n1") is not None
    assert len(state.posts) == cfg.known_ids_max
    trimmed = [t for t in state.tombstones if t.reason == REASON_TRIMMED]
    assert sorted(t.content_id for t in trimmed) == ["mid", "old"]


def test_stale_reminder_is_one_shot_and_resets_on_new_post():
    prev = AuthorState(
        sec_user_id="u1",
        nickname="A",
        ever_had_posts=True,
        initialized_at=T0 - timedelta(days=20),
        last_update_at=T0 - timedelta(days=20),
        posts=(PostState(content_id="1", title="t", created_at=T0 - timedelta(days=20)),),
    )
    events, state = diff(prev, page=page(post("1", minutes_ago=28800)), now=T0, cfg=CFG)
    assert EventKind.STALE_NO_UPDATE in kinds(events)
    assert state.stale_alerted is True

    events2, _state2 = diff(state, page=page(post("1", minutes_ago=28800)), now=at(60), cfg=CFG)
    assert EventKind.STALE_NO_UPDATE not in kinds(events2)

    events3, state3 = diff(state, page=page(post("1", minutes_ago=28800), post("2")), now=at(120), cfg=CFG)
    assert state3.stale_alerted is False
    assert EventKind.NEW_POST in kinds(events3)


# ---------------------------------------------------------------- 归档交叉确认


def test_archive_verdict_only_ever_accelerates_never_blocks():
    """归档的两种结论方向不对称，这是有意的。

    * `availability=deleted` 是 DTK 自己核对过"这条作品没了"的**正向**证据 → 少等一轮。
    * `availability=live` 只是"我们上次看它时它还在"，而归档的可用性由 DTK 的 recheck
      更新（默认 6 小时一批、只查 7 天没查过的），所以它可能只是**陈旧**——
      拿它去否决一个刚刚观察到的消失，会把真实删除压成"再等等"，而那个等没有终点。

    两个分支用同一份输入，只有归档结论不同。
    """
    from dywatch.models import ArchiveItem

    def run(verdict: str):
        prev = AuthorState(
            sec_user_id="u1",
            ever_had_posts=True,
            posts=(
                PostState(content_id="keep", title="留", created_at=T0 - timedelta(days=1)),
                PostState(content_id="gone", title="走", created_at=T0 - timedelta(days=2)),
            ),
        )
        return diff(
            prev,
            page=page(post("keep", minutes_ago=1440, title="留")),
            now=at(1),
            cfg=CFG,
            archive={"gone": ArchiveItem(content_id="gone", availability=verdict)},
        )

    events_live, state_live = run("live")
    assert EventKind.POST_REMOVED not in kinds(events_live)
    assert state_live.post("gone").absent_rounds == 1  # 正常走两轮确认

    events_deleted, state_deleted = run("deleted")
    assert EventKind.POST_REMOVED in kinds(events_deleted)  # 第一轮就够
    assert state_deleted.post("gone") is None


# ---------------------------------------------------------------- 不变量


@pytest.mark.parametrize("rounds", [1, 2, 3, 5])
def test_tombstones_never_grow_without_bound(rounds: int):
    cfg = DiffConfig(fetch_count=15, removed_max=5, removed_ttl_days=7)
    state = AuthorState(sec_user_id="u1", ever_had_posts=True)
    for index in range(rounds * 3):
        _events, state = diff(
            state,
            page=page(post(f"p{index}")),
            now=at(index * 10),
            cfg=cfg,
        )
        # 下一轮把这一条也拿掉，制造 tombstone
        _events2, state = diff(state, page=page(), now=at(index * 10 + 1), cfg=cfg)
    assert len(state.tombstones) <= 5
