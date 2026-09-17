"""配置：一张声明式注册表，`.env` 只做覆盖。

照搬 DTK v5 的 `SettingSpec` 思路：所有设置项集中登记（键、默认值、类型、说明），
于是 README 的配置章节、`--config-check` 的输出、以及跨项校验读的都是同一份事实，
不存在"代码里有、文档里没有"或反之的情况。

三条规矩：

* **缺省即合法。** 每一项都有可用的默认值，`.env` 只是覆盖。只有 `DTK_API_KEY`
  是必须由人填的（它的默认值空着，并在启动自检时明确报错）。
* **跨项约束集中在这里。** `DTK_WAIT < DTK_TIMEOUT`、`POLL_INTERVAL_MIN >= 10`
  这类关系放在 `validate()` 里一次说清，而不是散落在用到它们的地方。
* **来源可追溯。** `--config-check` 会打出每个生效值来自默认值、`.env` 还是环境变量；
  排查"我明明改了怎么没生效"时这是唯一需要的东西。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping

#: 通知渠道名 -> 该渠道必填的键（缺失时启动报错，而不是到了要推送时才发现）
CHANNEL_REQUIRED: Final[Mapping[str, tuple[str, ...]]] = {
    "dingtalk": ("DINGTALK_TOKEN", "DINGTALK_SECRET"),
    "wecom": ("WECOM_WEBHOOK_KEY",),
    "bark": ("BARK_DEVICE_KEY",),
    "serverchan": ("SERVERCHAN_SENDKEY",),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    "webhook": ("WEBHOOK_URL",),
}

LOG_LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR")
INCLUDE_RAW_MODES: Final[tuple[str, ...]] = ("auto", "always", "never")

#: 节奏的硬下限。低于 10 秒意味着整体请求速率接近 20 次/分钟，风控暴露面明显上升，
#: 所以配置成更低的值是**拒绝**而不是被悄悄抬高——抬高了等于没告诉你它没按你的意思跑。
MIN_POLL_INTERVAL: Final[int] = 10
#: 抖音单页上限（DTK 自己的 max_page_size 也是 50）
MAX_FETCH_COUNT: Final[int] = 50


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """One entry in the settings table."""

    key: str
    default: Any
    cast: str  # str | int | float | bool | csv
    note: str


SETTINGS: Final[tuple[SettingSpec, ...]] = (
    # ---------------------------------------------------------------- 上游
    SettingSpec("DTK_BASE_URL", "http://127.0.0.1:8000", "str", "DTK v5 的地址"),
    SettingSpec("DTK_API_KEY", "", "str", "DTK API Key，形态 dtk_<12hex>_<32b64>（必填）"),
    SettingSpec("DTK_WAIT", 25, "int", "?wait= 秒数；0 表示走纯异步（202 + 轮询）"),
    SettingSpec("DTK_TIMEOUT", 35, "int", "HTTP 超时秒数，必须大于 DTK_WAIT"),
    SettingSpec("DTK_REFRESH", True, "bool", "必须为 true，否则命中 DTK 列表缓存（300s）"),
    SettingSpec("INCLUDE_RAW", "auto", "str", "置顶标志获取策略：auto / always / never"),
    SettingSpec("RAW_REFRESH_ROUNDS", 20, "int", "auto 模式下最多隔多少轮带一次 include_raw"),
    SettingSpec("DTK_USER_AGENT", "dywatch/0.1", "str", "请求 UA，便于在 DTK 侧日志辨认"),
    # ---------------------------------------------------------------- 节奏
    SettingSpec("REQUEST_INTERVAL_MIN", 3.0, "float", "全局相邻请求最小间隔（秒）"),
    SettingSpec("REQUEST_INTERVAL_MAX", 8.0, "float", "全局相邻请求最大间隔（秒）"),
    SettingSpec("POLL_INTERVAL_MIN", 15, "int", "轮末随机等待下限（秒），不得低于 10"),
    SettingSpec("POLL_INTERVAL_MAX", 40, "int", "轮末随机等待上限（秒）"),
    SettingSpec("MAX_CONCURRENT", 5, "int", "并发检查上限"),
    SettingSpec("FETCH_COUNT", 15, "int", "单页条数（抖音频返回 count + 置顶数）"),
    # ---------------------------------------------------------------- 判定
    SettingSpec("DELETE_CONFIRM_ROUNDS", 2, "int", "普通作品消失确认轮数，不得低于 2"),
    SettingSpec("DELETE_CONFIRM_ROUNDS_TOP", 3, "int", "置顶作品消失确认轮数"),
    SettingSpec("DELETE_CONFIRM_ROUNDS_ALL", 3, "int", "全部作品同时消失的确认轮数"),
    SettingSpec("KNOWN_IDS_MAX", 50, "int", "每账号跟踪的作品上限"),
    SettingSpec("REMOVED_MAX", 200, "int", "tombstone 条数上限"),
    SettingSpec("REMOVED_TTL_DAYS", 7, "int", "tombstone 存活天数"),
    SettingSpec("EMPTY_ROUNDS_ALERT", 3, "int", "连续空列表几轮后告警（never_seen）"),
    SettingSpec("STALE_FALLBACK_DAYS", 14, "int", "长期无新作品的一次性兜底提醒"),
    # ---------------------------------------------------------------- 失败
    SettingSpec("MAX_CONSECUTIVE_FAILS", 5, "int", "连续失败几次告警"),
    SettingSpec("FAIL_COOLDOWN", 300, "int", "同类失败告警冷却（秒）"),
    SettingSpec("BACKOFF_AFTER", 2, "int", "连续失败几次后全局闸门开始翻倍"),
    SettingSpec("BACKOFF_MAX_SECONDS", 600, "int", "全局闸门退避上限（秒）"),
    # ---------------------------------------------------------------- 归档
    SettingSpec("ARCHIVE_ENABLED", True, "bool", "是否用 DTK 归档做删除交叉确认"),
    SettingSpec(
        "ARCHIVE_DOWNLOAD_ENABLED", False, "bool",
        "检测到新作品时是否让 DTK 顺手下载媒体存档（需要 API Key 带 media:write，默认关闭）",
    ),
    SettingSpec(
        "ARCHIVE_DOWNLOAD_PIN", False, "bool",
        "存下来的媒体是否永久保留。false（默认）：DTK 媒体目录默认上限 2G，装满会自动删最旧的，"
        "等于只留最近一批；true：每份都锁定、不自动删，但占满 2G 之后新下载会一直失败，"
        "需要人工去 DTK 删一些腾地方",
    ),
    SettingSpec(
        "ARCHIVE_DOWNLOAD_MAX_PER_ROUND", 10, "int",
        "每轮最多触发几条归档下载。请求会过全局节奏器（3~8 秒一次），所以这个数直接"
        "决定旁路最多把一轮拖长多久；超出预算的条目排队等下一轮，不会丢",
    ),
    # ---------------------------------------------------------------- 通知
    SettingSpec("NOTIFY_CHANNELS", ["dingtalk"], "csv", "启用的渠道，逗号分隔"),
    SettingSpec("SILENT_MODE", False, "bool", "跳过全部推送，监控与面板照常"),
    SettingSpec("NOTIFY_GAP", 1.0, "float", "相邻两条通知的间隔（秒）"),
    SettingSpec("DINGTALK_TOKEN", "", "str", "钉钉机器人 access_token"),
    SettingSpec("DINGTALK_SECRET", "", "str", "钉钉加签密钥（SEC 开头）"),
    SettingSpec("AT_MOBILES", [], "csv", "告警时 @ 的手机号，逗号分隔"),
    SettingSpec("WECOM_WEBHOOK_KEY", "", "str", "企业微信群机器人 key"),
    SettingSpec("BARK_SERVER", "https://api.day.app", "str", "Bark 服务器地址"),
    SettingSpec("BARK_DEVICE_KEY", "", "str", "Bark 设备 Key"),
    SettingSpec("SERVERCHAN_SENDKEY", "", "str", "Server 酱 SendKey"),
    SettingSpec("TELEGRAM_BOT_TOKEN", "", "str", "Telegram Bot token"),
    SettingSpec("TELEGRAM_CHAT_ID", "", "str", "Telegram 目标 chat id"),
    SettingSpec("WEBHOOK_URL", "", "str", "通用 webhook 地址（POST JSON）"),
    # ---------------------------------------------------------------- 面板
    SettingSpec("WEB_ENABLED", False, "bool", "是否启用只读面板"),
    SettingSpec("WEB_HOST", "127.0.0.1", "str", "面板监听地址（无鉴权，默认只听回环）"),
    SettingSpec("WEB_PORT", 8787, "int", "面板监听端口"),
    # ---------------------------------------------------------------- 其他
    SettingSpec("LOG_LEVEL", "INFO", "str", "终端日志级别（不影响日志文件）"),
    SettingSpec("MONITOR_HOME", "", "str", "工作目录；留空则用当前目录"),
    SettingSpec("EVENTS_KEEP_DAYS", 90, "int", "events 审计保留天数"),
    SettingSpec("ROUNDS_KEEP_DAYS", 30, "int", "rounds 汇总保留天数"),
)

SPECS: Final[Mapping[str, SettingSpec]] = {spec.key: spec for spec in SETTINGS}


def _to_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _cast(kind: str, raw: Any) -> Any:
    if kind == "str":
        return str(raw).strip()
    if kind == "csv":
        if isinstance(raw, (list, tuple)):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [part.strip() for part in str(raw).split(",") if part.strip()]
    if kind == "int":
        return int(str(raw).strip())
    if kind == "float":
        return float(str(raw).strip())
    if kind == "bool":
        return _to_bool(str(raw)) if isinstance(raw, str) else bool(raw)
    raise ValueError(f"unknown cast: {kind}")


@dataclass(frozen=True, slots=True)
class Settings:
    """An immutable snapshot of the effective settings."""

    values: Mapping[str, Any]
    sources: Mapping[str, str]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    @property
    def home(self) -> Path:
        raw = str(self.values.get("MONITOR_HOME") or "").strip()
        return Path(raw) if raw else Path.cwd()

    # ---- 路径（都相对工作目录） ------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.home / "data" / "dywatch.db"

    @property
    def users_conf(self) -> Path:
        return self.home / "users.conf"

    @property
    def status_path(self) -> Path:
        return self.home / "data" / "status.json"

    @property
    def pid_path(self) -> Path:
        return self.home / "monitor.pid"

    @property
    def log_dir(self) -> Path:
        return self.home / "log"

    def validate(self) -> list[str]:
        """Cross-field checks. Returns human-readable errors (empty == fine)."""
        v = self.values
        errors: list[str] = []

        if not v["DTK_API_KEY"]:
            errors.append(
                "DTK_API_KEY 未配置 —— 这是唯一必填项，留空时每个账号都会 401，"
                "且不会让进程退出，只会一直安静地失败。先跑 `dywatch doctor` 确认。"
            )

        if not v["DTK_BASE_URL"].startswith(("http://", "https://")):
            errors.append("DTK_BASE_URL 必须以 http:// 或 https:// 开头")
        if v["DTK_WAIT"] < 0:
            errors.append("DTK_WAIT 不能为负")
        if v["DTK_WAIT"] and v["DTK_WAIT"] >= v["DTK_TIMEOUT"]:
            errors.append(
                f"DTK_TIMEOUT({v['DTK_TIMEOUT']}) 必须大于 DTK_WAIT({v['DTK_WAIT']})，"
                "否则长轮询还没返回就先超时了"
            )

        if v["REQUEST_INTERVAL_MIN"] <= 0:
            errors.append("REQUEST_INTERVAL_MIN 必须大于 0")
        if v["REQUEST_INTERVAL_MIN"] > v["REQUEST_INTERVAL_MAX"]:
            errors.append("REQUEST_INTERVAL_MIN 不能大于 REQUEST_INTERVAL_MAX")

        if v["POLL_INTERVAL_MIN"] < MIN_POLL_INTERVAL:
            errors.append(
                f"POLL_INTERVAL_MIN 不得低于 {MIN_POLL_INTERVAL} 秒"
                "（再低只会把风控暴露面拉高，不会带来更好的时间分辨率）"
            )
        if v["POLL_INTERVAL_MIN"] > v["POLL_INTERVAL_MAX"]:
            errors.append("POLL_INTERVAL_MIN 不能大于 POLL_INTERVAL_MAX")

        if not 1 <= v["FETCH_COUNT"] <= MAX_FETCH_COUNT:
            errors.append(f"FETCH_COUNT 必须在 1..{MAX_FETCH_COUNT} 之间")
        if v["MAX_CONCURRENT"] < 1:
            errors.append("MAX_CONCURRENT 至少为 1")

        for key in ("DELETE_CONFIRM_ROUNDS", "DELETE_CONFIRM_ROUNDS_TOP", "DELETE_CONFIRM_ROUNDS_ALL"):
            if v[key] < 2:
                errors.append(f"{key} 不得低于 2（1 轮会把接口抖动直接当成删除）")
        if v["DELETE_CONFIRM_ROUNDS_TOP"] < v["DELETE_CONFIRM_ROUNDS"]:
            errors.append("DELETE_CONFIRM_ROUNDS_TOP 不应小于 DELETE_CONFIRM_ROUNDS")

        if v["KNOWN_IDS_MAX"] < 2:
            errors.append("KNOWN_IDS_MAX 至少为 2")
        if v["REMOVED_MAX"] < 1:
            errors.append("REMOVED_MAX 至少为 1")

        if v["INCLUDE_RAW"] not in INCLUDE_RAW_MODES:
            errors.append(f"INCLUDE_RAW 只能是 {' / '.join(INCLUDE_RAW_MODES)}")
        if v["LOG_LEVEL"] not in LOG_LEVELS:
            errors.append(f"LOG_LEVEL 只能是 {' / '.join(LOG_LEVELS)}")
        if not 1 <= v["WEB_PORT"] <= 65535:
            errors.append("WEB_PORT 必须在 1..65535 之间")

        unknown = [c for c in v["NOTIFY_CHANNELS"] if c not in CHANNEL_REQUIRED]
        if unknown:
            errors.append(
                f"NOTIFY_CHANNELS 里有未知渠道 {unknown}；"
                f"可用：{' / '.join(sorted(CHANNEL_REQUIRED))}"
            )
        if not v["SILENT_MODE"]:
            if not v["NOTIFY_CHANNELS"]:
                errors.append("NOTIFY_CHANNELS 为空且未开启 SILENT_MODE，将没有任何推送渠道")
            for channel in v["NOTIFY_CHANNELS"]:
                missing = [k for k in CHANNEL_REQUIRED.get(channel, ()) if not v.get(k)]
                if missing:
                    errors.append(f"渠道 {channel} 缺少必填项：{', '.join(missing)}")

        if v["BARK_SERVER"] and not v["BARK_SERVER"].startswith(("http://", "https://")):
            errors.append("BARK_SERVER 必须以 http:// 或 https:// 开头")
        if v["WEBHOOK_URL"] and not v["WEBHOOK_URL"].startswith(("http://", "https://")):
            errors.append("WEBHOOK_URL 必须以 http:// 或 https:// 开头")

        return errors

    def warnings(self) -> list[str]:
        """Non-fatal remarks worth printing at startup."""
        v = self.values
        out: list[str] = []
        # DTK_API_KEY 为空已经是 validate() 里的硬错误，这里只补充"配了但形态像是错的"
        if v["DTK_API_KEY"] and not v["DTK_API_KEY"].startswith("dtk_"):
            out.append(
                "DTK_API_KEY 不以 dtk_ 开头，形态可能不对"
                "（DTK 的 Key 是 dtk_<12位十六进制>_<32位base64url>，共 49 字符）"
            )

        # 速率余量：pacer 决定上限，与账号数无关
        avg = (v["REQUEST_INTERVAL_MIN"] + v["REQUEST_INTERVAL_MAX"]) / 2
        if avg > 0:
            per_min = 60.0 / avg
            out.append(
                f"请求速率上限约 {per_min:.1f} 次/分钟"
                f"（DTK 默认限额 {120}/分钟，身份池 target_size 通常 8）"
            )
            if per_min > 100:
                out.append(
                    "速率偏高：请调大 REQUEST_INTERVAL_MIN/MAX，"
                    "或提高该 API Key 的 rate_limit"
                )
        if v["INCLUDE_RAW"] == "always":
            out.append(
                f"INCLUDE_RAW=always：每轮响应体积约为 {v['FETCH_COUNT']} × 109 KB"
                "（约 1.6 MB），仅在确需逐轮精确同步置顶状态时使用"
            )
        return out

    def describe(self) -> list[str]:
        """Every setting, its effective value and where it came from."""
        lines: list[str] = []
        for spec in SETTINGS:
            value = self.values.get(spec.key, spec.default)
            if spec.key in ("DTK_API_KEY", "DINGTALK_TOKEN", "DINGTALK_SECRET", "WECOM_WEBHOOK_KEY",
                            "BARK_DEVICE_KEY", "SERVERCHAN_SENDKEY", "TELEGRAM_BOT_TOKEN"):
                shown = "***" if value else "(空)"
            elif isinstance(value, list):
                shown = ",".join(str(x) for x in value) or "(空)"
            else:
                shown = str(value)
            lines.append(f"{spec.key:26s} = {shown:34s}  [{self.sources.get(spec.key, 'default')}]")
        return lines

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)


def load_settings(
    env_file: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Read the file, then let the real environment win.

    The order matters and is the conventional one: a value in `.env` describes the
    deployment, a value in the environment describes *this run* — a systemd
    `Environment=` or a one-off `VAR=... python -m dywatch once` should be able to
    override what is written down.
    """
    file_values: dict[str, str] = {}
    if env_file is not None and Path(env_file).is_file():
        try:
            from dotenv import dotenv_values

            file_values = {
                str(k): str(v) for k, v in dotenv_values(env_file).items() if v is not None
            }
        except ImportError:  # pragma: no cover - dotenv is a declared dependency
            file_values = _parse_env_file(Path(env_file))

    env = dict(os.environ if environ is None else environ)

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for spec in SETTINGS:
        if spec.key in env and env[spec.key] != "":
            raw, source = env[spec.key], "env"
        elif spec.key in file_values and file_values[spec.key] != "":
            raw, source = file_values[spec.key], ".env"
        else:
            values[spec.key] = spec.default
            sources[spec.key] = "default"
            continue
        try:
            values[spec.key] = _cast(spec.cast, raw)
            sources[spec.key] = source
        except (TypeError, ValueError):
            values[spec.key] = spec.default
            sources[spec.key] = f"{source}(非法,已回退默认)"
    return Settings(values=values, sources=sources)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader, used only if python-dotenv is unavailable."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def resolve_env_file(explicit: str | None = None, environ: Mapping[str, str] | None = None) -> Path:
    """Which `.env` to read: explicit flag, then `$MONITOR_HOME/.env`, then `./.env`."""
    if explicit:
        return Path(explicit)
    env = os.environ if environ is None else environ
    home = (env.get("MONITOR_HOME") or "").strip()
    if home:
        return Path(home) / ".env"
    return Path.cwd() / ".env"


def settings_for_cli(explicit_env: str | None = None) -> Settings:
    return load_settings(resolve_env_file(explicit_env))


def cast_value(key: str, raw: str) -> Any:
    """Expose the caster for callers that read a value from somewhere else."""
    return _cast(SPECS[key].cast, raw)


def all_keys() -> Iterable[str]:
    return (spec.key for spec in SETTINGS)


def _unused(_: Callable[..., Any]) -> None:  # pragma: no cover
    """Keep the Callable import honest for type checkers reading this file."""
