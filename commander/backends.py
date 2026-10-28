"""可插拔后端：自由文本对话（AI）与本地任务（脚本）。

- EchoBackend：离线回声（演示/自检）。
- OpenAIBackend：OpenAI 兼容接口（DeepSeek 等），标准库 urllib 实现；没配 key 时不可用。
- JobRunner：执行 config 里登记的本地任务命令（白名单），后台线程跑，输出尾部回传。
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.request


class EchoBackend:
    name = "echo"

    def chat(self, user_id: str, text: str) -> str:
        return f"（回声）{text}"


class OpenAIBackend:
    name = "ai"

    def __init__(self, base_url: str, api_key: str, model: str, system: str = ""):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.model = model
        self.system = system or "你是微信遥控台的对话后端，回答简短，只用纯文本，不用 Markdown。"
        self._history: dict[str, list[dict]] = {}

    def chat(self, user_id: str, text: str) -> str:
        hist = self._history.setdefault(user_id, [])
        msgs = [{"role": "system", "content": self.system}] + hist[-8:] + [{"role": "user", "content": text}]
        body = json.dumps({"model": self.model, "messages": msgs}).encode()
        req = urllib.request.Request(self.base + "/chat/completions", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.key}")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            reply = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return f"AI 后端暂时不可用：{e}"
        hist.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
        return reply[:1500]


class JobRunner:
    """登记在 config.jobs 里的本地任务：{名称: {cmd: [...], cwd: ...}}，白名单制。"""

    def __init__(self, jobs: dict):
        self.jobs = jobs or {}
        self._running: dict[str, dict] = {}
        self._lock = threading.Lock()

    def names(self) -> list[str]:
        return sorted(self.jobs)

    def is_running(self, name: str) -> bool:
        with self._lock:
            return name in self._running

    def start(self, name: str, on_done) -> str:
        spec = self.jobs.get(name)
        if spec is None:
            return f"没有登记名为「{name}」的任务（可用：{'、'.join(self.names()) or '无'}）"
        with self._lock:
            if name in self._running:
                return f"任务「{name}」正在运行中"
            self._running[name] = {"started": True}

        def _run():
            try:
                proc = subprocess.run(spec["cmd"], cwd=spec.get("cwd") or None,
                                      capture_output=True, text=True, timeout=spec.get("timeout", 1800))
                tail = (proc.stdout or "")[-800:] + (("\n" + proc.stderr[-300:]) if proc.stderr else "")
                result = f"任务「{name}」完成（退出码 {proc.returncode}）\n{tail.strip()[-800:]}"
            except subprocess.TimeoutExpired:
                result = f"任务「{name}」超时被终止"
            except Exception as e:
                result = f"任务「{name}」启动失败：{e}"
            with self._lock:
                self._running.pop(name, None)
            on_done(result)

        threading.Thread(target=_run, daemon=True).start()
        return f"任务「{name}」已启动，完成后把结果发你"
