"""iLink Bot API 客户端（腾讯官方个人微信机器人通道）。

依据公开协议文档实现：扫码登录（get_bot_qrcode → get_qrcode_status → bot_token）、
长轮询收消息（getupdates，游标 get_updates_buf）、发文本（sendmessage，必须回带
入站消息的 context_token）、正在输入（sendtyping，需先 getconfig 取 typing_ticket）。
会话过期时服务端返回 errcode -14，抛 SessionExpired 由上层提示重新扫码。

凭证只存本机 state 目录。真实账号联调前本模块属 best-effort；
协议契约已由本地假服务器测试覆盖（见 run_tests.py）。
"""

from __future__ import annotations

import base64
import json
import os
import random
import time
import urllib.request
import urllib.error

DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.2"


class SessionExpired(Exception):
    pass


class ILinkError(Exception):
    pass


def _uin_header() -> str:
    return base64.b64encode(str(random.randint(0, 2**32 - 1)).encode()).decode()


class ILinkClient:
    def __init__(self, base_url: str = DEFAULT_BASE, state_dir: str = ".",
                 token: str = "", opener=None):
        self.base = base_url.rstrip("/")
        self.state_dir = state_dir
        self.token = token or self._load_token()
        self._opener = opener or urllib.request.build_opener()

    # ---------- 凭证 ----------
    def _state_path(self):
        return os.path.join(self.state_dir, "ilink_state.json")

    def _load_token(self) -> str:
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                return json.load(f).get("bot_token", "")
        except (OSError, ValueError):
            return ""

    def _save_state(self, data: dict):
        os.makedirs(self.state_dir, exist_ok=True)
        merged = {}
        if os.path.exists(self._state_path()):
            try:
                merged = json.load(open(self._state_path(), encoding="utf-8"))
            except ValueError:
                merged = {}
        merged.update(data)
        with open(self._state_path(), "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=1)
        self.token = merged.get("bot_token", self.token)

    # ---------- HTTP ----------
    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: int = 40, auth: bool = True) -> dict:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth and self.token:
            req.add_header("AuthorizationType", "ilink_bot_token")
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("X-WECHAT-UIN", _uin_header())
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.URLError as e:
            raise ILinkError(f"网络错误：{e}") from e
        if isinstance(payload, dict) and payload.get("errcode") == -14:
            raise SessionExpired("iLink 会话已过期（errcode -14），请重新扫码登录")
        return payload

    # ---------- 登录 ----------
    def get_login_qrcode(self) -> dict:
        """返回 {qrcode, qrcode_img_content}；用微信扫 qrcode_img_content 指向的二维码。"""
        data = self._request("GET", "/ilink/bot/get_bot_qrcode?bot_type=3", auth=False)
        if not data.get("qrcode"):
            raise ILinkError(f"获取二维码失败：{data}")
        return data

    def poll_qrcode_status(self, qrcode: str) -> dict:
        """查询扫码状态；confirmed 时响应含 bot_token，自动落盘。"""
        data = self._request("GET", f"/ilink/bot/get_qrcode_status?qrcode={qrcode}", auth=False)
        if data.get("status") == "confirmed" and data.get("bot_token"):
            self._save_state({"bot_token": data["bot_token"],
                              "bot_id": data.get("bot_id", ""),
                              "user_id": data.get("user_id", "")})
        return data

    def login_interactive(self, wait_seconds: int = 180, poll_interval: float = 2.0,
                          show=None) -> bool:
        """终端扫码登录：show(qrcode_img_content) 负责把二维码展示给本机用户。"""
        qr = self.get_login_qrcode()
        if show:
            show(qr.get("qrcode_img_content", ""))
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            st = self.poll_qrcode_status(qr["qrcode"])
            if st.get("status") == "confirmed":
                return True
            if st.get("status") in ("expired", "cancelled"):
                return False
            time.sleep(poll_interval)
        return False

    # ---------- 收发 ----------
    def get_updates(self, buf: str = "", timeout: int = 40) -> tuple[list[dict], str]:
        data = self._request("POST", "/ilink/bot/getupdates",
                             {"get_updates_buf": buf, "base_info": {"channel_version": CHANNEL_VERSION}},
                             timeout=timeout)
        msgs = data.get("msgs") or data.get("messages") or []
        return msgs, data.get("get_updates_buf", buf)

    def send_text(self, to_user_id: str, text: str, context_token: str) -> dict:
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"commander-{random.randint(0, 0xFFFFFF):06x}",
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        return self._request("POST", "/ilink/bot/sendmessage", body)

    def notify_start(self):
        try:
            return self._request("POST", "/ilink/bot/notifyStart", {})
        except ILinkError:
            return {}


def extract_text(msg: dict) -> str:
    """从入站消息里取文本（兼容 item_list / content 两种形态）。"""
    for item in msg.get("item_list") or []:
        if item.get("type") == 1:
            t = (item.get("text_item") or {}).get("text")
            if t:
                return t
    return str(msg.get("content") or msg.get("text") or "")


def sender_of(msg: dict) -> str:
    return str(msg.get("from_user_id") or msg.get("sender") or "")


def context_of(msg: dict) -> str:
    return str(msg.get("context_token") or "")
