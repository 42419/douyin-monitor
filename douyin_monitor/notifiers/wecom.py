"""企业微信群机器人推送。文档：https://developer.work.weixin.qq.com/document/path/91770"""

from __future__ import annotations

from typing import List, Optional

import requests

from ..config import HTTP_TIMEOUT
from .base import BaseNotifier


class WeComNotifier(BaseNotifier):
    """企业微信群自定义机器人推送（markdown 消息）。"""

    name = "wecom"

    def __init__(self, webhook_key: str):
        self._url = (
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
            f"?key={webhook_key}"
        )

    def send_text(self, title: str, content: str, at_mobiles: Optional[List[str]] = None) -> bool:
        text = f"#### {title}\n\n{content}"
        payload = {"msgtype": "markdown", "markdown": {"content": text}}
        try:
            resp = requests.post(self._url, json=payload, timeout=HTTP_TIMEOUT)
            data = resp.json() if resp.content else {}
            ok = resp.status_code == 200 and data.get("errcode") == 0
            return self._log_result(ok, str(data) if not ok else "")
        except requests.exceptions.RequestException as e:
            return self._log_result(False, str(e))
