#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音多用户视频更新监听脚本
========================================
定期检查多个抖音账号是否发布了新视频或删除了旧视频，并通过钉钉群机器人推送通知。

依赖：requests、python-dotenv、DingtalkChatbot（见同目录 requirements.txt）。

核心特性：
  - 多用户监控，users.conf 支持热加载（运行中修改无需重启脚本）
  - 新视频 / 删除视频检测，自动区分"真实删除"与"被新视频挤出抓取窗口"两种情况
  - 置顶视频的置顶状态变化只做静默同步，不会被误判为新增或删除
  - 连续请求失败告警与自动恢复通知，均带冷却时间，避免刷屏
  - 两级 Cookie 失效检测：API 响应内容长时间无变化 + 长期无新视频兜底
  - 每个监控账号的运行状态独立保存为一个 JSON 文件，重启不丢失
  - 日志拆分为 info/debug 两个文件夹保存，各自独立轮转并自动 gzip 压缩归档
  - PID 文件锁防止重复启动，支持 SIGTERM/SIGINT 优雅退出

用法：
    python3 douyin_monitor.py            # 常驻监控
    python3 douyin_monitor.py --once     # 只检测一轮后退出（便于调试/接入 cron）
    python3 douyin_monitor.py --status   # 查看最近一次状态快照

详细的安装、配置、部署说明见同目录下的 README.md。
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import logging
import logging.handlers
import os
import random
import shutil
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import dotenv_values
from dingtalkchatbot.chatbot import ActionCard, CardItem, DingtalkChatbot

# =================== 路径配置 ===================
WORK_DIR = Path(os.environ.get("DOUYIN_MONITOR_HOME", "/opt/douyin-monitor"))
USERS_CONF = WORK_DIR / "users.conf"
STATE_DIR = WORK_DIR / "state"
LOG_DIR = WORK_DIR / "log"
LOG_INFO_DIR = LOG_DIR / "info"     # 只存 INFO 及以上级别（关键事件 + 每轮汇总）
LOG_DEBUG_DIR = LOG_DIR / "debug"   # 存全部级别（含逐用户调试细节），用于深入排查
LOG_INFO_FILE = LOG_INFO_DIR / "monitor.log"
LOG_DEBUG_FILE = LOG_DEBUG_DIR / "monitor.log"
ENV_FILE = WORK_DIR / ".env"
STATUS_FILE = WORK_DIR / "status.json"
PID_FILE = WORK_DIR / "monitor.pid"

# =================== 固定参数 ===================
KNOWN_IDS_MAX = 50                 # 每用户已知视频 ID 保留上限
LOG_MAX_SIZE = 10 * 1024 * 1024    # 单个日志文件最大 10MB（info/debug 各自独立计算）
LOG_KEEP = 3                       # 保留最近 3 个归档（自动 gzip 压缩存档）
MAX_CONSECUTIVE_FAILS = 5          # 连续失败 N 次触发告警
FAIL_COOLDOWN = 300                # 失败告警冷却时间（秒）
STALE_ALERT_COOLDOWN = 3600        # 过时告警冷却时间（秒）
STALE_FALLBACK_DAYS = 14           # 无新视频兜底告警天数
HTTP_TIMEOUT = 10                  # 单次 HTTP 请求超时（秒）
DEFAULT_API_URL = "http://localhost/api/douyin/web/fetch_user_post_videos"

# DingtalkChatbot 库内部发请求时没有显式设置超时（requests.post 不传 timeout），
# 这里用全局 socket 超时兜底，避免钉钉接口异常挂起时把整个监控主循环卡死。
# 我们自己发起的请求（fetch_user_videos）已经单独传了 timeout，不受影响。
socket.setdefaulttimeout(HTTP_TIMEOUT)

stop_event = threading.Event()


# =================== 时间工具 ===================
def now() -> datetime:
    return datetime.now()


def now_iso() -> str:
    return now().isoformat()


def now_str() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def seconds_since(iso_str: Optional[str]) -> Optional[float]:
    dt = parse_iso(iso_str)
    if dt is None:
        return None
    delta = (now() - dt).total_seconds()
    return max(0.0, delta)


# =================== .env 解析 ===================
def load_env(path: Path) -> Dict[str, str]:
    """用 python-dotenv 解析 .env 文件（支持引号、export 前缀、注释、空行等写法）。"""
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


# =================== 配置 ===================
@dataclass
class Config:
    dingtalk_token: str
    dingtalk_secret: str
    api_url: str = DEFAULT_API_URL
    stale_threshold: int = 7 * 86400
    fetch_count: int = 10
    at_mobiles: List[str] = field(default_factory=list)
    log_level: str = "INFO"

    @classmethod
    def load(cls) -> "Config":
        if not ENV_FILE.exists():
            logging.error(f"❌ 错误：环境变量文件未找到 {ENV_FILE}")
            sys.exit(1)
        env = load_env(ENV_FILE)

        token = env.get("DINGTALK_TOKEN")
        if not token:
            logging.error("❌ 错误：.env 中未配置 DINGTALK_TOKEN")
            sys.exit(1)
        secret = env.get("DINGTALK_SECRET")
        if not secret:
            logging.error("❌ 错误：.env 中未配置 DINGTALK_SECRET")
            sys.exit(1)
        if not secret.startswith("SEC"):
            logging.warning(
                "⚠️ DINGTALK_SECRET 不是以 SEC 开头，这通常意味着填错了密钥"
                "（钉钉「加签」密钥都是 SEC 开头）。如果机器人安全设置选的是「加签」，"
                "请去钉钉群机器人设置页面重新复制正确的密钥，否则推送会一直签名失败。"
            )

        at_mobiles = [m.strip() for m in env.get("AT_MOBILES", "").split(",") if m.strip()]

        def _int_env(key: str, default: int) -> int:
            val = env.get(key)
            if not val:
                return default
            try:
                return int(val)
            except ValueError:
                logging.warning(f"⚠️ .env 中 {key}={val!r} 不是合法整数，使用默认值 {default}")
                return default

        log_level = (env.get("LOG_LEVEL") or "INFO").upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            logging.warning(f"⚠️ .env 中 LOG_LEVEL={log_level!r} 不是合法级别，使用默认值 INFO")
            log_level = "INFO"

        return cls(
            dingtalk_token=token,
            dingtalk_secret=secret,
            api_url=env.get("API_URL", DEFAULT_API_URL),
            stale_threshold=_int_env("STALE_THRESHOLD", 7 * 86400),
            fetch_count=_int_env("FETCH_COUNT", 10),
            at_mobiles=at_mobiles,
            log_level=log_level,
        )


# =================== users.conf ===================
def load_users_conf(path: Path) -> List[Tuple[str, str]]:
    """格式：sec_user_id|nickname，# 开头为注释，空行忽略。"""
    if not path.exists():
        return []
    users: List[Tuple[str, str]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        sec_user_id, nickname = parts[0].strip(), parts[1].strip()
        if sec_user_id and nickname:
            users.append((sec_user_id, nickname))
    return users


# =================== 每用户状态（JSON，按 sec_user_id 命名） ===================
class UserState:
    """
    每个用户一个 JSON 状态文件：STATE_DIR/{sec_user_id}.json
    修复点：所有运行时状态（失败计数/告警冷却/响应哈希等）都用 sec_user_id 做 key，
    使用 sec_user_id 而非昵称命名，避免昵称重复或被用户修改导致状态混淆。
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: Dict = self._load()

    def _load(self) -> Dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logging.warning(f"⚠️ 状态文件损坏，将重新初始化: {self.path}")
        return {
            "sec_user_id": None,
            "nickname": None,
            "initialized_at": None,
            "last_update_at": None,
            "videos": {},  # video_id -> {title, create_time, is_top}
            "consecutive_fails": 0,
            "last_fail_alert_at": None,
            "fail_alerted": False,
            "resp_hash": None,
            "resp_hash_since": None,
            "last_stale_alert_at": None,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # 原子替换


# =================== markdown 转义 ===================
def md_escape(text: Optional[str]) -> str:
    if not text:
        return ""
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


# =================== 钉钉群机器人推送 ===================
class DingTalkNotifier:
    """钉钉群自定义机器人推送，底层用 DingtalkChatbot 库处理加签、限流和消息格式，
    这里只负责把"新视频/删除/告警"这几种业务场景拼成对应的消息内容。"""

    def __init__(self, token: str, secret: str, default_at_mobiles: Optional[List[str]] = None):
        webhook = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
        self._bot = DingtalkChatbot(webhook, secret=secret)
        self.default_at_mobiles = default_at_mobiles or []

    def _safe_send(self, fn, *args, **kwargs) -> bool:
        """统一处理发送结果：DingtalkChatbot 在网络异常/参数异常时会抛异常，
        这里兜底捕获，绝不让一次推送失败拖垮整个监控主循环。"""
        try:
            result = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - 推送失败不应让监控主流程崩溃
            logging.error(f"❌ 钉钉推送异常: {e}")
            return False
        errcode = result.get("errcode", 0) if isinstance(result, dict) else 0
        if errcode == 0:
            logging.info("📤 钉钉推送成功")
            return True
        logging.error(f"❌ 钉钉返回错误: errcode={errcode} errmsg={result.get('errmsg')}")
        return False

    def send_text(self, title: str, content: str, at_mobiles: Optional[List[str]] = None) -> bool:
        mobiles = at_mobiles if at_mobiles is not None else self.default_at_mobiles
        text = f"#### {title}\n\n{content}"
        return self._safe_send(self._bot.send_markdown, title=title, text=text, at_mobiles=mobiles)

    def send_video(
        self,
        nickname: str,
        video_id: str,
        title: str,
        create_time: int,
        cover_url: Optional[str] = None,
    ) -> bool:
        time_str = (
            datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S")
            if create_time
            else "未知"
        )
        video_url = f"https://www.douyin.com/video/{video_id}"
        # 把视频封面图嵌入到钉钉 actionCard 的 markdown 文本中
        cover_md = f"![cover]({cover_url})\n\n" if cover_url else ""
        text = (
            f"{cover_md}**作者：** {md_escape(nickname)}\n\n"
            f"**标题：** {md_escape(title)}\n\n"
            f"**发布时间：** {time_str}\n\n"
            f"**检测时间：** {now_str()}"
        )
        # btns 只传一个按钮时，DingtalkChatbot 会自动生成"整体跳转"样式的
        # actionCard（即 singleTitle/singleURL），和原来手写的效果一致。
        card = ActionCard(
            title=f"{nickname} 发布新视频",
            text=text,
            btns=[CardItem(title="观看视频", url=video_url)],
        )
        return self._safe_send(self._bot.send_action_card, card)

    def send_deleted(
        self,
        nickname: str,
        deleted_entries: List[Tuple[str, dict]],
        at_mobiles: Optional[List[str]] = None,
    ) -> bool:
        lines = []
        for _vid, meta in deleted_entries:
            title = md_escape(meta.get("title") or "(标题未知)")
            date_str = ""
            ct = meta.get("create_time")
            if ct:
                date_str = datetime.fromtimestamp(ct).strftime("%Y-%m-%d")
            lines.append(f"- **{title}**" + (f" ({date_str})" if date_str else ""))

        content = (
            f"用户：**{nickname}**\n\n"
            f"删除数量：**{len(deleted_entries)}** 条\n\n"
            f"被删除的视频：\n" + "\n".join(lines) + "\n\n---\n"
            f"⏰ {now_str()}"
        )
        return self.send_text(f"🗑️ {nickname} 删除了视频", content, at_mobiles)


# =================== API 请求 ===================
def fetch_user_videos(api_url: str, sec_user_id: str, count: int) -> Tuple[int, str]:
    """返回 (http_code, raw_text)。http_code == 0 表示网络层异常（raw_text 为异常说明）。"""
    try:
        resp = requests.get(
            api_url,
            params={"sec_user_id": sec_user_id, "count": count},
            headers={"User-Agent": "douyin-monitor-py/1.0"},
            timeout=HTTP_TIMEOUT,
        )
        return resp.status_code, resp.text
    except requests.exceptions.RequestException as e:
        return 0, str(e)


# =================== 监控核心逻辑 ===================
class Monitor:
    def __init__(self, cfg: Config, notifier: DingTalkNotifier):
        self.cfg = cfg
        self.notifier = notifier

    # ---- 失败 / 恢复处理 ----
    def _on_fail(self, state: UserState, nickname: str) -> None:
        fails = state.data.get("consecutive_fails", 0) + 1
        state.data["consecutive_fails"] = fails
        logging.warning(f"⚠️ 用户 {nickname} API 连续失败: {fails}/{MAX_CONSECUTIVE_FAILS}")

        if fails >= MAX_CONSECUTIVE_FAILS:
            elapsed = seconds_since(state.data.get("last_fail_alert_at"))
            if elapsed is None or elapsed >= FAIL_COOLDOWN:
                logging.error(f"🚨 用户 {nickname} 连续失败 {fails} 次")
                self.notifier.send_text(
                    "🚨 抖音监控异常",
                    f"用户 **{nickname}** 连续 **{fails}** 次请求失败\n\n"
                    "可能原因：\n"
                    "- 抖音 Cookie 已过期，需更新\n"
                    "- API 服务未运行或异常\n"
                    "- 网络问题\n\n"
                    f"请尽快检查！\n\n⏱ {now_str()}",
                )
                state.data["last_fail_alert_at"] = now_iso()
                state.data["fail_alerted"] = True

    def _on_success(self, state: UserState, nickname: str) -> None:
        fails = state.data.get("consecutive_fails", 0)
        if fails >= MAX_CONSECUTIVE_FAILS and state.data.get("fail_alerted"):
            logging.info(f"✅ 用户 {nickname} API 已恢复正常（之前连续失败 {fails} 次）")
            self.notifier.send_text(
                f"✅ 用户 {nickname} 监控已恢复",
                f"用户 **{nickname}** 之前连续失败 **{fails}** 次，现已恢复正常。\n\n⏱ {now_str()}",
            )
            state.data["fail_alerted"] = False
        state.data["consecutive_fails"] = 0

    # ---- 过时 / Cookie 失效检测 ----
    def _check_stale(self, state: UserState, nickname: str, aweme_list: Optional[list]) -> None:
        # 检测 1：API 响应哈希连续不变（修复：不再因"从未更新过"而整体跳过此检测）
        if aweme_list is not None:
            ids_sorted = sorted(item.get("aweme_id", "") for item in aweme_list)
            resp_hash = hashlib.sha256("\n".join(ids_sorted).encode("utf-8")).hexdigest()
            prev_hash = state.data.get("resp_hash")

            if prev_hash == resp_hash and prev_hash is not None:
                elapsed = seconds_since(state.data.get("resp_hash_since")) or 0
                if elapsed >= self.cfg.stale_threshold:
                    hours = int(elapsed // 3600)
                    last_alert_elapsed = seconds_since(state.data.get("last_stale_alert_at"))
                    if last_alert_elapsed is None or last_alert_elapsed >= STALE_ALERT_COOLDOWN:
                        logging.error(
                            f"🚨 用户 {nickname} API 响应连续 {hours} 小时无变化，疑似 Cookie 过期"
                        )
                        self.notifier.send_text(
                            "🚨 抖音数据可能过时",
                            f"用户 **{nickname}** 的 API 响应已连续 **{hours}** 小时未变化\n\n"
                            "强烈疑似 Cookie 已过期，API 返回缓存旧数据\n\n"
                            "建议：更新 Cookie 后重启监控脚本\n\n"
                            f"⏱ {now_str()}",
                        )
                        state.data["last_stale_alert_at"] = now_iso()
            else:
                state.data["resp_hash"] = resp_hash
                state.data["resp_hash_since"] = now_iso()

        # 检测 2：长期无新视频兜底
        last_update = state.data.get("last_update_at") or state.data.get("initialized_at")
        elapsed_days = seconds_since(last_update)
        if elapsed_days is not None and elapsed_days >= STALE_FALLBACK_DAYS * 86400:
            # 优化：若已有未恢复的"连续失败"告警在生效，跳过此提醒，避免重复刷群
            if not state.data.get("fail_alerted"):
                last_alert_elapsed = seconds_since(state.data.get("last_stale_alert_at"))
                if last_alert_elapsed is None or last_alert_elapsed >= STALE_ALERT_COOLDOWN:
                    days = int(elapsed_days // 86400)
                    logging.info(f"ℹ️ 用户 {nickname} 已 {days} 天无新视频")
                    self.notifier.send_text(
                        "ℹ️ 长期无更新提醒",
                        f"用户 **{nickname}** 已 **{days}** 天没有发布新视频\n\n"
                        "可能是用户近期未更新，或 Cookie 已过期\n\n"
                        f"⏱ {now_str()}",
                    )
                    state.data["last_stale_alert_at"] = now_iso()

    # ---- 单用户检测主逻辑 ----
    def check_user(self, sec_user_id: str, nickname: str) -> dict:
        """返回本次检测结果，供主循环汇总成一行轮次摘要日志，
        不再需要在每个用户身上都打印一遍"检查中/无更新"。"""
        state = UserState(STATE_DIR / f"{sec_user_id}.json")
        result = {"status": "ok", "new_count": 0, "deleted_count": 0}
        try:
            inner_result = self._check_user_inner(state, sec_user_id, nickname)
            if inner_result:
                result = inner_result
        finally:
            state.save()
        return result

    def _check_user_inner(self, state: UserState, sec_user_id: str, nickname: str) -> dict:
        state.data["sec_user_id"] = sec_user_id
        state.data["nickname"] = nickname  # 昵称可能变化，始终保持最新展示名

        # 优化：逐用户的"开始检查"噪音日志降级为 DEBUG。这类记录始终完整保存在
        # log/debug/monitor.log 里（排查问题去那边翻），但不会进 log/info/monitor.log，
        # 也不会刷屏终端（除非把 .env 里 LOG_LEVEL 设成 DEBUG）。
        logging.debug(f"🔍 检查用户: {nickname}")
        http_code, raw = fetch_user_videos(self.cfg.api_url, sec_user_id, self.cfg.fetch_count)

        if http_code == 0:
            logging.error(f"❌ 用户 {nickname} 网络请求异常: {raw}")
            self._on_fail(state, nickname)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}
        if http_code != 200:
            logging.error(f"❌ 用户 {nickname} API HTTP 错误: {http_code}")
            self._on_fail(state, nickname)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}
        if not raw:
            logging.error(f"❌ 用户 {nickname} API 响应体为空")
            self._on_fail(state, nickname)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}

        try:
            resp = json.loads(raw)
        except json.JSONDecodeError:
            logging.error(f"❌ 用户 {nickname} API 响应非合法 JSON")
            self._on_fail(state, nickname)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}

        if resp.get("code") != 200:
            logging.error(f"❌ 用户 {nickname} API 返回错误: {resp.get('msg', '未知错误')}")
            self._on_fail(state, nickname)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}

        aweme_list = (resp.get("data") or {}).get("aweme_list") or []
        if not aweme_list:
            logging.warning(f"⚠️ 用户 {nickname} API 返回空列表，可能 Cookie 过期")
            self._on_fail(state, nickname)
            self._check_stale(state, nickname, aweme_list)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}

        # API 正常
        self._on_success(state, nickname)

        videos: Dict[str, dict] = state.data.setdefault("videos", {})
        current_map: Dict[str, dict] = {}
        for item in aweme_list:
            vid = item.get("aweme_id")
            if not vid:
                continue
            current_map[vid] = {
                "title": item.get("item_title") or item.get("desc") or "无标题",
                "create_time": int(item.get("create_time") or 0),
                "is_top": bool(item.get("is_top")),
                "cover_url": ((item.get("cover_original_scale") or {}).get("url_list") or [None])[0],
            }

        current_ids = set(current_map)
        known_ids = set(videos)

        # 首次记录：全部加入已知列表，不发通知
        if not videos:
            for vid, meta in current_map.items():
                videos[vid] = {
                    "title": meta["title"],
                    "create_time": meta["create_time"],
                    "is_top": meta["is_top"],
                }
            ts = now_iso()
            state.data["initialized_at"] = ts
            state.data["last_update_at"] = ts
            logging.info(f"🆕 首次记录用户 {nickname}，初始化已知视频列表（{len(current_map)} 条）")
            self._check_stale(state, nickname, aweme_list)
            return {"status": "init", "new_count": len(current_map), "deleted_count": 0}

        # 修复：置顶状态变化只做静默同步，不再触发误判的"新视频"/"删除"通知
        for vid in current_ids & known_ids:
            videos[vid]["is_top"] = current_map[vid]["is_top"]

        new_ids = current_ids - known_ids
        disappeared_ids = known_ids - current_ids

        if not new_ids and not disappeared_ids:
            # 优化：常态"无更新"日志降级为 DEBUG（完整记录在 log/debug/ 里，
            # 不进 log/info/ 的精简日志，避免淹没真正的事件）
            logging.debug(f"✅ 用户 {nickname} 无更新")
            self._check_stale(state, nickname, aweme_list)
            return {"status": "ok", "new_count": 0, "deleted_count": 0}

        content_changed = False
        real_deleted_count = 0

        # --- 删除检测：消失的视频是"被新视频挤出窗口"还是"被用户删除"？ ---
        # 有新视频 → 视为窗口滚动挤出，静默清理；无新视频 → 视为真实删除，告警。
        # （与原脚本相同的启发式，但现在基于完整已知集合，不再受置顶状态干扰）
        if disappeared_ids:
            if new_ids:
                logging.info(
                    f"🔄 用户 {nickname} 有 {len(disappeared_ids)} 条视频被新视频挤出窗口，静默清理"
                )
                for vid in disappeared_ids:
                    videos.pop(vid, None)
            else:
                deleted_entries = [(vid, videos.get(vid, {})) for vid in disappeared_ids]
                logging.info(f"🗑️ 用户 {nickname} 检测到 {len(deleted_entries)} 条视频被删除")
                self.notifier.send_deleted(nickname, deleted_entries, self.cfg.at_mobiles)
                for vid in disappeared_ids:
                    videos.pop(vid, None)
                content_changed = True
                real_deleted_count = len(deleted_entries)
                time.sleep(1)  # 避免钉钉限流

        # --- 新视频通知（按发布时间从早到晚）---
        if new_ids:
            if len(new_ids) >= self.cfg.fetch_count:
                logging.warning(
                    f"⚠️ 用户 {nickname} 本轮新增 {len(new_ids)} 条视频，已达到抓取窗口上限 "
                    f"(FETCH_COUNT={self.cfg.fetch_count})，可能存在漏检，建议调大 FETCH_COUNT"
                )
            new_items = sorted(
                ({**current_map[v], "id": v} for v in new_ids),
                key=lambda x: x["create_time"],
            )
            logging.info(f"🎉 用户 {nickname} 检测到 {len(new_items)} 条新视频")
            for item in new_items:
                self.notifier.send_video(
                    nickname, item["id"], item["title"], item["create_time"], item.get("cover_url")
                )
                videos[item["id"]] = {
                    "title": item["title"],
                    "create_time": item["create_time"],
                    "is_top": item["is_top"],
                }
                time.sleep(1)  # 避免钉钉限流
            content_changed = True

        if content_changed:
            state.data["last_update_at"] = now_iso()

        # 裁剪已知视频列表上限（保留最新的 N 条）
        if len(videos) > KNOWN_IDS_MAX:
            overflow = len(videos) - KNOWN_IDS_MAX
            oldest = sorted(videos.items(), key=lambda kv: kv[1].get("create_time", 0))[:overflow]
            for vid, _ in oldest:
                videos.pop(vid, None)

        return {"status": "ok", "new_count": len(new_ids), "deleted_count": real_deleted_count}


    # ---- 状态快照 ----
    def write_status_snapshot(self, users: List[Tuple[str, str]]) -> None:
        # 直接扫描状态目录而不是只遍历当前 users.conf 列表，这样哪怕某用户
        # 后来从 users.conf 里删掉了，只要状态文件还在，快照里依然能看到它，
        # 便于排查"是不是漏配置/曾经监控过哪些人"。
        current_ids = {sec_user_id for sec_user_id, _ in users}
        entries = []
        if STATE_DIR.exists():
            for path in sorted(STATE_DIR.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                sec_user_id = data.get("sec_user_id") or path.stem
                nickname = data.get("nickname") or sec_user_id
                last_update = data.get("last_update_at") or data.get("initialized_at")
                elapsed = seconds_since(last_update)
                age_hours = int(elapsed // 3600) if elapsed is not None else None
                entries.append(
                    {
                        "sec_user_id": sec_user_id,
                        "nickname": nickname,
                        "last_update": last_update,
                        "initialized_at": data.get("initialized_at"),
                        "known_videos": len(data.get("videos", {})),
                        "hours_since_update": age_hours,
                        "consecutive_fails": data.get("consecutive_fails", 0),
                        "in_users_conf": sec_user_id in current_ids,
                    }
                )

        snapshot = {"timestamp": now_str(), "pid": os.getpid(), "users": entries}
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATUS_FILE)


# =================== 日志 ===================
def _gzip_namer(default_name: str) -> str:
    """轮转后的归档文件名加上 .gz 后缀（monitor.log.1 -> monitor.log.1.gz）。"""
    return default_name + ".gz"


def _gzip_rotator(source: str, dest: str) -> None:
    """实际执行轮转归档：把超过大小上限的日志压缩成 .gz 保存，
    减少长期运行后归档日志占用的磁盘空间。"""
    with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(source)


def _make_rotating_handler(path: Path, level: int) -> logging.handlers.RotatingFileHandler:
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_MAX_SIZE, backupCount=LOG_KEEP, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.rotator = _gzip_rotator
    handler.namer = _gzip_namer
    return handler


def setup_logging(console_level: str = "INFO") -> logging.StreamHandler:
    """
    日志分两路保存：
      - log/info/monitor.log  只记录 INFO 及以上级别（关键事件 + 每轮汇总），日常查看用
      - log/debug/monitor.log 记录全部级别（含逐用户调试细节），排查问题时翻这个
    两边都达到 LOG_MAX_SIZE 后各自独立轮转，归档文件自动 gzip 压缩（monitor.log.1.gz ...），
    保留最近 LOG_KEEP 份。

    根 logger 始终设为 DEBUG，让所有日志先无差别地往下传，具体输出到哪里、
    以什么级别过滤，交给各个 handler 自己决定 —— 这样无需重启脚本，
    debug 文件夹永远有完整记录可查。

    返回控制台 handler，方便调用方之后用 LOG_LEVEL 配置项调整终端可见的详细程度
    （不影响两个文件的内容，文件始终各自完整记录该级别应有的内容）。
    """
    LOG_INFO_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # requests 底层的 urllib3 在 DEBUG 级别会打印连接池细节（"Starting new HTTP
    # connection"之类），跟我们自己的根 logger 共用一套 handler 时会被一起写进
    # debug 文件夹，纯属传输层噪音、没有排查价值，这里单独把它调高到 WARNING。
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    info_handler = _make_rotating_handler(LOG_INFO_FILE, logging.INFO)
    info_handler.setFormatter(fmt)
    logger.addHandler(info_handler)

    debug_handler = _make_rotating_handler(LOG_DEBUG_FILE, logging.DEBUG)
    debug_handler.setFormatter(fmt)
    logger.addHandler(debug_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(getattr(logging, console_level, logging.INFO))
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return stream_handler


# =================== PID 锁（防止重复启动） ===================
def acquire_lock():
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    fp = open(PID_FILE, "w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("❌ 监控脚本已在运行，请勿重复启动", file=sys.stderr)
        sys.exit(1)
    fp.write(str(os.getpid()))
    fp.flush()
    return fp  # 必须持有此引用，文件被 GC 关闭会释放锁


# =================== 信号处理 ===================
def _handle_signal(signum, _frame):
    logging.info(f"👋 收到退出信号 ({signum})，准备优雅停止...")
    stop_event.set()


# =================== --status ===================
def print_status() -> None:
    if not STATUS_FILE.exists():
        print("暂无状态数据（脚本可能未运行过）")
        return
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("⚠️ 状态文件解析失败")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


# =================== 主循环 ===================
def cleanup_orphaned_tmp_files() -> None:
    """清理上次进程被强杀（kill -9）时可能残留的状态临时文件。
    用于在进程被强行终止（kill -9）后下次启动时做兜底清理。"""
    if not STATE_DIR.exists():
        return
    for f in STATE_DIR.glob(".*.json.tmp.*"):
        try:
            f.unlink()
            logging.info(f"🧹 清理残留临时文件: {f.name}")
        except OSError:
            pass


def run_loop(once: bool = False) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    console_handler = setup_logging()  # 文件按固定规则记录；终端详细程度稍后按配置调整
    cleanup_orphaned_tmp_files()
    lock_fp = acquire_lock()  # noqa: F841 - 持有引用以保持文件锁
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    cfg = Config.load()
    console_handler.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    notifier = DingTalkNotifier(cfg.dingtalk_token, cfg.dingtalk_secret, cfg.at_mobiles)
    monitor = Monitor(cfg, notifier)

    logging.info(f"✅ 抖音监控服务已启动（PID {os.getpid()}，使用钉钉群机器人推送）")
    logging.info(
        f"📋 过时检测: API 响应不变 {cfg.stale_threshold // 86400} 天 | 兜底 {STALE_FALLBACK_DAYS} 天 "
        f"| 抓取窗口 {cfg.fetch_count} 条/用户 | 终端日志级别 {cfg.log_level}（文件日志始终完整记录，"
        f"info/ 存关键事件，debug/ 存全部细节）"
    )


    last_users_mtime = None
    while not stop_event.is_set():
        users = load_users_conf(USERS_CONF)
        if not users:
            logging.warning(f"⚠️ users.conf 为空或不存在: {USERS_CONF}")
        else:
            try:
                mtime = USERS_CONF.stat().st_mtime
                if last_users_mtime is not None and mtime != last_users_mtime:
                    logging.info("🔄 检测到 users.conf 变更，已重新加载")
                last_users_mtime = mtime
            except OSError:
                pass

        # 优化：不再逐用户打日志，改为按轮次汇总成一行，
        # 大幅降低"长期无事发生"场景下的日志体积（真正的事件——新视频/
        # 删除/失败告警等——仍然各自完整记录，不受此项影响）。
        checked = 0
        round_new = 0
        round_init = 0
        round_deleted = 0
        round_fail = 0
        for sec_user_id, nickname in users:
            if stop_event.is_set():
                break
            try:
                result = monitor.check_user(sec_user_id, nickname)
            except Exception:  # noqa: BLE001 - 单用户异常不应影响其他用户
                logging.exception(f"❌ 检查用户 {nickname} 时发生未捕获异常")
                result = {"status": "fail", "new_count": 0, "deleted_count": 0}
            checked += 1
            status = result.get("status")
            if status == "init":
                # 首次记录基线，不算作"检测到新视频并推送通知"，单独计数避免误导
                round_init += 1
            else:
                round_new += result.get("new_count", 0)
            round_deleted += result.get("deleted_count", 0)
            if status == "fail":
                round_fail += 1
            stop_event.wait(3 + random.randint(0, 5))

        monitor.write_status_snapshot(users)

        summary = f"💤 本轮完成：检查 {checked} 个用户"
        if round_init:
            summary += f"，新增初始化 {round_init} 个"
        if round_new:
            summary += f"，新视频 {round_new} 条"
        if round_deleted:
            summary += f"，删除 {round_deleted} 条"
        if round_fail:
            summary += f"，{round_fail} 个用户请求失败"
        if not (round_init or round_new or round_deleted or round_fail):
            summary += "，均无变化"

        if once:
            logging.info(summary)
            break

        wait_time = 15 + random.randint(0, 25)
        logging.info(f"{summary}，等待 {wait_time} 秒...")
        stop_event.wait(wait_time)

    logging.info("👋 监控脚本已停止")


def main() -> None:
    args = sys.argv[1:]
    if "--status" in args:
        print_status()
        return
    once = "--once" in args
    run_loop(once=once)


if __name__ == "__main__":
    main()
