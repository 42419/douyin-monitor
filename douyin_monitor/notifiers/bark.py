"""Bark (iOS) 推送。文档：https://bark.day.app/"""

from __future__ import annotations

from typing import List, Optional

import requests

from ..config import HTTP_TIMEOUT
from .base import BaseNotifier


class BarkNotifier(BaseNotifier):
    """通过 Bark 服务器推送到 iOS 设备。"""

    name = "bark"

    def __init__(self, server: str, device_key: str):
        self._server = server.rstrip("/")
        self._device_key = device_key

    def send_text(self, title: str, content: str, at_mobiles: Optional[List[str]] = None) -> bool:
        url = f"{self._server}/{self._device_key}"
        payload = {
            "title": title,
            "body": content,
            "group": "douyin-monitor",
        }
        try:
            resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            data = resp.json() if resp.content else {}
            ok = resp.status_code == 200 and data.get("code") == 200
            return self._log_result(ok, str(data) if not ok else "")
        except requests.exceptions.RequestException as e:
            return self._log_result(False, str(e))
