"""users.conf 解析：给人手改的文件要容错，但它是输入校验点。"""

from __future__ import annotations

from dywatch.users import is_safe_id, parse_users, resolve_input


class Recorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    def warning(self, event: str, **fields) -> None:  # noqa: ANN003
        self.messages.append((event, fields))


def test_parses_the_documented_format():
    entries = parse_users(
        """
        # 注释
        MS4wLjABAAAA_1|示例账号A
        MS4wLjABAAAA_2|示例账号B
        """
    )
    assert [(e.sec_user_id, e.nickname) for e in entries] == [
        ("MS4wLjABAAAA_1", "示例账号A"),
        ("MS4wLjABAAAA_2", "示例账号B"),
    ]


def test_inline_comment_needs_only_one_space_before_the_hash():
    """规则与 `NOTIFY_TARGETS` 一致：`#` 前面有空白就是注释，到行尾为止。

    早先只认"两个空格或 tab"，于是 `市场部 # 主账号` 里的注释会跟着进每一条通知、
    面板和 `/metrics` 的 label——两种写法长得几乎一样，却只有一种生效。
    """
    entries = parse_users(
        "MS4wLjABAAAA_1|市场部 # 主账号\n"
        "MS4wLjABAAAA_2|运营\t# 备注\n"
        "MS4wLjABAAAA_3|摄影   # 两个空格也行\n"
    )

    assert [e.nickname for e in entries] == ["市场部", "运营", "摄影"]


def test_a_hash_inside_a_nickname_survives_when_nothing_precedes_it():
    """昵称里真要带 `#`：只要它前面没有空白就原样保留（两种常见写法都保住）。"""
    entries = parse_users("MS4wLjABAAAA_1|账号#1\nMS4wLjABAAAA_2|#1 账号\n")

    assert [e.nickname for e in entries] == ["账号#1", "#1 账号"]


def test_blank_nickname_falls_back_to_the_id_tail():
    entries = parse_users("MS4wLjABAAAA_abcdef|x\nMS4wLjABAAAA_abcdefg|\n"[:80])
    assert entries[0].nickname == "x"


def test_duplicate_ids_keep_the_first_and_report_the_second():
    log = Recorder()
    entries = parse_users("AAA|第一个\nAAA|第二个\n", logger=log)
    assert len(entries) == 1
    assert entries[0].nickname == "第一个"
    assert [event for event, _ in log.messages] == ["users.duplicate"]


def test_lines_without_a_separator_are_skipped_with_a_reason():
    log = Recorder()
    entries = parse_users("AAA\n", logger=log)
    assert entries == []
    assert log.messages[0][0] == "users.bad_line"


def test_ids_with_whitespace_or_control_characters_are_refused():
    log = Recorder()
    entries = parse_users("AA A|空格\nAA\tB|制表\n", logger=log)
    assert entries == []
    assert [event for event, _ in log.messages] == ["users.bad_id", "users.bad_id"]


def test_is_safe_id_bounds():
    assert is_safe_id("MS4wLjABAAAA_4MjTvxSsNOjHfi9kfyRdu0KMKRHA1dPNv1WQQwW0OKY")
    assert not is_safe_id("")
    assert not is_safe_id("x" * 201)
    assert not is_safe_id("a|b")
    assert not is_safe_id("a/b")
    assert not is_safe_id("a\\b")


def test_resolve_input_accepts_a_link_tail():
    assert resolve_input("MS4wLjABAAAA_x") == "MS4wLjABAAAA_x"
    assert resolve_input("https://www.douyin.com/user/MS4wLjABAAAA_x") == "MS4wLjABAAAA_x"
    assert resolve_input("https://www.douyin.com/user/MS4wLjABAAAA_x/") == "MS4wLjABAAAA_x"
    assert resolve_input("https://www.douyin.com/user/MS4wLjABAAAA_x?from=web") == "MS4wLjABAAAA_x"
