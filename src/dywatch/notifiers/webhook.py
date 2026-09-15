"""通用 webhook：只 POST 一个 JSON。

载荷形状是**我们的契约**，所以它被列进文档并且保持稳定：

    {"source": "dywatch", "event": "new_post", "severity": "info",
     "subject": "...", "text": "...", "markdown": "...",
     "sec_user_id": "...", "content_id": "..."}

想接自建服务、n8n、Home Assistant 之类的地方就用它——它不需要对方懂任何第三方格式。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import HttpChannel


@dataclass(frozen=True, slots=True)
class WebhookChannel(HttpChannel):
    name: str = "webhook"
    url: str = ""

    def request(self, message: Any) -> tuple[str, dict[str, Any]]:
        return self.url, message.as_dict()
