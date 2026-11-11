"""校验配置参考与 .env.example 不会漏掉 SETTINGS 注册表里的键。

这两个文件都自称"照着 SETTINGS 维护"，但没有人守着它——新增一个配置项、忘了同步文档，
是这类项目最典型的一类漂移（且没有任何运行时症状）。这里把它钉成一个测试：注册表里
有的键，两个对外文档都必须出现。键以**子串**比对：`.env.example` 里被注释掉的
可选项（如 `RETRY_AFTER_MAX_SECONDS`）同样算已记录。
"""

from __future__ import annotations

from pathlib import Path

from dywatch.settings import SETTINGS

ROOT = Path(__file__).resolve().parents[2]

#: 通知的旧写法（一个类型只能一个实例）：代码仍然认，但 `.env.example` 只示范新写法
#: `NOTIFY_TARGETS`（一行一个目标），所以这些键不要求出现在示例里；配置参考页仍要写全。
LEGACY_NOTIFY_KEYS = frozenset(
    {
        "NOTIFY_CHANNELS",
        "DINGTALK_TOKEN",
        "DINGTALK_SECRET",
        "WECOM_WEBHOOK_KEY",
        "BARK_SERVER",
        "BARK_DEVICE_KEY",
        "SERVERCHAN_SENDKEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "WEBHOOK_URL",
    }
)


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_env_example_documents_every_setting() -> None:
    text = _text(".env.example")
    missing = [
        spec.key
        for spec in SETTINGS
        if spec.key not in text and spec.key not in LEGACY_NOTIFY_KEYS
    ]
    assert missing == [], f".env.example 漏了这些键：{missing}"


def test_config_reference_documents_every_setting() -> None:
    text = _text("docs/config/reference.md")
    missing = [spec.key for spec in SETTINGS if spec.key not in text]
    assert missing == [], f"docs/config/reference.md 漏了这些键：{missing}"
