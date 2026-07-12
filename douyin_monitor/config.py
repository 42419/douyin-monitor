"""配置管理：.env 解析和 Config 数据类。"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from dotenv import dotenv_values


# =================== 路径配置 ===================
import os

_DEFAULT_WORK_DIR = Path(__file__).resolve().parent.parent


def _resolve_work_dir() -> Path:
    """支持 DOUYIN_MONITOR_HOME 环境变量覆盖工作目录。"""
    custom = os.environ.get("DOUYIN_MONITOR_HOME")
    return Path(custom) if custom else _DEFAULT_WORK_DIR


WORK_DIR = _resolve_work_dir()
USERS_CONF = WORK_DIR / "users.conf"
STATE_DIR = WORK_DIR / "state"
LOG_DIR = WORK_DIR / "log"
LOG_INFO_DIR = LOG_DIR / "info"
LOG_DEBUG_DIR = LOG_DIR / "debug"
LOG_INFO_FILE = LOG_INFO_DIR / "monitor.log"
LOG_DEBUG_FILE = LOG_DEBUG_DIR / "monitor.log"
ENV_FILE = WORK_DIR / ".env"
STATUS_FILE = WORK_DIR / "status.json"
PID_FILE = WORK_DIR / "monitor.pid"


# =================== 固定参数 ===================
KNOWN_IDS_MAX = 50
LOG_MAX_SIZE = 10 * 1024 * 1024
LOG_KEEP = 3
MAX_CONSECUTIVE_FAILS = 5
FAIL_COOLDOWN = 300
# "响应哈希连续不变"这种细粒度检测仍然按冷却时间重复提醒（默认 24 小时一次）。
# "长期无新视频"兜底检测改成一次性提醒（见 monitor.py 的 _check_stale），
# 不再需要冷却时间常量。
STALE_HASH_ALERT_COOLDOWN = 24 * 3600
STALE_FALLBACK_DAYS = 14
HTTP_TIMEOUT = 10
DEFAULT_API_URL = "http://localhost/api/douyin/web/fetch_user_post_videos"
DELETE_CONFIRM_ROUNDS = 2
DELETE_CONFIRM_ROUNDS_TOP = 3
USER_REQUEST_INTERVAL_MIN = 3
USER_REQUEST_INTERVAL_MAX = 8


# =================== .env 解析 ===================
def load_env(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


# =================== 配置 ===================
DEFAULT_NOTIFY_CHANNELS = ["dingtalk"]
DEFAULT_BARK_SERVER = "https://api.day.app"


@dataclass
class Config:
    dingtalk_token: str = ""
    dingtalk_secret: str = ""
    api_url: str = DEFAULT_API_URL
    stale_threshold: int = 7 * 86400
    fetch_count: int = 10
    at_mobiles: List[str] = field(default_factory=list)
    log_level: str = "INFO"
    max_concurrent_users: int = 5
    poll_interval_min: int = 15
    poll_interval_max: int = 40
    notify_channels: List[str] = field(default_factory=lambda: list(DEFAULT_NOTIFY_CHANNELS))
    bark_server: str = DEFAULT_BARK_SERVER
    bark_device_key: str = ""
    wecom_webhook_key: str = ""
    serverchan_sendkey: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    web_enabled: bool = False
    web_host: str = "127.0.0.1"
    web_port: int = 8787

    @classmethod
    def load(cls) -> "Config":
        if not ENV_FILE.exists():
            logging.error(f"错误：环境变量文件未找到 {ENV_FILE}")
            sys.exit(1)
        env = load_env(ENV_FILE)

        notify_channels = [
            c.strip().lower()
            for c in env.get("NOTIFY_CHANNELS", "").split(",")
            if c.strip()
        ] or list(DEFAULT_NOTIFY_CHANNELS)

        token = env.get("DINGTALK_TOKEN", "")
        secret = env.get("DINGTALK_SECRET", "")
        if "dingtalk" in notify_channels:
            if not token:
                logging.error("错误：已启用 dingtalk 渠道，但 .env 中未配置 DINGTALK_TOKEN")
                sys.exit(1)
            if not secret:
                logging.error("错误：已启用 dingtalk 渠道，但 .env 中未配置 DINGTALK_SECRET")
                sys.exit(1)
            if not secret.startswith("SEC"):
                logging.warning(
                    "DINGTALK_SECRET 不是以 SEC 开头，这通常意味着填错了密钥"
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
                logging.warning(f".env 中 {key}={val!r} 不是合法整数，使用默认值 {default}")
                return default

        log_level = (env.get("LOG_LEVEL") or "INFO").upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            logging.warning(f".env 中 LOG_LEVEL={log_level!r} 不是合法级别，使用默认值 INFO")
            log_level = "INFO"

        max_concurrent_users = _int_env("MAX_CONCURRENT_USERS", 5)
        if max_concurrent_users < 1:
            logging.warning(
                f".env 中 MAX_CONCURRENT_USERS={max_concurrent_users} 不合法，使用默认值 5"
            )
            max_concurrent_users = 5

        poll_interval_min = _int_env("POLL_INTERVAL_MIN", 15)
        poll_interval_max = _int_env("POLL_INTERVAL_MAX", 40)
        if poll_interval_min < 1:
            poll_interval_min = 15
        if poll_interval_max < poll_interval_min:
            poll_interval_max = poll_interval_min + 25

        def _bool_env(key: str, default: bool) -> bool:
            val = env.get(key)
            if val is None or not val.strip():
                return default
            return val.strip().lower() in ("1", "true", "yes", "on")

        web_port = _int_env("WEB_PORT", 8787)
        if not (1 <= web_port <= 65535):
            logging.warning(f".env 中 WEB_PORT={web_port} 不合法，使用默认值 8787")
            web_port = 8787

        return cls(
            dingtalk_token=token,
            dingtalk_secret=secret,
            api_url=env.get("API_URL", DEFAULT_API_URL),
            stale_threshold=_int_env("STALE_THRESHOLD", 7 * 86400),
            fetch_count=_int_env("FETCH_COUNT", 10),
            at_mobiles=at_mobiles,
            log_level=log_level,
            max_concurrent_users=max_concurrent_users,
            poll_interval_min=poll_interval_min,
            poll_interval_max=poll_interval_max,
            notify_channels=notify_channels,
            bark_server=env.get("BARK_SERVER", DEFAULT_BARK_SERVER).rstrip("/") or DEFAULT_BARK_SERVER,
            bark_device_key=env.get("BARK_DEVICE_KEY", ""),
            wecom_webhook_key=env.get("WECOM_WEBHOOK_KEY", ""),
            serverchan_sendkey=env.get("SERVERCHAN_SENDKEY", ""),
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
            web_enabled=_bool_env("WEB_ENABLED", False),
            web_host=env.get("WEB_HOST", "127.0.0.1"),
            web_port=web_port,
        )


# =================== users.conf ===================
def load_users_conf(path: Path) -> List[Tuple[str, str]]:
    """格式：sec_user_id|nickname，# 开头为注释，空行忽略。按 sec_user_id 去重。"""
    if not path.exists():
        return []
    users: List[Tuple[str, str]] = []
    seen_ids: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        sec_user_id, nickname = parts[0].strip(), parts[1].strip()
        if not sec_user_id or not nickname:
            continue
        if sec_user_id in seen_ids:
            logging.warning(
                f"users.conf 中 sec_user_id={sec_user_id} 重复配置"
                f"（已保留昵称「{seen_ids[sec_user_id]}」，忽略「{nickname}」），建议清理配置文件"
            )
            continue
        seen_ids[sec_user_id] = nickname
        users.append((sec_user_id, nickname))
    return users
