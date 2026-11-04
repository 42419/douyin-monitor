"""启动横幅：一段可扫读的多行摘要，而不是每项一条带前缀的日志。"""

from __future__ import annotations

from dywatch.cli import _startup_banner


class _Runtime:
    def describe(self):
        return [
            "  DTK 实例      : http://127.0.0.1:8000",
            "  抓取窗口      : 15 条/账号",
            "  读取身份      : 定向 11db09e3…（不走身份池）",
        ]


class _Settings:
    def warnings(self):
        return ["请求速率上限约 10.9 次/分钟（DTK 默认限额 120/分钟）"]


def test_banner_is_one_compact_block() -> None:
    text = _startup_banner(
        _Runtime(),
        _Settings(),
        once=False,
        panel_urls=["http://127.0.0.1:8888/"],
        panel_exposed=None,
    )
    lines = text.splitlines()
    assert lines[0].startswith("dywatch ")
    assert "常驻" in lines[0]
    assert "DTK 实例" in text
    assert "提示" in text and "请求速率上限约 10.9" in text
    assert "面板" in text and "http://127.0.0.1:8888/" in text
    # 不再是每项一条 config 行，也不残留旧的分隔线
    assert "config" not in text
    assert "====" not in text


def test_panel_exposed_is_surfaced_as_a_note() -> None:
    text = _startup_banner(
        _Runtime(),
        _Settings(),
        once=True,
        panel_urls=["http://0.0.0.0:8888/"],
        panel_exposed="0.0.0.0",
    )
    assert "单轮" in text
    assert "0.0.0.0" in text and "无鉴权" in text
