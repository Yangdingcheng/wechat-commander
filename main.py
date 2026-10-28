#!/usr/bin/env python3
"""微信遥控台入口。

用法：
  python main.py --demo     离线演示（模拟一段微信对话，不连真微信、不真的关机）
  python main.py --login    扫码登录 iLink（在本机完成，凭证只存本地 state/）
  python main.py            正式运行（读 config.json；管理页 http://127.0.0.1:8760）
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from commander.app import CommanderApp, ILinkTransport, MockTransport, serve_admin
from commander.ilink import ILinkClient

BASE = os.path.dirname(os.path.abspath(__file__))


def load_config() -> dict:
    for name in ("config.json", "config.example.json"):
        p = os.path.join(BASE, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("state_dir", os.path.join(BASE, "state"))
            return cfg
    return {"state_dir": os.path.join(BASE, "state"), "chat_backend": "echo"}


def run_demo():
    import tempfile
    os.environ["COMMANDER_DRY_RUN"] = "1"  # 演示不真的关机/锁屏
    cfg = load_config()
    cfg.update({"chat_backend": "echo", "owners": [], "jobs": {},
                "state_dir": tempfile.mkdtemp()})
    transport = MockTransport()
    app = CommanderApp(cfg, transport)
    code = app.auth.pairing_code
    me, stranger = "wxid_me", "wxid_stranger"
    script = [
        (me, f"/配对 {code}"),
        (me, "/状态"),
        (me, "/提醒 1m 检查流水线"),
        (stranger, "/关机"),
        (me, "/关机"),
        (me, "确认"),
        (me, "今天天气不错"),
        (me, "/任务"),
    ]
    print("=== 微信遥控台离线演示（模拟对话）===\n")
    for uid, text in script:
        before = len(transport.sent)
        transport.inbox = [(uid, text)]
        app.step()
        who = "主人" if uid == me else "陌生人"
        print(f"{who}：{text}")
        for _u, reply in transport.sent[before:]:
            print(f"机器人：{reply}")
        print()
    audit_n = len(open(os.path.join(cfg["state_dir"], "audit.jsonl")).readlines())
    print(f"（演示结束：审计日志 {audit_n} 条；关机为演示模式，未真正执行）")


def main():
    ap = argparse.ArgumentParser(description="微信遥控台")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--login", action="store_true")
    args = ap.parse_args()
    cfg = load_config()

    if args.demo:
        run_demo()
        return
    if args.login:
        client = ILinkClient(state_dir=cfg["state_dir"])
        ok = client.login_interactive(show=lambda url: print(f"请用微信扫描二维码：{url}"))
        print("登录成功，凭证已存本机。" if ok else "登录未完成，请重试。")
        return

    client = ILinkClient(state_dir=cfg["state_dir"])
    if not client.token:
        print("还没有 iLink 凭证，先运行：python main.py --login")
        return
    client.notify_start()
    app = CommanderApp(cfg, ILinkTransport(client))
    port = int(cfg.get("admin_port", 8760))
    serve_admin(app, port)
    print(f"本地管理页：http://127.0.0.1:{port}")
    app.run_forever()


if __name__ == "__main__":
    main()
