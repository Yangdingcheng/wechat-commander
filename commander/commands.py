"""命令注册表与分发器：解析 → 鉴权 → 风险分级 → 执行/待确认 → 回执。

分发规则：
- 非主人消息一律忽略（配对指令除外，且仅在未绑定主人时有效）。
- 危险命令先登记待确认，主人 60 秒内回复「确认」才执行。
- 不以 / 开头的自由文本转对话后端（AI 或回声）。
"""

from __future__ import annotations

import os
import subprocess
import time

from . import sysinfo
from .auth import RISK_DANGER, RISK_NORMAL, RISK_SAFE, AuthManager
from .backends import JobRunner
from .scheduler import Scheduler, parse_delay


class Dispatcher:
    def __init__(self, auth: AuthManager, scheduler: Scheduler, jobs: JobRunner,
                 config: dict, send, chat_backend=None):
        self.auth = auth
        self.scheduler = scheduler
        self.jobs = jobs
        self.config = config
        self.send = send  # send(user_id, text) 供异步结果回传
        self.chat_backend = chat_backend
        self.commands = {
            "help": (RISK_SAFE, self._help, "指令列表"),
            "状态": (RISK_SAFE, self._status, "电脑状态（系统/磁盘/运行时间）"),
            "时间": (RISK_SAFE, lambda u, a: time.strftime("现在 %Y-%m-%d %H:%M:%S"), "当前时间"),
            "磁盘": (RISK_SAFE, self._disks, "磁盘余量"),
            "进程": (RISK_SAFE, lambda u, a: sysinfo.top_processes(), "主要进程"),
            "任务": (RISK_SAFE, self._tasks, "定时条目与可用任务"),
            "提醒": (RISK_NORMAL, self._remind, "/提醒 30m 喝水 — 定时提醒"),
            "每天": (RISK_NORMAL, self._daily, "/每天 07:30 /稿件 — 每日定时执行"),
            "取消": (RISK_NORMAL, self._cancel, "/取消 <编号> — 取消定时条目"),
            "文件": (RISK_NORMAL, self._file, "/文件 报告 — 在白名单目录里找文件"),
            "后端": (RISK_NORMAL, self._backend, "查看对话后端"),
            "截图": (RISK_NORMAL, self._shot, "截屏并回传路径"),
            "关机": (RISK_DANGER, self._shutdown, "/关机 [分钟] — 定时关机"),
            "取消关机": (RISK_SAFE, lambda u, a: sysinfo.cancel_shutdown(), "取消计划关机"),
            "锁屏": (RISK_DANGER, lambda u, a: sysinfo.lock_screen(), "锁定屏幕"),
            "打开": (RISK_DANGER, self._open_app, "/打开 记事本 — 打开白名单程序"),
            "跑": (RISK_DANGER, self._run_job, "/跑 稿件 — 运行登记的本地任务"),
        }
        if "稿件" in self.jobs.jobs:
            self.commands["稿件"] = (RISK_DANGER, lambda u, a: self._run_job(u, "稿件"), "运行「稿件」任务")

    # ---------- 主入口 ----------
    def handle(self, user_id: str, text: str) -> str | None:
        text = (text or "").strip()
        if not text:
            return None
        if text.startswith("/配对"):
            code = text.replace("/配对", "", 1).strip()
            return "绑定成功，你已成为主人。发 /help 查看指令。" if self.auth.try_pair(user_id, code) else None
        if not self.auth.is_owner(user_id):
            return None
        if text == "确认":
            action = self.auth.pop_pending(user_id)
            if action is None:
                return "没有待确认的操作"
            return action()
        if self.auth.has_pending(user_id):
            self.auth.pop_pending(user_id)  # 发了别的内容 = 取消待确认
        if not text.startswith("/"):
            if self.chat_backend is not None:
                return self.chat_backend.chat(user_id, text)
            return "我是遥控台，只执行指令。发 /help 查看。"
        parts = text[1:].split(None, 1)
        name = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        entry = self.commands.get(name)
        if entry is None:
            return f"没有「{name}」这个指令，发 /help 查看全部。"
        risk, handler, desc = entry
        if risk == RISK_DANGER:
            self.auth.set_pending(user_id, lambda: handler(user_id, args))
            return f"⚠️ 危险操作：{desc}。60 秒内回复「确认」执行，回复其他内容取消。"
        return handler(user_id, args)

    # ---------- 各命令 ----------
    def _help(self, u, a):
        lines = ["可用指令（危险操作会要求二次确认）："]
        for name, (risk, _h, desc) in self.commands.items():
            mark = " ⚠️" if risk == RISK_DANGER else ""
            lines.append(f"/{name} — {desc}{mark}")
        lines.append("直接发文字 = 和对话后端聊天（可关）")
        return "\n".join(lines)

    def _status(self, u, a):
        return sysinfo.status_text(self.config.get("extra_disks"))

    def _disks(self, u, a):
        lines = [ln for ln in sysinfo.status_text(self.config.get("extra_disks")).splitlines() if "磁盘" in ln]
        return "\n".join(lines) or "读不到磁盘信息"

    def _tasks(self, u, a):
        job_lines = "、".join(self.jobs.names()) or "无"
        running = [n for n in self.jobs.names() if self.jobs.is_running(n)]
        return (f"已登记任务：{job_lines}\n运行中：{'、'.join(running) or '无'}\n"
                f"定时条目：\n{self.scheduler.list_text()}")

    def _remind(self, u, a):
        parts = a.split(None, 1)
        if len(parts) < 2:
            return "用法：/提醒 30m 喝水（支持 30m / 2h / 45=分钟）"
        delay = parse_delay(parts[0])
        if delay is None:
            return "时间格式不对，用 30m / 2h / 45"
        item = self.scheduler.add_once(delay, parts[1], kind="remind")
        return f"好，{delay // 60} 分钟后提醒你：{parts[1]}（编号 {item['id']}）"

    def _daily(self, u, a):
        parts = a.split(None, 1)
        if len(parts) < 2:
            return "用法：/每天 07:30 /稿件"
        item = self.scheduler.add_daily(parts[0], parts[1])
        if item is None:
            return "时间格式不对，用 HH:MM，如 07:30"
        return f"已设定每天 {parts[0]} 执行：{parts[1]}（编号 {item['id']}）"

    def _cancel(self, u, a):
        target = a.strip()
        return "已取消" if self.scheduler.cancel(target) else f"没有编号 {target} 的条目"

    def _file(self, u, a):
        keyword = a.strip()
        if not keyword:
            return "用法：/文件 关键词"
        hits = []
        for root in self.config.get("file_dirs", []):
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if keyword.lower() in fn.lower():
                        p = os.path.join(dirpath, fn)
                        try:
                            hits.append((p, os.path.getsize(p)))
                        except OSError:
                            pass
                if len(hits) >= 10:
                    break
        if not hits:
            return f"白名单目录里没找到含「{keyword}」的文件"
        return "\n".join(f"{p}（{s / 1024:.0f} KB）" for p, s in hits[:10])

    def _backend(self, u, a):
        name = getattr(self.chat_backend, "name", "关闭")
        return f"当前对话后端：{name}（自由文本会转给它；命令不受影响）"

    def _shot(self, u, a):
        dest = os.path.join(self.config.get("state_dir", "state"), "shots")
        return sysinfo.take_screenshot(dest)

    def _shutdown(self, u, a):
        try:
            minutes = int(a.strip()) if a.strip() else 1
        except ValueError:
            return "用法：/关机 10（10 分钟后关机）"
        return sysinfo.schedule_shutdown(max(1, minutes))

    def _open_app(self, u, a):
        apps = self.config.get("apps", {})
        name = a.strip()
        target = apps.get(name)
        if not target:
            return f"「{name}」不在白名单程序里（可用：{'、'.join(apps) or '无'}）"
        try:
            subprocess.Popen([target])
            return f"已打开 {name}"
        except Exception as e:
            return f"打开失败：{e}"

    def _run_job(self, u, a):
        name = a.strip()
        if not name:
            return f"用法：/跑 任务名（可用：{'、'.join(self.jobs.names()) or '无'}）"
        return self.jobs.start(name, on_done=lambda result: self.send(u, result))
