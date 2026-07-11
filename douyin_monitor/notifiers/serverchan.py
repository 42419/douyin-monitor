"""Server 酱（Turbo）推送，可转发到微信。文档：https://sct.ftqq.com/"""

from __future__ import annotations

from typing import List, Optional

import requests

from ..config import HTTP_TIMEOUT
from .base import BaseNotifier


class ServerChanNotifier(BaseNotifier):
    """Server 酱 Turbo 推送。"""

    name = "serverchan"

    def __init__(self, sendkey: str):
        self._sendkey = sendkey
        if sendkey.startswith("sctp"):
            # Server 酱³ Pro 版接口路径不同
            self._url = f"https://{sendkey}.push.ft07.com/send"
        else:
            self._url = f"https://sctapi.ftqq.com/{sendkey}.send"

    def send_text(self, title: str, content: str, at_mobiles: Optional[List[str]] = None) -> bool:
        payload = {"title": title, "desp": content}
        try:
            resp = requests.post(self._url, data=payload, timeout=HTTP_TIMEOUT)
            data = resp.json() if resp.content else {}
            ok = resp.status_code == 200 and data.get("code") == 0
            return self._log_result(ok, str(data) if not ok else "")
        except requests.exceptions.RequestException as e:
            return self._log_result(False, str(e))
