"""系统信息与受控系统动作（跨平台、标准库；危险动作都只提供函数，确认逻辑在命令层）。"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time


def uptime_text() -> str:
    try:
        if os.path.exists("/proc/uptime"):
            secs = float(open("/proc/uptime").read().split()[0])
        elif platform.system() == "Windows":
            import ctypes
            secs = ctypes.windll.kernel32.GetTickCount64() / 1000
        else:
            return "未知"
    except Exception:
        return "未知"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return f"{d} 天 {h} 小时 {m} 分" if d else f"{h} 小时 {m} 分"


def status_text(extra_disks: list[str] | None = None) -> str:
    lines = [f"系统：{platform.system()} {platform.release()}（{platform.machine()}）",
             f"主机：{platform.node()} · 已运行 {uptime_text()}",
             f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"]
    paths = [os.path.abspath(os.sep)] + [p for p in (extra_disks or []) if os.path.exists(p)]
    for p in dict.fromkeys(paths):
        try:
            u = shutil.disk_usage(p)
            lines.append(f"磁盘 {p}：共 {u.total / 1e9:.0f} GB · 剩余 {u.free / 1e9:.0f} GB（{u.free / u.total * 100:.0f}%）")
        except OSError:
            pass
    try:
        import resource
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        lines.append(f"本服务内存峰值约 {mem_mb:.0f} MB")
    except Exception:
        pass
    return "\n".join(lines)


def top_processes(limit: int = 8) -> str:
    try:
        if platform.system() == "Windows":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=10).stdout.strip().splitlines()
            names = [ln.split('","')[0].strip('"') for ln in out if ln.strip()]
            from collections import Counter
            top = Counter(names).most_common(limit)
            return "进程（按实例数）：" + "、".join(f"{n}×{c}" for n, c in top)
        out = subprocess.run(["ps", "-eo", "comm", "--sort=-pcpu"], capture_output=True,
                             text=True, timeout=10).stdout.strip().splitlines()[1:]
        return "进程（按 CPU）：" + "、".join(x.strip() for x in out[:limit] if x.strip())
    except Exception as e:
        return f"读取进程失败：{e}"


def _dry_run() -> bool:
    return bool(os.environ.get("COMMANDER_DRY_RUN"))


def lock_screen() -> str:
    if _dry_run():
        return "（演示模式）已锁屏"
    system = platform.system()
    if system == "Windows":
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return "已锁屏"
    if system == "Darwin":
        subprocess.run(["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession", "-suspend"],
                       capture_output=True, timeout=10)
        return "已锁屏"
    for cmd in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, capture_output=True, timeout=10)
            return "已锁屏"
    return "当前系统没有可用的锁屏方式"


def schedule_shutdown(minutes: int) -> str:
    if _dry_run():
        return f"（演示模式）已设定 {minutes} 分钟后关机"
    system = platform.system()
    if system == "Windows":
        subprocess.run(["shutdown", "/s", "/t", str(minutes * 60)], capture_output=True, timeout=10)
    else:
        subprocess.run(["shutdown", f"+{minutes}"], capture_output=True, timeout=10)
    return f"已设定 {minutes} 分钟后关机（/取消关机 可撤销）"


def cancel_shutdown() -> str:
    if _dry_run():
        return "（演示模式）已取消计划关机"
    system = platform.system()
    if system == "Windows":
        subprocess.run(["shutdown", "/a"], capture_output=True, timeout=10)
    else:
        subprocess.run(["shutdown", "-c"], capture_output=True, timeout=10)
    return "已取消计划关机"


def take_screenshot(dest_dir: str) -> str:
    """截图并保存到 dest_dir，返回文件路径。发送图片到微信需媒体上传流程（路线图），先回路径。"""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png")
    system = platform.system()
    try:
        if system == "Windows":
            ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                  "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
                  "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                  "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                  f"$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                  f"$bmp.Save('{path}')")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20)
        elif system == "Darwin":
            subprocess.run(["screencapture", "-x", path], capture_output=True, timeout=20)
        elif shutil.which("gnome-screenshot"):
            subprocess.run(["gnome-screenshot", "-f", path], capture_output=True, timeout=20)
        elif shutil.which("scrot"):
            subprocess.run(["scrot", path], capture_output=True, timeout=20)
        else:
            return "当前环境没有可用截图工具"
    except Exception as e:
        return f"截图失败：{e}"
    return f"截图已保存：{path}" if os.path.exists(path) else "截图失败（未生成文件）"
