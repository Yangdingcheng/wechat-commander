"""应用装配：传输层（iLink / 演示）+ 分发器 + 调度 tick + 审计 + 本地管理页。"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from .auth import AuthManager
from .backends import EchoBackend, JobRunner, OpenAIBackend
from .commands import Dispatcher
from .ilink import ILinkClient, SessionExpired, context_of, extract_text, sender_of
from .scheduler import Scheduler


class AuditLog:
    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, "audit.jsonl")
        os.makedirs(state_dir, exist_ok=True)

    def write(self, user_id: str, text: str, result: str):
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "user": user_id,
               "text": text[:200], "result": (result or "")[:200]}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def tail(self, n: int = 50) -> str:
        try:
            lines = open(self.path, encoding="utf-8").read().strip().splitlines()
            return "\n".join(lines[-n:]) or "（暂无记录）"
        except OSError:
            return "（暂无记录）"


class MockTransport:
    """离线演示/测试传输：scripted 入站消息 + 记录出站。"""

    def __init__(self, scripted: list[tuple[str, str]] | None = None):
        self.inbox = list(scripted or [])
        self.sent: list[tuple[str, str]] = []

    def poll(self) -> list[tuple[str, str, str]]:
        out, self.inbox = self.inbox, []
        return [(u, t, "ctx-demo") for u, t in out]

    def send(self, user_id: str, text: str) -> bool:
        self.sent.append((user_id, text))
        return True


class ILinkTransport:
    def __init__(self, client: ILinkClient):
        self.client = client
        self.buf = ""
        self._ctx: dict[str, str] = {}

    def poll(self) -> list[tuple[str, str, str]]:
        msgs, self.buf = self.client.get_updates(self.buf)
        out = []
        for m in msgs:
            uid, text, ctx = sender_of(m), extract_text(m), context_of(m)
            if uid and text:
                self._ctx[uid] = ctx
                out.append((uid, text, ctx))
        return out

    def send(self, user_id: str, text: str) -> bool:
        ctx = self._ctx.get(user_id, "")
        if not ctx:
            return False
        try:
            self.client.send_text(user_id, text, ctx)
            return True
        except Exception:
            return False


class CommanderApp:
    def __init__(self, config: dict, transport):
        self.config = config
        self.state_dir = config.get("state_dir", "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.transport = transport
        self.auth = AuthManager(self.state_dir, preset_owners=config.get("owners"))
        self.scheduler = Scheduler(self.state_dir)
        self.jobs = JobRunner(config.get("jobs"))
        self.audit = AuditLog(self.state_dir)
        backend = self._make_backend(config)
        self.dispatcher = Dispatcher(self.auth, self.scheduler, self.jobs, config,
                                     send=lambda uid, txt: self.transport.send(uid, txt),
                                     chat_backend=backend)
        self.session_expired = False
        self._stopped = False

    @staticmethod
    def _make_backend(config: dict):
        ai = config.get("ai") or {}
        if ai.get("enabled") and ai.get("api_key"):
            return OpenAIBackend(ai.get("base_url", "https://api.deepseek.com/v1"),
                                 ai["api_key"], ai.get("model", "deepseek-chat"))
        if config.get("chat_backend") == "echo":
            return EchoBackend()
        return None

    def step(self):
        """处理一轮：收消息 → 分发 → 回复；再触发到期定时条目。"""
        try:
            incoming = self.transport.poll()
        except SessionExpired:
            self.session_expired = True
            raise
        for user_id, text, _ctx in incoming:
            reply = self.dispatcher.handle(user_id, text)
            self.audit.write(user_id, text, reply or "(忽略)")
            if reply:
                self.transport.send(user_id, reply)
        for item in self.scheduler.due():
            owners = self.auth.owners()
            if not owners:
                continue
            owner = owners[0]
            if item["kind"] == "remind":
                self.transport.send(owner, f"⏰ 提醒：{item['text']}")
                self.audit.write("scheduler", item["text"], "提醒已发送")
            else:
                reply = self.dispatcher.handle(owner, item["text"])
                if reply:
                    self.transport.send(owner, reply)
                self.audit.write("scheduler", item["text"], reply or "(无回执)")

    def run_forever(self, interval: float = 1.0):
        print("微信遥控台已启动。")
        if self.auth.pairing_code:
            print(f"尚未绑定主人：在微信里给机器人发 /配对 {self.auth.pairing_code} 完成绑定。")
        while not self._stopped:
            try:
                self.step()
            except SessionExpired:
                print("⚠️ iLink 会话已过期，请重新运行 --login 扫码。服务退出。")
                return
            except Exception as e:
                print(f"轮询异常（继续）：{e}")
            time.sleep(interval)

    def stop(self):
        self._stopped = True


def serve_admin(app: CommanderApp, port: int):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/audit"):
                body = app.audit.tail(50)
            elif self.path.startswith("/commands"):
                body = app.dispatcher.handle(app.auth.owners()[0] if app.auth.owners() else "?", "/help") or ""
            else:
                owners = app.auth.owners()
                body = (f"微信遥控台状态\n主人数量：{len(owners)}\n"
                        f"会话过期：{app.session_expired}\n"
                        f"配对码：{app.auth.pairing_code or '（已绑定或未启用）'}\n"
                        f"定时条目：\n{app.scheduler.list_text()}\n\n/audit 看审计 · /commands 看指令")
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
