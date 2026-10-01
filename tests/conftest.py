"""Test bootstrap: make `src/` importable without an editable install."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_configure(config) -> None:  # noqa: ANN001 - pytest 的钩子签名
    """让 `tmp_path` 在受限环境（沙箱/只读挂载/权限收紧的 CI）下也能收尾。

    pytest 在会话结束时会对基准目录做一次 `cleanup_dead_symlinks()` 的清扫，
    它要走一遍目录树。这条路在正常机器上什么都不会发生，但在"工作目录里只允许写
    某一层"的沙箱里会抛 `PermissionError`：**测试全绿，退出码却是 1，而且汇总行
    根本不打印**——看起来像测试挂了，实际只是收尾清不掉自己建的临时目录。

    所以这里只把这一步包成"尽力而为"：清扫失败不影响测试结论，残留的临时目录由
    环境自己去管。正常环境下行为完全不变（清扫照常做）。
    """
    from _pytest import pathlib as pytest_pathlib
    from _pytest import tmpdir as pytest_tmpdir

    original = pytest_pathlib.cleanup_dead_symlinks

    def tolerant(root) -> None:  # noqa: ANN001 - 包一层就够
        try:
            original(root)
        except OSError:
            return

    # `tmpdir` 用的是 `from _pytest.pathlib import cleanup_dead_symlinks`，
    # 所以两处都要换：只改 pathlib 的话，收尾时调用的还是原来那个函数。
    pytest_pathlib.cleanup_dead_symlinks = tolerant
    pytest_tmpdir.cleanup_dead_symlinks = tolerant
