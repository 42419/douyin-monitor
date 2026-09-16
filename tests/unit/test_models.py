"""`models.Kind`/`EventKind` 依赖 `str(member) == member.value` 这条行为——
`alerts.py`/`render.py`/`state.py` 里不少地方直接 f-string 拼、直接塞进 dict 再
`json.dumps`。Python 3.11+ 用标准库 `enum.StrEnum` 天然满足；3.10 用
`models.py` 里手工补的 shim。这个文件在两边跑的是同一份断言，
但只有在 3.10 上跑测试时才真正执行到 shim 那条代码路径。
"""

import json

from dywatch.models import EventKind, Kind


def test_kind_stringifies_to_its_plain_value_not_the_member_name():
    assert str(Kind.VIDEO) == "video"
    assert f"{Kind.VIDEO}" == "video"
    assert Kind.VIDEO == "video"


def test_event_kind_stringifies_to_its_plain_value_not_the_member_name():
    assert str(EventKind.NEW_POST) == "new_post"
    assert f"{EventKind.NEW_POST}" == "new_post"


def test_kind_json_dumps_as_a_plain_string_even_without_calling_dot_value():
    # 代码里大多数地方是显式 .value，但这条测试锁的是"就算漏写 .value 也不会
    # 序列化出 'Kind.VIDEO' 这种类名"，防止以后有人漏加
    assert json.dumps({"kind": Kind.VIDEO}) == '{"kind": "video"}'


def test_kind_parse_falls_back_to_unknown_on_garbage():
    assert Kind.parse("video") is Kind.VIDEO
    assert Kind.parse("something-dtk-has-not-shipped-yet") is Kind.UNKNOWN
    assert Kind.parse(None) is Kind.UNKNOWN
