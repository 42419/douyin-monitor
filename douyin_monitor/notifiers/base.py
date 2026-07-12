"""通知渠道抽象基类：所有推送渠道统一实现这三个方法。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Tuple

from ..utils import md_escape, now_str

__all__ = ["BaseNotifier", "format_count", "format_duration", "md_escape"]


def format_count(n: int) -> str:
    """格式化数字：1234 -> 1234, 12345 -> 1.2万, 12345678 -> 1234.5万"""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def format_duration(ms: int) -> str:
    """将毫秒时长格式化为可读字符串，如 38634 -> 38秒, 128000 -> 2分8秒"""
    if ms <= 0:
        return ""
    total_sec = ms // 1000
    if total_sec < 60:
        return f"{total_sec}秒"
    minutes = total_sec // 60
    seconds = total_sec % 60
    return f"{minutes}分{seconds}秒" if seconds else f"{minutes}分钟"


class BaseNotifier(ABC):
    """所有推送渠道的统一接口。

    子类只需要实现 send_text；send_video / send_deleted 默认会退化为
    调用 send_text（把内容拼成 Markdown 文本），渠道如果支持更丰富的
    卡片/按钮样式（比如钉钉的 ActionCard），可以自行覆盖。
    """

    name: str = "base"

    @abstractmethod
    def send_text(self, title: str, content: str, at_mobiles: Optional[List[str]] = None) -> bool:
        """发送一条纯文本/Markdown 通知，返回是否发送成功。"""
        raise NotImplementedError

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
        display_title = md_escape(desc) if desc else md_escape(title)
        duration_str = format_duration(duration_ms)
        stats_line = (
            f"❤ 点赞: {format_count(digg_count)}   "
            f"💬 评论: {format_count(comment_count)}   "
            f"🔗 分享: {format_count(share_count)}   "
            f"⭐ 收藏: {format_count(collect_count)}"
        )
        gap_line = f"**距上次发布新视频间隔：** {gap_days} 天\n\n" if gap_days and gap_days >= 1 else ""
        content = (
            f"**作者：** {md_escape(nickname)}\n\n"
            f"**标题：** {display_title}\n\n"
            f"**数据：** {stats_line}\n\n"
            f"**时长：** {duration_str}\n\n"
            f"**发布时间：** {time_str}\n\n"
            f"{gap_line}"
            f"**检测时间：** {now_str()}\n\n"
            f"**链接：** {video_url}"
        )
        return self.send_text(f"{nickname} 发布新视频", content)

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

    def _log_result(self, ok: bool, detail: str = "") -> bool:
        if ok:
            logging.info(f"[{self.name}] 推送成功")
        else:
            logging.error(f"[{self.name}] 推送失败{': ' + detail if detail else ''}")
        return ok
