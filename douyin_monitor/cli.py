"""命令行入口：主循环、信号处理、PID 锁、状态查看。"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import random
import signal
import sys
import threading
from typing import List, Tuple

from .config import (
    Config,
    PID_FILE,
    STATUS_FILE,
    USER_REQUEST_INTERVAL_MAX,
    USER_REQUEST_INTERVAL_MIN,
    USERS_CONF,
    load_users_conf,
)
from .logging_setup import setup_logging
from .monitor import Monitor
from .notifiers import build_notifier
from .pacer import RequestPacer
from .utils import now_str
from .webui import start_web_server

# 全局停止事件
stop_event = threading.Event()


# =================== PID 锁 ===================
def acquire_lock():
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    fp = open(PID_FILE, "w")
    try:
        import fcntl

        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        # Windows: 跳过文件锁（fcntl 不可用）
        pass
    except OSError:
        print("监控脚本已在运行，请勿重复启动", file=sys.stderr)
        sys.exit(1)
    fp.write(str(os.getpid()))
    fp.flush()
    return fp


# =================== 信号处理 ===================
def _handle_signal(signum, _frame):
    logging.info(f"收到退出信号 ({signum})，准备优雅停止...")
    stop_event.set()


# =================== --status ===================
def print_status() -> None:
    if not STATUS_FILE.exists():
        print("暂无状态数据（脚本可能未运行过）")
        return
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("状态文件解析失败")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


# =================== 临时文件清理 ===================
def cleanup_orphaned_tmp_files() -> None:
    from .config import STATE_DIR

    if STATE_DIR.exists():
        for f in STATE_DIR.glob(".*.json.tmp.*"):
            try:
                f.unlink()
                logging.info(f"清理残留临时文件: {f.name}")
            except OSError:
                pass

    status_tmp = STATUS_FILE.with_suffix(".tmp")
    if status_tmp.exists():
        try:
            status_tmp.unlink()
            logging.info(f"清理残留临时文件: {status_tmp.name}")
        except OSError:
            pass


# =================== 主循环 ===================
def run_loop(once: bool = False) -> None:
    from .config import STATE_DIR

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    console_handler = setup_logging()
    cleanup_orphaned_tmp_files()
    lock_fp = acquire_lock()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    cfg = Config.load()
    console_handler.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    try:
        notifier = build_notifier(cfg)
    except ValueError as e:
        logging.error(f"错误：{e}")
        sys.exit(1)
    monitor = Monitor(cfg, notifier, stop_event)

    if cfg.web_enabled:
        try:
            start_web_server(cfg.web_host, cfg.web_port, stop_event, cfg.notify_channels)
        except OSError as e:
            logging.error(f"状态面板启动失败（端口 {cfg.web_port} 可能被占用）: {e}")

    logging.info(
        f"抖音监控服务已启动（PID {os.getpid()}，推送渠道: {', '.join(cfg.notify_channels)}）"
    )
    logging.info(
        f"过时检测: API 响应不变 {cfg.stale_threshold // 86400} 天 | 兜底 14 天 "
        f"| 抓取窗口 {cfg.fetch_count} 条/用户 "
        f"| 用户间请求间隔 {USER_REQUEST_INTERVAL_MIN}~{USER_REQUEST_INTERVAL_MAX}秒 "
        f"| 轮询间隔 {cfg.poll_interval_min}~{cfg.poll_interval_max}秒 "
        f"(并发安全阀 {cfg.max_concurrent_users}) "
        f"| 终端日志级别 {cfg.log_level}"
    )

    last_users_mtime = None
    cached_users: List[Tuple[str, str]] = []
    request_pacer = RequestPacer(USER_REQUEST_INTERVAL_MIN, USER_REQUEST_INTERVAL_MAX, stop_event)

    while not stop_event.is_set():
        try:
            mtime = USERS_CONF.stat().st_mtime
        except OSError:
            mtime = None

        if mtime is None:
            cached_users = []
            last_users_mtime = None
        elif last_users_mtime is None or mtime != last_users_mtime:
            cached_users = load_users_conf(USERS_CONF)
            if last_users_mtime is not None:
                logging.info("检测到 users.conf 变更，已重新加载")
            last_users_mtime = mtime

        users = cached_users
        if not users:
            logging.warning(f"users.conf 为空或不存在: {USERS_CONF}")

        checked = 0
        round_new = 0
        round_init = 0
        round_deleted = 0
        round_fail = 0
        round_title_changed = 0

        def _check_one(sec_user_id: str, nickname: str) -> dict:
            request_pacer.wait_for_turn()
            if stop_event.is_set():
                return {"status": "skipped"}
            try:
                return monitor.check_user(sec_user_id, nickname)
            except Exception:
                logging.exception(f"检查用户 {nickname} 时发生未捕获异常")
                return {"status": "fail", "new_count": 0, "deleted_count": 0}

        if users and not stop_event.is_set():
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, cfg.max_concurrent_users)
            ) as pool:
                future_to_nickname = {
                    pool.submit(_check_one, sec_user_id, nickname): nickname
                    for sec_user_id, nickname in users
                }
                for future in concurrent.futures.as_completed(future_to_nickname):
                    nickname = future_to_nickname[future]
                    try:
                        result = future.result()
                    except Exception:
                        logging.exception(f"检查用户 {nickname} 时线程本身异常")
                        result = {"status": "fail", "new_count": 0, "deleted_count": 0}

                    status = result.get("status")
                    if status == "skipped":
                        continue
                    checked += 1
                    if status == "init":
                        round_init += 1
                    else:
                        round_new += result.get("new_count", 0)
                    round_deleted += result.get("deleted_count", 0)
                    round_title_changed += result.get("title_changed_count", 0)
                    if status == "fail":
                        round_fail += 1

        monitor.write_status_snapshot(users)

        summary = f"本轮完成：检查 {checked} 个用户"
        if round_init:
            summary += f"，新增初始化 {round_init} 个"
        if round_new:
            summary += f"，新视频 {round_new} 条"
        if round_deleted:
            summary += f"，删除 {round_deleted} 条"
        if round_title_changed:
            summary += f"，标题变更 {round_title_changed} 条"
        if round_fail:
            summary += f"，{round_fail} 个用户请求失败"
        if not (round_init or round_new or round_deleted or round_fail or round_title_changed):
            summary += "，均无变化"

        if once:
            logging.info(summary)
            break

        wait_time = cfg.poll_interval_min + random.randint(0, cfg.poll_interval_max - cfg.poll_interval_min)
        logging.info(f"{summary}，等待 {wait_time} 秒...")
        stop_event.wait(wait_time)

    logging.info("监控脚本已停止")


# =================== main ===================
def main() -> None:
    args = sys.argv[1:]
    if "--status" in args:
        print_status()
        return
    once = "--once" in args
    run_loop(once=once)
