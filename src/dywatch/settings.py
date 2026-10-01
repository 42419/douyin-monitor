"""配置：一张声明式注册表，`.env` 只做覆盖。

照搬 DTK v5 的 `SettingSpec` 思路：所有设置项集中登记（键、默认值、类型、说明），
于是文档站的配置参考页、`--config-check` 的输出、以及跨项校验读的都是同一份事实，
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

from .targets import TargetSet, parse_targets

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
    # ---------------------------------------------------------------- 隐藏作品核验
    SettingSpec(
        "HIDDEN_POST_CHECK_ENABLED", False, "bool",
        "游客身份有时看不到作者主页最新发布的作品（抖音的访客限制，与本工具无关）。"
        "开启后，在账号初始化 / 本轮有新作品 / 作品消失确认 / 长期无更新兜底触发的"
        "那一刻，顺手核对一次作者的发布总数：数字对不上时，才用一次登录态身份把这一轮"
        "的列表重新拉一遍，把「对访客不可见」和「真的被删了」分开。默认关闭；"
        "不是逐轮轮询——除了上面那四种事件，还有一条每账号 HIDDEN_CHECK_INTERVAL_MINUTES"
        "分钟的低频保底（见那一项为什么是必需的）。",
    ),
    SettingSpec(
        "PINNED_IDENTITY_ID", "", "str",
        "登录态身份的 UUID（DTK 控制台 Identities 页面可查），核验时把请求定向到这一个"
        "身份。HIDDEN_POST_CHECK_ENABLED=true 时必填。需要 Key 带 identity:manage scope，"
        "且 Key 的 owner 账号至少 operator——比监控本身用的 scope 高一截，建议用",
    ),
    SettingSpec(
        "PIN_DTK_API_KEY", "", "str",
        "定向核验专用的 Key（留空则复用 DTK_API_KEY）。identity:manage 能解密查看"
        "任意身份的 cookie 明文，权限比监控本身重得多，建议单独开一把 Key、只给这一处用，"
        "泄露的影响面才不会牵连到主监控用的只读凭据",
    ),
    SettingSpec(
        "HIDDEN_CHECK_INTERVAL_MINUTES", 30, "int",
        "低频保底：基准值超过这么久没核对过，就无条件核对一次发布总数（分钟，0 = 关闭）。"
        "这条不是优化而是必需——两类变化在游客视角完全不留痕迹：新作品从发布起就不可见、"
        "已知对访客不可见的作品被删；没有它只能等 STALE_FALLBACK_DAYS 那次兜底。"
        "代价是每账号每 30 分钟一次（相对每账号每轮的抓取可忽略）",
    ),
    # ---------------------------------------------------------------- 通知
    SettingSpec("NOTIFY_CHANNELS", ["dingtalk"], "csv", "启用的渠道，逗号分隔"),
    SettingSpec(
        "NOTIFY_TARGETS", TargetSet(), "targets",
        "通知目标（可多实例）：`类型:字段=值,字段=值;类型:…`，见 .env.example / 配置参考",
    ),
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
    SettingSpec("EVENTS_KEEP_DAYS", 30, "int", "events 审计保留天数"),
    SettingSpec("ROUNDS_KEEP_DAYS", 5, "int", "rounds 汇总保留天数（每轮一行；本表没有读取方，纯排障用）"),
)

SPECS: Final[Mapping[str, SettingSpec]] = {spec.key: spec for spec in SETTINGS}

#: `config-check`（`Settings.describe()`）里**必须打码**的键。
#:
#: 这份清单是"对外输出"的防线：`config-check` 的输出被排障文档鼓励贴进 issue，漏一个
#: 就等于公开一把钥匙。曾经漏了两个：`PIN_DTK_API_KEY`——它带 `identity:manage`，
#: 能解密任意身份的 cookie 明文，比主监控那把只读钥匙重得多——以及旧写法的
#: `WEBHOOK_URL`（回调地址通常自带密钥）。新增凭据类配置时**必须同步加到这里**。
MASKED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "DTK_API_KEY",
        "PIN_DTK_API_KEY",
        "DINGTALK_TOKEN",
        "DINGTALK_SECRET",
        "WECOM_WEBHOOK_KEY",
        "BARK_DEVICE_KEY",
        "SERVERCHAN_SENDKEY",
        "TELEGRAM_BOT_TOKEN",
        "WEBHOOK_URL",
    }
)


#: 布尔配置认哪些写法。**不收别的一律报错**：多认一种拼写，就多一次"以为生效了"的机会；
#: 而这些已经是各服务配置里的常见写法，够用了。
BOOL_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "on", "enable", "enabled"})
BOOL_FALSE: Final[frozenset[str]] = frozenset({"0", "false", "no", "n", "off", "disable", "disabled"})


def _to_bool(raw: str) -> bool:
    """布尔取值：`on/off`、`yes/no`、`1/0`、`true/false` 都认。

    **认不出来就抛 ValueError**，不静默当成 False。原因是"写错一个字母"和"明确关掉"
    在那之前长得一模一样：`ARCHIVE_DOWNLOAD_PIN=ture` 会安静地变成 false，而人以为
    自己打开了。抛错之后，`load_settings` 会回退到该项的默认值、并把来源标成
    `非法,已回退默认`——`config-check` 一眼能看出是哪一个键写错了。
    """
    text = raw.strip().lower()
    if text in BOOL_TRUE:
        return True
    if text in BOOL_FALSE:
        return False
    raise ValueError(
        f"无法识别的布尔值 {raw!r}：可用 1/0、true/false、yes/no、on/off（大小写不敏感）"
    )


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
    if kind == "targets":
        # 刻意不抛：解析问题收在 `TargetSet.errors` 里，由 `validate()` 逐条报出来
        # （抛出去会被当成"这个值坏了"，用户只会看到"目标全没了"）
        return parse_targets(raw)
    raise ValueError(f"unknown cast: {kind}")


@dataclass(frozen=True, slots=True)
class Settings:
    """An immutable snapshot of the effective settings."""

    values: Mapping[str, Any]
    sources: Mapping[str, str]
    #: `.env` 文件本身的问题（写了却读不出来的键）。它是**启动错误**，不是提示：
    #: python-dotenv 遇到配不平的引号只往 stderr 打一行、然后把整条语句丢掉——
    #: 那会让这个键悄悄退回默认值（例如 `NOTIFY_TARGETS` 被丢 → 静默回落旧凭据）。
    file_problems: tuple[str, ...] = ()

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

        errors.extend(self.file_problems)

        targets = v["NOTIFY_TARGETS"]
        for problem in targets.errors:
            errors.append(f"NOTIFY_TARGETS：{problem}")
        if targets.configured:
            # 新写法生效时那几个旧键会被忽略，所以不校验它们（`warnings()` 里会说明）
            if not v["SILENT_MODE"] and not targets.targets:
                errors.append(
                    "NOTIFY_TARGETS 里没有一个能用的目标，且未开启 SILENT_MODE，将没有任何推送渠道"
                )
        else:
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

        if v["HIDDEN_POST_CHECK_ENABLED"] and not v["PINNED_IDENTITY_ID"]:
            errors.append(
                "HIDDEN_POST_CHECK_ENABLED=true 但 PINNED_IDENTITY_ID 未配置 —— "
                "核验时不知道该定向到哪个身份，去 DTK 控制台 Identities 页面复制一个"
                "登录态身份的 UUID 填进来"
            )
        if v["HIDDEN_CHECK_INTERVAL_MINUTES"] < 0:
            # 负数会被 `interval > 0` 的守卫当成"关闭保底"——静默失效是最坏的一种，
            # 因为读配置的人会以为自己配了个值（跟 POLL_INTERVAL_MIN 那条同一个道理）
            errors.append("HIDDEN_CHECK_INTERVAL_MINUTES 不能为负（0 = 关闭保底）")

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
        if v["HIDDEN_POST_CHECK_ENABLED"] and v["HIDDEN_CHECK_INTERVAL_MINUTES"] == 0:
            out.append(
                "HIDDEN_CHECK_INTERVAL_MINUTES=0 关掉了低频保底：有两类变化在游客视角"
                "完全不留痕迹（新作品从发布起就不可见、已标记为对访客不可见的作品被删），"
                "事件驱动的触发永远等不到它们——它们只会在 STALE_FALLBACK_DAYS"
                f"（{v['STALE_FALLBACK_DAYS']} 天）那次兜底时被顺带发现，甚至更久"
            )
        targets = v["NOTIFY_TARGETS"]
        if targets.configured and self.sources.get("NOTIFY_CHANNELS") in ("env", ".env"):
            out.append(
                "同时配了 NOTIFY_TARGETS 与 NOTIFY_CHANNELS：**新写法生效**，`NOTIFY_CHANNELS` 与 "
                "DINGTALK_TOKEN 这类单值凭据会被忽略（想少一份困惑就把旧的那几行删掉）"
            )
        if len(targets.targets) >= 4:
            # 串行发送：每个目标最坏 2×8 秒超时 + 1 秒退避。这个数字是照 notifiers/base.py
            # 的常量算的，目标一多就会把排在后面的通知（尤其作品消失类）推得很晚
            out.append(
                f"通知目标 {len(targets.targets)} 个、**串行**发送：一条消息最坏约 "
                f"{len(targets.targets) * 17} 秒才发完"
            )
        if v["INCLUDE_RAW"] == "always":
            out.append(
                f"INCLUDE_RAW=always：每轮响应体积约为 {v['FETCH_COUNT']} × 109 KB"
                "（约 1.6 MB），仅在确需逐轮精确同步置顶状态时使用"
            )
        return out

    def describe(self) -> list[str]:
        """Every setting, its effective value and where it came from.

        键的列宽**按最长键算**而不是写死：注册表随时会长（`ARCHIVE_DOWNLOAD_MAX_PER_ROUND`
        就让写死的 26 崩过一次），列一处写死，早晚有一列会歪。
        """
        key_width = max(len(spec.key) for spec in SETTINGS)
        lines: list[str] = []
        for spec in SETTINGS:
            value = self.values.get(spec.key, spec.default)
            extra: list[str] = []
            if spec.key in MASKED_KEYS:
                shown = "***" if value else "(空)"
            elif isinstance(value, TargetSet):
                # 目标多的时候一行塞不下：每个目标单起一行，凭据由 `masked()` 打码
                shown = f"{len(value.targets)} 个目标" if value.targets else "(空)"
                extra = [f"    - {target.masked()}" for target in value.targets]
            elif isinstance(value, list):
                shown = ",".join(str(x) for x in value) or "(空)"
            else:
                shown = str(value)
            lines.append(
                f"{spec.key:{key_width}s} = {shown:34s}  [{self.sources.get(spec.key, 'default')}]"
            )
            lines.extend(extra)
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
    file_problems: list[str] = []
    if env_file is not None and Path(env_file).is_file():
        try:
            from dotenv import dotenv_values

            file_values = {
                str(k): str(v) for k, v in dotenv_values(env_file).items() if v is not None
            }
        except ImportError:  # pragma: no cover - dotenv is a declared dependency
            file_values = _parse_env_file(Path(env_file))
        file_problems.extend(_dropped_env_keys(Path(env_file), file_values))

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
    return Settings(values=values, sources=sources, file_problems=tuple(file_problems))


def _dropped_env_keys(path: Path, parsed: Mapping[str, str]) -> list[str]:
    """`.env` 里写了、但解析器**没读出来**的已注册键。

    这类"安静的丢"很难查：python-dotenv 遇到配不平的引号（典型场景是**多行值里又用了
    同一种引号**）只往 stderr 打一行警告，然后把整条语句丢掉。丢掉之后那个键就退回默认值——
    例如 `NOTIFY_TARGETS` 丢了会静默回落到旧写法的那套凭据（我们明确不允许这种事）。

    只看**已注册**的键：多行块里那些 `dingtalk token=xxx` 的行也含 `=`，但它们的"键"里带空格，
    本来就不会被当成环境变量。
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:  # pragma: no cover - 文件在读之前刚判过存在
        return []

    declared: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if key in SPECS and key not in declared:
            declared.append(key)

    out: list[str] = []
    for key in declared:
        if key in parsed:
            continue
        hint = ""
        if key == "NOTIFY_TARGETS":
            hint = (
                "——多行值里要用引号时，请换**另一种**引号（外层是 \" 内层就用 '），"
                "或者把它写成一行"
            )
        out.append(f".env 里的 {key} 没能被解析出来（多半是引号没配平）{hint}")
    return out


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader, used only if python-dotenv is unavailable.

    **带引号的多行值也要认**：`NOTIFY_TARGETS` 就是那么写的（一行一个目标）。不认的话会读出
    一个孤零零的引号，所有通知目标凭空消失——比"报错"更难查。
    """
    out: dict[str, str] = {}
    # `utf-8-sig`：Windows 编辑器存出来的 .env 常带 BOM，不去掉的话第一个键（通常是
    # DTK_API_KEY）会匹配不上——那会表现成"Key 没配"，很难往编码上想
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in ("'", '"') and not value[1:].rstrip().endswith(value[:1]):
            quote = value[0]
            chunk = [value[1:]]
            while index < len(lines):
                piece = lines[index]
                index += 1
                if piece.rstrip().endswith(quote):
                    chunk.append(piece.rstrip()[:-1])
                    break
                chunk.append(piece)
            value = "\n".join(chunk)
        else:
            value = value.strip("'\"")
        out[key.strip()] = value
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
