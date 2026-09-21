"""`NOTIFY_TARGETS` 的语法：解析、实例名，以及"写错了要说清楚"。

这个语法是写在一个 `.env` 值里的（多行、一行一个目标），写错的形态五花八门
（少个 `=`、名字撞了、字段拼错、还用着上一版的逗号写法），所以重点不是"能解析"，
而是**报错要指到具体那一处**——否则用户只看到"目标全没了"。
"""

from __future__ import annotations

import pytest

from dywatch.settings import CHANNEL_REQUIRED
from dywatch.targets import SPECS, TargetSet, parse_targets

#: 一行一个目标；第三个被注释掉，第二个带行内注释
RAW = """
# 生产两个群
dingtalk name=市场部 token=tok-A secret=SECa
dingtalk token=tok-B
# dingtalk token=临时停用的那个
telegram bot_token=1:AA chat_id=-100
"""


def test_one_target_per_line_with_comments():
    parsed = parse_targets(RAW)

    assert parsed.configured and not parsed.errors
    assert [t.kind for t in parsed.targets] == ["dingtalk", "dingtalk", "telegram"]
    assert [t.name for t in parsed.targets] == ["市场部", "dingtalk-2", "telegram"]
    assert parsed.targets[0].fields == {"token": "tok-A", "secret": "SECa"}
    assert parsed.targets[1].fields == {"token": "tok-B"}, "没写 secret 就不该凭空补一个"
    assert parsed.targets[2].fields == {"bot_token": "1:AA", "chat_id": "-100"}


def test_inline_comment_after_fields():
    parsed = parse_targets("dingtalk token=x secret=SECy   # 市场部那个群")

    assert not parsed.errors
    assert parsed.targets[0].fields == {"token": "x", "secret": "SECy"}


def test_semicolons_allow_writing_everything_on_one_line():
    parsed = parse_targets("dingtalk token=a;dingtalk token=b;wecom key=k")

    assert [t.name for t in parsed.targets] == ["dingtalk", "dingtalk-2", "wecom"]


def test_the_type_may_be_followed_by_a_colon():
    """`dingtalk:token=x` / `dingtalk: token=x` / `dingtalk token=x` 三种都认。

    文档表格里当初就写的是 `类型:字段=值` 那种形态——**它必须真能用**，否则照抄文档的人
    会得到一个"看起来配好、实际被拒"的配置（审查时抓到的 P1）。
    """
    for line in ("dingtalk:token=x,secret=S", "dingtalk: token=x", "dingtalk token=x"):
        parsed = parse_targets(line)
        assert not parsed.errors, (line, parsed.errors)
        assert parsed.targets[0].kind == "dingtalk"
        assert parsed.targets[0].fields["token"] == "x"

    # 值里的冒号不能被吃掉（Telegram 的 bot_token 就是 `123:AA` 这种）
    parsed = parse_targets("telegram:bot_token=123:AA, chat_id=-100")
    assert parsed.targets[0].fields["bot_token"] == "123:AA"


def test_a_hash_inside_an_unquoted_value_is_an_error_not_a_silent_truncation():
    """`secret=SECx#备注` 这种：不报错的话密钥就变成 `SECx#备注`，加签失败、通知**静默**发不出去。

    所以这里刻意**不猜**用户是想写注释还是想把它当值：给引号，或者给空格。
    """
    parsed = parse_targets("dingtalk token=abc secret=SECx#这是注释")

    assert parsed.errors, "没加引号的 # 必须报错"
    assert "没加引号的 `#`" in parsed.errors[0], parsed.errors

    # 留个空格 → 真的是注释
    spaced = parse_targets("dingtalk token=abc secret=SECx   # 这是注释")
    assert not spaced.errors
    assert spaced.targets[0].fields == {"token": "abc", "secret": "SECx"}

    # 值里真要 `#`（URL 片段）→ 加引号
    quoted = parse_targets('webhook url="https://x/a#frag"')
    assert not quoted.errors
    assert quoted.targets[0].fields["url"] == "https://x/a#frag"


def test_the_same_field_twice_is_an_error():
    parsed = parse_targets("dingtalk token=A token=B")

    assert parsed.errors and "写了两遍" in parsed.errors[0], parsed.errors


def test_auto_numbering_is_per_type():
    parsed = parse_targets("dingtalk token=a\ndingtalk token=b\ndingtalk token=c\nwecom key=k\nwecom key=k2")

    assert [t.name for t in parsed.targets] == [
        "dingtalk", "dingtalk-2", "dingtalk-3", "wecom", "wecom-2",
    ]


def test_fields_may_be_separated_by_commas():
    """逗号与空格等价——"只有空格不够直观"，两种都行，混着写也行。"""
    for line in (
        "dingtalk name=市场部, token=xxx, secret=SECyyy",
        "dingtalk name=市场部,token=xxx,secret=SECyyy",
        "dingtalk name=市场部 token=xxx, secret=SECyyy",
        "dingtalk name=市场部 , token=xxx , secret=SECyyy",
    ):
        parsed = parse_targets(line)
        assert not parsed.errors, (line, parsed.errors)
        assert parsed.targets[0].name == "市场部"
        assert parsed.targets[0].fields == {"token": "xxx", "secret": "SECyyy"}


def test_a_trailing_comma_and_an_inline_comment_together():
    parsed = parse_targets("dingtalk name=市场部, token=x,   # 市场部那个群")

    assert not parsed.errors
    assert parsed.targets[0].fields == {"token": "x"}


def test_values_may_contain_equals_colon_hash_and_unquoted_commas_never_break_silently():
    """base64 的 `=`、Telegram 的 `123:AA`、值里的 `#` 都该原样当值。"""
    parsed = parse_targets("telegram bot_token=123:AA-GG=, chat_id=-100")

    assert not parsed.errors
    assert parsed.targets[0].fields["bot_token"] == "123:AA-GG="

    # 逗号现在是分隔符了：值里要带逗号就得加引号，不加会**报错**而不是悄悄截断
    broken = parse_targets("webhook url=https://x/a,b")
    assert broken.errors and "不是 key=value" in broken.errors[0], broken.errors


def test_quotes_allow_spaces_and_commas_inside_values():
    parsed = parse_targets('telegram name="我的 手机", bot_token=1:AA, chat_id=-100')

    assert not parsed.errors
    assert parsed.targets[0].name == "我的 手机"
    assert parsed.targets[0].fields["chat_id"] == "-100"

    url = parse_targets('webhook url="https://example.com/a,b?q=1#frag"')
    assert url.targets[0].fields["url"] == "https://example.com/a,b?q=1#frag"


def test_unclosed_quote_is_reported_instead_of_swallowing_the_rest():
    parsed = parse_targets('webhook url="https://x/a')

    assert parsed.errors and "引号没有闭合" in parsed.errors[0], parsed.errors


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("telegrm bot_token=x", "未知渠道类型"),
        ("telegram chat_id=-100", "缺少必填字段：bot_token"),
        ("dingtalk token=x token2=y", "不认识字段"),          # 拼错字段名要说出来
        ("dingtalk token=x bogus", "不是 key=value 形式"),
        ("dingtalk name=群 token=a\ntelegram name=群 bot_token=b chat_id=1", "重复"),
    ],
)
def test_every_mistake_points_at_the_offending_entry(raw, fragment):
    parsed = parse_targets(raw)

    assert parsed.errors, f"{raw!r} 应该报错"
    assert any(fragment in error for error in parsed.errors), parsed.errors


def test_an_unknown_type_that_looks_like_a_field_gets_a_hint():
    """类型位置写了一整段配置（`token=x,secret=y` 而没有类型）时，错误要顺带给出正确形状。"""
    parsed = parse_targets("token=x,secret=S")

    assert parsed.errors and "未知渠道类型" in parsed.errors[0]
    assert "一行一个目标" in parsed.errors[0], "错误信息里要带正确写法"


def test_the_earliest_comma_form_still_works():
    """第一版我写的 `类型:字段=值,字段=值;类型:…` 现在依然能被解析（变成兼容写法）。"""
    parsed = parse_targets("dingtalk:token=A,secret=S;telegram:bot_token=1:AA,chat_id=-100")

    assert not parsed.errors, parsed.errors
    assert [t.kind for t in parsed.targets] == ["dingtalk", "telegram"]
    assert parsed.targets[0].fields == {"token": "A", "secret": "S"}


def test_bad_entry_does_not_take_down_the_good_ones():
    """一条写错不该让别的目标跟着失效——那正是"少一条"而不是"全部静默"。"""
    parsed = parse_targets("dingtalk token=good\ntelegrm bot_token=x\nwecom key=ok")

    assert [t.kind for t in parsed.targets] == ["dingtalk", "wecom"]
    assert len(parsed.errors) == 1


def test_field_name_case_and_type_case_are_insensitive():
    parsed = parse_targets("DingTalk Token=T Secret=S")

    assert parsed.targets[0].kind == "dingtalk"
    assert parsed.targets[0].fields == {"token": "T", "secret": "S"}


def test_empty_means_not_configured_so_the_legacy_keys_keep_working():
    for raw in ("", "   ", "\n\n", None, []):
        parsed = parse_targets(raw)
        assert not parsed.configured, raw
        assert parsed.targets == () and parsed.errors == ()
        assert not parsed, "没有目标就该是假值"


def test_commented_out_targets_are_configured_but_empty():
    """全注释掉 = "配了这个键，但没有目标"。这跟"没配"不同：**不能**因此回落到旧写法，
    而且 `validate()` 会明说"没有任何推送渠道"——那正是用户注释掉全部渠道的后果。"""
    parsed = parse_targets("# dingtalk token=x\n# 全停了\n")

    assert parsed.configured, "键写了内容（哪怕只有注释）就算配过"
    assert parsed.targets == () and parsed.errors == ()


def test_name_collision_with_an_auto_generated_name_is_caught():
    """第二个钉钉取 `dingtalk-2` 时，前面已经有人叫这个名字。"""
    parsed = parse_targets("dingtalk name=dingtalk-2 token=a\ndingtalk token=b")

    assert any("重复" in error for error in parsed.errors)


def test_masked_keeps_non_secret_fields_readable():
    """`config-check` 里要看得出"配了哪几个目标"，但凭据必须是星号。"""
    parsed = parse_targets("telegram bot_token=secret-value chat_id=-100")

    shown = parsed.targets[0].masked()
    assert "secret-value" not in shown
    assert "chat_id=-100" in shown and "telegram" in shown


def test_specs_cover_every_channel_type():
    """`SPECS` 与旧的 `CHANNEL_REQUIRED` 必须指同一批类型——加了渠道别忘了这里。"""
    assert set(SPECS) == set(CHANNEL_REQUIRED)


def test_every_spec_has_required_fields_and_marked_secrets():
    for kind, spec in SPECS.items():
        assert spec.required, f"{kind} 没有必填字段？"
        assert set(spec.secrets) <= set(spec.required) | set(spec.optional), kind


def test_quotes_protect_a_semicolon_too():
    """文档承诺"值里要带 `;` 就加引号"，那就得真的能带（`;` 是目标分隔符）。"""
    parsed = parse_targets('webhook url="https://x/a;b?q=1"')

    assert not parsed.errors, parsed.errors
    assert parsed.targets[0].fields["url"] == "https://x/a;b?q=1"

    # 引号外的 `;` 仍然是目标分隔符
    two = parse_targets('dingtalk token=a;webhook url="https://x/a;b"')
    assert [t.kind for t in two.targets] == ["dingtalk", "webhook"], two.errors


def test_garbage_glued_after_a_closing_quote_is_rejected():
    """`url="https://x"GARBAGE` 不报错的话会静默拼成 `https://xGARBAGE`。"""
    parsed = parse_targets('webhook url="https://x"GARBAGE')

    assert parsed.errors and "没有分隔" in parsed.errors[0], parsed.errors


def test_an_empty_name_is_an_error():
    parsed = parse_targets("dingtalk name=, token=x")

    assert parsed.errors and "name= 是空的" in parsed.errors[0], parsed.errors


def test_a_bad_field_drops_the_whole_entry():
    """`name=a,b`：留着"半个目标"会带着被截断的名字跑，不如整条丢掉。"""
    parsed = parse_targets("dingtalk name=a,b token=x")

    assert parsed.targets == (), "有字段错误就不该产出目标"
    assert any("不是 key=value" in e for e in parsed.errors), parsed.errors
    assert any("已忽略" in e for e in parsed.errors), "要说明这一条被丢了"


def test_target_set_names_helper():
    parsed = parse_targets(RAW)
    assert isinstance(parsed, TargetSet)
    assert parsed.names == ("市场部", "dingtalk-2", "telegram")


def test_the_documented_example_actually_parses():
    """配置参考页里那个例子必须能被真解析——手写语法最容易"代码改了、文档没改"。"""
    import re
    from pathlib import Path

    doc = Path("docs/config/reference.md").read_text(encoding="utf-8")
    block = re.search(r'NOTIFY_TARGETS="\n(.*?)\n"', doc, re.S)
    assert block, "配置参考页里找不到 NOTIFY_TARGETS 的示例"
    parsed = parse_targets(block.group(1))

    assert not parsed.errors, parsed.errors
    assert [t.kind for t in parsed.targets] == ["dingtalk", "dingtalk", "telegram"]
