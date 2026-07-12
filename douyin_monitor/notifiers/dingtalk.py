"""钉钉群机器人推送。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional, Tuple

from dingtalkchatbot import chatbot as _dingtalk_chatbot_module
from dingtalkchatbot.chatbot import ActionCard, CardItem, DingtalkChatbot

from ..config import HTTP_TIMEOUT
from ..utils import md_escape, now_str
from .base import BaseNotifier, format_count, format_duration

# DingtalkChatbot 库内部调用 requests.post 时完全没传 timeout（源码里是
# `requests.post(self.webhook, headers=self.headers, data=post_data)`），
# 网络卡住时会无限期挂起，进而把 check_user 的线程池堵死、拖慢所有账号的
# 检测。这里只 monkeypatch dingtalkchatbot.chatbot 模块自己引用的那个
# requests.post，用 setdefault 只在调用方没传 timeout 时才补上默认值，
# 不会覆盖其它地方显式传的 timeout；也不影响 webui.py 的 web server
# （它用的是标准库 http.server 的裸 socket，跟 requests 库完全无关），
# 比之前全局 socket.setdefaulttimeout() 的做法精准得多。
if not getattr(_dingtalk_chatbot_module.requests.post, "_douyin_monitor_patched", False):
    _original_post = _dingtalk_chatbot_module.requests.post

    def _post_with_default_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        return _original_post(*args, **kwargs)

    _post_with_default_timeout._douyin_monitor_patched = True
    _dingtalk_chatbot_module.requests.post = _post_with_default_timeout


class DingTalkNotifier(BaseNotifier):
    """钉钉群自定义机器人推送。"""

    name = "dingtalk"

    def __init__(self, token: str, secret: str, default_at_mobiles: Optional[List[str]] = None):
        webhook = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
        self._bot = DingtalkChatbot(webhook, secret=secret)
        self.default_at_mobiles = default_at_mobiles or []

    def _safe_send(self, fn, *args, **kwargs) -> bool:
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            return self._log_result(False, str(e))
        errcode = result.get("errcode", 0) if isinstance(result, dict) else 0
        if errcode == 0:
            return self._log_result(True)
        return self._log_result(False, f"errcode={errcode} errmsg={result.get('errmsg')}")

    def send_text(self, title: str, content: str, at_mobiles: Optional[List[str]] = None) -> bool:
        mobiles = at_mobiles if at_mobiles is not None else self.default_at_mobiles
        text = f"#### {title}\n\n{content}"
        return self._safe_send(self._bot.send_markdown, title=title, text=text, at_mobiles=mobiles)

    def send_video(
        self,
        nickname: str,
        video_id: str,
        title: str,
        create_time: int,
        cover_url: Optional[str] = None,
        digg_count: int = 0,
        comment_count: int = 0,
        share_count: int = 0,
        collect_count: int = 0,
        duration_ms: int = 0,
        desc: str = "",
        gap_days: Optional[int] = None,
    ) -> bool:
        time_str = (
            datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S")
            if create_time
            else "未知"
        )
        video_url = f"https://www.douyin.com/video/{video_id}"
        cover_md = f"![cover]({cover_url})\n\n" if cover_url else ""

        # 标题行：使用 desc（含 # 话题标签）
        display_title = md_escape(desc) if desc else md_escape(title)

        # 时长
        duration_str = format_duration(duration_ms)

        # 互动数据（带文字标签，空格隔开）
        stats_line = (
            f"❤ 点赞: {format_count(digg_count)}   "
            f"💬 评论: {format_count(comment_count)}   "
            f"🔗 分享: {format_count(share_count)}   "
            f"⭐ 收藏: {format_count(collect_count)}"
        )

        gap_line = f"**距上次发布新视频间隔：** {gap_days} 天\n\n" if gap_days and gap_days >= 1 else ""

        text = (
            f"{cover_md}"
            f"**标题：** {display_title}\n\n"
            f"**数据：** {stats_line}\n\n"
            f"**时长：** {duration_str}\n\n"
            f"**发布时间：** {time_str}\n\n"
            f"{gap_line}"
            f"**检测时间：** {now_str()}"
        )

        # 卡片标题：nickname + 互动数据（无文字标签，空格隔开）
        card_title = (
            f"{nickname} "
            f"❤{format_count(digg_count)} "
            f"💬{format_count(comment_count)} "
            f"🔗{format_count(share_count)} "
            f"⭐{format_count(collect_count)}"
        )

        card = ActionCard(
            title=card_title,
            text=text,
            btns=[CardItem(title="观看视频", url=video_url)],
        )
        return self._safe_send(self._bot.send_action_card, card)

    def send_deleted(
        self,
        nickname: str,
        deleted_entries: List[Tuple[str, dict]],
        at_mobiles: Optional[List[str]] = None,
    ) -> bool:
        lines = []
        for _vid, meta in deleted_entries:
            title = md_escape(meta.get("title") or "(标题未知)")
            date_str = ""
            ct = meta.get("create_time")
            if ct:
                date_str = datetime.fromtimestamp(ct).strftime("%Y-%m-%d")
            lines.append(f"- **{title}**" + (f" ({date_str})" if date_str else ""))

        content = (
            f"用户：**{nickname}**\n\n"
            f"删除数量：**{len(deleted_entries)}** 条\n\n"
            f"被删除的视频：\n" + "\n".join(lines)
        )
        return self.send_text(f"{nickname} 删除了视频", content, at_mobiles)
