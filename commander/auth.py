"""主人鉴权：配对码绑定 + 危险命令二次确认。

- owner_ids 为空 → 配对模式：本机生成 6 位配对码，第一个发「/配对 <码>」的用户成为主人。
- 主人名单持久化在 state 目录，与 config 里的预置名单合并。
- 危险命令不立即执行：登记为待确认（默认 60 秒），主人回「确认」才执行、回其他内容取消。
"""

from __future__ import annotations

import json
import os
import random
import time

RISK_SAFE = "safe"
RISK_NORMAL = "normal"
RISK_DANGER = "danger"


class AuthManager:
    def __init__(self, state_dir: str, preset_owners: list[str] | None = None,
                 confirm_ttl: int = 60):
        self.state_dir = state_dir
        self.preset = list(preset_owners or [])
        self.confirm_ttl = confirm_ttl
        self.pairing_code = f"{random.randint(0, 999999):06d}" if not self.owners() else ""
        self._pending: dict[str, tuple[object, float]] = {}  # user_id -> (action, expires)

    # ---------- 主人名单 ----------
    def _path(self):
        return os.path.join(self.state_dir, "owners.json")

    def _saved(self) -> list[str]:
        try:
            return json.load(open(self._path(), encoding="utf-8")).get("owners", [])
        except (OSError, ValueError):
            return []

    def owners(self) -> list[str]:
        return sorted(set(self.preset) | set(self._saved()))

    def is_owner(self, user_id: str) -> bool:
        return user_id in self.owners()

    def try_pair(self, user_id: str, code: str) -> bool:
        if self.owners() or not self.pairing_code:
            return False
        if code.strip() != self.pairing_code:
            return False
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self._path(), "w", encoding="utf-8") as f:
            json.dump({"owners": [user_id]}, f, ensure_ascii=False)
        self.pairing_code = ""
        return True

    # ---------- 二次确认 ----------
    def set_pending(self, user_id: str, action) -> None:
        self._pending[user_id] = (action, time.time() + self.confirm_ttl)

    def pop_pending(self, user_id: str):
        item = self._pending.pop(user_id, None)
        if not item:
            return None
        action, expires = item
        return action if time.time() <= expires else None

    def has_pending(self, user_id: str) -> bool:
        item = self._pending.get(user_id)
        return bool(item and time.time() <= item[1])
