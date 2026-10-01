"""命令行与文件权限的回归测试。

这里守的都是"用户看得到结论、但结论是假的"那一类缺陷：
`add` 报告"已加入"而解析器随后把它丢掉、`status` 对着一份坏快照抛 traceback、
`--env` 指了别的文件却照旧打一份完整报告、以及 DESIGN 要求的状态库/日志权限
（0600 / 0700）根本没有落地。
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from dywatch import cli
from dywatch.settings import load_settings
from dywatch.state import StateStore
from dywatch.users import load_users_conf

GOOD_ID = "MS4wLjABAAAA4MjTvxSsNOjHfi9kfyRdu0KMKRHA1dPNv1WQQwW0OKY"


def settings_for(home: Path, **overrides: Any) -> Any:
    env = {
        "MONITOR_HOME": str(home),
        "DTK_API_KEY": "dtk_38c817704d0f_A5CMPbL1a7TowGb9LAfXYNAvYK6bjIwn",
        "SILENT_MODE": "true",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return load_settings(None, environ=env)


# --------------------------------------------------------------------- add
@pytest.fixture()
def add_home(tmp_path: Path) -> Path:
    """`add` 只碰 users.conf，所以给一个干净目录就够。"""
    (tmp_path / "users.conf").write_text("", encoding="utf-8")
    return tmp_path


async def test_add_refuses_an_id_the_parser_would_silently_drop(add_home, capsys):
    """`add` 以前不校验就把行写进 users.conf，而解析器会静默丢弃非法 ID。

    结果是"✓ 已加入"和"这个账号从来没被监控过"同时成立——用户要等到发现它
    从来没推过东西才会察觉。
    """
    settings = settings_for(add_home)

    code = await cli.cmd_add(settings, "MS4wLjABAAAA x", "")

    assert code == 2
    assert "不合法" in capsys.readouterr().out
    assert (add_home / "users.conf").read_text(encoding="utf-8").strip() == ""


async def test_add_refuses_an_id_with_a_zero_width_character(add_home, capsys):
    settings = settings_for(add_home)

    code = await cli.cmd_add(settings, f"MS4wLjABAAAA\u200bxyz", "")

    assert code == 2
    assert "不合法" in capsys.readouterr().out


async def test_add_folds_a_newline_in_the_nickname_instead_of_injecting_a_line(add_home, capsys):
    """昵称里的换行会写成一整行格式里的第二条记录——那是往配置里注入账号。"""
    settings = settings_for(add_home)

    code = await cli.cmd_add(settings, GOOD_ID, "市场部\nMS4wLjABAAAAevil|偷渡")

    assert code == 0
    entries = load_users_conf(add_home / "users.conf")
    assert [entry.sec_user_id for entry in entries] == [GOOD_ID]
    assert entries[0].nickname == "市场部 MS4wLjABAAAAevil|偷渡"
    assert "折叠为空格" in capsys.readouterr().out


async def test_add_writes_and_reports_the_line(add_home):
    settings = settings_for(add_home)

    code = await cli.cmd_add(settings, GOOD_ID, "示例账号")

    assert code == 0
    assert load_users_conf(add_home / "users.conf")[0].nickname == "示例账号"


async def test_add_detects_a_duplicate_by_whole_id_not_by_substring(add_home, capsys):
    """所有真实 ID 都以 `MS4wLjABAAAA` 开头：子串匹配会把新账号误判成重复而拒绝写入。"""
    settings = settings_for(add_home)
    (add_home / "users.conf").write_text(f"{GOOD_ID}|已有账号\n", encoding="utf-8")
    shorter = GOOD_ID[:20]          # 是已有 ID 的前缀，但不是同一个账号

    duplicate = await cli.cmd_add(settings, GOOD_ID, "重复")
    fresh = await cli.cmd_add(settings, shorter, "另一个")

    assert duplicate == 1, "同一个 ID 应当拒绝"
    assert fresh == 0, "只是前缀相同，不该被当成重复"
    assert "已在 users.conf" in capsys.readouterr().out


# --------------------------------------------------------------------- status
def _snapshot(**overrides: Any) -> dict[str, Any]:
    data = {
        "timestamp": "2026-09-16T20:00:00+08:00",
        "pid": 4321,
        "rounds": 137,
        "rounds_total": 3214,
        "gate": {"open": True, "reason": None, "remaining_seconds": 0},
        "notify": {"channels": ["dingtalk"], "silent": False},
        "users": [],
    }
    data.update(overrides)
    return data


def test_status_reports_a_corrupt_snapshot_instead_of_raising(tmp_path, capsys):
    """同一份文件、同一种坏法，面板当成"没有数据"，命令行以前直接 traceback。"""
    settings = settings_for(tmp_path)
    settings.status_path.parent.mkdir(parents=True, exist_ok=True)
    settings.status_path.write_text('{"users": [', encoding="utf-8")

    assert cli.cmd_status(settings) == 1
    assert "读不出来" in capsys.readouterr().out


def test_status_reports_a_json_array_snapshot_instead_of_raising(tmp_path, capsys):
    settings = settings_for(tmp_path)
    settings.status_path.parent.mkdir(parents=True, exist_ok=True)
    settings.status_path.write_text("[1, 2, 3]", encoding="utf-8")

    assert cli.cmd_status(settings) == 1
    assert "读不出来" in capsys.readouterr().out


def test_status_survives_hostile_field_types(tmp_path, capsys):
    """`gate` / `notify` 被改成字符串时也得能打印，而不是让整条命令崩掉。"""
    settings = settings_for(tmp_path)
    settings.status_path.parent.mkdir(parents=True, exist_ok=True)
    settings.status_path.write_text(
        json.dumps(_snapshot(gate="closed", notify=["dingtalk"], users="nope")), encoding="utf-8"
    )

    assert cli.cmd_status(settings) == 0
    printed = capsys.readouterr().out
    assert "上游闸门" in printed and "推送渠道" in printed


def test_status_prints_both_round_counts_with_their_own_caliber(tmp_path, capsys):
    settings = settings_for(tmp_path)
    settings.status_path.parent.mkdir(parents=True, exist_ok=True)
    settings.status_path.write_text(json.dumps(_snapshot()), encoding="utf-8")

    assert cli.cmd_status(settings) == 0
    printed = capsys.readouterr().out
    assert "本次运行 137 轮" in printed and "累计 3214 轮" in printed


# --------------------------------------------------------------------- config-check
def test_config_check_prints_the_env_file_it_actually_read(tmp_path, capsys):
    """`--env` 指了别的文件时，报告抬头不能还写着 `./.env`。"""
    settings = settings_for(tmp_path)
    explicit = tmp_path / "custom.env"

    cli.cmd_config_check(settings, env_file=explicit)

    assert str(explicit) in capsys.readouterr().out


def test_explicit_env_that_does_not_exist_refuses_to_run(tmp_path, capsys):
    """`--env` 是"我要读这个文件"的明确承诺：文件不存在就该报错，而不是静默换一份配置跑。

    （报告头只修了"显示读了哪个文件"，改不了"实际读了哪个文件"——拼错一个字符就会拿默认
    路径那份配置跑起来，而输出里看不出任何异常。）
    """
    missing = tmp_path / "typo.env"

    code = cli.main(["--env", str(missing), "config-check"])

    captured = capsys.readouterr()
    assert code == 2
    assert str(missing) in captured.err
    assert "配置文件不存在" in captured.err


def test_without_explicit_env_a_missing_file_is_still_fine(tmp_path, monkeypatch, capsys):
    """没写 `--env` 时，`.env` 不存在是**正常**的（用户可以完全用环境变量），不该报错。"""
    monkeypatch.chdir(tmp_path)

    cli.main(["config-check"])

    assert "配置文件不存在" not in capsys.readouterr().err


def test_config_check_masks_the_identity_key_and_webhook_url(tmp_path, capsys):
    """这份输出会被贴进 issue（排障文档就是这么建议的），凭据一个都不能明文。"""
    settings = settings_for(
        tmp_path,
        PIN_DTK_API_KEY="dtk_bbbbbbbbbbbb_IDENTITYKEYIDENTITYKEYIDENTITYKEY1234",
        WEBHOOK_URL="https://hooks.example.com/T0KEN-IN-THE-PATH",
    )

    cli.cmd_config_check(settings)
    printed = capsys.readouterr().out

    assert "IDENTITYKEY" not in printed
    assert "T0KEN-IN-THE-PATH" not in printed
    assert "PIN_DTK_API_KEY" in printed, "打码不等于不显示这个键"


# --------------------------------------------------------------------- 文件权限
#: Windows 的 NTFS 没有 POSIX 权限位：`os.chmod` 在那里是**空操作**（实测 chmod 调用成功、
#: 读回来仍是 0o666），所以这两个断言只在 POSIX 上成立。这不是"跳过没意义的东西"——
#: 部署目标就是 Ubuntu，而这条要求（0600 / 0700）在 Linux 上才是真的能挡人的。
posix_only = pytest.mark.skipif(
    os.name != "posix", reason="NTFS 没有 POSIX 权限位，chmod 是空操作；这条断言只在 Linux 上有意义"
)


@posix_only
def test_state_database_is_created_owner_only(tmp_path):
    """DESIGN §1.1：状态库 0600、**它所在的目录 0700**。

    共享主机上默认的 0644 就是"同机谁都能读监控名单"。目录这一层同样要收：WAL 模式会在
    同一个目录里再长出 `-wal` / `-shm`（还没 checkpoint 的**已提交数据**），面板读的
    `status.json` 也在那儿——只收主库文件，旁路文件照样按 umask 落盘。
    """
    old_umask = os.umask(0o022)
    try:
        # pytest 给的 tmp_path 自己就是 0700，先放宽成 0755，模拟"按 umask 建出来的目录"
        os.chmod(tmp_path, 0o755)
        store = StateStore(tmp_path / "dywatch.db")
        store.migrate()
        file_mode = stat.S_IMODE(os.stat(tmp_path / "dywatch.db").st_mode)
        dir_mode = stat.S_IMODE(os.stat(tmp_path).st_mode)
        store.close()
    finally:
        os.umask(old_umask)

    assert file_mode == 0o600, oct(file_mode)
    assert dir_mode == 0o700, oct(dir_mode)


def test_the_store_asks_for_private_modes_even_where_they_are_not_enforced(tmp_path, monkeypatch):
    """上面那条在 Windows 上会被跳过（chmod 是空操作），但**请求**本身要一直在。

    部署目标是 Linux：哪天有人把这两行 chmod 删掉，这条在哪儿都会红；而它不依赖文件系统
    真的去执行权限位。
    """
    seen: list[tuple[str, int]] = []
    real_chmod = os.chmod

    def spy(path: Any, mode: int) -> None:
        seen.append((str(path), mode))
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", spy)
    store = StateStore(tmp_path / "data" / "dywatch.db")
    store.migrate()
    store.close()

    wanted = {mode for _path, mode in seen}
    assert 0o700 in wanted, f"没给状态库目录请求 0700：{seen}"
    assert 0o600 in wanted, f"没给状态库文件请求 0600：{seen}"


@posix_only
def test_log_directories_are_owner_only(tmp_path):
    """DESIGN §1.1：日志目录 0700（日志里有账号 ID、昵称与作品标题）。"""
    from dywatch.runtime import setup_logging

    old_umask = os.umask(0o022)
    try:
        settings = settings_for(tmp_path)
        setup_logging(settings)
    finally:
        os.umask(old_umask)

    for directory in (settings.log_dir, settings.log_dir / "info", settings.log_dir / "debug"):
        mode = stat.S_IMODE(os.stat(directory).st_mode)
        assert mode == 0o700, (directory, oct(mode))


# --------------------------------------------------------------------- doctor
async def test_doctor_requires_archive_scope_only_when_archive_is_enabled(tmp_path, monkeypatch):
    """`archive:read` 的必需性跟着 `ARCHIVE_ENABLED` 走——两边都不能错。

    默认开启 + 缺 scope：以前打印了 ✗ 却仍然"✓ 自检通过"并 exit 0（把 exit code 当门禁
    的 CI 会放过去，而归档交叉确认实际上一直在失败）。
    `ARCHIVE_ENABLED=false`：以前还在报 ✗（那是配置选择，不是问题）。
    """

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...
        async def __aenter__(self) -> "_Client":
            return self
        async def __aexit__(self, *exc: Any) -> None: ...
        async def me(self) -> dict[str, Any]:
            return {"user": {"username": "u", "role": "viewer", "via": "api_key",
                             "scopes": ["douyin:read"]}}
        async def system_status(self) -> dict[str, Any]:
            return {"version": "5.1.2", "pool": {"douyin": {"active": 3}}}
        async def author_posts(self, *args: Any, **kwargs: Any) -> Any:
            from dywatch.models import Page
            return Page(items=())

    monkeypatch.setattr(cli, "DtkClient", _Client)
    # 零个账号也能跑 doctor（自检的对象是实例、凭据与 scope，不是账号列表）
    (tmp_path / "users.conf").write_text("", encoding="utf-8")

    enabled = settings_for(tmp_path)
    code_enabled = await cli.cmd_doctor(enabled)
    assert code_enabled == 1, "默认开启归档 + 缺 archive:read 必须判为需要处理"

    disabled = settings_for(tmp_path, ARCHIVE_ENABLED="false")
    code_disabled = await cli.cmd_doctor(disabled)
    assert code_disabled == 0, "关掉归档时缺 archive:read 不该算问题"
