"""组合通知器：把同一条消息广播给多个已启用的推送渠道。"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from .base import BaseNotifier


class CompositeNotifier(BaseNotifier):
    """依次调用所有子渠道，只要有一个成功就整体视为成功。

    单个渠道抛出异常或推送失败不会影响其它渠道，也不会中断主流程。
    """

    name = "composite"

    def __init__(self, notifiers: List[BaseNotifier]):
        if not notifiers:
            raise ValueError("至少需要启用一个通知渠道")
        self._notifiers = notifiers

    def _broadcast(self, method: str, *args, **kwargs) -> bool:
        any_ok = False
        for notifier in self._notifiers:
            try:
                ok = getattr(notifier, method)(*args, **kwargs)
            except Exception:
                logging.exception(f"[{notifier.name}] 推送时发生未捕获异常")
                ok = False
            any_ok = any_ok or ok
        return any_ok

    def send_text(self, title: str, content: str, at_mobiles: Optional[List[str]] = None) -> bool:
        return self._broadcast("send_text", title, content, at_mobiles)

    def send_video(
        self,
        nickname: str,
        video_id: str,
        title: str,
        create_time: int,
        cover_url: Optional[str] = None,
    ) -> bool:
        return self._broadcast("send_video", nickname, video_id, title, create_time, cover_url)

    def send_deleted(
        self,
        nickname: str,
        deleted_entries: List[Tuple[str, dict]],
        at_mobiles: Optional[List[str]] = None,
    ) -> bool:
        return self._broadcast("send_deleted", nickname, deleted_entries, at_mobiles)
