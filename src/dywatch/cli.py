"""命令行入口。

    dywatch                 常驻监控（systemd 用这个）
    dywatch once            只跑一轮后退出——上线前确认配置对不对就用它
    dywatch status          打印最近一轮的状态快照
    dywatch doctor          自检：实例可达、Key 有效、scope 够用、账号读得出来
    dywatch config-check    打印全部生效配置与每一项的来源，并做跨项校验
    dywatch add <链接|ID>    把账号加进 users.conf（链接会用 DTK 解析成 sec_user_id）
    dywatch test-notify     给每个渠道发一条测试消息

`doctor` 是整个工具最该先跑的命令：它把"配错了"和"上游坏了"这两类问题分开，
而这两类的处理方式完全不同——前者要改配置，后者只需要等。
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .dtk import DtkClient, MonitorError
from .runtime import build_runtime, setup_logging, single_instance_lock
from .settings import Settings, resolve_env_file, load_settings
from .users import is_safe_id, load_users_conf, resolve_input, strip_inline_comment
from .webui import read_status

REQUIRED_SCOPES = ("douyin:read", "archive:read")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dywatch",
        description="抖音账号视频监控（基于 Douyin_TikTok_Download_API v5，只读客户端）",
    )
    parser.add_argument(
        "--env",
        metavar="PATH",
        help="指定 .env 路径（默认 $MONITOR_HOME/.env 或 ./.env）",
    )
    parser.add_argument("--version", action="version", version=f"dywatch {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="常驻监控（默认）")
    sub.add_parser("once", help="只检查一轮后退出")
    sub.add_parser("status", help="打印最近一次状态快照")
    sub.add_parser("doctor", help="自检：实例、凭据、scope、账号")
    sub.add_parser("config-check", help="打印全部生效配置与来源")
    sub.add_parser("test-notify", help="给每个渠道发一条测试消息")

    add = sub.add_parser("add", help="把账号加进 users.conf")
    add.add_argument("target", help="抖音主页链接，或 sec_user_id")
    add.add_argument(
        "nickname", nargs="?", default="", help="展示用昵称（默认取 ID 尾部）"
    )

    # `--env` 也接受写在子命令**后面**（`dywatch doctor --env /srv/x/.env`）。
    # 文档说"所有命令都接受 --env"，而只在顶层注册的话这种写法会被 argparse 拒掉。
    #
    # `default=SUPPRESS` 是必需的（这里踩过一次）：子解析器一旦给 `--env` 设了普通默认值
    # （哪怕是 None），它就会把**顶层那份** `--env` 覆盖掉——`dywatch --env X doctor`
    # 于是静默退回 `./.env`。用 SUPPRESS 时，子命令没写这个选项就完全不碰命名空间，
    # 顶层那份得以保留；两处都写时子命令那份（后解析）生效。
    for name, child in sub.choices.items():
        child.add_argument(
            "--env",
            metavar="PATH",
            default=argparse.SUPPRESS,
            help="指定 .env 路径（等价于写在子命令前面；两处都给时以子命令后面这个为准）",
        )
    return parser


def _env_path(args: argparse.Namespace) -> Path:
    """这次实际读的 `.env` 路径：子命令后面的 `--env` 优先，其次顶层的。"""
    explicit = getattr(args, "env", None) or None
    return resolve_env_file(explicit)


def _load(args: argparse.Namespace) -> Settings:
    return load_settings(_env_path(args))


# ---------------------------------------------------------------------------
# config-check / status
# ---------------------------------------------------------------------------


def cmd_config_check(settings: Settings, *, env_file: Path | None = None) -> int:
    print(f"dywatch {__version__} —— 生效配置")
    # 打的是**这次实际读的那个文件**。以前固定打 `resolve_env_file(None)`，于是
    # `--env /srv/dywatch/.env` 的报告头上写着 `./.env`；再叠上"显式路径不存在时
    # 静默跳过"的行为，一个拼错的 `--env` 会得到一份别的文件的完整报告而毫无提示。
    print(f"配置文件: {env_file if env_file is not None else resolve_env_file(None)}")
    lines = settings.describe()
    # 分隔线跟着最长的一行走：键名长了（比如 ARCHIVE_DOWNLOAD_MAX_PER_ROUND），
    # 写死 78 会让表格比它自己的框还宽，读起来像是溢出
    rule = "-" * max(78, *(len(line) for line in lines))
    print(rule)
    for line in lines:
        print(line)
    print(rule)
    for note in settings.warnings():
        print(f"提示: {note}")
    errors = settings.validate()
    if errors:
        print("-" * 78)
        for error in errors:
            print(f"错误: {error}")
        return 2

    users = load_users_conf(settings.users_conf)
    print(f"users.conf: {settings.users_conf} -> {len(users)} 个账号")
    for entry in users:
        print(f"  - {entry.nickname}  {entry.sec_user_id}")
    return 0


def cmd_status(settings: Settings) -> int:
    path = settings.status_path
    if not path.is_file():
        print(f"暂无状态快照（{path} 不存在），先跑一次 dywatch once")
        return 1
    # 与面板同一个口径：快照读不出来就说读不出来，而不是抛一个 traceback。
    # 面板把"文件损坏"当成"没有数据"，命令行不该比它更脆（同一份文件、同一种坏法）。
    data = read_status(settings)
    if not data:
        print(
            f"状态快照读不出来（{path} 损坏或不是 JSON 对象），等下一轮覆盖，或直接删掉它"
        )
        return 1
    print(f"快照时间: {data.get('timestamp')}  PID: {data.get('pid')}")
    gate = data.get("gate") or {}
    print(
        f"上游闸门: {'正常' if gate.get('open', True) else '已关闭(' + str(gate.get('reason')) + ')'}"
    )
    notify = data.get("notify") or {}
    print(f"推送渠道: {', '.join(notify.get('channels') or []) or '（静默/无）'}")
    # 两个轮次数并排显示并标口径：只给"本次运行"会让人以为轮数被重置了
    # （`rounds` 是进程内存计数，`rounds_total` 才是状态库里跨重启累计的那个）
    print(
        f"轮次: 本次运行 {data.get('rounds') if data.get('rounds') is not None else '—'} 轮，"
        f"累计 {data.get('rounds_total') if data.get('rounds_total') is not None else '—'} 轮"
    )
    print("-" * 78)
    header = f"{'账号':<22}{'状态':<10}{'作品':>4}{'失败':>5}  {'频率':<8}{'最新作品':<12}备注"
    print(header)
    for user in data.get("users") or []:
        status = "正常"
        if user.get("consecutive_fails"):
            status = f"失败{user['consecutive_fails']}"
        elif not user.get("ever_had_posts"):
            status = "无作品"
        elif not user.get("configured"):
            status = "已移除"
        # "最新作品多久前发布"，不是"上次检测到变化"——后者会被一次删除/改名刷新，
        # 看起来像账号很活跃（与面板同一口径）
        hours = user.get("hours_since_newest_post")
        when = "—" if hours is None else (f"{hours} 小时前" if hours else "刚刚")
        print(
            f"{(user.get('nickname') or '')[:20]:<22}{status:<10}"
            f"{int(user.get('known_posts') or 0):>4}{int(user.get('consecutive_fails') or 0):>5}  "
            f"{(user.get('update_frequency') or '—')[:6]:<8}{when:<12}"
            f"{user.get('last_error_code') or ''}"
        )
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


async def cmd_doctor(settings: Settings) -> int:
    print(f"dywatch {__version__} —— 自检")
    print(f"上游实例: {settings['DTK_BASE_URL']}")
    problems: list[str] = []

    if not settings["DTK_API_KEY"]:
        print("✗ DTK_API_KEY 未配置")
        return 2
    if not settings["DTK_API_KEY"].startswith("dtk_"):
        print(
            "! DTK_API_KEY 不以 dtk_ 开头 —— DTK 的 Key 是 dtk_<12位hex>_<32位base64url>，共 49 字符"
        )

    async with DtkClient(
        settings["DTK_BASE_URL"],
        settings["DTK_API_KEY"],
        wait=0,
        timeout=float(settings["DTK_TIMEOUT"]),
        user_agent=str(settings["DTK_USER_AGENT"]),
    ) as client:
        # 1) 凭据本身
        try:
            me = await client.me()
        except MonitorError as exc:
            print(f"✗ 凭据不可用：{exc.code} —— {exc.message}")
            if exc.code == "UNAUTHENTICATED":
                print(
                    "  这不是权限不足（那是 403 FORBIDDEN_SCOPE），而是 Key 本身没被接受。"
                )
                print("  请确认复制的是完整的 dtk_... 字符串，且未被吊销/过期。")
            return 2

        user = me.get("user") or {}
        scopes = set(user.get("scopes") or [])
        print(
            f"✓ 凭据有效：username={user.get('username')} role={user.get('role')} via={user.get('via')}"
        )
        print(f"  scopes: {', '.join(sorted(scopes))}")
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
        missing = [scope for scope in required if scope not in scopes]
        optional_missing = [
            scope
            for scope in REQUIRED_SCOPES
            if scope not in required and scope not in scopes
        ]
        if missing:
            print(f"✗ 缺少必需的 scope: {', '.join(missing)}")
            print("  douyin:read  -> user/posts、video、tools/parse-url、tasks/{id}")
            print(
                "  archive:read -> 归档交叉确认（可在配置里用 ARCHIVE_ENABLED=false 关掉）"
            )
            for scope in missing:
                problems.append(f"缺少 {scope}")
        else:
            print("✓ 必需的 scope 齐备")
        if optional_missing:
            print(
                f"  ! 缺 {', '.join(optional_missing)}：ARCHIVE_ENABLED=false，"
                "删除判定少一个零成本的第二信源（这是配置选择，不算问题）"
            )

        if settings["ARCHIVE_DOWNLOAD_ENABLED"]:
            if "media:write" not in scopes:
                print("✗ ARCHIVE_DOWNLOAD_ENABLED=true，但 Key 缺少 media:write scope")
                problems.append("缺少 media:write（归档下载需要）")
            else:
                try:
                    storage = await client.download_storage()
                    used = int(storage.get("bytes_total") or 0)
                    ceiling = int(storage.get("max_bytes") or 0)
                    pct = (used / ceiling * 100) if ceiling else 0.0
                    downloader = storage.get("downloader") or {}
                    print(
                        f"✓ 归档下载可用：已用 {used / 1024 / 1024:.1f}MB"
                        f" / 上限 {ceiling / 1024 / 1024:.0f}MB（{pct:.0f}%）"
                        f"，已 pin {storage.get('pinned', 0)} 条，下载器 "
                        f"{'在线' if downloader.get('available') else '离线'}"
                    )
                    if pct >= 90:
                        print("  ! 存储已接近上限，未 pin 的旧下载很快会被自动淘汰")
                    if not downloader.get("available"):
                        print(
                            "  ! 下载器当前离线：新的归档下载会一直排队，不会报错也不会成功"
                        )
                        problems.append("下载器离线")
                except MonitorError as exc:
                    print(f"! 读取存储用量失败：{exc.code}")

        if settings["HIDDEN_POST_CHECK_ENABLED"]:
            if not settings["PINNED_IDENTITY_ID"]:
                print("✗ HIDDEN_POST_CHECK_ENABLED=true，但 PINNED_IDENTITY_ID 未配置")
                problems.append("缺少 PINNED_IDENTITY_ID（隐藏作品核验需要）")
            else:
                pin_key_configured = bool(settings["PIN_DTK_API_KEY"])
                if pin_key_configured:
                    # 独立 Key：跟主 Key 不是一回事，得单独查一次它自己的 scopes
                    try:
                        async with DtkClient(
                            settings["DTK_BASE_URL"],
                            settings["PIN_DTK_API_KEY"],
                            wait=0,
                            timeout=float(settings["DTK_TIMEOUT"]),
                            user_agent=str(settings["DTK_USER_AGENT"]),
                        ) as pin_client:
                            pin_me = await pin_client.me()
                        pin_scopes = set((pin_me.get("user") or {}).get("scopes") or [])
                    except MonitorError as exc:
                        print(f"✗ PIN_DTK_API_KEY 不可用：{exc.code} —— {exc.message}")
                        problems.append("PIN_DTK_API_KEY 不可用")
                        pin_scopes = set()
                else:
                    pin_scopes = (
                        scopes  # 没配独立 Key，复用主 Key，scopes 前面已经查过了
                    )

                if "identity:manage" not in pin_scopes:
                    which = "PIN_DTK_API_KEY" if pin_key_configured else "DTK_API_KEY"
                    print(
                        f"✗ HIDDEN_POST_CHECK_ENABLED=true，但 {which} 缺少 identity:manage scope"
                    )
                    print(
                        "  这个 scope 能解密查看任意身份的 cookie 明文，建议单独开一把 Key 只给这一处用"
                    )
                    problems.append("缺少 identity:manage（隐藏作品核验需要）")
                else:
                    print(
                        f"✓ 隐藏作品核验可用：定向身份 {str(settings['PINNED_IDENTITY_ID'])[:8]}…"
                        f"（{'独立 Key' if pin_key_configured else '复用主 Key'}）"
                    )

        rate = me.get("rate_limit_per_min")
        print(
            f"  速率上限: {rate if rate is not None else '未单独设置（用实例默认 120/分钟）'}"
        )

        # 2) 实例健康与身份池
        try:
            status = await client.system_status()
            pool = status.get("pool") or {}
            douyin = pool.get("douyin") or {}
            print(
                f"✓ 实例 {status.get('version')}：身份池 douyin active={douyin.get('active')} "
                f"cooling={douyin.get('cooling')} degraded={douyin.get('degraded')}"
            )
            if not douyin.get("active"):
                print("! 身份池里没有可用的 douyin 身份 —— 任何抓取都会 503")
                problems.append("身份池为空")
        except MonitorError as exc:
            print(f"! 读取实例状态失败：{exc.code}")

        # 3) 每个账号真拉一次（这是唯一能证明"端到端可用"的办法）
        #
        # 这里刻意带 `include_raw=true`：置顶标志是本设计里唯一一处"必须靠 raw 才能拿到"
        # 的字段（v5 的归一化模型不暴露它，详情接口的 is_top 又恒为 0），
        # 自检正是验证这条链路的地方——代价是这一轮响应大一些，值得。
        users = load_users_conf(settings.users_conf)
        if not users:
            print("! users.conf 里没有账号，跳过账号检查")
        else:
            print(
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
                    print(f"  ✗ {entry.nickname}: {exc.code} {exc.message[:60]}")
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
                    problems.append(
                        f"{entry.nickname} 的 raw 缺失，置顶分级会退化为统一 2 轮"
                    )
                print(
                    f"  ✓ {entry.nickname}: {len(page.items)} 条"
                    f"（非置顶 {non_top} / 置顶 {pinned}，count={settings['FETCH_COUNT']}）"
                    f" {elapsed}ms has_more={page.has_more}{note}"
                )

    # 4) 速率余量
    avg = (
        float(settings["REQUEST_INTERVAL_MIN"])
        + float(settings["REQUEST_INTERVAL_MAX"])
    ) / 2
    per_min = 60.0 / avg if avg else 0
    print(f"请求节奏: 约 {per_min:.1f} 次/分钟（pacer 决定，与账号数无关）")
    if problems:
        print("-" * 78)
        for problem in problems:
            print(f"需要处理: {problem}")
        return 1
    print("✓ 自检通过")
    return 0


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


async def cmd_add(settings: Settings, target: str, nickname: str) -> int:
    candidate = resolve_input(target)
    if not candidate.startswith("MS4wLjAB") and "://" in target:
        async with DtkClient(
            settings["DTK_BASE_URL"], settings["DTK_API_KEY"], wait=0
        ) as client:
            try:
                info = await client.identify_url(target)
            except MonitorError as exc:
                print(f"✗ 无法识别链接：{exc.code} {exc.message}")
                return 2
        if info.get("needs_expansion"):
            print("✗ 这是短链（需要先跟随跳转才能知道目标）。请改用主页链接。")
            return 2
        if not info.get("allowed") or info.get("platform") != "douyin":
            print(f"✗ 不是可识别的抖音链接：{info}")
            return 2
        if info.get("resource") != "user":
            print(f"✗ 这条链接指向的是 {info.get('resource')}，不是账号主页。")
            return 2
        candidate = str(info.get("resource_id") or "")
        print(f"链接已解析为 sec_user_id: {candidate}")

    if not candidate:
        print("✗ 没能得到 sec_user_id")
        return 2

    # **写进文件之前先过一遍校验器**：`users.conf` 的解析器会静默丢弃非法 ID，
    # 于是"写进去了但没人监控"和"✓ 已加入"可以同时成立——用户要等到发现那个账号
    # 从来没推过东西才会察觉。这里用同一个 `is_safe_id` 把它拦在入口。
    if not is_safe_id(candidate):
        print(f"✗ 这个 sec_user_id 不合法：{candidate!r}")
        print("  它里面含空白、控制字符（含零宽字符/BOM）或 | / \\ 之类的字符，")
        print("  写进去也会被 users.conf 的解析器丢弃。请改成主页链接重新 add 一遍。")
        return 2
    # 昵称只用于展示，但它是**一行一条**的格式：带换行会把一行拆成两条记录。
    clean_nickname = " ".join(str(nickname).split()) if nickname else ""
    if nickname and clean_nickname != nickname:
        print(f"! 昵称里的换行/连续空白已折叠为空格：{clean_nickname!r}")
    # 与解析器共用同一个判定：写进去的昵称必须是读回来的昵称。`小王 #1` 里 `#` 前有空白，
    # 解析器会把 `#1` 当行尾注释，读回来只剩 `小王`——所以在入口就拦下，而不是写完再说"✓"。
    if strip_inline_comment(clean_nickname) != clean_nickname:
        print(f"✗ 昵称里有「空白 + #」：{clean_nickname!r}")
        print("  users.conf 会把它当行尾注释，读回来只剩 # 前面那部分。")
        print("  去掉 # 前面的空格（写成 账号#1），或者不要用 #。")
        return 2

    path = settings.users_conf
    # 去重按**整行 ID 字段**比，不用子串匹配：所有真实 ID 都以 `MS4wLjABAAAA` 开头，
    # 子串匹配会把"与某个已有 ID 前缀相同"的新账号误判成重复而拒绝写入。
    existing = (
        {entry.sec_user_id for entry in load_users_conf(path)}
        if path.is_file()
        else set()
    )
    if candidate in existing:
        print(f"! 该账号已在 users.conf 中：{candidate}")
        return 1

    line = f"{candidate}|{clean_nickname or candidate[-8:]}"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(f"✓ 已加入 {path}：{line}")
    print("  运行中的实例会在下一轮自动加载（按 mtime 热加载，无需重启）")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


async def cmd_run(settings: Settings, *, once: bool) -> int:
    errors = settings.validate()
    if errors:
        for error in errors:
            print(f"配置错误: {error}", file=sys.stderr)
        return 2

    logger, _raw = setup_logging(settings)
    for note in settings.warnings():
        logger.warning("config.note", note=note)

    runtime = build_runtime(settings, logger=logger)
    stop = asyncio.Event()
    runtime.loop.stop = stop

    def _request_stop(signum: int) -> None:
        logger.info("signal.received", signal=signum)
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop, int(sig))
        except (
            NotImplementedError,
            AttributeError,
        ):  # pragma: no cover - dev on Windows
            signal.signal(sig, lambda s, _f: _request_stop(int(s)))

    started = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info(
        "dywatch.started", version=__version__, pid=__import__("os").getpid(), once=once
    )
    for line in runtime.describe():
        logger.info("config", line=line.strip())
    if settings["WEB_ENABLED"]:
        try:
            from .webui import PanelServer

            panel = PanelServer(settings)
            panel.start()
            for url in panel.urls(settings):
                logger.info("panel.listening", url=url)
            logger.info(
                "panel.readonly",
                hint="只读且无鉴权；列表读 status.json、详情读状态库，不发上游请求、不消耗身份",
            )
            if str(settings["WEB_HOST"]) not in ("127.0.0.1", "::1", "localhost"):
                logger.warning(
                    "panel.exposed",
                    host=str(settings["WEB_HOST"]),
                    hint="按这个地址监听等于把面板摊在网络上，而它没有鉴权；"
                    "云服务器还要记得放通端口时自己评估",
                )
        except OSError as exc:
            logger.error("panel.failed", error=str(exc))
    logger.info("=" * 60)

    try:
        await runtime.loop.run(once=once)
    finally:
        elapsed = int((datetime.now(timezone.utc) - started).total_seconds())
        logger.info("dywatch.stopped", uptime_seconds=elapsed)
        await runtime.aclose()
    return 0


async def cmd_test_notify(settings: Settings) -> int:
    from .notifiers import build_notifier

    notifier = build_notifier(settings)
    names = notifier.names
    if not names:
        # 静默模式是**用户自己开的开关**，不是"凭据没配"。这里以前一律说"检查凭据"，
        # 于是照着自己配好的凭据反复核对，而真实原因（SILENT_MODE=true）从未被提到。
        if settings["SILENT_MODE"]:
            print("SILENT_MODE=true：推送被整体关掉了，所以没有渠道可测。")
            print(
                "  想验证渠道凭据，先把 SILENT_MODE 设为 false 再跑一次（测完可以改回来）。"
            )
            return 2
        print("没有任何可用渠道（检查 NOTIFY_CHANNELS / NOTIFY_TARGETS 与对应的凭据）")
        return 2
    print(f"向 {', '.join(names)} 发送测试消息…")
    delivery = await notifier.send_test()
    for name in delivery.sent:
        print(f"  ✓ {name}")
    for name, reason in delivery.failed.items():
        print(f"  ✗ {name}: {reason}")
    await notifier.aclose()
    return 0 if delivery.sent else 1


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # 显式给了 `--env` 就是"我要读这个文件"的承诺：那个文件不存在时**报错退出**。
    # 以前是静默跳过——拼错一个字符就会拿默认路径那份配置跑起来，而输出里看不出异常
    # （报告头只修了"显示读了哪个文件"，改不了"实际读了哪个文件"）。
    # 没写 `--env` 时默认路径不存在是正常的：用户可以完全用环境变量，那种情况仍然静默。
    explicit = getattr(args, "env", None) or None
    if explicit and not Path(explicit).is_file():
        print(f"配置文件不存在：{explicit}", file=sys.stderr)
        print(f"没写 --env 时读的是 {resolve_env_file(None)}", file=sys.stderr)
        return 2
    settings = _load(args)
    command = args.command or "run"

    if command == "config-check":
        return cmd_config_check(settings, env_file=_env_path(args))
    if command == "status":
        return cmd_status(settings)
    if command in ("doctor", "add", "test-notify"):
        runner = {
            "doctor": lambda: cmd_doctor(settings),
            "add": lambda: cmd_add(settings, args.target, args.nickname),
            "test-notify": lambda: cmd_test_notify(settings),
        }[command]
        return asyncio.run(runner())

    # run：需要单实例锁，拿不到就拒绝启动，避免两个常驻进程互相抢速率预算
    if command == "run":
        with single_instance_lock(settings.pid_path) as locked:
            if not locked:
                print(
                    f"已有实例在运行（{settings.pid_path}），或当前平台不支持文件锁。\n"
                    "如果确认没有实例在跑，删掉该文件再试。",
                    file=sys.stderr,
                )
                return 1
            return asyncio.run(cmd_run(settings, once=False))

    # once：不需要锁（本来就是给已经常驻运行时的人工抽查用的），但拿不到锁时提示一声——
    # 状态库是 WAL 模式（busy_timeout=15s），两边同时读写不会损坏数据，顶多互相等一下。
    if command == "once":
        with single_instance_lock(settings.pid_path) as locked:
            if not locked:
                print(
                    f"提示：{settings.pid_path} 显示已有常驻实例在跑，这次 once 会跟它"
                    "共用同一个状态库（WAL 模式，安全但可能互相等待）。",
                    file=sys.stderr,
                )
        return asyncio.run(cmd_run(settings, once=True))

    return asyncio.run(cmd_run(settings, once=True))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
