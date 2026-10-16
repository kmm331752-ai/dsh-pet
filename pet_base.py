#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSH 桌宠 (Desktop Pet)
======================
一只悬浮在桌面上的小宠物，实时显示 DeepSeek Harness 的任务运行状态。

状态来源（全部为只读，不干扰 DSH 运行）:
  1. <DSH_HOME>/storages/session_projcache/sessions/session-<id>.json
     — 明文投影缓存，含 openStep / pendingCalls / 模型 / 上下文压力 / 标题
  2. <DSH_HOME>/sessions/**/session-<id>/session*.jsonl.zstd
     — 追加写的 zstd 事件日志，按帧魔数增量解码，取 tool/call 的工具名

四个状态:
  idle      空闲 —— 等待你的指令
  thinking  思考 —— 模型正在生成（openStep 存在，无挂起工具）
  tools     执行 —— 有工具调用在跑（pendingCalls 非空），显示工具名
  offline   离线 —— 没找到任何 DSH 会话

用法:
  python dsh_pet.py                 # 启动桌宠
  python dsh_pet.py --demo          # 每 3 秒轮换状态预览
  python dsh_pet.py --shot out      # 不建窗口，把 4 个状态渲染成 out-<state>.png
  python dsh_pet.py --session <id>  # 固定监视某个会话
"""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import glob
import json
import math
import os
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from ctypes import wintypes
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

# ─────────────────────────────────────────────────────────────────────────────
# 基本配置
# ─────────────────────────────────────────────────────────────────────────────


DSH_HOME = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
PROJ_DIR = os.path.join(DSH_HOME, "storages", "session_projcache", "sessions")
SESS_DIR = os.path.join(DSH_HOME, "sessions")


def app_dir() -> str:
    """只读资源所在目录。

    打包成单文件 exe 后，PyInstaller 会把资源解到临时目录（sys._MEIPASS），
    源码运行就是脚本自己所在目录。
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    """可写目录：配置与相处记录放这儿。

    exe 运行时解包目录是临时的，配置必须落在 exe 旁边才留得住。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ── 可变路径 ────────────────────────────────────────────────────────────────
# 这些刻意做成**函数**而不是模块级常量。
# 拆文件之前它们是常量，测试直接改那个常量就能换目录；
# 拆开之后常量会被复制进每个模块，改一处不影响别处 —— 静默失效。
# 函数只有一个真实来源（data_dir），改它全都跟着变。

def config_path() -> str:
    return os.path.join(data_dir(), "pet_config.json")


def error_log_path() -> str:
    """出错日志。exe 模式下 stderr 是没人看的 —— 用户只会说"它不显示了"，
    所以任何异常都要落盘，让问题可追。"""
    return os.path.join(data_dir(), "pet_error.log")


def mood_path() -> str:
    return os.path.join(data_dir(), "pet_mood.json")


def lines_path() -> str:
    return os.path.join(data_dir(), "pet_lines.json")


def record_path() -> str:
    """导出的相处记录图。"""
    return os.path.join(data_dir(), "相处记录.png")


def week_path() -> str:
    """导出的周报图。"""
    return os.path.join(data_dir(), "周报.png")


def sounds_dir() -> str:
    """自定义音效包目录。放 <事件>.wav 就会盖掉系统音。"""
    return os.path.join(data_dir(), "sounds")


# 事件 -> Windows 系统事件音。
# 用系统别名而不是自带 wav：不增加体积，而且用户换系统主题时音效跟着变。
# 想换掉就在 sounds/ 里放同名 wav。
SOUND_ALIASES = {
    "done": "SystemAsterisk",       # 任务完成
    "failed": "SystemHand",         # 出错
    "waiting": "SystemExclamation",  # 等你回答
    "pomo": "SystemNotification",   # 番茄钟到点
    "feed": "SystemDefault",        # 喂食
    "pet": "SystemDefault",         # 被摸
}


# ── 高 DPI ──────────────────────────────────────────────────────────────────
# 不做 DPI 感知的话，Windows 会把窗口位图拉伸到物理像素 —— 4K 屏上
# 整只桌宠是糊的，字也糊。做了感知就得自己按 DPI 放大尺寸，
# 否则 248 逻辑像素在 200% 缩放下会变成半个手掌大。

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def set_dpi_awareness() -> bool:
    """声明本进程自己做 DPI 缩放。返回是否成功。

    PerMonitorV2 是 Win10 1703+ 才有的；老系统退回 System DPI 感知，
    再不行就什么都不做（模糊但能用）。
    """
    try:
        user32 = ctypes.windll.user32
    except Exception:                                    # noqa: BLE001
        return False
    try:
        if user32.SetProcessDpiAwarenessContext(
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return True
    except Exception:                                    # noqa: BLE001
        pass
    try:
        # Win8.1+：PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return True
    except Exception:                                    # noqa: BLE001
        pass
    try:
        user32.SetProcessDPIAware()                      # Vista+
        return True
    except Exception:                                    # noqa: BLE001
        return False


def dpi_scale() -> float:
    """主显示器的缩放倍数（96 DPI = 1.0）。取不到就当 1.0。"""
    try:
        # GetDpiForSystem 是 Win10 1607+；老系统用桌面 DC 的 LOGPIXELSX
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
            if dpi:
                return max(1.0, min(4.0, dpi / 96.0))
        except Exception:                                # noqa: BLE001
            pass
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            LOGPIXELSX = 88
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi:
            return max(1.0, min(4.0, dpi / 96.0))
    except Exception:                                    # noqa: BLE001
        pass
    return 1.0


# 单实例锁用的本地端口（只绑 127.0.0.1，不对外）
SINGLE_INSTANCE_PORT = 51937

WIN_W = 248                      # 窗口逻辑宽度
PET_AREA_H = 182                 # 角色区高度
DETAIL_CARD_H = 148              # 详情展开时的信息卡高度
WIN_H = PET_AREA_H + DETAIL_CARD_H
# 滚轮缩放范围。窗口和内容一起缩，太小会看不清信息卡，太大又挡屏幕。
SCALE_MIN, SCALE_MAX = 0.6, 2.0
SS = 2                           # 超采样倍数（素材 306px，2 倍即原生分辨率）
POLL_MS = 350                    # 状态轮询间隔
TICK_MS = 95                     # 动画帧间隔

# 调色板：每个状态一套（状态色 + 文案）
PALETTE = {
    "idle":        dict(accent="#4A8FD4", label="空闲",   dot="#7FB3E8"),
    "thinking":    dict(accent="#7B5CC7", label="思考中", dot="#A98BE6"),
    "tools":       dict(accent="#C97A22", label="执行中", dot="#F2A65A"),
    "waiting":     dict(accent="#C74C77", label="等你回答", dot="#EE7BA0"),
    "failed":      dict(accent="#B93B37", label="出错重试", dot="#E8615C"),
    "celebrating": dict(accent="#2E9E63", label="已完成", dot="#5FC98B"),
    "offline":     dict(accent="#6B7075", label="离线",   dot="#9AA0A6"),
    # ── 陪伴模式的状态（没有 DSH 任务时她自己过日子）──
    "roaming":     dict(accent="#3F9E9E", label="溜达",   dot="#6FC7C7"),
    "gazing":      dict(accent="#4E8FB8", label="看你",   dot="#7FBEDC"),
    "peeking":     dict(accent="#6E8FB0", label="探头",   dot="#9BB8D4"),
    "yawning":     dict(accent="#8A7BA8", label="犯困",   dot="#B3A6CC"),
    "dozing":      dict(accent="#5E6E92", label="打盹",   dot="#8B9BC0"),
    "sleeping":    dict(accent="#4A5568", label="睡着了", dot="#7A8699"),
    "waking":      dict(accent="#C79A3C", label="刚醒",   dot="#E8C173"),
    "petted":      dict(accent="#D2658F", label="好舒服", dot="#EE92B4"),
}

# 隐私模式下的通用副行（不泄露提示词内容）
GENERIC_DETAIL = {
    "thinking": "模型正在生成",
    "tools": "工具调用进行中",
    "waiting": "等待你的回答",
    "failed": "请求出错，重试中",
    "celebrating": "任务已完成",
    "idle": "等待你的下一条指令",
    "offline": "未发现 DSH 会话",
}

INK = (38, 40, 46, 255)          # 描边色（配件用）
CARD_BG = (24, 26, 32, 238)      # 信息卡背景
CARD_LINE = (72, 78, 92, 255)
TEXT_MAIN = (240, 243, 248, 255)
TEXT_DIM = (152, 160, 176, 255)

STATE_ORDER = ["idle", "thinking", "tools", "waiting", "failed", "celebrating", "offline",
               "roaming", "gazing", "peeking", "yawning", "dozing", "sleeping", "waking",
               "petted"]

# 状态优先级：越小越优先成为「聚合状态」——
# 等你回答最要紧（卡在你这），其次出错，再是干活
STATE_RANK = {"waiting": 0, "failed": 1, "tools": 2, "thinking": 3, "celebrating": 4, "idle": 5}
ACTIVE_STATES = ("waiting", "failed", "tools", "thinking")

# 陪伴模式才用的状态：只有 DSH 那边一个活跃会话都没有时才会出现
COMPANION_STATES = ("roaming", "gazing", "peeking", "yawning", "dozing",
                    "sleeping", "waking", "petted")
# 这些状态不该被 DSH 的任务状态覆盖掉（睡着了就别被"空闲"叫醒）
COMPANION_HOLD = ("dozing", "sleeping", "waking", "petted")

CELEBRATE_S = 6.0                # 完成庆祝持续秒数

# ── 交互参数 ────────────────────────────────────────────────────────────────
HOVER_DELAY = 1.0                # 鼠标在她身上停多久，她注意到你
LONG_PRESS_MS = 2000             # 按住不动多久算「抱起来」
SHAKE_WINDOW = 0.6               # 判定甩动的时间窗（秒）
SHAKE_DIRS = 5                   # 窗口内要换几次方向才算甩
SHAKE_DIZZY_S = 3.2              # 甩晕持续秒数
ANNOY_AFTER = 5                  # 连续摸几次会烦
ANNOY_WINDOW = 6.0               # 连续摸的判定窗口（秒）
ANNOY_S = 2.6                    # 烦多久
EDGE_SNAP = 6                    # 拖到离屏幕边缘多近算「抓住」（像素）
GRAVITY = 2.2                    # 松手下落加速度
TASKBAR_MARGIN = 8               # 落地时离屏幕底部留多少
GAZE_FRESH = 2.5                 # 鼠标动过多久之内她会看过去（秒）
GAZE_MIN_DX = 90                 # 鼠标离她多远才值得转头（像素）
GAZE_HOLD = 1.6                  # 看多久
WANDER_STEP = 3                  # 溜达时每帧移动多少像素（越小越自然）

# 角色素材来自 1190fasheqi/dafeiyu-pet（MIT），见 assets/ATTRIBUTION.md


def assets_dir() -> str:
    return os.path.join(app_dir(), "assets")


def skins_dir() -> str:
    """皮肤目录。

    注意这里**调 assets_dir() 而不是读 ASSETS 常量** —— 常量在
    `from pet_base import *` 时会被复制进每个模块，测试改一处不影响别处。
    函数体里的名字是在 **pet_base 自己的命名空间**里解析的，所以只要
    测试改 pet_base.assets_dir，所有模块（pet_render / pet_skin_install）
    看到的都跟着变，哪怕它们各自持有一份 skins_dir 的引用。

    这个坑在 SKINS_DIR 上踩了两次：第一次是安装器写进临时目录而
    discover_skins 还在看真目录，第二次是改成函数后仍然改不动 ——
    因为函数里读的还是常量。
    """
    return os.path.join(assets_dir(), "skins")


# 素材根目录（渲染时用）。要换目录请改 assets_dir()
ASSETS = assets_dir()


VIEW_FOR_STATE = {"idle": "front", "thinking": "front", "tools": "side",
                  "waiting": "front", "failed": "front",
                  "celebrating": "front", "offline": "back",
                  # 陪伴模式：溜达用侧身，睡着翻到背面（缩成一团的感觉）
                  "roaming": "side", "gazing": "side", "peeking": "side",
                  "yawning": "front", "dozing": "front",
                  "sleeping": "back", "waking": "front", "petted": "front"}
_VIEW_CACHE: dict = {}
_GLOW_CACHE: dict = {}

# ─────────────────────────────────────────────────────────────────────────────
# Win32 分层窗口（逐像素透明）
# ─────────────────────────────────────────────────────────────────────────────
# tkinter 本身无法做逐像素透明；用 WS_EX_LAYERED + UpdateLayeredWindow 推送
# 一张带 alpha 的 32 位位图，即可得到平滑边缘的真透明窗口。
# 若该路径不可用（非 Windows / 调用失败），回退到 -transparentcolor 方案。

WS_EX_LAYERED = 0x00080000
GWL_EXSTYLE = -20
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def _premultiplied_bgra(img: Image.Image) -> bytes:
    """UpdateLayeredWindow 要求预乘 alpha 的 BGRA 字节序。

    用 ImageChops.multiply（C 实现）做逐通道乘 alpha，再让 PIL 直接吐出
    BGRA 原始字节 —— 比 Python 逐像素循环快两个数量级。
    """
    from PIL import ImageChops
    r, g, b, a = img.convert("RGBA").split()
    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)
    return Image.merge("RGBA", (r, g, b, a)).tobytes("raw", "BGRA")


def _rgba(color, alpha: int = 255):
    """十六进制色串 -> RGBA 元组（已是元组则替换 alpha）。"""
    if isinstance(color, tuple):
        return color[:3] + (alpha,)
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha)


class LayeredDisplay:
    """逐像素透明窗口后端。"""

    def __init__(self, root, w: int, h: int):
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32
        self.w, self.h = w, h

        root.update_idletasks()
        wid = root.winfo_id()
        parent = self.user32.GetParent(wid)
        self.hwnd = parent if parent else wid

        ex = self.user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        self.user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)

        self._screen_dc = self.user32.GetDC(0)
        self._mem_dc = self.gdi32.CreateCompatibleDC(self._screen_dc)
        self._bitmap = None
        self._bits = None

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h                                # 负数 = 自上而下
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = BI_RGB
        self._bmi = bmi

        self._ppv = ctypes.c_void_p()
        self._bitmap = self.gdi32.CreateDIBSection(
            self._mem_dc, ctypes.byref(self._bmi), 0, ctypes.byref(self._ppv), None, 0)
        if not self._bitmap:
            raise OSError("CreateDIBSection failed")
        self._bits = self._ppv

        self._old = self.gdi32.SelectObject(self._mem_dc, self._bitmap)

    def push(self, img: Image.Image) -> None:
        data = _premultiplied_bgra(img)
        ctypes.memmove(self._bits, data, len(data))
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        size = SIZE(self.w, self.h)
        pt_src = POINT(0, 0)
        # pptDst 传 NULL：只更新画面，不改动窗口位置。
        # 若在这里传坐标，UpdateLayeredWindow 会把窗口搬到那个坐标 —— 每帧一次，
        # 窗口就被钉死在那个点上，Tk 设置的 geometry 全被覆盖。
        ok = self.user32.UpdateLayeredWindow(
            self.hwnd, self._screen_dc, None, ctypes.byref(size),
            self._mem_dc, ctypes.byref(pt_src), 0, ctypes.byref(blend), ULW_ALPHA)
        if not ok:
            raise OSError("UpdateLayeredWindow failed")


class ColorKeyDisplay:
    """回退后端：用 -transparentcolor 抠掉纯色背景。"""

    KEY = "#000000"

    def __init__(self, root, w: int, h: int):
        import tkinter as tk
        from PIL import ImageTk
        self._tk, self._ImageTk = tk, ImageTk
        self.w, self.h = w, h
        self.key_rgb = (0, 0, 0)
        root.configure(bg=self.KEY)
        try:
            root.wm_attributes("-transparentcolor", self.KEY)
        except Exception:
            pass
        self.label = tk.Label(root, bd=0, bg=self.KEY, highlightthickness=0)
        self.label.pack()
        self._photo = None

    def push(self, img: Image.Image) -> None:
        base = Image.new("RGB", img.size, self.key_rgb)
        base.paste(img, (0, 0), img)
        self._photo = self._ImageTk.PhotoImage(base)
        self.label.configure(image=self._photo)


def make_display(root, w: int, h: int):
    try:
        return LayeredDisplay(root, w, h), "layered"
    except Exception as exc:                             # noqa: BLE001
        log_error("分层窗口不可用，回退到透明色方案", exc)
        return ColorKeyDisplay(root, w, h), "colorkey"


# ─────────────────────────────────────────────────────────────────────────────
# 字体


# ─────────────────────────────────────────────────────────────────────────────
_FONT_CACHE: dict = {}


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    windir = os.environ.get("WINDIR", r"C:\Windows")
    names = ["msyhbd.ttc", "msyh.ttc", "seguisb.ttf", "segoeui.ttf"] if bold \
        else ["msyh.ttc", "msyhbd.ttc", "segoeui.ttf"]
    font = None
    for n in names:
        p = os.path.join(windir, "Fonts", n)
        if os.path.exists(p):
            try:
                font = ImageFont.truetype(p, size)
                break
            except Exception:                            # noqa: BLE001
                continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


# ─────────────────────────────────────────────────────────────────────────────
# 精灵绘制（Pillow）
# ─────────────────────────────────────────────────────────────────────────────

def _gear(d: ImageDraw.ImageDraw, cx, cy, r, teeth, angle, fill, outline, w):
    pts = []
    for i in range(teeth * 2):
        a = angle + i * math.pi / teeth
        rr = r * 1.3 if i % 2 == 0 else r
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=fill, outline=outline)
    d.ellipse([cx - r * 0.34, cy - r * 0.34, cx + r * 0.34, cy + r * 0.34], fill=outline)


def virtual_screen() -> tuple:
    """所有显示器合起来的虚拟桌面范围 (x, y, w, h)。

    多显示器下主屏左上角不一定是 (0,0) —— 副屏在主屏左边时 x 会是负数，
    所以不能拿单屏尺寸做边界判断。
    """
    u = ctypes.windll.user32
    return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),      # SM_X/YVIRTUALSCREEN
            u.GetSystemMetrics(78), u.GetSystemMetrics(79))      # SM_CX/CYVIRTUALSCREEN


def clamp_position(x: int, y: int) -> tuple:
    """把窗口位置夹回可见范围。

    拔掉副屏之后，上次记的坐标可能落在已经不存在的区域里，
    那样窗口会「跑到屏幕外」，看着像程序没启动。
    """
    vx, vy, vw, vh = virtual_screen()
    if vw <= 0 or vh <= 0:
        return x, y
    # 至少留 60px 在可见区内，够抓住拖回来
    margin = 60
    x = max(vx - WIN_W + margin, min(x, vx + vw - margin))
    y = max(vy, min(y, vy + vh - margin))
    return int(x), int(y)


def default_position() -> tuple:
    """默认位置：主屏右下角往上一点。"""
    u = ctypes.windll.user32
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)        # SM_CX/YSCREEN
    return sw - WIN_W - 40, sh - WIN_H - 90


# ─────────────────────────────────────────────────────────────────────────────
# 全局热键


# ─────────────────────────────────────────────────────────────────────────────
# 打包成 exe 之后 stderr 是黑洞 —— 双击运行的窗口程序没有控制台，
# print 出去的东西谁也没看见。所以异常必须落盘，否则用户只能反馈
# "它不显示了"，我们一点线索都没有。
#
# 只记异常和警告，不记正常流程；超过 256KB 就截掉前半，避免无限增长。

ERROR_LOG_MAX = 256 * 1024
# 同一个位置多久之内不重复往控制台打。文件里照记，只是别刷屏 ——
# _tick 是 30ms 一次的循环，它要是坏了，控制台会被瞬间刷爆。
ERROR_STDERR_THROTTLE = 10.0
_last_stderr: dict = {}


def log_error(where: str, exc: BaseException) -> None:
    """把一个异常写进 pet_error.log。本身绝不能抛 —— 它已经在处理异常了。"""
    try:
        import traceback
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        line = f"\n[{stamp}] {where}\n{tb}"
        # 先看看会不会超限，超了就保留后半
        try:
            size = os.path.getsize(error_log_path())
        except OSError:
            size = 0
        if size + len(line.encode("utf-8", "replace")) > ERROR_LOG_MAX:
            try:
                with open(error_log_path(), "rb") as fh:
                    tail = fh.read()[-ERROR_LOG_MAX // 2:]
                with open(error_log_path(), "wb") as fh:
                    fh.write(b"...(log truncated)...\n" + tail)
            except OSError:
                pass
        with open(error_log_path(), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:                                    # noqa: BLE001
        pass
    # 控制台也留一份（从源码跑时看得到），但同一个位置节流，
    # 免得循环里的错误把终端刷爆。
    try:
        now = time.time()
        if now - _last_stderr.get(where, 0.0) >= ERROR_STDERR_THROTTLE:
            _last_stderr[where] = now
            print(f"[error] {where}: {exc}", file=sys.stderr)
    except Exception:                                    # noqa: BLE001
        pass


def install_excepthook() -> None:
    """兜住没被捕获的异常，别让它静默消失。"""
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log_error("未捕获异常", exc)

    sys.excepthook = hook


def read_error_log(max_lines: int = 40) -> str:
    """读日志尾部，给设置窗口显示用。"""
    try:
        with open(error_log_path(), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""
