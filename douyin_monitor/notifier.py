"""钉钉群机器人推送通知。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional, Tuple

from dingtalkchatbot.chatbot import ActionCard, CardItem, DingtalkChatbot

from .utils import md_escape, now_str


class DingTalkNotifier:
    """钉钉群自定义机器人推送。"""

    def __init__(self, token: str, secret: str, default_at_mobiles: Optional[List[str]] = None):
        webhook = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
        self._bot = DingtalkChatbot(webhook, secret=secret)
        self.default_at_mobiles = default_at_mobiles or []

    def _safe_send(self, fn, *args, **kwargs) -> bool:
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            logging.error(f"钉钉推送异常: {e}")
            return False
        errcode = result.get("errcode", 0) if isinstance(result, dict) else 0
        if errcode == 0:
            logging.info("钉钉推送成功")
            return True
        logging.error(f"钉钉返回错误: errcode={errcode} errmsg={result.get('errmsg')}")
        return False

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
    ) -> bool:
        time_str = (
            datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S")
            if create_time
            else "未知"
        )
        video_url = f"https://www.douyin.com/video/{video_id}"
        cover_md = f"![cover]({cover_url})\n\n" if cover_url else ""
        text = (
            f"{cover_md}**作者：** {md_escape(nickname)}\n\n"
            f"**标题：** {md_escape(title)}\n\n"
            f"**发布时间：** {time_str}\n\n"
            f"**检测时间：** {now_str()}"
        )
        card = ActionCard(
            title=f"{nickname} 发布新视频",
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
            f"被删除的视频：\n" + "\n".join(lines) + "\n\n---\n"
            f"{now_str()}"
        )
        return self.send_text(f"{nickname} 删除了视频", content, at_mobiles)
