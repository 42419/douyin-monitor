"""隐藏作品核验（`HIDDEN_POST_CHECK_ENABLED`）：见 DESIGN.md §4.10。

这个文件锁的是几条关键不变量：

1. **不是逐轮轮询** —— 只在账号初始化 / 本轮有新作品 / 有作品被确认消失 / 长期无更新
   兜底触发这四种情况下才会调用 `/api/v1/douyin/user`
2. **数字对得上就不定向核验** —— 差量能被"这轮的新增/删除"完全解释，就不消耗登录身份
3. **对不上才定向核验，且按方向分流**：
   - 总数比预期**多** ⇒ 有本地完全不知道的作品：它是**对访客不可见**的，
     标记 `hidden_from_guest` 并发 `HIDDEN_FROM_GUEST`（不是 new_post / revived）
   - 总数比预期**少** ⇒ 有作品真没了：用登录视角确认哪些不在了，报 `POST_REMOVED`
4. **那个标记会阻止 `diff` 把它的缺席当成可疑信号** —— 这是停住"访客说没了、登录说还在"
   死循环的关键（上线后真实发生过：同一条作品每 1~3 分钟"消失 → 核验 → 回来"）
5. **核验层的失败不影响主判定** —— `author_profile` / 定向请求任何一个失败，这一轮已经
   算好的结果原样返回，不抛异常
6. **两个额外请求都过节奏器**，不会绕开限速突然发出去
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from dywatch.alerts import TRIGGERS, Deduplicator, should_send
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


def _store(tmp_path: Any, sec_user_id: str = "u1") -> StateStore:
    """建一个已迁移、且已有账号行的库（`run_author` 依赖它）。"""
    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    store.ensure_author(sec_user_id, "示例", datetime.now(timezone.utc))
    return store


def _hidden_check(pin: PinClient) -> HiddenCheckConfig:
    return HiddenCheckConfig(pinned_identity="identity-abc", pin_client=pin)


async def _run(
    *,
    tmp_path: Any,
    author: AuthorState,
    client: Client,
    hidden_check: HiddenCheckConfig | None,
    now: datetime | None = None,
    cfg: DiffConfig | None = None,
    store: StateStore | None = None,
) -> tuple[Any, StateStore, Notifier, Pacer]:
    """跑一轮。`store` 可以传进来复用，用于"连续几轮"的用例。"""
    store = store or StateStore(tmp_path / "db.sqlite")
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
    """缺值和"确实是 0 条"是两件事：缺值必须是 None，不能当 0 参与比对。"""
    assert parse_author_profile({}).content_count is None
    assert parse_author_profile({"stats": {}}).content_count is None
    assert parse_author_profile({"stats": {"content_count": 0}}).content_count == 0


# --------------------------------------------------------------------- 构造
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


def _author_with_a_hidden_post(now: datetime, *, hidden_since: datetime | None = None) -> AuthorState:
    """两条已知帖子：一条访客能看到的 `visible1`，一条**已标记为对访客不可见**的 `hidden1`。"""
    return AuthorState(
        sec_user_id="u1", nickname="示例", ever_had_posts=True, runs=10,
        initialized_at=now - timedelta(days=30),
        last_update_at=now - timedelta(minutes=2),
        last_seen_at=now - timedelta(minutes=1),
        baseline_content_count=2, baseline_content_count_at=now - timedelta(hours=2),
        posts=(
            PostState(content_id="visible1", created_at=now - timedelta(days=2)),
            PostState(
                content_id="hidden1", created_at=now - timedelta(days=1),
                hidden_from_guest_at=hidden_since or now - timedelta(hours=1),
            ),
        ),
    )


# ------------------------------------------------------------------ 触发时机
async def test_initialization_round_establishes_baseline_without_verifying(tmp_path):
    """账号第一次被记录：只立基准值，不做比较、不核验。"""
    now = datetime.now(timezone.utc)
    author = AuthorState(sec_user_id="u1", nickname="示例")
    client = Client(posts_items=(make_content("p1", created_at=now),), content_count=3)
    pin = PinClient()

    _result, store, _notifier, _pacer = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    assert client.profile_calls == 1
    assert pin.calls == [], "初始化那轮没有可比较的基准值，不该定向核验"
    assert store.load_authors()["u1"].baseline_content_count == 3


async def test_plain_round_with_only_new_posts_refreshes_baseline_but_does_not_verify(tmp_path):
    """纯新作品的一轮：刷新基准值（否则基准会漂移），但不定向核验。"""
    now = datetime.now(timezone.utc)
    author = AuthorState(
        sec_user_id="u1", nickname="示例", ever_had_posts=True,
        baseline_content_count=1, baseline_content_count_at=now - timedelta(hours=1),
        posts=(PostState(content_id="old1", created_at=now - timedelta(days=1)),),
    )
    client = Client(
        posts_items=(make_content("new1", created_at=now), make_content("old1", created_at=now - timedelta(days=1))),
        content_count=2,
    )
    pin = PinClient()

    result, store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    assert any(e.kind is EventKind.NEW_POST for e in result.events)
    assert pin.calls == [], "这一轮的新增能被数字解释，不需要核验"
    assert store.load_authors()["u1"].baseline_content_count == 2


async def test_numbers_match_after_deletion_does_not_trigger_verification(tmp_path):
    """预期 4（5 − 1）、实际也是 4：账目对得上，不核验。"""
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), content_count=4)
    pin = PinClient()

    result, _store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    assert any(e.kind is EventKind.POST_REMOVED for e in result.events)
    assert pin.calls == []


async def test_feature_disabled_never_touches_profile_endpoint(tmp_path):
    """`HIDDEN_POST_CHECK_ENABLED=false`（`hidden_check=None`）：一次都不碰总数接口。"""
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), content_count=99)

    _result, _store, _n, pacer = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=None, now=now
    )

    assert client.profile_calls == 0, "关掉功能就不该碰总数接口"
    assert pacer.calls == 1, "只有主链路抓列表那一次（核验层不该多占）"


# --------------------------------------------------- 总数偏多 ⇒ 标记"对访客不可见"
async def test_mismatch_marks_the_post_hidden_from_guest(tmp_path):
    """预期 4（5 − 1），DTK 说 5——多出来的 1 条只能是核验才能看到的隐藏作品。"""
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), content_count=5)
    hidden = make_content("hidden1", created_at=now, title="被隐藏的新作品")
    pin = PinClient(posts_items=(hidden,))

    result, store, notifier, pacer = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    assert pin.calls == ["identity-abc"], "数字对不上才应该定向核验"
    kinds = [e.kind for e in result.events]
    assert EventKind.HIDDEN_FROM_GUEST in kinds
    assert EventKind.NEW_POST not in kinds, "被隐藏的作品不该报成新作品——用户该做的事不一样"
    assert EventKind.REVIVED not in kinds
    event = next(e for e in result.events if e.kind is EventKind.HIDDEN_FROM_GUEST)
    assert [item["content_id"] for item in event.payload["hidden"]] == ["hidden1"]
    # 这个事件**不推送**（`SILENT_KINDS`）：平台对访客的展示限制不是账号故障。上面那句
    # `notifier.sent >= 1` 曾经是靠同轮的 `post_removed` 蒙对的，注释则说成"这条要推送出去"
    assert notifier.sent, "本轮应该有别的推送（确认消失那条）"
    assert all(m.event is not EventKind.HIDDEN_FROM_GUEST for m in notifier.sent), \
        "静默事件不该出现在投递记录里"

    state = store.load_authors()["u1"]
    assert state.baseline_content_count == 5
    marked = next(p for p in state.posts if p.content_id == "hidden1")
    assert marked.hidden_from_guest_at is not None, "要打上标记，否则下一轮又会判它消失"
    # 主判定抓列表 + 查总数 + 定向核验
    assert pacer.calls >= 3


async def test_hidden_post_cancels_the_contradicting_removal_in_the_same_round(tmp_path):
    """同一轮里 `diff` 刚确认 `gone1` 消失、核验又证明它只是对访客不可见：
    那条"作品消失"必须被撤销，否则会同时推出两条互相打脸的通知。"""
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), content_count=5)
    pin = PinClient(posts_items=(_staying_post(now), make_content("gone1", created_at=now - timedelta(days=5))))

    result, store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    assert EventKind.POST_REMOVED not in [e.kind for e in result.events]
    assert any(e.kind is EventKind.HIDDEN_FROM_GUEST for e in result.events)
    state = store.load_authors()["u1"]
    assert any(p.content_id == "gone1" and p.hidden_from_guest_at is not None for p in state.posts)
    assert all(t.content_id != "gone1" for t in state.tombstones), "它不是消失了，不该留墓碑"


async def test_removal_kept_when_other_posts_in_the_same_event_are_real(tmp_path):
    """同一批"消失"里只有一部分被核验推翻时：只摘掉那一条，其余原样保留。"""
    now = datetime.now(timezone.utc)
    cfg = DiffConfig()
    author = _author_with_one_post_about_to_be_confirmed_removed(now).with_updates(
        posts=(
            PostState(content_id="staying", created_at=now - timedelta(days=2)),
            PostState(content_id="hidden1", created_at=now - timedelta(days=3),
                      absent_rounds=cfg.delete_rounds - 1),
            PostState(content_id="gone2", created_at=now - timedelta(days=6),
                      absent_rounds=cfg.delete_rounds - 1),
        )
    )
    client = Client(posts_items=(_staying_post(now),), content_count=5)
    pin = PinClient(posts_items=(_staying_post(now),
                                 make_content("hidden1", created_at=now - timedelta(days=3))))

    result, _store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    removed_events = [e for e in result.events if e.kind is EventKind.POST_REMOVED]
    assert len(removed_events) == 1
    ids = [item["content_id"] for item in removed_events[0].payload["removed"]]
    assert ids == ["gone2"], "只有没被核验推翻的那条才算真的消失"


async def test_mismatch_with_nothing_new_in_verification_page_is_not_an_error(tmp_path):
    """数字对不上、核验页里也没有本地不认识的 id：只记一条日志，不当异常、不吃掉基准值。"""
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), content_count=5)
    pin = PinClient(posts_items=())  # 核验页面也是空的

    result, store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    assert not any(e.kind is EventKind.HIDDEN_FROM_GUEST for e in result.events)
    state = store.load_authors()["u1"]
    assert state.baseline_content_count == 5, "缺口没解释清楚，基准值不该被推进"
    assert state.content_count_drift_rounds == 1


# ------------------------------------- 标记之后：循环必须停（上线故障的回归测试）
async def test_hidden_post_stops_accumulating_absences_across_rounds(tmp_path):
    """**核心回归**：标记为"对访客不可见"之后，访客列表继续看不到它也不该再有任何动静。

    上线后真实发生过的死循环：访客视角说"没了" → 判确认删除 → 核验说"还在" →
    填回来 → 访客视角又说"没了"……每 1~3 分钟一条「作品回归」，每小时烧掉约 40 次
    登录态请求。这条用例锁住"标记生效 ⇒ 缺席不再累计 ⇒ 不再核验"。
    """
    now = datetime.now(timezone.utc)
    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    # 基准值刚核对过（低频保底不该在这几轮里插进来）——这里要测的是"标记生效后
    # 一轮接一轮都安静"，不是保底本身
    author = _author_with_a_hidden_post(now).with_updates(
        baseline_content_count_at=now - timedelta(minutes=1)
    )
    store.ensure_author("u1", "示例", now)
    client = Client(posts_items=(make_content("visible1", created_at=now - timedelta(days=2)),), content_count=2)
    pin = PinClient(posts_items=())

    for round_no in range(3):
        # 第 1 轮用构造好的状态（库里那行是 `ensure_author` 刚建的空壳），之后从库里读
        state = author if round_no == 0 else store.load_authors()["u1"]
        result, store, _n, _p = await _run(
            tmp_path=tmp_path, author=state, client=client, hidden_check=_hidden_check(pin),
            now=now + timedelta(minutes=round_no), store=store,
        )
        assert not any(e.kind in (EventKind.POST_REMOVED, EventKind.ALL_GONE) for e in result.events), (
            f"第 {round_no + 1} 轮又把它判成消失了 —— 死循环回来了"
        )
        assert not any(e.kind is EventKind.HIDDEN_FROM_GUEST for e in result.events), "只该标记一次"

    assert client.profile_calls == 0, "没有新作品也没有消失，不该去查总数"
    assert pin.calls == [], "更不该动用登录身份"
    state = store.load_authors()["u1"]
    hidden = next(p for p in state.posts if p.content_id == "hidden1")
    assert hidden.absent_rounds == 0, "已知对访客不可见的作品不该累计缺席轮数"
    assert all(t.content_id != "hidden1" for t in state.tombstones)


async def test_hidden_post_reappearing_clears_the_marker_silently(tmp_path):
    """它重新出现在访客列表里 ⇒ 清掉标记；刻意不发事件（我们从没报过它消失）。"""
    now = datetime.now(timezone.utc)
    author = _author_with_a_hidden_post(now)
    client = Client(
        posts_items=(make_content("visible1", created_at=now - timedelta(days=2)),
                     make_content("hidden1", created_at=now - timedelta(days=1))),
        content_count=2,
    )

    result, store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=None, now=now
    )

    state = store.load_authors()["u1"]
    assert all(p.hidden_from_guest_at is None for p in state.posts if p.content_id == "hidden1")
    assert not any(e.kind in (EventKind.REVIVED, EventKind.POST_REMOVED) for e in result.events)


# ------------------------------------------------- 总数偏少 ⇒ 有一条真被删了
async def test_total_drop_reports_the_hidden_post_as_removed(tmp_path):
    """基准 2、实际 1：少的那条正是"对访客不可见"的——它的消失不会让访客列表少任何东西，
    只有用登录视角核对才能发现，而且必须报出来。"""
    now = datetime.now(timezone.utc)
    author = _author_with_a_hidden_post(now)
    client = Client(posts_items=(make_content("visible1", created_at=now - timedelta(days=2)),), content_count=1)
    # 登录视角里只剩 visible1：hidden1 确实被作者删了
    pin = PinClient(posts_items=(make_content("visible1", created_at=now - timedelta(days=2)),))

    result, store, notifier, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    assert pin.calls == ["identity-abc"]
    removed = [e for e in result.events if e.kind is EventKind.POST_REMOVED]
    assert len(removed) == 1
    assert [item["content_id"] for item in removed[0].payload["removed"]] == ["hidden1"]
    assert len(notifier.sent) >= 1
    state = store.load_authors()["u1"]
    assert all(p.content_id != "hidden1" for p in state.posts)
    assert any(t.content_id == "hidden1" and t.reason == "confirmed" for t in state.tombstones)
    assert state.baseline_content_count == 1


async def test_total_drop_keeps_the_hidden_post_when_login_still_sees_it(tmp_path):
    """总数少了，但那条隐藏作品在登录视角里还在 ⇒ 少的是别的（本工具看不到的），不动它。"""
    now = datetime.now(timezone.utc)
    author = _author_with_a_hidden_post(now)
    client = Client(posts_items=(make_content("visible1", created_at=now - timedelta(days=2)),), content_count=1)
    pin = PinClient(posts_items=(
        make_content("visible1", created_at=now - timedelta(days=2)),
        make_content("hidden1", created_at=now - timedelta(days=1)),
    ))

    result, _store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    assert not any(e.kind is EventKind.POST_REMOVED for e in result.events)


async def test_total_drop_ignores_hidden_post_outside_the_login_window(tmp_path):
    """隐藏作品比登录页最旧那条还旧 ⇒ 它可能只是没出现在这一页，不能据此判删除。"""
    now = datetime.now(timezone.utc)
    author = _author_with_a_hidden_post(now).with_updates(
        posts=(
            PostState(content_id="visible1", created_at=now - timedelta(days=1)),
            PostState(content_id="hidden1", created_at=now - timedelta(days=400),
                      hidden_from_guest_at=now - timedelta(days=390)),
        )
    )
    client = Client(posts_items=(make_content("visible1", created_at=now - timedelta(days=1)),), content_count=1)
    pin = PinClient(posts_items=(make_content("visible1", created_at=now - timedelta(days=1)),))

    result, _store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    assert not any(e.kind is EventKind.POST_REMOVED for e in result.events), (
        "窗口外的作品与已删除在单页响应里无法区分，不能判删除"
    )


# ----------------------------------------------------------------- 降级
async def test_profile_fetch_failure_does_not_break_the_round(tmp_path):
    """查总数失败：这一轮已经算好的结果原样返回，不抛异常、不浪费一次定向核验。

    基准值按**本地已知的增量**推进（这轮确认删了 1 条 → 5 折成 4）：删除已经确认过了，
    没有理由因为"总数读不到"就把这 1 条再从账上撤回来——撤回来只会让下一次触发时
    出现一个根本没发生的缺口，白烧一轮定向核验。
    """
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(
        posts_items=(_staying_post(now),),
        profile_error=MonitorError("upstream_unreachable", "boom"),
    )
    pin = PinClient()

    result, store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    assert any(e.kind is EventKind.POST_REMOVED for e in result.events), "主判定不受影响"
    assert pin.calls == [], "总数都读不到，不该再花一次定向核验"
    assert store.load_authors()["u1"].baseline_content_count == 4


async def test_profile_fetch_failure_backs_off_instead_of_polling_every_round(tmp_path):
    """读总数失败后必须退避：否则低频保底每轮都判"该核对了"，退化成逐轮轮询上游。

    低频保底靠 `baseline_content_count_at` 计时。失败时若不把它推到现在，每一轮都会
    再次满足"超过 interval 没核对过"，于是每轮都去敲一次总数接口——正是这套机制
    声称要避免的事（`HIDDEN_CHECK_INTERVAL_MINUTES` 的意义就没了）。
    """
    now = datetime.now(timezone.utc)
    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    # 基准值是 2 小时前核对的 → 第一次调用时就该触发保底
    author = _author_with_a_hidden_post(now).with_updates(
        baseline_content_count_at=now - timedelta(hours=2)
    )
    store.ensure_author("u1", "示例", now)
    client = Client(
        posts_items=(_staying_post(now),),
        profile_error=MonitorError("upstream_unreachable", "boom"),
    )
    # 关掉逐条缺席确认：这几轮里 `visible1` 一直在缺席，默认阈值（2 轮）会让它在这一轮
    # 被确认删除，那条事件自己就会触发核验，测不出"保底有没有退避"
    cfg = DiffConfig(delete_rounds=99)

    _r1, store, _n, _p = await _run(
        tmp_path=tmp_path, store=store, author=author, client=client, cfg=cfg,
        hidden_check=_hidden_check(PinClient()), now=now,
    )
    assert client.profile_calls == 1, "保底触发，试了一次"
    attempted_at = store.load_authors()["u1"].baseline_content_count_at
    assert attempted_at is not None and attempted_at >= now, (
        "失败也要把'上次核对'推到现在，作为退避计时"
    )

    # 紧接着的下一轮：还在退避窗口内，不该再敲总数接口
    later = now + timedelta(minutes=1)
    _r2, store, _n, _p = await _run(
        tmp_path=tmp_path, store=store, author=store.load_authors()["u1"], client=client,
        cfg=cfg, hidden_check=_hidden_check(PinClient()), now=later,
    )
    assert client.profile_calls == 1, "同一 interval 内失败一次就够了，不能每轮重试"

    # 但退避不是永久放弃：越过 interval 之后要重新试
    after_interval = now + timedelta(minutes=31)
    _r3, _s, _n, _p = await _run(
        tmp_path=tmp_path, store=store, author=store.load_authors()["u1"], client=client,
        cfg=cfg, hidden_check=_hidden_check(PinClient()), now=after_interval,
    )
    assert client.profile_calls == 2, "越过保底间隔应当重试"


async def test_profile_fetch_failure_still_folds_new_posts_into_the_baseline(tmp_path):
    """读失败那轮的新作品已经进了 `known_ids`，不会再产出 `NEW_POST`。

    如果这时不把 +1 折进基准值，这个增量就永久丢了：基准值偏低，下一次触发时表现为
    `实际 > 预期` 的**假缺口**，白白消耗一轮定向核验（还要等 5 轮 drift 才放弃）。
    """
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now).with_updates(
        posts=(
            PostState(content_id="staying", created_at=now - timedelta(days=2)),
            PostState(
                content_id="gone1", created_at=now - timedelta(days=5),
                absent_rounds=0, absent_is_top=False,
            ),
        ),
        baseline_content_count=5,
        baseline_content_count_at=now - timedelta(hours=2),
    )
    client = Client(
        posts_items=(_staying_post(now), make_content("brand_new", created_at=now)),
        profile_error=MonitorError("upstream_unreachable", "boom"),
    )

    result, store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client,
        hidden_check=_hidden_check(PinClient()), now=now,
    )

    assert any(e.kind is EventKind.NEW_POST for e in result.events)
    state = store.load_authors()["u1"]
    assert state.baseline_content_count == 6, "5 + 新增 1 条（没有任何删除）"
    assert state.content_count_drift_rounds == 0, "这是'读不到'，不是'对不上'"


async def test_pinned_verification_failure_does_not_break_the_round(tmp_path):
    """定向核验失败：记警告，缺口留给下一次触发，不推进基准值。"""
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), content_count=5)
    pin = PinClient(error=MonitorError("PINNED_UNAVAILABLE", "identity retired"))

    result, store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    assert any(e.kind is EventKind.POST_REMOVED for e in result.events)
    state = store.load_authors()["u1"]
    assert state.baseline_content_count == 5, "缺口没解释清楚，基准值不该被推进"
    assert state.content_count_drift_rounds == 1


async def test_baseline_stays_accurate_across_a_new_post_round_then_a_removal_round(tmp_path):
    """跨轮：新作品轮刷新基准 → 删除轮的预期值仍然算得对（这是"基准值漂移"那个 bug 的回归）。"""
    now = datetime.now(timezone.utc)
    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    author = AuthorState(
        sec_user_id="u1", nickname="示例", ever_had_posts=True,
        baseline_content_count=2, baseline_content_count_at=now - timedelta(hours=2),
        posts=(PostState(content_id="p1", created_at=now - timedelta(days=3)),),
    )
    store.ensure_author("u1", "示例", now)
    cfg = DiffConfig()

    # 第 1 轮：新作品 p2 出现（访客可见），总数 3
    round1_client = Client(
        posts_items=(make_content("p2", created_at=now), make_content("p1", created_at=now - timedelta(days=3))),
        content_count=3,
    )
    result1, store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=round1_client,
        hidden_check=_hidden_check(PinClient(posts_items=())), now=now, store=store,
    )
    assert any(e.kind is EventKind.NEW_POST for e in result1.events)
    assert store.load_authors()["u1"].baseline_content_count == 3

    # 第 2 轮：p1 连续第 2 轮缺席 → 确认删除，预期 3 − 1 = 2，实际也是 2 → 不该核验
    state = store.load_authors()["u1"].with_updates(
        posts=tuple(
            p.with_updates(absent_rounds=cfg.delete_rounds - 1) if p.content_id == "p1" else p
            for p in store.load_authors()["u1"].posts
        )
    )
    pin2 = PinClient(posts_items=())
    round2_client = Client(posts_items=(make_content("p2", created_at=now),), content_count=2)
    result2, store, _n, _p = await _run(
        tmp_path=tmp_path, author=state, client=round2_client,
        hidden_check=_hidden_check(pin2), now=now + timedelta(minutes=1), store=store,
    )
    assert any(e.kind is EventKind.POST_REMOVED for e in result2.events)
    assert pin2.calls == [], "账目对得上，不该白跑一次定向核验"


async def test_unresolved_drift_gives_up_after_max_rounds(tmp_path):
    """连续解释不了的缺口：达到上限后放弃追踪、接受实际值，不会无限重试。"""
    now = datetime.now(timezone.utc)
    from dywatch.pipeline import MAX_UNRESOLVED_DRIFT_ROUNDS

    store = StateStore(tmp_path / "db.sqlite")
    store.migrate()
    author = _author_with_one_post_about_to_be_confirmed_removed(now).with_updates(
        content_count_drift_rounds=MAX_UNRESOLVED_DRIFT_ROUNDS - 1
    )
    store.ensure_author("u1", "示例", now)
    client = Client(posts_items=(_staying_post(now),), content_count=5)
    pin = PinClient(posts_items=())  # 解释不了

    _result, store, _n, _p = await _run(
        tmp_path=tmp_path, author=author, client=client, hidden_check=_hidden_check(pin), now=now
    )

    state = store.load_authors()["u1"]
    assert state.content_count_drift_rounds == 0
    assert state.baseline_content_count == 5, "放弃追踪后接受当下的实际值"


# ----------------------------------------------------------------- 抑制窗口
def test_strip_from_removals_never_breaks_an_event_with_a_hostile_payload():
    """`removed` 不是列表时原样放行——它在通知路径上，抛异常等于这一轮通知全丢。

    逐字符迭代一个字符串会把它拆成**字列表**再写回 payload，也就是把这条通知弄坏；
    只认字典条目（其余原样留着）同理：不能靠"上游一定给字典"来省这个判断。
    """
    from dywatch.models import Event, EventKind
    from dywatch.pipeline import _strip_from_removals

    def ev(payload):
        return Event(EventKind.POST_REMOVED, sec_user_id="u1", nickname="n", payload=payload)

    malformed = ev({"removed": "字符串不是列表"})
    assert _strip_from_removals((malformed,), {"h1"}) == (malformed,), "畸形载荷原样放行"
    assert _strip_from_removals((ev({}),), {"h1"})[0].payload == {}, "缺 removed 也要放行"

    mixed = ev({"removed": [None, 1, {"content_id": "h1"}]})
    kept = _strip_from_removals((mixed,), {"h1"})[0].payload["removed"]
    assert kept == [None, 1], "只摘掉真正的字典条目，其它原样留着"

    untouched = ev({"removed": [{"content_id": "real"}]})
    assert _strip_from_removals((untouched,), {"h1"})[0] is untouched, "没摘到东西就不该重建事件"

    all_hidden = ev({"removed": [{"content_id": "h1"}]})
    assert _strip_from_removals((all_hidden,), {"h1"}) == (), "整条都是假象时撤销这条事件"


async def test_verified_log_records_post_count_and_direction(tmp_path):
    """`hidden_check.verified` 记的是**条数**和方向，不是事件数。

    两个分支都只产出一条聚合事件，所以曾经那个 `count=len(events)` 恒为 1、没有信息量。
    """
    now = datetime.now(timezone.utc)
    author = _author_with_one_post_about_to_be_confirmed_removed(now)
    client = Client(posts_items=(_staying_post(now),), content_count=5)
    pin = PinClient(posts_items=(
        make_content("hidden1", created_at=now, title="看不见 A"),
        make_content("hidden2", created_at=now, title="看不见 B"),
    ))
    logger = Logger()
    await run_author(
        author=author, nickname=author.nickname, client=client,
        store=_store(tmp_path), notifier=Notifier(), dedup=Deduplicator(),
        pacer=Pacer(), gate=GlobalGate(), cfg=DiffConfig(), now=now, archive_enabled=False,
        hidden_check=_hidden_check(pin), logger=logger,
    )
    verified = logger.events("hidden_check.verified")
    assert len(verified) == 1
    assert verified[0]["posts"] == 2, "两条未知作品 → posts=2"
    assert verified[0]["kind"] == "hidden_from_guest", "方向也要能看出来"
    assert verified[0]["expected"] == 4 and verified[0]["actual"] == 5


async def test_sample_due_is_logged_when_only_the_fallback_triggers(tmp_path):
    """只有低频保底能解释这次核对时，记一条 debug —— 排障时要能回答"这轮为什么去查总数"。"""
    now = datetime.now(timezone.utc)
    author = _author_with_a_hidden_post(now).with_updates(
        baseline_content_count_at=now - timedelta(hours=2)   # 保底到期
    )
    client = Client(
        posts_items=(make_content("visible1", created_at=now - timedelta(days=2), title="标题visible1"),
                     make_content("hidden1", created_at=now - timedelta(days=1), title="标题hidden1")),
        content_count=2,
    )
    logger = Logger()
    await run_author(
        author=author, nickname=author.nickname, client=client,
        store=_store(tmp_path), notifier=Notifier(), dedup=Deduplicator(),
        pacer=Pacer(), gate=GlobalGate(), cfg=DiffConfig(), now=now, archive_enabled=False,
        hidden_check=_hidden_check(PinClient()), logger=logger,
    )
    assert client.profile_calls == 1, "保底到期 → 查一次总数"
    assert logger.events("hidden_check.sample_due"), "只有保底触发时要留下痕迹"
    assert not logger.events("hidden_check.verified"), "账目对得上，没有要解释的缺口"


def test_hidden_from_guest_has_no_suppression_window():
    """这个事件**必须没有**抑制窗口——它的信息是一次性的，压掉就是永久丢。

    曾经给过"6 小时/账号"的窗口，理由写的是"同一条作品只会被标记一次，所以不会压掉
    又一条新作品的提醒"——那个理由恰好是反的：正因为标记让同一条作品的重复不可能，
    窗口唯一能压掉的只有"6 小时内又发现**另一条**被藏的作品"，而那条通知不会补发
    （标记已落库 → 不会再核验），所以窗口不是兜重复，是丢新信息。这条测试钉住这个结论。
    """
    def event(title: str) -> Event:
        return Event(
            EventKind.HIDDEN_FROM_GUEST,
            sec_user_id="u1", nickname="示例",
            payload={"hidden": [{"content_id": "x", "title": title, "created_at": None}]},
        )

    dedup = Deduplicator()
    first, key = should_send(event("第一条被藏的作品"), dedup)
    assert first is True
    assert key == "", "无窗口事件不该占用去重键（投递失败时也没有键要还回去）"

    # 一小时后发现的是**另一条**作品：两条都必须送出去
    second, _ = should_send(event("一小时后发现的另一条"), dedup)
    assert second is True, "另一个新发现不能被账号级窗口压掉"

    # 同一账号连着好几条被藏时，一次核验会聚合成一条事件（见 pipeline 的聚合），
    # 所以这里放行不会变成刷屏
    assert not TRIGGERS.get(EventKind.HIDDEN_FROM_GUEST), (
        "它不该被加回 TRIGGERS：任何窗口都只会丢新发现"
    )
