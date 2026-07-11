from douyin_monitor.config import load_users_conf


def test_load_users_conf_parses_and_dedups(tmp_path):
    conf = tmp_path / "users.conf"
    conf.write_text(
        "\n".join(
            [
                "# 这是注释",
                "",
                "sec_a|小明",
                "sec_b|小红",
                "sec_a|重复应被忽略",
                "格式错误没有分隔符",
                "  sec_c  |  带空格  ",
            ]
        ),
        encoding="utf-8",
    )

    users = load_users_conf(conf)

    assert users == [("sec_a", "小明"), ("sec_b", "小红"), ("sec_c", "带空格")]


def test_load_users_conf_missing_file_returns_empty(tmp_path):
    assert load_users_conf(tmp_path / "not_exist.conf") == []


def test_load_users_conf_ignores_empty_id_or_nickname(tmp_path):
    conf = tmp_path / "users.conf"
    conf.write_text("|昵称为空\nsec_x|\n", encoding="utf-8")

    assert load_users_conf(conf) == []
