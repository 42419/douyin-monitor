"""每用户运行状态管理（JSON 持久化）。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict


class UserState:
    """每个用户一个 JSON 状态文件：STATE_DIR/{sec_user_id}.json"""

    def __init__(self, path: Path):
        self.path = path
        self.data: Dict = self._load()

    def _load(self) -> Dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logging.warning(f"状态文件损坏，将重新初始化: {self.path}")
        return {
            "sec_user_id": None,
            "nickname": None,
            "initialized_at": None,
            "last_update_at": None,
            "videos": {},
            "pending_deletes": {},
            "consecutive_fails": 0,
            "last_fail_alert_at": None,
            "fail_alerted": False,
            "resp_hash": None,
            "resp_hash_since": None,
            "last_stale_alert_at": None,
            "last_hash_stale_alert_at": None,
            "last_fallback_stale_alert_at": None,
            "stale_alerted": False,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
