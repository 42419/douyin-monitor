"""配置注册表：类型转换、跨项校验、来源追溯。"""

from __future__ import annotations

import pytest

from dywatch.targets import parse_targets
from dywatch.targets import parse_targets
from dywatch.settings import (
    CHANNEL_REQUIRED,
    SETTINGS,
    Settings,
    load_settings,
    resolve_env_file,
)


def test_every_spec_has_a_default_and_a_note():
    """注册表是配置参考页与 .env.example 的唯一来源，所以每一项都得说明白自己在干什么。"""
    for spec in SETTINGS:
        assert spec.default is not None or spec.key == "DTK_API_KEY"
        assert spec.note, f"{spec.key} 缺少说明"


def test_archive_download_defaults_to_disabled():
    """需要额外的 media:write scope，不能默认开——升级到带这个功能的版本不该悄悄
    多申请一层权限。"""
    settings = load_settings(None, environ={"DTK_API_KEY": "dtk_x"})
    assert settings["ARCHIVE_DOWNLOAD_ENABLED"] is False
    assert settings["ARCHIVE_DOWNLOAD_PIN"] is False


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
        ({"HIDDEN_CHECK_INTERVAL_MINUTES": "-1"}, "不能为负"),
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


# --------------------------------------------------- RETRY_AFTER_MAX_SECONDS 与升级兼容
VALID_ENV = {
    "DTK_API_KEY": "dtk_x",
    "DINGTALK_TOKEN": "t",
    "DINGTALK_SECRET": "SECxxx",
}


def test_an_old_env_with_a_large_backoff_cap_still_starts_after_upgrade():
    """升级之前合法的 `.env`（`BACKOFF_MAX_SECONDS=7200`）没碰过新键，不能因此起不来。

    新键默认 3600 < 7200：如果校验直接拿默认值去比，用户什么都没改、升级之后服务就报
    启动错误。没显式配置时它**跟着退避上限走**。
    """
    settings = load_settings(None, environ={**VALID_ENV, "BACKOFF_MAX_SECONDS": "7200"})

    assert settings.validate() == []
    assert settings.retry_after_max == 7200


def test_the_default_retry_after_cap_is_one_hour():
    settings = load_settings(None, environ=VALID_ENV)

    assert settings.validate() == []
    assert settings.retry_after_max == 3600


def test_an_explicit_retry_after_cap_below_the_backoff_cap_is_still_refused():
    """"显式写了一个更小的值"才是配错：那会让闸门在上游要求等得更久时提前放开。"""
    env = {**VALID_ENV, "RETRY_AFTER_MAX_SECONDS": "300"}

    errors = load_settings(None, environ=env).validate()

    assert any("不能小于 BACKOFF_MAX_SECONDS" in error for error in errors), errors


def test_an_explicit_retry_after_cap_is_used_as_written():
    env = {**VALID_ENV, "BACKOFF_MAX_SECONDS": "600", "RETRY_AFTER_MAX_SECONDS": "1800"}
    settings = load_settings(None, environ=env)

    assert settings.validate() == []
    assert settings.retry_after_max == 1800


def test_an_unreadable_retry_after_cap_counts_as_not_configured():
    """写成 `abc` 会回退默认值（来源标成"非法,已回退默认"）——那不是"显式配置"，也要跟随。"""
    env = {**VALID_ENV, "BACKOFF_MAX_SECONDS": "7200", "RETRY_AFTER_MAX_SECONDS": "abc"}
    settings = load_settings(None, environ=env)

    assert settings.retry_after_max == 7200
    assert not any("RETRY_AFTER_MAX_SECONDS" in error for error in settings.validate())


def test_config_check_shows_the_value_that_is_actually_in_effect():
    """`config-check` 不能写 3600 而闸门实际用的是 7200。"""
    following = load_settings(None, environ={**VALID_ENV, "BACKOFF_MAX_SECONDS": "7200"})
    plain = load_settings(None, environ=VALID_ENV)

    line_following = next(l for l in following.describe() if l.startswith("RETRY_AFTER_MAX_SECONDS"))
    line_plain = next(l for l in plain.describe() if l.startswith("RETRY_AFTER_MAX_SECONDS"))

    assert "7200" in line_following and "跟随退避上限" in line_following
    assert "3600" in line_plain and "跟随" not in line_plain


def test_low_poll_interval_is_refused_not_silently_raised():
    """配置成 5 秒要报错，而不是悄悄按 10 秒跑——后者会让人以为自己改生效了。"""
    settings = load_settings(None, environ={"POLL_INTERVAL_MIN": "5"})
    assert settings["POLL_INTERVAL_MIN"] == 5  # 值如实保留
    assert any("不得低于 10" in error for error in settings.validate())


def test_silent_mode_does_not_require_a_channel():
    settings = load_settings(
        None,
        environ={"DTK_API_KEY": "dtk_x", "SILENT_MODE": "true", "NOTIFY_CHANNELS": ""},
    )
    assert settings.validate() == []


def test_disabling_the_hidden_check_fallback_says_what_it_costs():
    """`HIDDEN_CHECK_INTERVAL_MINUTES=0` 是合法配置，但代价必须说出来。

    它关掉的是唯一能发现那两类"游客视角不留痕迹"的变化的时机（新作品从发布起就不可见、
    对访客不可见的作品被删）——静默接受会让人以为功能还完整。
    """
    settings = load_settings(
        None,
        environ={"DTK_API_KEY": "dtk_x", "DINGTALK_TOKEN": "t", "DINGTALK_SECRET": "s",
                 "HIDDEN_POST_CHECK_ENABLED": "true", "PINNED_IDENTITY_ID": "uuid-1",
                 "HIDDEN_CHECK_INTERVAL_MINUTES": "0"},
    )
    assert settings.validate() == [], "0 是合法值，不该当成错误"
    assert any("低频保底" in note and "不留痕迹" in note for note in settings.warnings()), (
        settings.warnings()
    )


def test_negative_fallback_interval_is_refused_rather_than_read_as_off():
    """负数会被 `interval > 0` 的守卫读成"关掉保底"——静默失效，所以必须是硬错误。"""
    settings = load_settings(
        None,
        environ={"DTK_API_KEY": "dtk_x", "DINGTALK_TOKEN": "t", "DINGTALK_SECRET": "s",
                 "HIDDEN_CHECK_INTERVAL_MINUTES": "-5"},
    )
    assert settings["HIDDEN_CHECK_INTERVAL_MINUTES"] == -5, "值如实保留，由校验负责拒绝"
    assert any("不能为负" in error for error in settings.validate())

    # 功能关着时不唠叨这两条
    quiet = load_settings(None, environ={"DTK_API_KEY": "dtk_x", "DINGTALK_TOKEN": "t",
                                         "DINGTALK_SECRET": "s",
                                         "HIDDEN_CHECK_INTERVAL_MINUTES": "0"})
    assert not any("低频保底" in note for note in quiet.warnings())


def _targets_settings(raw: str, **extra):
    env = {"DTK_API_KEY": "dtk_x", "NOTIFY_TARGETS": raw}
    env.update(extra)
    return load_settings(None, environ=env)


def test_real_env_file_multi_line_targets_are_loaded(tmp_path):
    """**从真实 `.env` 文件**走完整读取链路（python-dotenv → 解析 → 装配）。

    这是之前整条链路**零覆盖**的地方：所有 `NOTIFY_TARGETS` 用例都直接把值塞进 `environ`，
    于是没人发现"多行值里再用同一种引号 → dotenv 把整条语句丢掉 → 静默回落旧凭据"。
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DTK_API_KEY=dtk_x\n"
        "DINGTALK_TOKEN=legacy-tok\n"
        "DINGTALK_SECRET=SEClegacy\n"
        'NOTIFY_TARGETS="\n'
        "# 两个钉钉群\n"
        "dingtalk name=市场部, token=tok-A, secret=SECa\n"
        "dingtalk token=tok-B, secret=SECb\n"
        "webhook url='https://example.com/a,b#frag'\n"
        '"\n',
        encoding="utf-8",
    )

    settings = load_settings(env_file)
    targets = settings["NOTIFY_TARGETS"]

    assert settings.validate() == []
    assert settings.sources["NOTIFY_TARGETS"] == ".env"
    assert [t.name for t in targets.targets] == ["市场部", "dingtalk-2", "webhook"]
    assert targets.targets[0].fields["token"] == "tok-A"
    assert targets.targets[2].fields["url"] == "https://example.com/a,b#frag"


def test_a_key_that_the_env_parser_dropped_is_a_startup_error(tmp_path):
    """多行值里用了**同一种**引号 → python-dotenv 把整条语句丢掉。

    丢掉之后那个键会退回默认值 —— `NOTIFY_TARGETS` 退回默认就意味着**静默回落到旧凭据**，
    所以这里必须是启动错误（带改法），而不是"少配了一项"。
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DTK_API_KEY=dtk_x\n"
        "DINGTALK_TOKEN=legacy-tok\n"
        "DINGTALK_SECRET=SEClegacy\n"
        'NOTIFY_TARGETS="\n'
        'webhook url="https://example.com/a,b"\n'   # 外层也是双引号 → dotenv 解析不了
        '"\n',
        encoding="utf-8",
    )

    settings = load_settings(env_file)
    errors = settings.validate()

    assert any("NOTIFY_TARGETS 没能被解析出来" in e for e in errors), errors
    assert any("另一种" in e for e in errors), "错误里要给出改法"
    assert not settings["NOTIFY_TARGETS"].configured, "读不出来就是没配 —— 所以必须报错拦住"


def test_a_dropped_key_other_than_targets_is_also_reported(tmp_path):
    """"写了却读不出来"这个检查对所有已注册的键都生效（例如引号写瘸的 WEB_PORT）。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'DTK_API_KEY=dtk_x\nWEB_PORT="8080\nFETCH_COUNT=20\n',
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert any("WEB_PORT 没能被解析出来" in e for e in settings.validate()), settings.validate()



    """新写法的错要指到具体哪一条，而不是含糊地说"配置有问题"。"""
    mixed = _targets_settings("dingtalk token=x\ntelegrm bot_token=y\ntelegram chat_id=1")
    errors = [e for e in mixed.validate() if "NOTIFY_TARGETS" in e]
    assert any("未知渠道类型" in e for e in errors)
    assert any("缺少必填字段" in e for e in errors)
    assert not any("没有一个能用的目标" in e for e in errors), "第一条是好的，不该说没有渠道"

    broken = _targets_settings("telegrm bot_token=y\ntelegram chat_id=1\ndingtalk secret=SECx")
    assert any("没有一个能用的目标" in e for e in broken.validate()), "全写错时要明说没有渠道"


def test_targets_configured_replaces_the_legacy_channel_checks():
    """新写法生效时，`NOTIFY_CHANNELS` 那几项（含"为空"）都不该再报错。"""
    settings = _targets_settings("telegram bot_token=1:x chat_id=-100", NOTIFY_CHANNELS="")

    assert settings.validate() == []


def test_legacy_checks_still_run_when_targets_are_not_configured():
    settings = load_settings(None, environ={"DTK_API_KEY": "dtk_x", "NOTIFY_CHANNELS": "qq"})

    assert any("未知渠道" in e for e in settings.validate())


def test_both_writings_configured_warns_which_one_wins():
    settings = _targets_settings(
        "wecom key=new", NOTIFY_CHANNELS="dingtalk",
        DINGTALK_TOKEN="old", DINGTALK_SECRET="SECold",
    )

    assert settings.validate() == []
    assert any("新写法生效" in note for note in settings.warnings())


def test_many_targets_warn_about_serial_sending():
    """渠道是串行发的，目标一多会把排在后面的通知推得很晚——这件事要在启动时说清楚。"""
    many = "\n".join(f"dingtalk token=t{i}" for i in range(4))
    settings = _targets_settings(many)

    assert any("串行" in note and "68 秒" in note for note in settings.warnings())

    few = _targets_settings("dingtalk token=t0")
    assert not any("串行" in note for note in few.warnings())


def test_config_check_masks_target_credentials_but_shows_the_rest():
    settings = _targets_settings("telegram bot_token=super-secret chat_id=-100 name=手机")

    lines = settings.describe()
    line = next(l for l in lines if l.startswith("NOTIFY_TARGETS"))
    body = "\n".join(lines)
    assert "super-secret" not in body, "凭据不能出现在 config-check 输出里"
    assert "telegram:手机" in body and "chat_id=-100" in body, "非凭据字段要照实显示"
    assert "1 个目标" in line, "目标数写在主行，明细一行一个"


def test_fallback_env_reader_handles_a_quoted_multiline_value(tmp_path):
    """python-dotenv 不在时用兜底解析器：它也必须认多行值。

    不然 `NOTIFY_TARGETS` 那种写法只会读到一个孤零零的引号，**所有通知目标凭空消失**——
    比报错难查得多。
    """
    from dywatch.settings import _parse_env_file

    env_file = tmp_path / ".env"
    env_file.write_text(
        'DTK_API_KEY=dtk_x\n'
        'NOTIFY_TARGETS="\n'
        'dingtalk token=a secret=S\n'
        'telegram bot_token=1:AA chat_id=-100\n'
        '"\n'
        'FETCH_COUNT=20\n',
        encoding="utf-8",
    )

    values = _parse_env_file(env_file)
    assert values["FETCH_COUNT"] == "20", "多行值后面的键还得读得到"
    targets = parse_targets(values["NOTIFY_TARGETS"])
    assert [t.kind for t in targets.targets] == ["dingtalk", "telegram"], values["NOTIFY_TARGETS"]
    assert targets.targets[1].fields["chat_id"] == "-100"


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


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " True ", "yes", "on", "enabled", "y"])
def test_bool_accepts_common_truthy_spellings(raw):
    settings = load_settings(None, environ={"WEB_ENABLED": raw})
    assert settings["WEB_ENABLED"] is True
    assert settings.sources["WEB_ENABLED"] == "env"


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off", "disabled", "n"])
def test_bool_accepts_common_falsy_spellings(raw):
    settings = load_settings(None, environ={"DTK_REFRESH": raw})
    assert settings["DTK_REFRESH"] is False
    assert settings.sources["DTK_REFRESH"] == "env"


def test_unrecognized_bool_is_rejected_rather_than_read_as_false():
    """`ture` 这类手滑必须落回默认值并标出来。

    静默当成 false 的后果是双向的：默认关的项会被以为"已经打开了"，而默认开的项会被
    悄悄关掉（后者更危险——它改变的是正在运行的行为，却没有任何提示）。
    """
    settings = load_settings(
        None, environ={"ARCHIVE_DOWNLOAD_PIN": "ture", "DTK_REFRESH": "flase"}
    )
    assert settings["ARCHIVE_DOWNLOAD_PIN"] is False  # 该项的默认值
    assert settings["DTK_REFRESH"] is True  # 默认 true 的项没被手滑关掉
    assert "非法" in settings.sources["ARCHIVE_DOWNLOAD_PIN"]
    assert "非法" in settings.sources["DTK_REFRESH"]


def test_describe_columns_line_up_even_for_the_longest_key():
    """列宽必须自适应：长键（如 ARCHIVE_DOWNLOAD_MAX_PER_ROUND）曾经把 `=` 和来源列顶歪。"""
    lines = load_settings(None, environ={}).describe()
    assert len({line.index(" = ") for line in lines}) == 1, "= 不在同一列"
    assert len({line.rindex("  [") for line in lines}) == 1, "来源列不在同一列"


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


def test_empty_api_key_is_a_hard_validate_error_not_just_a_warning():
    """`doctor` 一直硬拒绝空 Key；`validate()`（`dywatch run` 实际走的路径）以前不拒绝，
    这条断言就是守住"两者行为一致"这件事的回归测试。"""
    settings = load_settings(
        None, environ={"DINGTALK_TOKEN": "t", "DINGTALK_SECRET": "SECxxx"}
    )
    errors = settings.validate()
    assert any("DTK_API_KEY" in error for error in errors)


def test_configured_api_key_does_not_repeat_the_missing_key_warning():
    settings = load_settings(
        None, environ={"DTK_API_KEY": "dtk_38c817704d0f_A5CMPbL1a7TowGb9LAfXYNAvYK6bjIwn"}
    )
    assert not any("DTK_API_KEY" in note for note in settings.warnings())


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
