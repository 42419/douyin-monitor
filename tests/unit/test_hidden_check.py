"""隐藏作品核验（`HIDDEN_POST_CHECK_ENABLED`）：见 DESIGN.md「隐藏作品核验」一节。

这个文件锁的是几条关键不变量：

1. **不是逐轮轮询** —— 只在账号初始化 / 有作品被确认消失 / 长期无更新兜底触发
   这三种情况下才会调用 `/api/v1/douyin/user`，其余轮次（哪怕有新作品）不产生
   任何额外请求
2. **数字对得上就不定向核验** —— `content_count` 的变化如果能用"这轮的新增/删除"
   完全解释，就不会去消耗登录身份的预算
3. **数字对不上才定向核验，且核验结果正确分类** —— 从没见过的 id 归 `new_post`，
   tombstone 里的 id 归 `revived`（并打上 `source=hidden_check`）
4. **`revived`（核验来源）绕开 6 小时抑制窗口**，跟 `new_post` 一样立刻发
5. **核验层的失败不影响主判定** —— `author_profile` / 定向请求任何一个失败，
   这一轮已经算好的结果原样返回，不抛异常
6. **两个额外请求都过节奏器**，不会绕开限速突然发出去
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from dywatch.alerts import Deduplicator, should_send
from dywatch.dtk import MonitorError, parse_author_profile
from dywatch.models import (
    AuthorProfile,
    AuthorState,
    Content,
    DiffConfig,
    Event,
    EventKind,
    Page,
    PostState,
    REVIVED_VIA_HIDDEN_CHECK,
    Tombstone,
)
from dywatch.pipeline import HiddenCheckConfig, run_author
from dywatch.scheduler import GlobalGate
from dywatch.state import StateStore


class Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _record(event: str, **fields: Any) -> None:
            self.records.append((level, event, fields))

        return _record

    def events(self, name: str) -> list[dict]:
        return [fields for _level, event, fields in self.records if event == name]


class Pacer:
    """只数次数，不真的等——节奏本身由 test_pacer.py 负责。"""

    def __init__(self) -> None:
        self.calls = 0

    async def wait_for_turn(self) -> float:
        self.calls += 1
        return 0.0


class Notifier:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> Any:
        self.sent.append(message)

        class _Delivery:
            def as_dict(self) -> dict:
                return {"sent": 1, "failed": 0}

        return _Delivery()


class Client:
    """同时扮演"抓列表"与"查发布总数"两个角色（走池子，不定向）。"""

    def __init__(
        self,
        *,
        posts_items: tuple[Content, ...] = (),
        content_count: int | None = None,
        profile_error: Exception | None = None,
    ) -> None:
        self.posts_items = posts_items
        self.content_count = content_count
        self.profile_error = profile_error
        self.profile_calls = 0

    async def author_posts(self, sec_user_id: str, count: int, **kwargs: Any) -> Page:
        assert "identity" not in kwargs or kwargs["identity"] is None, (
            "主链路的 author_posts 不该带 identity —— 那是定向核验专属"
        )
        return Page(items=self.posts_items)

    async def author_profile(self, sec_user_id: str) -> AuthorProfile:
        self.profile_calls += 1
        if self.profile_error is not None:
            raise self.profile_error
        return AuthorProfile(content_count=self.content_count)


class PinClient:
    """定向核验专用 client——只实现 author_posts，且要求带上 identity。"""

    def __init__(
        self,
        *,
        posts_items: tuple[Content, ...] = (),
        error: Exception | None = None,
        expected_identity: str = "identity-abc",
    ) -> None:
        self.posts_items = posts_items
        self.error = error
        self.expected_identity = expected_identity
        self.calls: list[str | None] = []

    async def author_posts(self, sec_user_id: str, count: int, **kwargs: Any) -> Page:
        identity = kwargs.get("identity")
        self.calls.append(identity)
        assert identity == self.expected_identity, "定向核验必须带上配置好的身份 uuid"
        if self.error is not None:
            raise self.error
        return Page(items=self.posts_items)


def make_content(content_id: str, *, created_at: datetime | None = None, title: str = "作品") -> Content:
    return Content(
        content_id=content_id,
        title=title,
        created_at=created_at or datetime.now(timezone.utc),
    )


async def _run(
    *,
    tmp_path: Any,
    author: AuthorState,
    client: Client,
    hidden_check: HiddenCheckConfig | None,
    now: datetime | None = None,
    cfg: DiffConfig | None = None,
) -> tuple[Any, StateStore, Notifier, Pacer]:
    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    store.ensure_author(author.sec_user_id, author.nickname, now or datetime.now(timezone.utc))
    notifier = Notifier()
    pacer = Pacer()
    gate = GlobalGate(default_seconds=60, backoff_after=2, backoff_max=600)
    result = await run_author(
        author=author,
        nickname=author.nickname,
        client=client,
        store=store,
        notifier=notifier,
        dedup=Deduplicator(),
        pacer=pacer,
        gate=gate,
        cfg=cfg or DiffConfig(),
        now=now or datetime.now(timezone.utc),
        archive_enabled=False,
        hidden_check=hidden_check,
        logger=Logger(),
    )
    return result, store, notifier, pacer


# --------------------------------------------------------------------- 解析
def test_parse_author_profile_reads_content_count():
    profile = parse_author_profile({"stats": {"content_count": 42, "follower_count": 999}})
    assert profile.content_count == 42


def test_parse_author_profile_missing_field_is_none_not_zero():
    """缺值和"确实是 0 条"是两件不同的事，不能悄悄当成 0。"""
    assert parse_author_profile({"stats": {}}).content_count is None
    assert parse_author_profile({}).content_count is None


# --------------------------------------------------------------------- 触发范围
async def test_plain_round_with_only_new_posts_refreshes_baseline_but_does_not_verify(tmp_path):
    """纯新作品的一轮：游客视角已经看到了，不需要定向核验，但要把 content_count
    的基准值跟着刷新（P0-2 修复：不然跨轮之间的新增会在下次触发时被漏算，
    导致 expected 公式失真）。"""
    now = datetime.now(timezone.utc)
    author = AuthorState(
        sec_user_id="u1", nickname="示例", ever_had_posts=True, runs=5,
        initialized_at=now - timedelta(days=10),
        baseline_content_count=3, baseline_content_count_at=now - timedelta(hours=1),
        posts=(PostState(content_id="old1", created_at=now - timedelta(days=1)),),
    )
    client = Client(posts_items=(
        make_content("new1", created_at=now),
        make_content("old1", created_at=now - timedelta(days=1)),
    ), content_count=4)  # 3（基准） + 1（这条新作品）= 4，跟预期对得上
    pin = PinClient()
    hidden_check = HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin)

    result, store, _notifier, _pacer = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=hidden_check, now=now
    )

    assert result.new_count == 1
    assert client.profile_calls == 1, "纯新作品轮也要刷新基准值"
    assert pin.calls == [], "对得上，不该触发定向核验"
    assert store.load_authors()["u1"].baseline_content_count == 4


async def test_initialization_round_establishes_baseline_without_verifying(tmp_path):
    now = datetime.now(timezone.utc)
    author = AuthorState(sec_user_id="u1", nickname="示例")  # 全新账号，ever_had_posts=False
    client = Client(
        posts_items=(make_content("p1", created_at=now), make_content("p2", created_at=now)),
        content_count=2,
    )
    pin = PinClient()
    hidden_check = HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin)

    result, store, _notifier, _pacer = await _run(tmp_path=tmp_path, author=author, client=client, hidden_check=hidden_check, now=now)

    assert result.status == "init"
    assert client.profile_calls == 1
    assert pin.calls == [], "第一次记录不该有基准可比，不触发定向核验"
    state = store.load_authors()["u1"]
    assert state.baseline_content_count == 2


# --------------------------------------------------------------------- 消失确认时的比对
def _author_with_one_post_about_to_be_confirmed_removed(now: datetime) -> AuthorState:
    """构造一个"这一轮再缺席一次就确认删除"的账号——`absent_rounds` 差一轮到阈值。

    刻意留两条已知帖子（`staying` + `gone1`）：只有一条已知帖子时，这一轮返回空
    列表会被 diff() 判成 `all_gone`（全部作品同时消失），走的是另一套按账号累计
    的确认轮数，不是这里要测的逐条 `absent_rounds` 路径。
    """
    cfg = DiffConfig()
    return AuthorState(
        sec_user_id="u1", nickname="示例", ever_had_posts=True, runs=10,
        initialized_at=now - timedelta(days=30),
        last_update_at=now - timedelta(days=1),
        last_seen_at=now - timedelta(minutes=1),
        baseline_content_count=5, baseline_content_count_at=now - timedelta(hours=2),
        posts=(
            PostState(content_id="staying", created_at=now - timedelta(days=2)),
            PostState(
                content_id="gone1", created_at=now - timedelta(days=5),
                absent_rounds=cfg.delete_rounds - 1, absent_is_top=False,
            ),
        ),
    )


#: 这一轮页面里"照常还在"的那条已知帖子——留着它，好让缺席的 gone1 走非 all_gone 路径
def _staying_post(now: datetime) -> Content:
    return make_content("staying", created_at=now - timedelta(days=2))


async def test_numbers_match_after_deletion_does_not_trigger_verification(tmp_path):
    """基准值 5，这轮确认删了 1 条，预期变成 4；DTK 也说是 4——对得上，不用核验。"""
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), content_count=4)  # gone1 缺席 -> 确认删除
    pin = PinClient()
    hidden_check = HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin)

    result, store, _notifier, _pacer = await _run(tmp_path=tmp_path, author=author, client=client, hidden_check=hidden_check, now=now)

    assert result.deleted_count == 1
    assert client.profile_calls == 1
    assert pin.calls == [], "数字对得上，不该消耗登录身份的预算"
    assert store.load_authors()["u1"].baseline_content_count == 4


async def test_mismatch_triggers_verification_and_finds_the_hidden_post(tmp_path):
    """预期是 4（5 - 1），DTK 却说 5——多出来的 1 条只能是核验才能找到的隐藏作品。"""
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), content_count=5)  # 对不上：预期 4，实际 5
    hidden = make_content("hidden1", created_at=now, title="被隐藏的新作品")
    pin = PinClient(posts_items=(hidden,))
    hidden_check = HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin)

    result, store, notifier, pacer = await _run(
        tmp_path=tmp_path,
        author=author, client=client, hidden_check=hidden_check, now=now
    )

    assert pin.calls == ["identity-abc"], "数字对不上才应该定向核验"
    assert any(e.kind is EventKind.NEW_POST and e.content_id == "hidden1" for e in result.events)
    # 找到的这条新作品要正常推送出去，不能被核验这条特殊路径漏掉
    assert len(notifier.sent) >= 1
    state = store.load_authors()["u1"]
    assert state.baseline_content_count == 5
    assert any(p.content_id == "hidden1" for p in state.posts)
    # 主判定的 pacer 调用（抓列表一次）之外，核验层至少还应该再占两次（查总数 + 定向核验）
    assert pacer.calls >= 3


async def test_reappeared_tombstoned_post_is_tagged_revived_not_new_post(tmp_path):
    """核验翻出来的 id 如果曾经被判定删除过（在 tombstone 里），该归 revived，不是 new_post。"""
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    author = author.with_updates(
        tombstones=(Tombstone(content_id="tomb1", removed_at=now - timedelta(days=1)),)
    )
    client = Client(posts_items=(_staying_post(now),), content_count=5)
    reappeared = make_content("tomb1", created_at=now - timedelta(days=3), title="其实一直都在")
    pin = PinClient(posts_items=(reappeared,))
    hidden_check = HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin)

    result, store, notifier, _pacer = await _run(
        tmp_path=tmp_path,
        author=author, client=client, hidden_check=hidden_check, now=now
    )

    revived = [e for e in result.events if e.kind is EventKind.REVIVED and e.content_id == "tomb1"]
    assert len(revived) == 1
    assert revived[0].payload.get("source") == REVIVED_VIA_HIDDEN_CHECK
    # 这类 revived 不该被 6 小时抑制窗口拦住——should_send 应该直接放行
    allowed, _key = should_send(revived[0], Deduplicator())
    assert allowed is True
    assert len(notifier.sent) >= 1
    state = store.load_authors()["u1"]
    assert all(t.content_id != "tomb1" for t in state.tombstones), "回归了就不该再是 tombstone"


async def test_mismatch_with_nothing_new_in_verification_page_is_not_an_error(tmp_path):
    """数字对不上，但定向核验拿到的页面里也没有本地不认识的 id——不当成异常，只是记录一下。"""
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), content_count=5)  # 对不上
    pin = PinClient(posts_items=())  # 核验页面也是空的——可能是别的置顶项被删了
    hidden_check = HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin)

    result, store, _notifier, _pacer = await _run(
        tmp_path=tmp_path,
        author=author, client=client, hidden_check=hidden_check, now=now
    )

    assert result.status == "ok"  # 不抛异常，主判定结果原样保留
    assert store.load_authors()["u1"].baseline_content_count == 5  # 基准值照样刷新成实际值


# --------------------------------------------------------------------- 失败容忍
async def test_profile_fetch_failure_does_not_break_the_round(tmp_path):
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), profile_error=MonitorError("DTK_UNREACHABLE", "boom"))
    pin = PinClient()
    hidden_check = HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin)

    result, store, _notifier, _pacer = await _run(
        tmp_path=tmp_path,
        author=author, client=client, hidden_check=hidden_check, now=now
    )

    assert result.status == "ok"
    assert result.deleted_count == 1, "核验层挂了，主判定（确认删除）不受影响"
    assert pin.calls == [], "拿不到实际值，无从比较，不该盲目触发定向核验"
    # 基准值没能刷新（这次没拿到实际值），下次触发时用的还是旧基准
    assert store.load_authors()["u1"].baseline_content_count == 5


async def test_pinned_verification_failure_does_not_break_the_round(tmp_path):
    """核验请求失败时：主判定结果不受影响，但**基准值不该推进**——这是 P0-3
    修复的核心行为。用 actual(6) 明显不同于旧基准值(5)，才能真正区分出
    "推进成了 6"（旧的错误行为）和"保留在 5"（修复后的正确行为），不然两个数字
    刚好撞在一起，断言测不出差别。
    """
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    # expected = 5(基准) + 0(新增) - 1(gone1 被确认删除) = 4；actual=6，缺口是 2
    client = Client(posts_items=(_staying_post(now),), content_count=6)
    pin = PinClient(error=MonitorError("IDENTITY_POOL_EXHAUSTED", "no identity"))
    hidden_check = HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin)

    result, store, _notifier, _pacer = await _run(
        tmp_path=tmp_path,
        author=author, client=client, hidden_check=hidden_check, now=now
    )

    assert result.status == "ok"
    assert pin.calls == ["identity-abc"]
    state = store.load_authors()["u1"]
    # 核验没成功、缺口没被解释，基准值必须保留旧值——不能悄悄推进到 6，
    # 不然这次没捞回来的差异就永久丢了，下次也不会再比对出同一个缺口
    assert state.baseline_content_count == 5
    assert state.content_count_drift_rounds == 1


# --------------------------------------------------------------------- 不开启就完全不碰
async def test_feature_disabled_never_touches_profile_endpoint(tmp_path):
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), content_count=999)  # 就算对不上，关着也不该管

    result, _store, _notifier, _pacer = await _run(
        tmp_path=tmp_path,
        author=author, client=client, hidden_check=None, now=now
    )

    assert result.deleted_count == 1
    assert client.profile_calls == 0


# --------------------------------------------------------------------- P0-2：跨轮不失真
async def test_baseline_stays_accurate_across_a_new_post_round_then_a_removal_round(tmp_path):
    """P0-2 回归测试：如果"纯新作品轮"不刷新基准值，第二轮算 expected 时会用
    过时的旧基准值，把一次正常的确认删除误判成"数字对不上"，白白触发一次
    定向核验。这里手动串两轮，验证不会发生这种误触发。
    """
    now = datetime.now(timezone.utc)
    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    store.ensure_author("u1", "示例", now)
    notifier = Notifier()
    pacer = Pacer()
    gate = GlobalGate(default_seconds=60, backoff_after=2, backoff_max=600)
    cfg = DiffConfig()

    # 第一轮：账号本来就有 staying，这轮多发一条 new1，游客视角正常看到 —— 基准值应该
    # 从 3 刷新成 4
    author = AuthorState(
        sec_user_id="u1", nickname="示例", ever_had_posts=True, runs=10,
        initialized_at=now - timedelta(days=30),
        last_update_at=now - timedelta(days=1),
        last_seen_at=now - timedelta(minutes=10),
        baseline_content_count=3, baseline_content_count_at=now - timedelta(hours=2),
        posts=(PostState(content_id="staying", created_at=now - timedelta(days=2)),),
    )
    client1 = Client(
        posts_items=(
            make_content("staying", created_at=now - timedelta(days=2)),
            make_content("new1", created_at=now - timedelta(minutes=5)),
        ),
        content_count=4,
    )
    pin1 = PinClient()
    result1 = await run_author(
        author=author, nickname="示例", client=client1, store=store, notifier=notifier,
        dedup=Deduplicator(), pacer=pacer, gate=gate, cfg=cfg,
        now=now - timedelta(minutes=5), archive_enabled=False,
        hidden_check=HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin1),
        logger=Logger(),
    )
    assert result1.new_count == 1
    assert pin1.calls == [], "第一轮对得上，不该核验"
    mid_state = store.load_authors()["u1"]
    assert mid_state.baseline_content_count == 4, "基准值必须跟着新作品轮刷新"

    # 第二轮：staying 缺席够轮数，正常确认删除一条；new1 照常还在（不然空列表会被
    # diff() 判成 all_gone，走的是另一套账号级累计确认，不是这里要测的逐条路径）。
    # 若基准值真的刷新成了 4，expected = 4 + 0 - 1 = 3，actual 也是 3，
    # 天衣无缝，不该触发核验。
    mid_state = mid_state.with_updates(
        posts=tuple(
            p.with_updates(absent_rounds=cfg.delete_rounds - 1) if p.content_id == "staying" else p
            for p in mid_state.posts
        )
    )
    client2 = Client(
        posts_items=(make_content("new1", created_at=now - timedelta(minutes=5)),),
        content_count=3,
    )
    pin2 = PinClient()
    result2 = await run_author(
        author=mid_state, nickname="示例", client=client2, store=store, notifier=notifier,
        dedup=Deduplicator(), pacer=pacer, gate=gate, cfg=cfg,
        now=now, archive_enabled=False,
        hidden_check=HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin2),
        logger=Logger(),
    )
    assert result2.deleted_count == 1
    assert pin2.calls == [], "用刷新后的基准值算，数字对得上，不该误触发定向核验"
    assert store.load_authors()["u1"].baseline_content_count == 3


# --------------------------------------------------------------------- 未解决缺口的上限
async def test_unresolved_drift_gives_up_after_max_rounds(tmp_path):
    """连续多轮都解释不了同一个缺口——不能无限期重试，达到上限后放弃追踪、
    直接接受当下的实际值，drift 计数器归零。
    """
    from dywatch.pipeline import MAX_UNRESOLVED_DRIFT_ROUNDS

    now = datetime.now(timezone.utc)
    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    store.ensure_author("u1", "示例", now)
    notifier = Notifier()
    pacer = Pacer()
    gate = GlobalGate(default_seconds=60, backoff_after=2, backoff_max=600)
    cfg = DiffConfig()

    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    hidden_check = HiddenCheckConfig(
        pinned_identity="identity-abc", pin_client=PinClient(posts_items=())
    )
    # 触发一轮 removed 确认后，接下来连续多轮都是"没有新变化"的普通轮次——drift 只在
    # 触发轮里累积，所以直接连续跑同样的"removed 确认"场景，模拟"问题一直没解决"
    for round_no in range(1, MAX_UNRESOLVED_DRIFT_ROUNDS + 1):
        state = store.load_authors().get("u1", author)
        if round_no == 1:
            state = author  # 第一轮用带 gone1 的初始状态
        else:
            # 后续几轮：重新造一个"又有一条新的缺席帖子"的状态，让 removed_count>0
            # 继续触发比对（用不同 id 避免跟上一轮的 tombstone 冲突）
            state = state.with_updates(
                posts=state.posts + (
                    PostState(
                        content_id=f"gone{round_no}", created_at=now - timedelta(days=1),
                        absent_rounds=cfg.delete_rounds - 1,
                    ),
                )
            )
        client = Client(posts_items=(_staying_post(now),), content_count=99)  # 永远解释不了的缺口
        result = await run_author(
            author=state, nickname="示例", client=client, store=store, notifier=notifier,
            dedup=Deduplicator(), pacer=pacer, gate=gate, cfg=cfg,
            now=now, archive_enabled=False, hidden_check=hidden_check, logger=Logger(),
        )
        assert result.status == "ok"

    final = store.load_authors()["u1"]
    assert final.content_count_drift_rounds == 0, "到上限后应该清零，不是继续累积"
    assert final.baseline_content_count == 99, "放弃追踪后要接受当下的实际值，不能悬空"


# --------------------------------------------------------------------- P0-5：同轮矛盾撤销
async def test_recovered_post_cancels_the_contradicting_removal_in_the_same_round(tmp_path):
    """同一轮内：gone1 被确认删除（产生 POST_REMOVED），核验又在同一轮发现 gone1
    其实还在（产生 REVIVED）——不该同批发出"作品消失"+"其实还在"两条自相矛盾的
    通知。gone1 是这一批消失里唯一的一条，所以 POST_REMOVED 应该被整条撤销。
    """
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)  # 有 staying + gone1(缺席)
    client = Client(posts_items=(_staying_post(now),), content_count=5)  # 触发核验
    # 定向核验拿到的页面里，gone1 其实还在（游客隐藏了它，不是真删除）
    recovered = make_content("gone1", created_at=now - timedelta(days=5), title="其实还在")
    pin = PinClient(posts_items=(_staying_post(now), recovered))
    hidden_check = HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin)

    result, store, notifier, _pacer = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=hidden_check, now=now
    )

    kinds_with_gone1 = [
        (e.kind, e.payload) for e in result.events
        if e.kind is EventKind.POST_REMOVED
        and any(item.get("content_id") == "gone1" for item in (e.payload.get("removed") or []))
    ]
    assert kinds_with_gone1 == [], "gone1 不该再出现在任何 POST_REMOVED 的 removed 列表里"
    revived = [e for e in result.events if e.kind is EventKind.REVIVED and e.content_id == "gone1"]
    assert len(revived) == 1
    # 通知里不该同时出现"消失"和"回归"两条自相矛盾的消息
    subjects = [getattr(m, "subject", str(m)) for m in notifier.sent]
    assert not any("消失" in s for s in subjects), f"不该有矛盾的消失通知：{subjects}"
    state = store.load_authors()["u1"]
    assert any(p.content_id == "gone1" for p in state.posts)
    assert all(t.content_id != "gone1" for t in state.tombstones)


async def test_recovered_post_is_only_stripped_from_removed_list_when_others_remain(tmp_path):
    """如果这一批消失里除了核验找回的那条，还有别的确实删除的，POST_REMOVED
    事件本身不该被整条撤销——只摘掉被推翻的那一条，其余照常报。
    """
    now = datetime.now(timezone.utc)
    cfg = DiffConfig()
    author = AuthorState(
        sec_user_id="u1", nickname="示例", ever_had_posts=True, runs=10,
        initialized_at=now - timedelta(days=30),
        last_update_at=now - timedelta(days=1),
        last_seen_at=now - timedelta(minutes=1),
        baseline_content_count=6, baseline_content_count_at=now - timedelta(hours=2),
        posts=(
            PostState(content_id="staying", created_at=now - timedelta(days=2)),
            PostState(
                content_id="gone1", created_at=now - timedelta(days=5),
                absent_rounds=cfg.delete_rounds - 1,
            ),
            PostState(
                content_id="really_gone", created_at=now - timedelta(days=6),
                absent_rounds=cfg.delete_rounds - 1,
            ),
        ),
    )
    # expected = 6 + 0 - 2(gone1 + really_gone 都被确认删除) = 4；actual=5，缺口是 1
    client = Client(posts_items=(_staying_post(now),), content_count=5)
    recovered = make_content("gone1", created_at=now - timedelta(days=5), title="其实还在")
    pin = PinClient(posts_items=(_staying_post(now), recovered))
    hidden_check = HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin)

    result, _store, _notifier, _pacer = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=hidden_check, now=now
    )

    removed_events = [e for e in result.events if e.kind is EventKind.POST_REMOVED]
    assert len(removed_events) == 1, "really_gone 是真删除，POST_REMOVED 不该被整条撤销"
    removed_ids = {item["content_id"] for item in removed_events[0].payload["removed"]}
    assert removed_ids == {"really_gone"}, "gone1 应该被摘掉，really_gone 照常保留"


# --------------------------------------------------------------------- P1-4：有界抑制窗口
def test_hidden_check_revived_is_rate_limited_not_unconditional():
    """核验来源的 revived 不该完全绕开抑制——同一账号短时间内第二次应该被压住，
    不然一旦游客隐藏问题持续存在（确认删除 -> 核验找回反复发生），会变成通知风暴。
    """
    dedup = Deduplicator()
    event = Event(
        EventKind.REVIVED, sec_user_id="u1", nickname="示例", content_id="p1",
        payload={"title": "t", "source": REVIVED_VIA_HIDDEN_CHECK},
    )
    allowed1, key1 = should_send(event, dedup)
    assert allowed1 is True
    allowed2, _key2 = should_send(event, dedup)  # 紧接着同账号又来一条
    assert allowed2 is False, "窗口内应该被压住，不能完全不设限"
    assert key1 != ""
