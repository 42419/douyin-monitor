import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from douyin_monitor import monitor as monitor_module  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """把 monitor 模块里的 STATE_DIR 指向一个临时目录，测试之间互不影响。"""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(monitor_module, "STATE_DIR", d)
    return d


class RecordingNotifier:
    """记录所有推送调用的假通知器，用于断言测试期望的通知行为。"""

    def __init__(self):
        self.texts = []
        self.videos = []
        self.deleted = []

    def send_text(self, title, content, at_mobiles=None):
        self.texts.append((title, content))
        return True

    def send_video(
        self,
        nickname,
        video_id,
        title,
        create_time,
        cover_url=None,
        digg_count=0,
        comment_count=0,
        share_count=0,
        collect_count=0,
        duration_ms=0,
        desc="",
    ):
        self.videos.append((nickname, video_id, title))
        return True

    def send_deleted(self, nickname, deleted_entries, at_mobiles=None):
        self.deleted.append((nickname, [vid for vid, _ in deleted_entries]))
        return True
