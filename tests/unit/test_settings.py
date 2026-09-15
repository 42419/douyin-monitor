"""配置注册表：类型转换、跨项校验、来源追溯。"""

from __future__ import annotations

import pytest

from dywatch.settings import (
    CHANNEL_REQUIRED,
    SETTINGS,
    Settings,
    load_settings,
    resolve_env_file,
)


def test_every_spec_has_a_default_and_a_note():
    """注册表是 README 配置章节的唯一来源，所以每一项都得说明白自己在干什么。"""
    for spec in SETTINGS:
        assert spec.default is not None or spec.key == "DTK_API_KEY"
        assert spec.note, f"{spec.key} 缺少说明"


def test_defaults_validate_once_a_key_and_channel_are_given():
    settings = load_settings(
        None,
        environ={
            "DTK_API_KEY": "dtk_38c817704d0f_A5CMPbL1a7TowGb9LAfXYNAvYK6bjIwn",
            "DINGTALK_TOKEN": "t",
            "DINGTALK_SECRET": "SECxxx",
        },
    )
    assert settings.validate() == []
    # 设计里锁定的默认值
    assert settings["FETCH_COUNT"] == 15
    assert (settings["REQUEST_INTERVAL_MIN"], settings["REQUEST_INTERVAL_MAX"]) == (3.0, 8.0)
    assert (settings["POLL_INTERVAL_MIN"], settings["POLL_INTERVAL_MAX"]) == (15, 40)
    assert settings["MAX_CONCURRENT"] == 5
    assert settings["DELETE_CONFIRM_ROUNDS_TOP"] == 3
    assert settings["INCLUDE_RAW"] == "auto"
    assert settings["ARCHIVE_ENABLED"] is True


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"DTK_WAIT": "40", "DTK_TIMEOUT": "35"}, "必须大于 DTK_WAIT"),
        ({"POLL_INTERVAL_MIN": "5"}, "不得低于 10"),
        ({"POLL_INTERVAL_MIN": "60", "POLL_INTERVAL_MAX": "40"}, "不能大于"),
        ({"REQUEST_INTERVAL_MIN": "9", "REQUEST_INTERVAL_MAX": "8"}, "不能大于"),
        ({"FETCH_COUNT": "60"}, "1..50"),
        ({"DELETE_CONFIRM_ROUNDS": "1"}, "不得低于 2"),
        ({"INCLUDE_RAW": "sometimes"}, "auto / always / never"),
        ({"WEB_PORT": "70000"}, "1..65535"),
        ({"NOTIFY_CHANNELS": "telegram"}, "TELEGRAM_BOT_TOKEN"),
        ({"NOTIFY_CHANNELS": "qq"}, "未知渠道"),
    ],
)
def test_cross_field_validation_catches_the_mistakes_people_actually_make(overrides, fragment):
    env = {
        "DTK_API_KEY": "dtk_x",
        "DINGTALK_TOKEN": "t",
        "DINGTALK_SECRET": "SECxxx",
        **overrides,
    }
    errors = load_settings(None, environ=env).validate()
    assert any(fragment in error for error in errors), errors


def test_low_poll_interval_is_refused_not_silently_raised():
    """配置成 5 秒要报错，而不是悄悄按 10 秒跑——后者会让人以为自己改生效了。"""
    settings = load_settings(None, environ={"POLL_INTERVAL_MIN": "5"})
    assert settings["POLL_INTERVAL_MIN"] == 5  # 值如实保留
    assert any("不得低于 10" in error for error in settings.validate())


def test_silent_mode_does_not_require_a_channel():
    settings = load_settings(None, environ={"SILENT_MODE": "true", "NOTIFY_CHANNELS": ""})
    assert settings.validate() == []


def test_env_overrides_file_and_file_overrides_default(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FETCH_COUNT=20\nWEB_PORT=9000\n", encoding="utf-8")

    from_file = load_settings(env_file, environ={})
    assert from_file["FETCH_COUNT"] == 20
    assert from_file.sources["FETCH_COUNT"] == ".env"
    assert from_file.sources["POLL_INTERVAL_MIN"] == "default"

    from_env = load_settings(env_file, environ={"FETCH_COUNT": "30"})
    assert from_env["FETCH_COUNT"] == 30
    assert from_env.sources["FETCH_COUNT"] == "env"


def test_bad_values_fall_back_to_the_default_and_say_so(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FETCH_COUNT=abc\n", encoding="utf-8")
    settings = load_settings(env_file, environ={})
    assert settings["FETCH_COUNT"] == 15
    assert "非法" in settings.sources["FETCH_COUNT"]


def test_secrets_are_masked_in_describe():
    settings = load_settings(
        None, environ={"DTK_API_KEY": "dtk_secret_value", "DINGTALK_TOKEN": "tok"}
    )
    blob = "\n".join(settings.describe())
    assert "dtk_secret_value" not in blob
    assert "tok" not in blob
    assert "***" in blob


def test_warnings_flag_a_key_that_is_not_a_dtk_key():
    settings = load_settings(None, environ={"DTK_API_KEY": "aX_7KvH7nhDWJKscKt"})
    assert any("dtk_" in note for note in settings.warnings())


def test_rate_warning_appears_when_the_pace_is_too_fast():
    settings = load_settings(
        None, environ={"REQUEST_INTERVAL_MIN": "0.2", "REQUEST_INTERVAL_MAX": "0.4"}
    )
    assert any("速率偏高" in note for note in settings.warnings())


def test_resolve_env_file_prefers_monitor_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONITOR_HOME", str(tmp_path))
    assert resolve_env_file(None) == tmp_path / ".env"
    assert resolve_env_file("/custom/.env") == __import__("pathlib").Path("/custom/.env")


def test_home_defaults_to_cwd_and_paths_hang_off_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = load_settings(None, environ={})
    assert settings.home == tmp_path
    assert settings.db_path == tmp_path / "data" / "dywatch.db"
    assert settings.users_conf == tmp_path / "users.conf"


def test_channel_required_table_covers_every_selectable_channel():
    from dywatch.settings import SPECS

    assert set(CHANNEL_REQUIRED) <= {"dingtalk", "wecom", "bark", "serverchan", "telegram", "webhook"}
    for keys in CHANNEL_REQUIRED.values():
        for key in keys:
            assert key in SPECS, f"{key} 不在 SETTINGS 表里"
