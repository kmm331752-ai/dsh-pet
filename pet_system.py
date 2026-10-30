"""系统集成：全局热键、更新检查、开机自启。"""

from __future__ import annotations

import pet_platform

import ctypes
import ctypes.wintypes as wintypes
import json
import math
import os
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from pet_base import *


# ─────────────────────────────────────────────────────────────────────────────
# RegisterHotKey 的 hwnd 传 NULL 时，WM_HOTKEY 会投递到【线程】的消息队列，
# 所以必须自己起一个线程跑 GetMessage —— Tk 的消息循环拿不到我们的消息。
# 回调不直接碰界面（Tk 只能在主线程操作），而是把动作名塞进队列，
# 由主线程的 _drain_tray 消费 —— 和托盘回调走同一条路。

MOD_ALT, MOD_CONTROL, MOD_SHIFT = 0x0001, 0x0002, 0x0004
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
VK_P, VK_F, VK_S = 0x50, 0x46, 0x53
VK_H, VK_D = 0x48, 0x44
VK_M = 0x4D

HOTKEYS = {
    1: (MOD_CONTROL | MOD_ALT, VK_P, "toggle"),      # 显示 / 隐藏
    2: (MOD_CONTROL | MOD_ALT, VK_F, "feed"),        # 喂一口
    3: (MOD_CONTROL | MOD_ALT, VK_S, "settings"),    # 打开设置
    4: (MOD_CONTROL | MOD_ALT, VK_H, "ghost"),       # 截图模式：整个隐藏（连托盘也不显示）
    5: (MOD_CONTROL | MOD_ALT, VK_D, "focus"),       # 专注模式：她安静待机不冒泡
    6: (MOD_CONTROL | MOD_ALT, VK_M, "music"),       # 播放 / 暂停
}

# 番茄钟时长选项（分钟）
POMODORO_CHOICES = (25, 45, 60)


class HotKeys(threading.Thread):
    """全局热键监听线程。命中的动作名交给 sink，由主线程处理。"""

    def __init__(self, sink):
        super().__init__(name="dsh-pet-hotkeys", daemon=True)
        self.sink = sink
        self.registered: list = []

    def run(self) -> None:
        # 全局热键只有 Windows 做得到（RegisterHotKey）。其他平台直接返回，
        # 线程空转一下就好 —— 别让 ctypes.windll 在 macOS 上抛 AttributeError。
        if not pet_platform.IS_WINDOWS:
            return
        u = ctypes.windll.user32
        for hid, (mods, vk, _) in HOTKEYS.items():
            if u.RegisterHotKey(None, hid, mods, vk):
                self.registered.append(hid)
        if not self.registered:
            return
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                entry = HOTKEYS.get(int(msg.wParam))
                if entry is not None:
                    try:
                        self.sink(entry[2])
                    except Exception:                    # noqa: BLE001
                        pass

    def stop(self) -> None:
        """注销并让消息循环退出（PostThreadMessage 打断 GetMessage）。"""
        if not pet_platform.IS_WINDOWS:
            return
        u = ctypes.windll.user32
        for hid in self.registered:
            u.UnregisterHotKey(None, hid)
        self.registered = []
        try:
            u.PostThreadMessageW(self.ident, WM_QUIT, 0, 0)
        except Exception:                                # noqa: BLE001
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 自动更新检查
# ─────────────────────────────────────────────────────────────────────────────
# 只查 GitHub Releases 的最新 tag，不自动下载 —— 装不装由用户决定。
# 网络不通、限流、被墙都只算「查不到」，绝不能让桌宠因此启动不了或弹错误。

REPO = "kmm331752-ai/dsh-pet"
APP_VERSION = "1.21.3"
UPDATE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"


def _ver_tuple(text: str) -> tuple:
    """把 v1.2.3 变成 (1,2,3)；解析不出来就当 (0,)。"""
    nums = []
    for part in str(text).lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums) or (0,)


def check_update(timeout: float = 6.0) -> Optional[str]:
    """有新版本就返回它的 tag，否则 None。"""
    import urllib.request
    try:
        req = urllib.request.Request(
            UPDATE_URL, headers={"User-Agent": f"dsh-pet/{APP_VERSION}",
                                 "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        tag = str(data.get("tag_name") or "")
        if tag and _ver_tuple(tag) > _ver_tuple(APP_VERSION):
            return tag
    except Exception:                                    # noqa: BLE001
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 开机自启（写 HKCU 的 Run 项，不需要管理员）
# ─────────────────────────────────────────────────────────────────────────────

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "DSH-Pet"


def entry_script() -> str:
    """**真正的入口脚本** —— `dsh_pet.py`，不是本文件。

    踩过：`4d3f645` 那次「把 3300 行的单文件拆成 6 个模块」，把开机自启
    这段搬到了 `pet_system.py`，而里面写的是：

        return f'"{exe}" "{os.path.abspath(__file__)}"'

    `__file__` 于是从 `dsh_pet.py` 变成了 `pet_system.py` —— 而
    `pet_system.py` 只有常量和 winreg 封装，**没有 `main()`**。
    开机跑起来是个瞬间退出、什么都不发生的进程。

    用户在设置里勾着「开机自启」，重启之后桌宠不出现，而且**没有任何
    报错**。和规则 2 说的「功能安静地不工作」是同一个形状。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "dsh_pet.py")


def autostart_command() -> str:
    """开机自启要跑的命令行。打包成 exe 之后就是 exe 自己。"""
    if getattr(sys, "frozen", False):                    # PyInstaller 打的包
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return f'"{exe}" "{entry_script()}"'


def autostart_enabled() -> bool:
    """开机自启。只有 Windows 实现了（注册表 Run 键）。

    其他平台返回 False —— 别让"读一下状态"就崩。
    """
    if not pet_platform.IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
        return True
    except Exception:                                    # noqa: BLE001
        return False


def repair_autostart() -> str:
    """把注册表里指错的自启命令改回来。返回发生了什么。

    **光修写入端不够。** 已经写进去的错值不会自己好 —— `4d3f645` 之后
    勾过自启的用户，注册表里到现在都还是 `pet_system.py`。

    所以每次启动查一遍：开着自启、但命令不是我们要的 → 重写。

    返回：
      · ``"ok"``      没开自启，或者本来就是对的
      · ``"fixed"``   发现指错了，已经改回来
      · ``"off"``     不是 Windows / 没有注册表权限
    """
    if not pet_platform.IS_WINDOWS:
        return "off"
    try:
        import winreg
        want = autostart_command()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            try:
                cur, _kind = winreg.QueryValueEx(key, AUTOSTART_NAME)
            except FileNotFoundError:
                return "ok"                              # 没开自启，不用管
        # **按 normcase 比。** Windows 路径不区分大小写，字符串 == 区分 ——
        # 于是 `DeepSeekWork` 和 `DeepSeekwork` 会被当成"指错了"，
        # 每次启动都重写一遍、还往日志里记一条。
        if os.path.normcase(str(cur)) == os.path.normcase(want):
            return "ok"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, want)
        log_error(f"自启命令指错了，已改回：{cur!r} -> {want!r}",
                  RuntimeError("autostart repaired"))
        return "fixed"
    except Exception:                                    # noqa: BLE001
        return "off"


def set_autostart(on: bool) -> bool:
    """开/关自启。非 Windows 直接返回 False。"""
    if not pet_platform.IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            if on:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:                             # noqa: BLE001
        log_error("设置开机自启失败", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 桌宠窗口
