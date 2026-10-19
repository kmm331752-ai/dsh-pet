"""活动源：知道你在用什么软件。

## 为什么这个能零新依赖做到

`GetForegroundWindow` + `GetWindowThreadProcessId` + `QueryFullProcessImageNameW`
都是 Win32 基础接口，`ctypes` 直接调 —— 这个项目本来就在用 ctypes，
不用装 psutil 或 pywin32。

实测读出来：`DeepSeek Harness.exe  pid=26328  '介绍一下你 — DeepSeek Harness'`

## 拿到这些干什么

不是为了监控，是为了**她别在该安静的时候吵你**：

  · 你在写代码 → 她安静待着，不弹话
  · 你在看视频 → 她跟着看（有音乐源的话知道在看什么）
  · 你在开会 / 视频通话 → 绝对不打扰
  · 同一个应用待太久 → 提醒你起来动动

## 隐私

**只读进程名和窗口标题，不记录、不落盘、不联网。**
窗口标题可能有敏感信息（文档名、聊天对象），所以标题**只在内存里**，
用来分类，不写进 `pet_mood.json`。这一点写在设置窗口的隐私说明里。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import time
from dataclasses import dataclass
from typing import Optional

# 前台窗口轮询间隔。比音乐慢一点够了 —— 切应用没那么频繁。
POLL_INTERVAL = 2.0

# 同一个应用待多久算"待太久"（提醒起来动动）
SAME_APP_LONG_S = 50 * 60

# ── 应用分类 ────────────────────────────────────────────────────────────────
# 按进程名匹配（小写，不带 .exe）。分这几类是因为**她该有的反应不同**。
CATEGORIES = {
    "coding": {
        "code", "cursor", "devenv", "pycharm64", "idea64", "webstorm64",
        "goland64", "clion64", "rider64", "sublime_text", "notepad++",
        "windowsterminal", "wt", "powershell", "pwsh", "cmd", "conhost",
        "mintty", "alacritty", "wezterm-gui", "tabby", "hyper",
        "deepseek harness", "electron", "node", "python", "pythonw",
        "git-bash", "bash", "wsl", "ubuntu", "vim", "nvim", "emacs",
        "android studio", "xcode", "fleet", "zed", "helix",
    },
    "browsing": {
        "chrome", "msedge", "firefox", "brave", "opera", "vivaldi",
        "safari", "arc", "chromium", "thorium", "librewolf",
    },
    "video": {
        "vlc", "mpc-hc64", "mpc-hc", "potplayermini64", "potplayer",
        "mpv", "kmplayer", "gomplayer", "bilibili", "youku", "iqiyi",
        "tencentvideo", "qqlive", "migu", "netflix", "prime video",
    },
    "music": {
        "cloudmusic", "qqmusic", "kugou", "kuwo", "spotify", "itunes",
        "applemusic", "foobar2000", "musicbee", "aimp", "wmplayer",
        "netease", "music.ui",
    },
    "meeting": {
        "zoom", "teams", "ms-teams", "webexmta", "dingtalk", "wemeetapp",
        "tencentmeeting", "feishu", "lark", "slack", "discord",
        "skype", "gotomeeting", "voov",
    },
    "game": {
        "steam", "steamwebhelper", "epicgameslauncher", "battle.net",
        "genshinimpact", "yuanshen", "starrail", "leagueclient",
        "dota2", "cs2", "valorant", "minecraft", "javaw",
    },
    "chat": {
        "wechat", "weixin", "qq", "tim", "telegram", "whatsapp",
        "signal", "line", "messenger",
    },
    "design": {
        "photoshop", "illustrator", "figma", "xd", "blender",
        "afterfx", "premiere", "davinci resolve", "krita", "gimp",
        "inkscape", "affinity", "clipstudiopaint", "sai2",
    },
    "office": {
        "winword", "excel", "powerpnt", "onenote", "outlook",
        "wps", "et", "wpp", "acrobat", "acrord32", "sumatrapdf",
        "notion", "obsidian", "typora", "onenote",
    },
}

# 反向索引：进程名 -> 类别
_BY_PROCESS = {}
for _cat, _names in CATEGORIES.items():
    for _n in _names:
        _BY_PROCESS[_n] = _cat


def classify(process: str) -> str:
    """按进程名猜类别。认不出来返回 'other'。

    去掉 .exe 后缀再匹配。也试前缀匹配 —— 有些应用版本号在名字里
    （`potplayermini64` 已经在表里，但 `code-insiders` 这种没有）。
    """
    if not process:
        return "other"
    p = process.lower()
    if p.endswith(".exe"):
        p = p[:-4]
    if p in _BY_PROCESS:
        return _BY_PROCESS[p]
    # 前缀匹配：取第一个匹配的（表不大，线性扫够了）
    for name, cat in _BY_PROCESS.items():
        if p.startswith(name) or name.startswith(p):
            return cat
    return "other"


@dataclass
class Activity:
    """当前前台是什么。"""

    process: str = ""               # 进程名（含 .exe）
    title: str = ""                 # 窗口标题（**只在内存里**）
    pid: int = 0
    category: str = "other"
    since: float = 0.0              # 这个应用从什么时候开始在前台

    @property
    def known(self) -> bool:
        return bool(self.process)

    def label(self) -> str:
        """给人看的名字：进程名去掉 .exe。"""
        return self.process[:-4] if self.process.lower().endswith(".exe") else self.process

    def same_app(self, other: Optional["Activity"]) -> bool:
        if other is None or not self.process or not other.process:
            return False
        return self.process.lower() == other.process.lower()

    def stuck_for(self, now: Optional[float] = None) -> float:
        """同一个应用待了多久（秒）。"""
        if not self.since:
            return 0.0
        now = now if now is not None else time.time()
        return max(0.0, now - self.since)


# ── Win32 ───────────────────────────────────────────────────────────────────
_user32 = ctypes.windll.user32 if os.name == "nt" else None
_kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _foreground() -> tuple:
    """(进程名, 窗口标题, pid)。拿不到返回 ('', '', 0)。

    不走 psutil —— 这几个都是 Win32 基础调用，ctypes 直接调。
    """
    if _user32 is None:
        return ("", "", 0)
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ("", "", 0)

        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value

        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        name = ""
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            try:
                b = ctypes.create_unicode_buffer(512)
                size = wintypes.DWORD(512)
                if _kernel32.QueryFullProcessImageNameW(h, 0, b, ctypes.byref(size)):
                    name = b.value.rsplit("\\", 1)[-1]
            finally:
                _kernel32.CloseHandle(h)

        return (name, title, int(pid.value))
    except Exception:                                    # noqa: BLE001
        return ("", "", 0)


class ActivitySource:
    """前台应用。**同步的，很快**（几个 Win32 调用），不需要线程。

    实测这几个调用是微秒级，直接在主循环里调没问题。
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and _user32 is not None
        self._last: Optional[Activity] = None
        self._last_poll = 0.0

    @property
    def available(self) -> bool:
        return self.enabled

    def poll(self) -> Optional[Activity]:
        """当前前台应用。带缓存 —— `POLL_INTERVAL` 内直接返回上次的。"""
        if not self.enabled:
            return None
        now = time.time()
        if self._last is not None and (now - self._last_poll) < POLL_INTERVAL:
            return self._last
        self._last_poll = now

        proc, title, pid = _foreground()
        if not proc:
            return self._last

        prev = self._last
        act = Activity(
            process=proc, title=title, pid=pid,
            category=classify(proc),
        )
        # 还是同一个应用就沿用起始时间，换了就重新计时
        if prev is not None and prev.same_app(act):
            act.since = prev.since or now
        else:
            act.since = now
        self._last = act
        return act
