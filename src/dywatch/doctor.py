"""`dywatch doctor` —— 自检。

它回答一个问题：**现在这套配置能不能真的跑起来**。所以检查按依赖顺序排：
Key → scope → 身份 → 实例 → 账号，前一步过不去，后一步的结论就没有意义。

放在独立模块而不是 `cli.py` 里：这是一整个子系统（五类检查，每类有自己的失败语义与
修复指引），而 `cli` 的职责只是"解析参数、分派"。两者的改动理由从来不在一起。

**每一项检查都必须给出可执行的下一步**：`✗` 说明这里过不去，紧跟的几行就是"怎么办"。
只说"失败了"的自检等于没做——而 `doctor` 恰恰是这个工具最该先跑的命令。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from . import __version__
from .dtk import DtkClient, MonitorError
from .settings import Settings
from .users import load_users_conf

#: 监控本身离不开的两个 scope。`archive:read` 的必需性跟着 `ARCHIVE_ENABLED` 走，
#: 见 `_check_scopes` —— 它不是"永远必需"，而"用得上它的时候必需"。
REQUIRED_SCOPES = ("douyin:read", "archive:read")

#: 输出口。默认 `print`；测试想捕获输出时可以换掉（现在的测试只看退出码）。
Out = Callable[[str], None]


async def run_doctor(settings: Settings, *, out: Out = print) -> int:
    """跑完全部检查，返回进程退出码：0 通过 / 1 有问题 / 2 凭据不可用。"""
    out(f"dywatch {__version__} —— 自检")
    out(f"上游实例: {settings['DTK_BASE_URL']}")
    problems: list[str] = []

    if not settings["DTK_API_KEY"]:
        out("✗ DTK_API_KEY 未配置")
        return 2
    if not settings["DTK_API_KEY"].startswith("dtk_"):
        out(
            "! DTK_API_KEY 不以 dtk_ 开头 —— DTK 的 Key 是 dtk_<12位hex>_<32位base64url>，共 49 字符"
        )

    async with DtkClient(
        settings["DTK_BASE_URL"],
        settings["DTK_API_KEY"],
        wait=0,
        timeout=float(settings["DTK_TIMEOUT"]),
        user_agent=str(settings["DTK_USER_AGENT"]),
        identity=str(settings["DTK_IDENTITY"]),
    ) as client:
        user = await _check_credentials(client, out, problems)
        if user is None:
            return 2
        scopes = _check_scopes(user, settings, out, problems)
        if settings["ARCHIVE_DOWNLOAD_ENABLED"]:
            await _check_archive_download(client, scopes, out, problems)
        _check_identity(user, settings, scopes, out, problems)
        await _check_instance(client, settings, out, problems)
        await _check_accounts(client, settings, out, problems)

    _report_pacing(settings, out)

    if problems:
        out("-" * 78)
        for problem in problems:
            out(f"需要处理: {problem}")
        return 1
    out("✓ 自检通过")
    return 0


async def _check_credentials(
    client: DtkClient, out: Out, problems: list[str]
) -> dict[str, Any] | None:
    """`auth/me`：Key 本身是否被接受。返回 `data.user`，不可用时返回 `None`。"""
    try:
        me = await client.me()
    except MonitorError as exc:
        out(f"✗ 凭据不可用：{exc.code} —— {exc.message}")
        if exc.code == "UNAUTHENTICATED":
            out("  这不是权限不足（那是 403 FORBIDDEN_SCOPE），而是 Key 本身没被接受。")
            out("  请确认复制的是完整的 dtk_... 字符串，且未被吊销/过期。")
        return None

    user = me.get("user") or {}
    scopes = set(user.get("scopes") or ())
    out(
        f"✓ 凭据有效：username={user.get('username')} role={user.get('role')} via={user.get('via')}"
    )
    out(f"  scopes: {', '.join(sorted(scopes))}")
    rate = me.get("rate_limit_per_min")
    out(
        f"  速率上限: {rate if rate is not None else '未单独设置（用实例默认 120/分钟）'}"
    )
    return user


def _check_scopes(
    user: dict[str, Any], settings: Settings, out: Out, problems: list[str]
) -> set[str]:
    """scope 是否够用。返回 Key 实际持有的 scope 集合，后两步要用。"""
    scopes = set(user.get("scopes") or ())
    # `archive:read` **只在用得上它的时候才算必需**：它在配置里的开关就是
    # `ARCHIVE_ENABLED`（关掉之后删除判定只是少一个零成本的第二信源）。
    # 以前这里不看开关，两个方向都错：默认开启 + 缺 scope 时打印了 ✗ 却仍然
    # "✓ 自检通过"并 exit 0（一个把 exit code 当门禁的 CI 会放过去，而归档交叉
    # 确认实际上一直在失败）；反过来 `ARCHIVE_ENABLED=false` 时还在报 ✗。
    required = [
        scope
        for scope in REQUIRED_SCOPES
        if scope != "archive:read" or settings["ARCHIVE_ENABLED"]
    ]
    if settings["DTK_IDENTITY"]:
        required.append("identity:manage")
    missing = [scope for scope in required if scope not in scopes]
    optional_missing = [
        scope
        for scope in REQUIRED_SCOPES
        if scope not in required and scope not in scopes
    ]

    if missing:
        out(f"✗ 缺少必需的 scope: {', '.join(missing)}")
        out("  douyin:read  -> user/posts、video、tools/parse-url、tasks/{id}")
        out("  archive:read -> 归档交叉确认（可在配置里用 ARCHIVE_ENABLED=false 关掉）")
        out("  identity:manage -> 把读请求钉在 DTK_IDENTITY 指定的身份上")
        for scope in missing:
            problems.append(f"缺少 {scope}")
    else:
        out("✓ 必需的 scope 齐备")
    if optional_missing:
        out(
            f"  ! 缺 {', '.join(optional_missing)}：ARCHIVE_ENABLED=false，"
            "删除判定少一个零成本的第二信源（这是配置选择，不算问题）"
        )
    return scopes


async def _check_archive_download(
    client: DtkClient, scopes: set[str], out: Out, problems: list[str]
) -> None:
    """归档下载：scope + 磁盘余量 + 下载器是否在线。只在开关打开时才有意义。"""
    if "media:write" not in scopes:
        out("✗ ARCHIVE_DOWNLOAD_ENABLED=true，但 Key 缺少 media:write scope")
        problems.append("缺少 media:write（归档下载需要）")
        return

    try:
        storage = await client.download_storage()
    except MonitorError as exc:
        out(f"! 读取存储用量失败：{exc.code}")
        return

    used = int(storage.get("bytes_total") or 0)
    ceiling = int(storage.get("max_bytes") or 0)
    pct = (used / ceiling * 100) if ceiling else 0.0
    downloader = storage.get("downloader") or {}
    out(
        f"✓ 归档下载可用：已用 {used / 1024 / 1024:.1f}MB"
        f" / 上限 {ceiling / 1024 / 1024:.0f}MB（{pct:.0f}%）"
        f"，已 pin {storage.get('pinned', 0)} 条，下载器 "
        f"{'在线' if downloader.get('available') else '离线'}"
    )
    if pct >= 90:
        out("  ! 存储已接近上限，未 pin 的旧下载很快会被自动淘汰")
    if not downloader.get("available"):
        out("  ! 下载器当前离线：新的归档下载会一直排队，不会报错也不会成功")
        problems.append("下载器离线")


def _check_identity(
    user: dict[str, Any],
    settings: Settings,
    scopes: set[str],
    out: Out,
    problems: list[str],
) -> None:
    """单身份模式（D27）的前提：配了 `DTK_IDENTITY`、有 scope、角色够。"""
    if not settings["DTK_IDENTITY"]:
        out("! 未配置 DTK_IDENTITY：会退回身份池轮换")
        out("  抖音对未登录访客隐藏最新作品，这条路拿不到完整列表，只适合排障")
        problems.append("未配置 DTK_IDENTITY（生产环境必填）")
        return

    if "identity:manage" not in scopes:
        out(
            "  定向读取需要 identity:manage；这个 scope 能解密查看任意身份的 cookie 明文，"
            "不要把带它的 Key 给别的工具用"
        )
        return

    role = str(user.get("role") or "")
    if role not in ("operator", "admin"):
        out(
            f"✗ 定向读取需要 operator 及以上角色，当前是 {role}"
            "（DTK 的 pin 校验 role，不只是 scope）"
        )
        problems.append("角色不足（定向读取需要 operator）")
        return
    out(f"✓ 定向读取身份 {str(settings['DTK_IDENTITY'])[:8]}…（不走身份池）")


async def _check_instance(
    client: DtkClient, settings: Settings, out: Out, problems: list[str]
) -> None:
    """实例版本与身份池。单身份模式下"池是空的"不算问题，但那个身份必须在。"""
    try:
        status = await client.system_status()
    except MonitorError as exc:
        out(f"! 读取实例状态失败：{exc.code}")
        return

    pool = status.get("pool") or {}
    douyin = pool.get("douyin") or {}
    out(
        f"✓ 实例 {status.get('version')}：身份池 douyin active={douyin.get('active')} "
        f"cooling={douyin.get('cooling')} degraded={douyin.get('degraded')}"
    )
    if douyin.get("active"):
        return
    if settings["DTK_IDENTITY"]:
        out(
            "  ! 身份池是空的——对本工具无影响（读请求点名 DTK_IDENTITY，"
            "不走池），但那个身份必须存在且 active"
        )
    else:
        out("! 身份池里没有可用的 douyin 身份 —— 任何抓取都会 503")
        problems.append("身份池为空")


async def _check_accounts(
    client: DtkClient, settings: Settings, out: Out, problems: list[str]
) -> None:
    """每个账号真拉一次——这是唯一能证明"端到端可用"的办法。

    刻意带 `include_raw=true`：置顶标志是本设计里唯一一处"必须靠 raw 才能拿到"
    的字段（v5 的归一化模型不暴露它，详情接口的 `is_top` 又恒为 0），自检正是
    验证这条链路的地方——代价是这一轮响应大一些，值得。
    """
    users = load_users_conf(settings.users_conf)
    if not users:
        out("! users.conf 里没有账号，跳过账号检查")
        return

    out(
        f"检查 {len(users)} 个账号（每个消耗 1 个身份，本轮带 include_raw 以验证置顶链路）…"
    )
    for entry in users:
        started = asyncio.get_running_loop().time()
        try:
            page = await client.author_posts(
                entry.sec_user_id,
                int(settings["FETCH_COUNT"]),
                include_raw=True,
            )
        except MonitorError as exc:
            out(f"  ✗ {entry.nickname}: {exc.code} {exc.message[:60]}")
            if exc.code == "INVALID_PARAM":
                problems.append(f"{entry.nickname} 的 ID 无效")
            continue

        elapsed = int((asyncio.get_running_loop().time() - started) * 1000)
        non_top = len(page.non_top())
        pinned = len(page.items) - non_top
        note = ""
        if not page.items:
            note = "  ← 返回空列表：ID 可能不存在（上游不会报错，只会给空）"
            problems.append(f"{entry.nickname} 返回空列表，请核实 ID")
        elif not page.raw_included:
            note = "  ← 没拿到 raw，置顶标志不可用"
            problems.append(f"{entry.nickname} 的 raw 缺失，置顶分级会退化为统一 2 轮")
        out(
            f"  ✓ {entry.nickname}: {len(page.items)} 条"
            f"（非置顶 {non_top} / 置顶 {pinned}，count={settings['FETCH_COUNT']}）"
            f" {elapsed}ms has_more={page.has_more}{note}"
        )


def _report_pacing(settings: Settings, out: Out) -> None:
    """请求节奏的换算。它不是"检查"，是让人对负载有个数量感。"""
    avg = (
        float(settings["REQUEST_INTERVAL_MIN"])
        + float(settings["REQUEST_INTERVAL_MAX"])
    ) / 2
    per_min = 60.0 / avg if avg else 0
    out(f"请求节奏: 约 {per_min:.1f} 次/分钟（pacer 决定，与账号数无关）")


__all__ = ["REQUIRED_SCOPES", "run_doctor"]
