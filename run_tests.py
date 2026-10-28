#!/usr/bin/env python3
"""微信遥控台测试：python run_tests.py（鉴权 / 命令 / 调度 / 任务 / iLink 契约）"""

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ["COMMANDER_DRY_RUN"] = "1"  # 测试不真的关机/锁屏

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from commander.app import CommanderApp, MockTransport
from commander.auth import AuthManager
from commander.ilink import ILinkClient, SessionExpired, extract_text
from commander.scheduler import Scheduler, next_daily_ts, parse_delay, parse_hhmm

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name)
    assert cond, name

# ---------- 鉴权 ----------
d = tempfile.mkdtemp()
auth = AuthManager(d)
code = auth.pairing_code
check("配对码生成", len(code) == 6)
check("配对-错码拒绝", auth.try_pair("u1", "000000") is False or code == "000000")
check("配对-正确绑定", auth.try_pair("u1", code) is True and auth.is_owner("u1"))
check("配对-绑定后失效", AuthManager(d).pairing_code == "" and AuthManager(d).is_owner("u1"))
auth2 = AuthManager(d, confirm_ttl=-1)
auth2.set_pending("u1", lambda: "x")
check("确认-过期作废", auth2.pop_pending("u1") is None)

# ---------- 调度 ----------
check("延时解析", parse_delay("30m") == 1800 and parse_delay("2h") == 7200 and parse_delay("45") == 2700)
check("时刻解析", parse_hhmm("07:30") == (7, 30) and parse_hhmm("25:00") is None)
check("每日下一次在未来", next_daily_ts(0, 0) > time.time())
sch = Scheduler(tempfile.mkdtemp())
item = sch.add_once(-10, "喝水", kind="remind")  # 已到期
fired = sch.due()
check("到期触发且移除", len(fired) == 1 and sch.list_text() == "没有定时条目")
daily = sch.add_daily("23:59", "/状态")
fired = sch.due(now=daily["run_at"] + 1)
check("每日触发后顺延", len(fired) == 1 and sch.items[0]["run_at"] > daily["run_at"])
check("取消条目", sch.cancel(daily["id"]) is True and sch.items == [])

# ---------- 应用与命令（MockTransport） ----------
state = tempfile.mkdtemp()
filedir = tempfile.mkdtemp()
open(os.path.join(filedir, "季度报告2026.txt"), "w").write("x")
cfg = {"state_dir": state, "owners": [], "chat_backend": "echo",
       "file_dirs": [filedir],
       "jobs": {"测试任务": {"cmd": [sys.executable, "-c", "print('job-ok')"], "timeout": 60}}}
transport = MockTransport()
app = CommanderApp(cfg, transport)
me = "wxid_me"

def say(uid, text):
    before = len(transport.sent)
    transport.inbox = [(uid, text)]
    app.step()
    new = transport.sent[before:]
    return new[0][1] if new else None

check("陌生人被忽略", say("wxid_x", "/状态") is None)
rcode = app.auth.pairing_code
check("配对绑定", "绑定成功" in (say(me, f"/配对 {rcode}") or ""))
check("状态命令", "系统" in (say(me, "/状态") or ""))
check("帮助含危险标记", "关机" in (say(me, "/help") or ""))
check("文件查找", "季度报告2026" in (say(me, "/文件 报告") or ""))
check("自由文本走回声", "（回声）" in (say(me, "你好") or ""))
check("未知指令提示", "没有" in (say(me, "/不存在") or ""))
prompt = say(me, "/关机")
check("危险命令要确认", prompt is not None and "确认" in prompt)
done = say(me, "确认")
check("确认后执行（演示）", done is not None and "演示模式" in done)
prompt = say(me, "/锁屏")
say(me, "/时间")
check("改发他话取消确认", (say(me, "确认") or "") == "没有待确认的操作")
check("提醒登记", "提醒你" in (say(me, "/提醒 30m 开会") or ""))
app.scheduler.items[-1]["run_at"] = time.time() - 1
before = len(transport.sent)
app.step()
check("提醒到期推送", any("⏰ 提醒：开会" in t for _u, t in transport.sent[before:]))
check("每日登记", "已设定每天" in (say(me, "/每天 07:30 /状态") or ""))
say(me, "/跑 测试任务")
say(me, "确认")
deadline = time.time() + 8
got = False
while time.time() < deadline:
    if any("job-ok" in t for _u, t in transport.sent):
        got = True
        break
    time.sleep(0.2)
check("本地任务执行并回传", got)
audit_lines = open(os.path.join(state, "audit.jsonl"), encoding="utf-8").read().strip().splitlines()
check("审计落盘", len(audit_lines) >= 10)

# ---------- iLink 契约（本地假服务器） ----------
captured = {}
class FakeILink(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if "get_bot_qrcode" in self.path:
            self._json({"qrcode": "q1", "qrcode_img_content": "https://example/qr"})
        elif "get_qrcode_status" in self.path:
            self._json({"status": "confirmed", "bot_token": "tok123", "bot_id": "b1"})
        else:
            self._json({})
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if "getupdates" in self.path:
            if body.get("get_updates_buf") == "EXPIRE":
                return self._json({"errcode": -14})
            if body.get("get_updates_buf"):
                return self._json({"msgs": [], "get_updates_buf": "buf2"})
            return self._json({"msgs": [{"from_user_id": "u1", "context_token": "ctx9",
                                         "item_list": [{"type": 1, "text_item": {"text": "/状态"}}]}],
                                "get_updates_buf": "buf2"})
        if "sendmessage" in self.path:
            captured["body"] = body
            captured["auth"] = self.headers.get("Authorization")
            return self._json({})
        self._json({})

server = HTTPServer(("127.0.0.1", 0), FakeILink)
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]
client = ILinkClient(base_url=f"http://127.0.0.1:{port}", state_dir=tempfile.mkdtemp())
qr = client.get_login_qrcode()
check("iLink-取二维码", qr["qrcode"] == "q1")
st = client.poll_qrcode_status("q1")
check("iLink-扫码确认存token", st["status"] == "confirmed" and client.token == "tok123")
msgs, buf = client.get_updates("")
check("iLink-收消息解析", len(msgs) == 1 and extract_text(msgs[0]) == "/状态" and buf == "buf2")
client.send_text("u1", "你好", "ctx9")
check("iLink-发消息带context_token", captured["body"]["msg"]["context_token"] == "ctx9"
      and captured["body"]["msg"]["item_list"][0]["text_item"]["text"] == "你好")
check("iLink-鉴权头", captured["auth"] == "Bearer tok123")
try:
    client.get_updates("EXPIRE")
    check("iLink-会话过期抛错", False)
except SessionExpired:
    check("iLink-会话过期抛错", True)
server.shutdown()

print(f"\n全部 {len(PASS)} 项测试通过 ✅" if all(c for _, c in PASS) else "\n存在失败 ❌")
