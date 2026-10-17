"""平台层：把 Windows 专属的东西收在一处。

**为什么单独一个文件**：原来 Win32 调用散在 7 个文件、46 处，想支持
macOS / Linux 就得满仓库找。收拢之后，"这个平台有没有这个能力"是一个
函数调用的事。

**诚实的边界**：这个文件里的非 Windows 分支**没有在真机上验证过** ——
开发机是 Windows。所以每个回退都写成"能降级就降级，不能就安静地不做"，
而不是抛异常。宁可少一个功能，不能起不来。

Windows 走的是原来的快路径（逐像素 alpha 的分层窗口），一行没改。
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from typing import Optional

SYSTEM = platform.system()          # "Windows" / "Darwin" / "Linux"
IS_WINDOWS = SYSTEM == "Windows"
IS_MAC = SYSTEM == "Darwin"
IS_LINUX = SYSTEM == "Linux"


def open_in_file_manager(path: str) -> bool:
    """在文件管理器里打开一个目录（或定位到文件）。

    Windows 用 os.startfile；macOS 用 open；Linux 用 xdg-open。
    """
    if not path or not os.path.exists(path):
        return False
    try:
        if IS_WINDOWS:
            os.startfile(path)                           # noqa: S606
        elif IS_MAC:
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return True
    except Exception:                                    # noqa: BLE001
        return False


def play_sound_file(path: str) -> bool:
    """放一个音频文件。放不了就返回 False，不抛。

    Windows 用 winsound（只吃 wav）；macOS 用 afplay；Linux 试 paplay / aplay。
    """
    if not path or not os.path.exists(path):
        return False
    try:
        if IS_WINDOWS:
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return True
        if IS_MAC:
            subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        for player in ("paplay", "aplay", "ffplay"):
            if _which(player):
                args = [player, path]
                if player == "ffplay":
                    args = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
                subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
    except Exception:                                    # noqa: BLE001
        pass
    return False


def _which(name: str) -> Optional[str]:
    from shutil import which
    return which(name)


def system_language() -> str:
    """系统界面语言，返回 'zh' / 'en' / ''（认不出来）。

    Windows 走 GetUserDefaultUILanguage；其他平台看 LANG / LC_ALL。
    认不出来返回空串，让调用方决定默认值。
    """
    try:
        if IS_WINDOWS:
            import ctypes
            # 0x0804 = 简体中文
            if ctypes.windll.kernel32.GetUserDefaultUILanguage() == 0x0804:
                return "zh"
            return "en"
        lang = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
        if lang.lower().startswith("zh"):
            return "zh"
        if lang:
            return "en"
    except Exception:                                    # noqa: BLE001
        pass
    return ""


def set_dpi_awareness() -> None:
    """让 Windows 别对高分屏做模糊拉伸。其他平台不需要。"""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                                    # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:                                # noqa: BLE001
            pass


def dpi_scale() -> float:
    """当前显示缩放。拿不到就返回 1.0。

    Windows 用 GetDpiForSystem；其他平台暂时返回 1.0 ——
    macOS 的 Retina 由系统自己处理，Tk 给的坐标已经是逻辑像素。
    """
    if not IS_WINDOWS:
        return 1.0
    try:
        import ctypes
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi:
            return max(1.0, dpi / 96.0)
    except Exception:                                    # noqa: BLE001
        pass
    return 1.0


def idle_seconds() -> float:
    """系统空闲了多久（秒）。拿不到就返回 0。

    Windows 用 GetLastInputInfo；其他平台没有等价的无依赖方案，
    返回 0 表示"一直在用"—— 这样久坐提醒在 mac/Linux 上会一直生效，
    比"永远不提醒"更符合预期。
    """
    if not IS_WINDOWS:
        return 0.0
    try:
        import ctypes
        import ctypes.wintypes as w

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(0.0, millis / 1000.0)
    except Exception:                                    # noqa: BLE001
        return 0.0


def cursor_pos() -> tuple:
    """鼠标在屏幕上的位置。拿不到返回 (0, 0)。"""
    if not IS_WINDOWS:
        return (0, 0)
    try:
        import ctypes
        import ctypes.wintypes as w

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return (pt.x, pt.y)
    except Exception:                                    # noqa: BLE001
        return (0, 0)


def virtual_screen() -> tuple:
    """整个虚拟桌面的 (x, y, w, h)。多显示器时是并集。"""
    if IS_WINDOWS:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
            SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
            return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
                    user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
                    user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
                    user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        except Exception:                                # noqa: BLE001
            pass
    # 其他平台：Tk 自己知道屏幕多大（拿不到多屏并集，先用主屏）
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        w, h = r.winfo_screenwidth(), r.winfo_screenheight()
        r.destroy()
        return (0, 0, w, h)
    except Exception:                                    # noqa: BLE001
        return (0, 0, 1920, 1080)


def move_window(hwnd: int, root, x: int, y: int) -> bool:
    """把窗口搬到 (x, y)。

    Windows 走 SetWindowPos —— 只有它能**把窗口搬到屏幕外**（贴边隐藏要用；
    Tk 的 geometry() 会把窗口夹回可见区域，实测想放 -236 实际停在 0）。

    其他平台退回 Tk 的 geometry()：夹就夹吧，贴边隐藏在那儿会退化成
    "贴边但不藏起来"，比不能跑强。
    """
    if IS_WINDOWS:
        try:
            import ctypes
            SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
            return bool(ctypes.windll.user32.SetWindowPos(
                hwnd, 0, int(x), int(y), 0, 0,
                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE))
        except Exception:                                # noqa: BLE001
            return False
    try:
        root.geometry(f"+{int(x)}+{int(y)}")
        return True
    except Exception:                                    # noqa: BLE001
        return False


def foreground_other_window(title_hints) -> bool:
    """把标题里含任一关键词的窗口切到前台。找不到返回 False。

    只在 Windows 上实现 —— 其他平台要 Accessibility 权限，做不了就安静地不做。
    """
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        import ctypes.wintypes as w
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
        def cb(hwnd, _lp):
            n = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, n, 256)
            title = n.value or ""
            if any(h in title for h in title_hints) and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(cb, 0)
        if not found:
            return False
        user32.ShowWindow(found[0], 9)                   # SW_RESTORE
        user32.SetForegroundWindow(found[0])
        return True
    except Exception:                                    # noqa: BLE001
        return False


def set_window_icon(root) -> None:
    """给窗口设图标。失败无所谓。"""
    try:
        if IS_WINDOWS:
            root.iconbitmap(default="")
    except Exception:                                    # noqa: BLE001
        pass


def platform_note() -> str:
    """给用户看的一句话：这个平台上哪些功能会降级。"""
    if IS_WINDOWS:
        return ""
    missing = []
    if not IS_MAC:
        pass
    missing.append("全局热键")
    missing.append("开机自启")
    missing.append("窗口切前台")
    if IS_LINUX:
        missing.append("系统空闲检测")
    return "这个平台上暂时没有：" + "、".join(missing) + "（其他功能正常）"
