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
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .dtk import DtkClient, MonitorError
from .runtime import build_runtime, setup_logging, single_instance_lock
from .settings import Settings, resolve_env_file, load_settings
from .users import load_users_conf, resolve_input

REQUIRED_SCOPES = ("douyin:read", "archive:read")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dywatch",
        description="抖音账号视频监控（基于 Douyin_TikTok_Download_API v5，只读客户端）",
    )
    parser.add_argument("--env", metavar="PATH", help="指定 .env 路径（默认 $MONITOR_HOME/.env 或 ./.env）")
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
    add.add_argument("nickname", nargs="?", default="", help="展示用昵称（默认取 ID 尾部）")

    return parser


def _load(args: argparse.Namespace) -> Settings:
    return load_settings(resolve_env_file(args.env))


# ---------------------------------------------------------------------------
# config-check / status
# ---------------------------------------------------------------------------


def cmd_config_check(settings: Settings) -> int:
    print(f"dywatch {__version__} —— 生效配置")
    print(f"配置文件: {resolve_env_file(None)}")
    print("-" * 78)
    for line in settings.describe():
        print(line)
    print("-" * 78)
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
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"快照时间: {data.get('timestamp')}  PID: {data.get('pid')}")
    gate = data.get("gate") or {}
    print(f"上游闸门: {'正常' if gate.get('open', True) else '已关闭(' + str(gate.get('reason')) + ')'}")
    notify = data.get("notify") or {}
    print(f"推送渠道: {', '.join(notify.get('channels') or []) or '（静默/无）'}")
    print("-" * 78)
    header = f"{'账号':<22}{'状态':<10}{'作品':>4}{'失败':>5}  {'频率':<8}{'上次更新':<12}备注"
    print(header)
    for user in data.get("users") or []:
        status = "正常"
        if user.get("consecutive_fails"):
            status = f"失败{user['consecutive_fails']}"
        elif not user.get("ever_had_posts"):
            status = "无作品"
        elif not user.get("configured"):
            status = "已移除"
        hours = user.get("hours_since_update")
        when = "—" if hours is None else (f"{hours} 小时前" if hours else "刚刚")
        print(
            f"{(user.get('nickname') or '')[:20]:<22}{status:<10}"
            f"{int(user.get('known_posts') or 0):>4}{int(user.get('consecutive_failures') or user.get('consecutive_fails') or 0):>5}  "
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
        print("! DTK_API_KEY 不以 dtk_ 开头 —— DTK 的 Key 是 dtk_<12位hex>_<32位base64url>，共 49 字符")

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
                print("  这不是权限不足（那是 403 FORBIDDEN_SCOPE），而是 Key 本身没被接受。")
                print("  请确认复制的是完整的 dtk_... 字符串，且未被吊销/过期。")
            return 2

        user = me.get("user") or {}
        scopes = set(user.get("scopes") or [])
        print(f"✓ 凭据有效：username={user.get('username')} role={user.get('role')} via={user.get('via')}")
        print(f"  scopes: {', '.join(sorted(scopes))}")
        missing = [scope for scope in REQUIRED_SCOPES if scope not in scopes]
        if missing:
            print(f"✗ 缺少必需的 scope: {', '.join(missing)}")
            print("  douyin:read  -> user/posts、video、tools/parse-url、tasks/{id}")
            print("  archive:read -> 归档交叉确认（可在配置里用 ARCHIVE_ENABLED=false 关掉）")
            if "douyin:read" in missing:
                problems.append("缺少 douyin:read")
        else:
            print("✓ 必需的 scope 齐备")

        rate = me.get("rate_limit_per_min")
        print(f"  速率上限: {rate if rate is not None else '未单独设置（用实例默认 120/分钟）'}")

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
            print(f"检查 {len(users)} 个账号（每个消耗 1 个身份，本轮带 include_raw 以验证置顶链路）…")
            for entry in users:
                started = asyncio.get_running_loop().time()
                try:
                    page = await client.author_posts(
                        entry.sec_user_id, int(settings["FETCH_COUNT"]), include_raw=True
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
                    problems.append(f"{entry.nickname} 的 raw 缺失，置顶分级会退化为统一 2 轮")
                print(
                    f"  ✓ {entry.nickname}: {len(page.items)} 条"
                    f"（非置顶 {non_top} / 置顶 {pinned}，count={settings['FETCH_COUNT']}）"
                    f" {elapsed}ms has_more={page.has_more}{note}"
                )

    # 4) 速率余量
    avg = (float(settings["REQUEST_INTERVAL_MIN"]) + float(settings["REQUEST_INTERVAL_MAX"])) / 2
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
        async with DtkClient(settings["DTK_BASE_URL"], settings["DTK_API_KEY"], wait=0) as client:
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

    path = settings.users_conf
    line = f"{candidate}|{nickname or candidate[-8:]}"
    if path.is_file() and candidate in path.read_text(encoding="utf-8"):
        print(f"! 该账号已在 users.conf 中：{candidate}")
        return 1
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
        except (NotImplementedError, AttributeError):  # pragma: no cover - dev on Windows
            signal.signal(sig, lambda s, _f: _request_stop(int(s)))

    started = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("dywatch.started", version=__version__, pid=__import__("os").getpid(), once=once)
    for line in runtime.describe():
        logger.info("config", line=line.strip())
    if settings["WEB_ENABLED"]:
        try:
            from .webui import PanelServer

            panel = PanelServer(settings)
            panel.start()
            for url in panel.urls(settings):
                logger.info("panel.listening", url=url)
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
        print("没有任何可用渠道（检查 NOTIFY_CHANNELS 与对应的凭据）")
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
    settings = _load(args)
    command = args.command or "run"

    if command == "config-check":
        return cmd_config_check(settings)
    if command == "status":
        return cmd_status(settings)
    if command in ("doctor", "add", "test-notify"):
        runner = {
            "doctor": lambda: cmd_doctor(settings),
            "add": lambda: cmd_add(settings, args.target, args.nickname),
            "test-notify": lambda: cmd_test_notify(settings),
        }[command]
        return asyncio.run(runner())

    # run / once：需要单实例锁
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
    return asyncio.run(cmd_run(settings, once=True))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
