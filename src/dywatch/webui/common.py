"""面板各处都要用的底层零件：读快照、类型归一化、HTML 转义、账号状态分级、LED / 数据条。

这些函数**不碰网络、不碰数据库**（唯一的 I/O 是读 `status.json`），所以任何一页都能安全地用，
也都能在测试里单独喂畸形输入。

两条贯穿本模块的纪律，都是从线上事故来的：

* **快照里的每个值都当"别人的输入"处理。** `status.json` 是本进程写的，但它是个文本文件，
  可能被人手工改坏、被旧版本写成另一种形状、被别的脚本覆写。面板是"出问题时打开的东西"，
  它自己不能先崩——所以 `_as_int` / `_as_number` / `_as_mapping` 三个函数把"认不出来"
  一律折成"没有值"，而不是抛异常。
* **外部字符串在出口处转义。** 昵称、标题、错误码都能流进 HTML，`_escape_html` 是所有出口
  唯一的写法；少一处就是 XSS，而这些字符串来自平台。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from ..models import EventKind
from ..render import SEVERITY
from ..settings import Settings
from .theme import FREQ_TAG, LEGEND_ITEM, ROW_TEMPLATE, STAT_TEMPLATE

#: LED 阵列的格子数。24 格是旧项目调出来的：再多读不出比例，再少小类别看不出存在。
LED_SLOTS = 24

#: 状态分类：(key, 颜色变量, 列表徽章文案)。顺序 == 图例顺序 == 数据条顺序。
STATUS_KEYS = ("green", "red", "amber", "blue", "off")
STATUS_LEGEND = {
    "green": ("正常", "var(--green)"),
    "red": ("失败", "var(--red)"),
    "amber": ("长期无更新", "var(--amber)"),
    "blue": ("从未有作品", "var(--blue)"),
    "off": ("已移除", "var(--off)"),
}


# =================== 类型归一化 ===================
# `status.json` 是本进程自己写的，但**不能假设它一定是我们写的**：它是个文本文件，
# 可能被人手工改过（调值时改坏了），也可能被别的脚本覆写。`/metrics` 尤其脆——
# 一个值抛异常等于这次抓取整体失败，比显示一个 0 严重得多（同一个教训在通知层上过：
# 渲染路径必须对畸形载荷免疫）。所以读快照的数值一律走这三个函数，认不出来就用
# "没有"而不是抛。


def _as_int(value: Any) -> int:
    """把快照里的值当整数读；认不出来（`"abc"` / 列表 / 字典）就当 0。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_number(value: Any) -> float | None:
    """快照里的小时数这类字段；不是数就当"没有作品"处理，而不是让比较抛异常。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _as_mapping(value: Any) -> dict[str, Any]:
    """快照里"应当是个对象"的字段；不是就当成空对象，而不是让调用方 `AttributeError`。"""
    return dict(value) if isinstance(value, Mapping) else {}


def _escape_html(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def read_status(settings: Settings) -> dict[str, Any]:
    """读状态快照。文件不存在/损坏都返回 `{}`——面板不该因此 500。

    **顶层字段一律做类型归一化**：快照是个普通 JSON 文件，手工改坏（或旧版本写下的
    另一种形状）都不该让 `/`、`/events`、`/metrics`、`/api/health` 在写出任何响应
    **之前**抛异常——那种情况下连接会被直接关掉，比返回 500 更难查。数值字段由
    `_as_int` / `_as_number` 兜，这里负责那几个"当映射/列表用"的字段。
    """
    try:
        data = json.loads(settings.status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    data["gate"] = _as_mapping(data.get("gate"))
    data["upstream"] = _as_mapping(data.get("upstream"))
    data["self_check"] = _as_mapping(data.get("self_check"))
    data["archive"] = _as_mapping(data.get("archive"))
    data["features"] = _as_mapping(data.get("features"))
    notify = _as_mapping(data.get("notify"))
    channels = notify.get("channels")
    notify["channels"] = (
        [str(item) for item in channels] if isinstance(channels, (list, tuple)) else []
    )
    data["notify"] = notify
    users = data.get("users")
    data["users"] = (
        [item for item in users if isinstance(item, dict)]
        if isinstance(users, (list, tuple))
        else []
    )
    return data


# =================== 状态分类 ===================


def classify_account(user: Mapping[str, Any], stale_days: int) -> tuple[str, str]:
    """一个账号的 `(颜色 key, 状态文案)`。

    判定顺序就是"该先看哪个问题"的顺序：已移出配置（不用管了）→ 请求失败（最急）→
    从未返回过作品（多半是 ID 写错了）→ 长期无更新 → 正常。

    `never_seen` 单独一色是新项目才有的状态：抖音对**形态合法但不存在**的
    `sec_user_id` 返回 `200 + items:[]`，上游永远不会报错，只有这里能把它标出来。
    """
    if not user.get("configured", True):
        return "off", "已移除"
    fails = _as_int(user.get("consecutive_fails"))
    if fails > 0:
        return "red", f"失败 {fails} 次"
    if not user.get("ever_had_posts"):
        return "blue", "从未有作品"
    hours = _as_number(user.get("hours_since_newest_post"))
    if hours is not None and hours >= stale_days * 24:
        return "amber", f"{hours // 24} 天无新作品"
    return "green", "正常"


def _format_post_age(hours: int | None) -> str:
    """距**最新一条作品发布**过了多久。

    注意不是"距上次检测到变化"：账号被删了一条作品、或改了标题，`last_update_at`
    就会刷新，于是"距上次更新"会显示"刚刚"，而它其实已经 20 天没发东西了。
    """
    if hours is None:
        return "还没有作品"
    if hours < 1:
        return "刚刚发布"
    if hours < 24:
        return f"{hours} 小时前发布"
    return f"{hours // 24} 天前发布"


def _overall_line(total: int, ok: int, failing: int, stale: int, never: int) -> str:
    if total == 0:
        return "还没有<b>监控账号</b>"
    if not (failing or stale or never):
        return f"{total} 个账号<b>全部正常</b>"
    parts: list[str] = []
    if failing:
        parts.append(f"<b>{failing}</b> 个请求失败")
    if stale:
        parts.append(f"<b>{stale}</b> 个长期无更新")
    if never:
        parts.append(f"<b>{never}</b> 个从未有作品")
    return "，".join(parts) + f"，{ok} 个正常"


# =================== LED 阵列与数据条 ===================


def _quantize_blocks(counts: Iterable[int], slots: int = LED_SLOTS) -> list[int]:
    """把各类别的账号数按比例分配成正好 `slots` 个整数格子（最大余数法）。

    先给每个非零类别保底 1 格（只要格子够用），再把剩余格子按原始比例用最大余数法
    分配。不这样做的话，极端比例下（比如 100:1:1:1:1）小类别会被直接舍成 0 格，
    阵列里完全看不出这个状态存在——而恰恰是那 1 个失败账号最需要被看见。
    """
    buckets = list(counts)
    n = len(buckets)
    total = sum(buckets)
    if total == 0:
        return [0] * n

    nonzero = [index for index, count in enumerate(buckets) if count > 0]
    base = [0] * n
    if len(nonzero) <= slots:
        for index in nonzero:
            base[index] = 1
        remaining = slots - len(nonzero)
    else:  # pragma: no cover - 类别数（5）永远小于格子数（24）
        remaining = slots

    if remaining > 0:
        raw = [(buckets[index] / total) * remaining for index in range(n)]
        extra = [int(value) for value in raw]
        leftover = remaining - sum(extra)
        order = sorted(range(n), key=lambda i: raw[i] - extra[i], reverse=True)
        for index in range(leftover):
            extra[order[index % n]] += 1
        for index in range(n):
            base[index] += extra[index]
    return base


def _render_ledarray(buckets: Mapping[str, int]) -> str:
    counts = [buckets.get(key, 0) for key in STATUS_KEYS]
    if sum(counts) == 0:
        return ""
    blocks = _quantize_blocks(counts)
    cells: list[str] = []
    for count, key in zip(blocks, STATUS_KEYS):
        cells.extend([f'<span class="led on-{key}"></span>'] * count)
    legend = "".join(
        LEGEND_ITEM.substitute(
            color=STATUS_LEGEND[key][1],
            label=STATUS_LEGEND[key][0],
            count=buckets.get(key, 0),
        )
        for key in STATUS_KEYS
        if buckets.get(key, 0)
    )
    return f'<div class="led-row">{"".join(cells)}</div><div class="led-legend mono">{legend}</div>'


def _render_stats(total: int, buckets: Mapping[str, int]) -> str:
    if total == 0:
        return ""
    cells = [STAT_TEMPLATE.substitute(value=total, label="账号总数", color="")]
    for key in STATUS_KEYS:
        count = buckets.get(key, 0)
        if count or key != "off":  # "已移除 0" 是噪音，其余四项固定成行
            cells.append(
                STAT_TEMPLATE.substitute(
                    value=count, label=STATUS_LEGEND[key][0], color=key
                )
            )
    return '<div class="stats">' + "".join(cells) + "</div>"


def _render_row(user: Mapping[str, Any], stale_days: int) -> str:
    """账号列表的一行。整行可点（`data-uid`），详情由前端 fetch。"""
    color, status_text = classify_account(user, stale_days)
    sec_user_id = str(user.get("sec_user_id") or "")
    freq_label = user.get("update_frequency")
    hint = user.get("freq_hint") or ""
    return ROW_TEMPLATE.substitute(
        row_class="" if user.get("configured", True) else " row-off",
        uid=_escape_html(sec_user_id),
        uid_tip=_escape_html(sec_user_id),
        badge_color=f"var(--{color})",
        status_color=color,
        status_text=_escape_html(status_text),
        nickname=_escape_html(user.get("nickname") or "-"),
        freq_tag=(
            FREQ_TAG.substitute(
                label=_escape_html(str(freq_label)), tip=_escape_html(str(hint))
            )
            if freq_label
            else ""
        ),
        known_posts=_as_int(user.get("known_posts")),
        post_age_text=_escape_html(
            _format_post_age(_as_number(user.get("hours_since_newest_post")))
        ),
    )


def _gate_html(gate: Mapping[str, Any]) -> str:
    """上游闸门关闭时的红色警示条。"""
    if gate.get("open", True):
        return ""
    reason = str(gate.get("reason") or "未知")
    remaining = gate.get("remaining_seconds")
    tail = ""
    if isinstance(remaining, (int, float)) and remaining:
        tail = f"，约 {int(remaining)} 秒后自动重试"
    # 上游要求的等待时间被封顶过：横幅要说出来。否则运维看到"闸门每十分钟开一次"，
    # 只会以为上游一直在限流，而真相是"上游要求等更久，我们没完全照办"（见 DESIGN 修正 #32）
    capped = _as_int(gate.get("retry_after_capped"))
    capped_note = (
        f"　注意：上游要求的等待时间已被封顶 {capped} 次"
        f"（上限见 RETRY_AFTER_MAX_SECONDS），闸门会比上游要求的更早放开。"
        if capped
        else ""
    )
    return (
        '<div class="gate mono">[ 闸门关闭 ] 上游不可用（'
        + _escape_html(reason)
        + "），本轮的请求已整体跳过，只记录不推送"
        + tail
        + "。这与某个账号无关，必要时去 DTK 控制台看身份池。"
        + capped_note
        + "</div>"
    )


def _selfcheck_html(self_check: Mapping[str, Any]) -> str:
    """自身降级的琥珀色横幅。

    形状照 `.gate`，颜色换成 amber——**两条是独立的**：`gate` 说的是"上游挂了"，
    这条说的是"我自己快不行了"（磁盘要满 / 数据目录写不进去 / 状态库写不进去）。
    两件事可以同时发生，所以两条都显示，而不是合成一条。

    文案里刻意给出读数而不是只给原因：看到"磁盘只剩 40MB"才知道要去删什么。
    """
    if not self_check or self_check.get("ok", True):
        return ""
    reasons = self_check.get("reasons")
    labels = [
        _SELF_CHECK_LABELS.get(str(code), str(code))
        for code in (reasons if isinstance(reasons, (list, tuple)) else [])
    ]
    if not labels:
        return ""
    detail = _self_check_readings(self_check)
    return (
        '<div class="selfcheck mono">[ 自身降级 ] '
        + "；".join(_escape_html(item) for item in labels)
        + (f"（{_escape_html(detail)}）" if detail else "")
        + "。监控本身受影响，但这与上游、与具体账号无关，通知链路照常工作。"
        + "</div>"
    )


#: 自身降级的原因码 → 人话。加新的检查项时要一起补。
_SELF_CHECK_LABELS: Mapping[str, str] = {
    "disk_low": "数据目录所在磁盘剩余空间不足",
    "data_dir_read_only": "数据目录不可写",
    "state_store_write_failed": "状态库最近一轮写失败",
}


def _self_check_readings(self_check: Mapping[str, Any]) -> str:
    """把检查时的读数拼成一行短句。缺值就跳过——**不写 "None"**。"""
    parts: list[str] = []
    free = self_check.get("free_mb")
    limit = self_check.get("free_limit_mb")
    if isinstance(free, int):
        parts.append(
            f"剩余 {free}MB" + (f" / 阈值 {limit}MB" if isinstance(limit, int) else "")
        )
    elif isinstance(self_check.get("error"), str):
        # 问不出余量本身不是降级（见 loop._self_check_reasons），但要说出来
        parts.append(f"磁盘余量读不到：{self_check['error']}")
    return "，".join(parts)


# =================== 事件色调 ===================
# 时间线的徽章颜色。**基础档直接取 `render.SEVERITY`**，只对少数几个事件做覆盖——
# 这样做有两个好处：新加事件时它自动有颜色（不会掉进"未覆盖"的洞里），而这个覆盖表
# 只列"info 里该被看见的那几个"，看一眼就知道它为什么存在。
#
# 不新增第三张"事件 → 颜色"的全量枚举表：本项目已经有多处按事件分派的地方，每多一处
# 就多一次"加了事件忘了改"的机会（见 DESIGN 第 10 章）。
_TONE_BY_SEVERITY: Mapping[str, str] = {
    "error": "bad",
    "warning": "warn",
    "info": "quiet",
}
_TONE_OVERRIDE: Mapping[EventKind, str] = {
    # 新作品是打开面板要问的第一件事，`info` 这一档（灰）配不上它
    EventKind.NEW_POST: "good",
    EventKind.REVIVED: "good",
    EventKind.INITIALIZED: "good",
    EventKind.ACCOUNT_RECOVERED: "good",
}


def event_tone(kind: EventKind) -> str:
    """事件徽章的颜色档（同时也是 `theme.CSS` 里 `k-*` 类名的后缀）。"""
    if kind in _TONE_OVERRIDE:
        return _TONE_OVERRIDE[kind]
    return _TONE_BY_SEVERITY.get(SEVERITY.get(kind, "info"), "quiet")


__all__ = [
    "LED_SLOTS",
    "STATUS_KEYS",
    "STATUS_LEGEND",
    "_as_int",
    "_as_mapping",
    "_as_number",
    "_escape_html",
    "_format_post_age",
    "_overall_line",
    "_quantize_blocks",
    "_render_ledarray",
    "_gate_html",
    "_render_row",
    "_render_stats",
    "_selfcheck_html",
    "classify_account",
    "event_tone",
    "read_status",
]
