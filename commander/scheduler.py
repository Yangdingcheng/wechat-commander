"""定时调度：一次性提醒 + 每日重复（把一条命令文本按时重新投递给命令分发器）。

条目持久化为 JSON；tick 由主循环周期调用。不依赖系统 cron，进程在则有效。
"""

from __future__ import annotations

import json
import os
import time
import uuid


def parse_delay(text: str) -> int | None:
    """'30m' / '2h' / '45'（分钟） → 秒；解析不了返回 None。"""
    t = text.strip().lower()
    try:
        if t.endswith("m"):
            return int(t[:-1]) * 60
        if t.endswith("h"):
            return int(t[:-1]) * 3600
        return int(t) * 60
    except ValueError:
        return None


def parse_hhmm(text: str) -> tuple[int, int] | None:
    try:
        h, m = text.strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except ValueError:
        pass
    return None


def next_daily_ts(h: int, m: int, now: float | None = None) -> float:
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    if target <= now:
        target += 86400
    return target


class Scheduler:
    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, "schedule.json")
        self.items: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            return json.load(open(self.path, encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=1)

    def add_once(self, delay_s: int, action_text: str, kind: str = "command") -> dict:
        item = {"id": uuid.uuid4().hex[:6], "kind": kind, "text": action_text,
                "run_at": time.time() + delay_s, "daily": False}
        self.items.append(item)
        self._save()
        return item

    def add_daily(self, hhmm: str, action_text: str) -> dict | None:
        parsed = parse_hhmm(hhmm)
        if not parsed:
            return None
        item = {"id": uuid.uuid4().hex[:6], "kind": "command", "text": action_text,
                "run_at": next_daily_ts(*parsed), "daily": True, "hhmm": hhmm}
        self.items.append(item)
        self._save()
        return item

    def cancel(self, item_id: str) -> bool:
        before = len(self.items)
        self.items = [i for i in self.items if i["id"] != item_id]
        if len(self.items) != before:
            self._save()
            return True
        return False

    def list_text(self) -> str:
        if not self.items:
            return "没有定时条目"
        lines = []
        for i in sorted(self.items, key=lambda x: x["run_at"]):
            when = time.strftime("%m-%d %H:%M", time.localtime(i["run_at"]))
            tag = f"每天 {i.get('hhmm')}" if i.get("daily") else when
            lines.append(f"[{i['id']}] {tag} → {i['text']}")
        return "\n".join(lines)

    def due(self, now: float | None = None) -> list[dict]:
        """取出到期条目；每日条目自动顺延到明天。"""
        now = now if now is not None else time.time()
        fired, keep = [], []
        for i in self.items:
            if i["run_at"] <= now:
                fired.append(i)
                if i.get("daily"):
                    h, m = parse_hhmm(i["hhmm"])
                    keep.append({**i, "run_at": next_daily_ts(h, m, now)})
            else:
                keep.append(i)
        if fired:
            self.items = keep
            self._save()
        return fired
