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


CONFIG_PATH = os.path.join(data_dir(), "pet_config.json")
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
ASSETS = os.path.join(app_dir(), "assets")
SKINS_DIR = os.path.join(ASSETS, "skins")
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
        print(f"[warn] 分层窗口不可用（{exc}），回退到透明色方案", file=sys.stderr)
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


# ── 角色形象 ────────────────────────────────────────────────────────────────
# 素材来自 1190fasheqi/dafeiyu-pet（MIT 协议）——「鲸鱼娘·大肥鱼」三视图。
# 三张视图已由上游做过边缘去污 + 预乘 alpha 缩放，这里直接使用。

def load_view(view: str) -> Image.Image:
    """读取一张角色视图（front / side / back），结果缓存。"""
    if view in _VIEW_CACHE:
        return _VIEW_CACHE[view]
    path = os.path.join(ASSETS, f"{view}.png")
    if not os.path.exists(path):
        raise SystemExit(
            f"[fatal] 缺少角色素材: {path}\n"
            f"        assets/ 下需要 front.png / side.png / back.png\n"
            f"        下载地址见 assets/ATTRIBUTION.md")
    im = Image.open(path).convert("RGBA")
    _VIEW_CACHE[view] = im
    return im


def _glow_layer(state: str) -> Image.Image:
    """角色身后的状态光晕，按状态缓存（做成模糊椭圆）。"""
    if state in _GLOW_CACHE:
        return _GLOW_CACHE[state]
    W, H = WIN_W * SS, PET_AREA_H * SS
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    col = _rgba(PALETTE[state]["dot"])
    cx, cy = W // 2, int(H * 0.58)
    rx, ry = int(W * 0.34), int(H * 0.30)
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=col[:3] + (60,))
    layer = layer.filter(ImageFilter.GaussianBlur(16 * SS))
    _GLOW_CACHE[state] = layer
    return layer


def _star_points(cx: float, cy: float, r_out: float, r_in: float, n: int,
                 rot: float) -> list:
    """五角星（或 n 角星）的顶点，用于晕眩星星。"""
    pts = []
    for i in range(n * 2):
        r = r_out if i % 2 == 0 else r_in
        a = rot + i * math.pi / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _overlay_layer(kind: str, phase: float) -> Image.Image:
    """画在角色之上的交互装饰。

    全部程序化生成，不依赖素材 —— 这样任何皮肤（包括只有静态图的 clawd
    主题）都能共用同一套交互反馈。每帧现画，但只有几个图元，开销可忽略。
    """
    W, H = WIN_W * SS, PET_AREA_H * SS
    s = SS
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if not kind:
        return layer
    d = ImageDraw.Draw(layer)
    cx = W // 2
    # 头顶位置：角色贴底站立，头顶大约在画面上方 8% 处
    top_y = int(H * 0.10)

    if kind == "stars":
        # 晕眩：三颗星绕头顶转圈，各自相位错开
        for i in range(3):
            a = phase * 2 * math.pi * 2 + i * (2 * math.pi / 3)
            r = W * 0.20
            sx = cx + r * math.cos(a)
            sy = top_y + r * 0.34 * math.sin(a)
            size = (7 + 2 * math.sin(a)) * s
            col = (255, 214, 92, 235) if i % 2 == 0 else (255, 178, 72, 225)
            d.polygon(_star_points(sx, sy, size, size * 0.44, 5, a), fill=col)

    elif kind == "zzz":
        # 睡觉：三个 z 依次往上飘并淡出
        for i in range(3):
            t = (phase + i / 3.0) % 1.0
            zx = cx + (W * 0.14) + t * (W * 0.12)
            zy = top_y + H * 0.26 - t * (H * 0.18)
            alpha = int(245 * (1.0 - t * 0.75))
            if alpha <= 20:
                continue
            size = int((13 + 9 * t) * s)
            f = load_font(size, bold=True)
            d.text((zx, zy), "z", font=f, fill=(214, 226, 255, alpha),
                   stroke_width=max(1, s), stroke_fill=(70, 84, 120, alpha))

    elif kind == "question":
        # 悬停：头顶冒一个上下轻晃的问号
        bob = math.sin(phase * 2 * math.pi * 2) * 4 * s
        qx, qy = cx + W * 0.17, top_y + H * 0.06 + bob
        size = 20 * s
        try:
            f = load_font(size)
        except Exception:                                # noqa: BLE001
            f = None
        if f is not None:
            d.text((qx, qy), "?", font=f, fill=(150, 200, 255, 240))
        else:
            d.ellipse([qx, qy, qx + size, qy + size], outline=(150, 200, 255, 240),
                      width=max(1, 2 * s))

    elif kind == "angry":
        # 摸烦了：头顶冒十字怒气符号
        ax, ay = cx + W * 0.18, top_y + H * 0.08
        r = 9 * s
        col = (240, 96, 96, 240)
        w = max(2, int(2.6 * s))
        d.line([ax - r, ay, ax + r, ay], fill=col, width=w)
        d.line([ax, ay - r, ax, ay + r], fill=col, width=w)
        d.line([ax - r * 0.6, ay - r * 0.6, ax + r * 0.6, ay + r * 0.6], fill=col, width=w)
        d.line([ax - r * 0.6, ay + r * 0.6, ax + r * 0.6, ay - r * 0.6], fill=col, width=w)

    elif kind == "heart":
        # 被摸开心：飘两颗心
        for i in range(2):
            t = (phase + i * 0.5) % 1.0
            hx = cx + W * 0.13 + i * W * 0.09
            hy = top_y + H * 0.12 - t * (H * 0.16)
            alpha = int(225 * (1.0 - t))
            if alpha <= 8:
                continue
            r = (6 + 3 * t) * s
            col = (255, 128, 168, alpha)
            d.ellipse([hx - r, hy - r, hx, hy], fill=col)
            d.ellipse([hx, hy - r, hx + r, hy], fill=col)
            d.polygon([(hx - r, hy - r * 0.25), (hx + r, hy - r * 0.25),
                       (hx, hy + r * 1.15)], fill=col)

    elif kind == "sweat":
        # 被抓起来挣扎：一颗汗
        t = (phase * 3) % 1.0
        sx = cx - W * 0.19
        sy = top_y + H * 0.10 + t * H * 0.10
        r = 6 * s
        d.ellipse([sx - r, sy - r * 1.3, sx + r, sy + r * 0.7],
                  fill=(150, 205, 255, 215))

    elif kind == "note":
        # 心情好：飘一个音符
        t = (phase + 0.2) % 1.0
        nx = cx + W * 0.13
        ny = top_y + H * 0.30 - t * (H * 0.18)
        alpha = int(240 * (1.0 - t * 0.7))
        if alpha > 20:
            r = 8 * s
            col = (150, 226, 176, alpha)
            d.ellipse([nx - r, ny, nx + r, ny + r * 1.7], fill=col)
            d.line([nx + r, ny + r * 0.9, nx + r, ny - r * 2.4],
                   fill=col, width=max(2, int(2.4 * s)))
            d.line([nx + r, ny - r * 2.4, nx + r * 2.4, ny - r * 1.7],
                   fill=col, width=max(2, int(2.4 * s)))

    return layer


def overlay_for(kind: str, phase: float):
    """取一张缩到最终尺寸的叠加图形；kind 为空返回 None。

    在 _tick 里对**所有皮肤**统一叠加 —— 契约和 clawd 主题的帧是预渲染的，
    画在 render_pet 里就只有内置皮肤能用。
    """
    if not kind:
        return None
    return _overlay_layer(kind, phase).resize((WIN_W, PET_AREA_H), Image.LANCZOS)


def session_dots(tasks: list, max_dots: int = 7):
    """她脚边的一排小圆点：每个活跃会话一个，颜色对应它的状态。

    多任务时一眼能看出「有几个在跑、分别什么状态」，不用展开详情卡。
    只有一个任务时不画 —— 那时候角色本身的状态就是答案。
    """
    if len(tasks) < 2:
        return None
    layer = Image.new("RGBA", (WIN_W, PET_AREA_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    shown = tasks[:max_dots]
    r = 3
    gap = 11
    total = len(shown) * gap - (gap - r * 2)
    x = (WIN_W - total) // 2
    y = PET_AREA_H - 5
    for t in shown:
        col = _rgba(PALETTE.get(t.state, PALETTE["idle"])["dot"])[:3]
        d.ellipse([x - r, y - r, x + r, y + r], fill=col + (235,))
        x += gap
    if len(tasks) > max_dots:
        d.text((x - 2, y - 6), f"+{len(tasks) - max_dots}", fill=(200, 206, 216, 230))
    return layer


def render_pet(state: str, phase: float) -> Image.Image:
    """渲染一帧角色：素材 + 呼吸/弹跳/摇摆 + 状态配件。

    返回 WIN_W x PET_AREA_H 的 RGBA 图。角色贴底站立，脚位于画面底部，
    因此换状态时人物不会跳动位置。
    """
    W, H = WIN_W * SS, PET_AREA_H * SS
    s = SS
    two_pi = 2 * math.pi
    breathe = math.sin(phase * two_pi)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    img.alpha_composite(_glow_layer(state))

    # ── 各状态的姿态 ──
    bob = 0.0
    sx = sy = 1.0
    rot = 0.0
    if state == "idle":
        bob = breathe * 2.0
        sy = 1 + breathe * 0.014
        sx = 1 - breathe * 0.009
    elif state == "thinking":
        bob = math.sin(phase * two_pi * 2) * 2.4
        sy = 1 + breathe * 0.008
        rot = math.sin(phase * two_pi) * 1.6
    elif state == "tools":
        t = abs(math.sin(phase * math.pi * 3))
        bob = t * 3.4
        sy = 1 - t * 0.022
        sx = 1 + t * 0.022
        rot = math.sin(phase * two_pi * 3) * 1.1
    else:                                                # offline
        bob = breathe * 0.9
        sy = 1 + breathe * 0.007

    sprite = load_view(VIEW_FOR_STATE.get(state, "front"))
    body = sprite.resize((max(1, int(round(sprite.width * sx))),
                          max(1, int(round(sprite.height * sy)))), Image.LANCZOS)
    if abs(rot) > 0.05:
        body = body.rotate(rot, resample=Image.BICUBIC, expand=True)

    feet_y = int(H * 0.985) + int(bob * s)
    body_x = (W - body.width) // 2
    body_y = feet_y - body.height

    # 落影：分层窗口支持真 alpha，可以做柔和边缘
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    rw = body.width * 0.32
    rh = 6 * s
    sd.ellipse([W // 2 - rw, feet_y - rh, W // 2 + rw, feet_y + rh],
               fill=(34, 38, 48, 130))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(4 * s)))

    img.alpha_composite(body, (body_x, body_y))

    # ── 状态配件 ──
    d = ImageDraw.Draw(img)
    accent = PALETTE[state]["accent"]
    half_w = body.width * 0.5
    head_y = body_y + body.height * 0.10

    if state == "thinking":
        for i in range(3):
            p = (phase * 2 + i * 0.22) % 1.0
            r = (3.4 + 1.8 * math.sin(p * math.pi)) * s
            cxx = W // 2 + half_w * 0.78 + i * 14 * s
            cyy = head_y + (-12 - 15 * math.sin(p * math.pi)) * s
            d.ellipse([cxx - r, cyy - r, cxx + r, cyy + r], fill=_rgba(accent))
    elif state == "tools":
        _gear(d, W // 2 + half_w + 14 * s, feet_y - body.height * 0.46,
              15 * s, 8, phase * two_pi * 2.0, (228, 234, 242, 235), INK, s)
        _gear(d, W // 2 - half_w - 14 * s, feet_y - body.height * 0.26,
              11 * s, 7, -phase * two_pi * 2.6, (206, 214, 226, 225), INK, s)
        drop = feet_y - body.height * 0.78 + math.sin(phase * two_pi * 2) * 3 * s
        dcx = W // 2 + half_w * 1.02
        d.ellipse([dcx - 6 * s, drop - 9 * s, dcx + 3 * s, drop + 3 * s],
                  fill=(126, 194, 246, 235))
    elif state == "idle" and phase > 0.58:
        for dx, dy, sz in ((0.62, 0.06, 15), (0.78, -0.05, 12), (0.92, -0.15, 10)):
            f = load_font(int(sz * s), bold=True)
            d.text((W // 2 + half_w * dx, head_y + body.height * dy), "Z",
                   font=f, fill=(168, 178, 198, 215))

    return img.resize((WIN_W, PET_AREA_H), Image.LANCZOS)


# ─────────────────────────────────────────────────────────────────────────────
# 皮肤：内置矢量形象 / Codex 桌宠精灵图契约
# ─────────────────────────────────────────────────────────────────────────────
# 契约（Codex 桌宠官方）：1536x1872，8 列 x 9 行，单格 192x208，全透明背景。
#   行 0 idle   1 向右跑   2 向左跑   3 挥手   4 跳跃
#   行 5 出错   6 等待回复  7 专注干活  8 思考审阅
# 任何符合契约的皮肤包都能直接导入（clawd-on-desk 主题、kun-like 精灵图等）。

ATLAS_COLS, ATLAS_ROWS = 8, 9
ATLAS_CW, ATLAS_CH = 192, 208
ATLAS_SIZE = (ATLAS_COLS * ATLAS_CW, ATLAS_ROWS * ATLAS_CH)

# 状态 -> 图集行。契约只有 9 行，陪伴模式的状态只能就近借：
# 溜达借「向右跑」，犯困/打盹借待机，刚醒借挥手（伸懒腰），被摸借跳跃。
ATLAS_ROW = {"idle": 0, "tools": 7, "thinking": 8, "waiting": 6,
             "failed": 5, "celebrating": 4, "offline": 0,
             "roaming": 1, "gazing": 1, "peeking": 0, "yawning": 0, "dozing": 0,
             "sleeping": 0, "waking": 3, "petted": 4}
# 行的 (中间帧, 末帧) 毫秒，取自契约的逐帧时长
ATLAS_TIMING = {
    0: (120, 320), 1: (110, 200), 3: (140, 280), 4: (140, 280),
    5: (140, 240), 6: (150, 260), 7: (120, 220), 8: (150, 280),
}
# 陪伴模式的状态放慢一点：打盹和睡觉不该跟待机一样精神
ATLAS_SLOWDOWN = {"yawning": 2.4, "dozing": 3.2, "sleeping": 4.5, "waking": 1.6}


class DafeiyuSkin:
    """内置皮肤：大肥鱼三视图 + 程序化姿态与配件。"""

    kind = "dafeiyu"

    def __init__(self, path=None, name="dafeiyu", display="大肥鱼（内置）", frames: int = 12):
        self.name, self.display = name, display
        self.n = frames

    def frames(self, state: str):
        return [(render_pet(state, i / self.n), TICK_MS) for i in range(self.n)]

    def reaction_frames(self, kind: str) -> list:
        """程序化的「被戳」反应：向上弹一下再落回，带轻微挤压。"""
        if kind != "poke":
            return []
        out = []
        n = 12
        for i in range(n):
            t = i / n
            dy = -math.sin(t * math.pi) * 11 * (1 - t * 0.35)
            squash = 1 - math.sin(t * math.pi) * 0.09
            out.append((_bounce_frame(render_pet("idle", i / self.n), dy, squash), 55))
        return out


class AtlasSkin:
    """Codex 桌宠精灵图契约皮肤。

    帧数不写死 —— 数该行有几个非空格子即可，因此不同作者画的帧数不同也能用。
    """

    kind = "atlas"

    def __init__(self, path: str, name: str, display: str, target_h: int = 174):
        self.path, self.name, self.display = path, name, display
        self.target_h = target_h
        self.sheet = Image.open(path).convert("RGBA")
        # 契约固定了「8 列 × 192 宽」与前 9 行的含义；行数允许更多 ——
        # openpets 就是 11 行（前 9 行同契约，后 2 行是它自己的扩展状态）。
        w, h = self.sheet.size
        if w != ATLAS_COLS * ATLAS_CW or h < ATLAS_ROWS * ATLAS_CH or h % ATLAS_CH:
            raise ValueError(
                f"精灵图尺寸不符契约：{self.sheet.size}；应为宽 {ATLAS_COLS * ATLAS_CW}、"
                f"高为 {ATLAS_CH} 的整数倍且不少于 {ATLAS_ROWS * ATLAS_CH}")
        self._rows: dict = {}
        self._cache: dict = {}

    def _row(self, row: int) -> list:
        if row not in self._rows:
            cells = []
            for col in range(ATLAS_COLS):
                box = (col * ATLAS_CW, row * ATLAS_CH,
                       (col + 1) * ATLAS_CW, (row + 1) * ATLAS_CH)
                cell = self.sheet.crop(box)
                if cell.getbbox() is not None:        # 空格子不算帧
                    cells.append(cell)
            self._rows[row] = cells
        return self._rows[row]

    def _frame(self, state: str, row: int, idx: int) -> Image.Image:
        key = (state, row, idx)
        if key not in self._cache:
            self._cache[key] = _compose_character(self._row(row)[idx], state, self.target_h)
        return self._cache[key]

    def frames(self, state: str):
        rows = [3, 4] if state == "celebrating" else [ATLAS_ROW.get(state, 0)]
        seq = [(r, i) for r in rows for i in range(len(self._row(r)))]
        if not seq:
            return []
        mid, last = ATLAS_TIMING.get(rows[-1], (140, 280))
        slow = ATLAS_SLOWDOWN.get(state, 1.0)
        return [(self._frame(state, r, i), int((last if k == len(seq) - 1 else mid) * slow))
                for k, (r, i) in enumerate(seq)]

    def reaction_frames(self, kind: str) -> list:
        """契约第 3 行是挥手 —— 拿来当「被戳」的反应。"""
        if kind != "poke":
            return []
        cells = self._row(3)
        if not cells:
            return []
        mid, _last = ATLAS_TIMING.get(3, (140, 280))
        return [(self._frame("idle", 3, i), mid) for i in range(len(cells))]


# ── clawd-on-desk 主题格式 ──────────────────────────────────────────────────
# 主题目录：theme.json + assets/<状态>.<扩展名>
# 资源格式：SVG / GIF / APNG / WebP / PNG / JPG 都行；只有做眼球追踪的状态必须是 SVG。

# 我的状态 -> clawd 的状态名（按优先级）
CLAWD_STATE_MAP = {
    "idle":        ["idle", "dozing", "yawning"],
    "thinking":    ["thinking", "sweeping"],
    "tools":       ["working", "carrying", "juggling", "sweeping"],
    "waiting":     ["attention", "notification"],
    "failed":      ["error"],
    "celebrating": ["notification", "attention"],
    "offline":     ["sleeping", "collapsing", "dozing"],
    # 陪伴模式：clawd 主题本来就有这些状态，正好对得上
    "roaming":     ["roam", "idle"],
    "gazing":      ["roam", "idle"],
    "peeking":     ["idle", "roam"],
    "yawning":     ["yawning", "dozing", "idle"],
    "dozing":      ["dozing", "sleeping", "idle"],
    "sleeping":    ["sleeping", "dozing", "collapsing"],
    "waking":      ["waking", "idle"],
    "petted":      ["notification", "attention", "idle"],
}
# clawd 的用户主题目录（%APPDATA%/clawd-on-desk/themes）
CLAWD_THEME_DIRS = [
    os.path.join(os.environ.get("APPDATA", ""), "clawd-on-desk", "themes"),
    os.path.join(SKINS_DIR, "clawd"),
]
RASTER_EXTS = (".apng", ".gif", ".webp", ".png", ".jpg", ".jpeg")
# 交互反应的种类：poke = 左键点一下，drag = 正在拖动
REACTION_KINDS = ("poke", "drag")


def _localized_name(value, fallback: str) -> str:
    """clawd 主题的 name 可能是多语言对象（whale-chan 就是 {en, zh, zh-TW}）。"""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("zh", "zh-TW", "en"):
            got = value.get(key)
            if isinstance(got, str) and got:
                return got
    return fallback


def _compose_character(cell: Image.Image, state: str, target_h: int) -> Image.Image:
    """把一张角色图缩到目标高度，加状态光晕与落影，贴到窗口画布上。"""
    w = max(1, round(cell.width * target_h / cell.height))
    cell = cell.resize((w, target_h), Image.LANCZOS)
    W, H = WIN_W, PET_AREA_H
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    img.alpha_composite(_glow_layer(state))
    feet = int(H * 0.97)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    rw = w * 0.30
    sd.ellipse([W // 2 - rw, feet - 5, W // 2 + rw, feet + 5], fill=(34, 38, 48, 130))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(4)))
    img.alpha_composite(cell, ((W - w) // 2, feet - target_h))
    return img


def _bounce_frame(img: Image.Image, dy: float, squash: float) -> Image.Image:
    """把一帧角色区整体上移 dy 像素、按 squash 纵向挤压（脚不动）。

    给内置皮肤做「被戳弹一下」的程序化反应 —— 它没有现成的反应用素材。
    挤压贴着底边做，所以看起来是脚踩在地上弹，而不是整块飘起来。
    """
    if abs(dy) < 0.5 and abs(squash - 1.0) < 0.01:
        return img
    W, H = img.size
    body = img.crop((0, 0, W, PET_AREA_H))
    card = img.crop((0, PET_AREA_H, W, H))
    nh = max(1, int(PET_AREA_H * squash))
    body = body.resize((W, nh), Image.LANCZOS)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.alpha_composite(body, (0, PET_AREA_H - nh + int(dy)))
    out.alpha_composite(card, (0, PET_AREA_H))
    return out


def raster_svg(svg_path: str, scale: float = 2.0) -> Image.Image:
    """把 SVG 光栅化成带透明通道的 RGBA 图。

    本机没有 cairo（cairosvg 与 renderPM 的三个后端都缺），所以走
    svglib -> PDF -> pypdfium2 这条路。PDF 渲染出来是不透明白底，于是
    **黑底、白底各渲一次再解方程**（与 dafeiyu 的预处理同一套办法）：

        alpha = 255 - max(白 - 黑)
        color = 黑 / (alpha/255)

    注意：脚本驱动的 SVG（clawd 的 cloudling 就是）只会得到一帧静止画面 ——
    它的动画由内嵌 JS 在运行时算，光栅化拿不到。
    """
    import numpy as np
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
    from reportlab.graphics.shapes import Drawing, Rect
    import pypdfium2 as pdfium

    drawing = svg2rlg(svg_path)

    def render_on(bg: str) -> "np.ndarray":
        page = Drawing(drawing.width, drawing.height)
        page.add(Rect(0, 0, drawing.width, drawing.height,
                      fillColor=bg, strokeColor=None))
        page.add(drawing)
        pdf = f"{svg_path}.{bg}.pdf"
        renderPDF.drawToFile(page, pdf)
        doc = pdfium.PdfDocument(pdf)
        try:
            img = doc[0].render(scale=scale).to_pil().convert("RGB")
        finally:
            doc.close()
        try:
            os.remove(pdf)
        except OSError:
            pass
        return np.asarray(img, dtype=np.float32)

    white = render_on("white")
    black = render_on("black")
    alpha = np.clip(255.0 - np.max(white - black, axis=2), 0, 255)
    t = np.maximum(alpha, 1.0)[..., None] / 255.0
    rgb = np.clip(black / t, 0, 255)
    out = np.zeros((*alpha.shape, 4), dtype=np.uint8)
    out[..., :3] = rgb.astype(np.uint8)
    out[..., 3] = alpha.astype(np.uint8)
    out[alpha < 6] = 0                                   # 极淡边缘直接透明
    return Image.fromarray(out, "RGBA")


class ClawdThemeSkin:
    """clawd-on-desk 主题格式的皮肤。

    只解析格式、不含任何素材：主题目录由用户自行提供（自己做的，或取得授权的）。
    """

    kind = "clawd"

    def __init__(self, path: str, name: str, display: str, target_h: int = 174):
        self.dir, self.name, self.display = path, name, display
        self.target_h = target_h
        with open(os.path.join(path, "theme.json"), encoding="utf-8") as fh:
            self.manifest = json.load(fh)
        self.states = self.manifest.get("states") or {}
        self._cache: dict = {}

    def _assets(self, key: str) -> list:
        files = self.states.get(key)
        if isinstance(files, dict):
            files = files.get("files") or []
        return files or []

    def _resolve(self, state: str):
        """找该状态的资源文件；眼球追踪用的 *-follow.svg 优先换成同名光栅兄弟。"""
        for key in CLAWD_STATE_MAP.get(state, []):
            for fn in self._assets(key):
                p = os.path.join(self.dir, "assets", fn)
                if not os.path.exists(p):
                    continue
                if fn.endswith("-follow.svg"):           # 这类 SVG 是给眼球追踪的
                    stem = fn[: -len("-follow.svg")]
                    for ext in RASTER_EXTS:              # 有光栅版本就用光栅的（动画完整）
                        sib = os.path.join(self.dir, "assets", stem + ext)
                        if os.path.exists(sib):
                            return sib
                return p
        return None

    def _load(self, path: str) -> list:
        """按扩展名取帧。返回 [(RGBA 图, 毫秒)]。"""
        if path.lower().endswith(".svg"):
            return [(raster_svg(path), 200)]             # 静止帧，给个慢一点的时长
        im = Image.open(path)
        n = getattr(im, "n_frames", 1)
        frames = []
        for i in range(n):
            im.seek(i)
            dur = int(im.info.get("duration") or 0)
            # 下限放到 16ms —— whale-chan 是 17ms/帧的 60fps 动画，
            # 卡到 40ms 会让整段动画慢一倍多。
            frames.append((im.convert("RGBA").copy(), max(16, min(dur or 120, 600))))
        return frames

    def _crop_union(self, raw: list) -> list:
        """按「所有帧的内容并集」裁切。

        clawd 主题画布的留白差异极大（calico 是 266x200 只占一角，cloudling 是
        88x72），按整图缩放角色会变得很小。用并集而不是逐帧裁切，角色才不会在
        帧间抖动。
        """
        box = None
        for im, _ in raw:
            b = im.getbbox()
            if b:
                box = b if box is None else (
                    min(box[0], b[0]), min(box[1], b[1]),
                    max(box[2], b[2]), max(box[3], b[3]))
        return [(im.crop(box), dur) for im, dur in raw] if box else raw

    def frames(self, state: str):
        if state not in self._cache:
            path = self._resolve(state) or self._resolve("idle")
            raw = self._crop_union(self._load(path)) if path else []
            self._cache[state] = [
                (_compose_character(im, state, self.target_h), dur) for im, dur in raw
            ]
        return self._cache[state]

    def reaction_frames(self, kind: str) -> list:
        """用主题自带的反应用素材（clawd 主题都有 reactions 段）。

        `clickLeft` 是点一下的反应，`drag` 是拖动时的姿态。reactions 里声明的
        duration 是整段的时长，所以按帧数均分，而不是用素材自带的逐帧时长 ——
        这样不同主题的反应快慢都跟着作者给的意图走。
        """
        key = "clickLeft" if kind == "poke" else kind
        spec = (self.manifest.get("reactions") or {}).get(key)
        if not isinstance(spec, dict):
            return []
        files = spec.get("files") or ([spec["file"]] if spec.get("file") else [])
        if not files:
            return []
        path = os.path.join(self.dir, "assets", files[0])
        if not os.path.exists(path):
            return []
        raw = self._load(path)
        if not raw:
            return []
        raw = self._crop_union(raw)
        dur = spec.get("duration")
        if isinstance(dur, (int, float)) and dur > 0 and len(raw) > 1:
            each = max(16, int(dur / len(raw)))
            raw = [(im, each) for im, _ in raw]
        return [(_compose_character(im, "idle", self.target_h), d) for im, d in raw]


def discover_skins() -> list:
    """内置皮肤 + Codex 契约包 + clawd 主题。"""
    out = [dict(id="dafeiyu", display="大肥鱼（内置）", kind="dafeiyu", path=None)]
    seen = {"dafeiyu"}
    # Codex 精灵图契约
    for root, tag in [(SKINS_DIR, ""),
                      (os.path.join(os.path.expanduser("~"), ".codex", "pets"), " (Codex)")]:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            if n in seen:
                continue
            for fn in ("spritesheet.webp", "spritesheet.png"):
                p = os.path.join(root, n, fn)
                if os.path.exists(p):
                    seen.add(n)
                    out.append(dict(id=n, display=f"{n}{tag}", kind="atlas", path=p))
                    break
    # clawd-on-desk 主题格式（含主题的顶层必须有 theme.json）
    for root in CLAWD_THEME_DIRS:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            if n in seen:
                continue
            p = os.path.join(root, n, "theme.json")
            if not os.path.exists(p):
                continue
            try:
                with open(p, encoding="utf-8") as fh:
                    name = _localized_name(json.load(fh).get("name"), n)
            except Exception:                            # noqa: BLE001
                name = n
            seen.add(n)
            out.append(dict(id=n, display=f"{name} (clawd)", kind="clawd", path=root + os.sep + n))
    return out


def load_skin(skin_id: str):
    """按 id 加载皮肤；找不到或加载失败就退回内置。"""
    for s in discover_skins():
        if s["id"] != skin_id:
            continue
        if s["kind"] == "atlas":
            try:
                return AtlasSkin(s["path"], s["id"], s["display"])
            except Exception as exc:                     # noqa: BLE001
                print(f"[warn] 皮肤 {skin_id} 加载失败（{exc}），改用内置", file=sys.stderr)
                break
        if s["kind"] == "clawd":
            try:
                return ClawdThemeSkin(s["path"], s["id"], s["display"])
            except Exception as exc:                     # noqa: BLE001
                print(f"[warn] clawd 主题 {skin_id} 加载失败（{exc}），改用内置",
                      file=sys.stderr)
                break
        return DafeiyuSkin()
    return DafeiyuSkin()


# ─────────────────────────────────────────────────────────────────────────────
# 信息卡（状态文字）
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_ms(ms: float) -> str:
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(int(s), 60)
    return f"{m}m{s:02d}s"


def _ellipsis(draw, text: str, font, max_w: float) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…" if text else "…"


def card_height(snap: "Snapshot", detail: bool) -> int:
    """信息卡高度：单任务 58；多任务按行数长高；详情固定 148。"""
    if detail:
        return DETAIL_CARD_H
    n = len(snap.tasks)
    return 58 if n <= 1 else 36 + 18 * min(n, 4)


def _task_row(d, s, x0, x1, y, task, f_small, label=None) -> None:
    """一行任务：状态点 + 标题 + 右侧「状态 计时」。"""
    pal = PALETTE[task.state]
    r = 3 * s
    cx = x0 + 16 * s
    d.ellipse([cx - r, y + 5 * s - r, cx + r, y + 5 * s + r], fill=_rgba(pal["dot"]))
    right = task.short_state
    if task.elapsed_ms is not None:
        right += f"  {_fmt_ms(task.elapsed_ms)}"
    rw = d.textlength(right, font=f_small)
    tx = x0 + 26 * s
    d.text((tx, y), _ellipsis(d, task.label if label is None else label, f_small,
                              x1 - tx - rw - 10 * s), font=f_small, fill=TEXT_MAIN)
    d.text((x1 - 10 * s - rw, y), right, font=f_small, fill=pal["dot"])


def render_card(snap: "Snapshot", detail: bool, privacy: bool = False) -> Image.Image:
    """渲染信息卡。单任务沿用一行的紧凑版式；多任务时逐行列出。"""
    ch = card_height(snap, detail)
    W, H = WIN_W * SS, ch * SS
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = SS
    pal = PALETTE[snap.state]

    f_main = load_font(13 * s, bold=True)
    f_sub = load_font(11 * s)
    f_small = load_font(10 * s)

    pad = 5 * s
    x0, y0, x1, y1 = pad, pad, W - pad, H - pad
    d.rounded_rectangle([x0, y0, x1, y1], radius=12 * s, fill=CARD_BG,
                        outline=CARD_LINE, width=1 * s)

    n_tasks = len(snap.tasks)
    dcy = y0 + 19 * s
    dcx = x0 + 15 * s
    dot_r = 5 * s
    d.ellipse([dcx - dot_r, dcy - dot_r, dcx + dot_r, dcy + dot_r], fill=_rgba(pal["dot"]))
    if snap.state in ("thinking", "tools", "waiting", "failed", "celebrating"):
        halo = dot_r + (2.5 + 2.5 * abs(math.sin(snap.phase * math.pi))) * s
        d.ellipse([dcx - halo, dcy - halo, dcx + halo, dcy + halo],
                  outline=_rgba(pal["dot"], 110), width=1 * s)

    tx = x0 + 28 * s

    # 隐私模式：标题换成「会话 N」，副行换成不含内容的通用文案
    def tlabel(idx: int, task) -> str:
        return f"会话 {idx + 1}" if privacy else task.label

    sub_text = GENERIC_DETAIL.get(snap.state, "") if privacy else snap.detail_line

    # ── 折叠态 & 多任务：标题 + 每个任务一行 ──
    if n_tasks > 1 and not detail:
        head = f"{n_tasks} 个任务进行中"
        right = _fmt_ms(snap.elapsed_ms) if snap.elapsed_ms is not None else ""
        rw = d.textlength(right, font=f_main) if right else 0
        d.text((tx, dcy - 8 * s), _ellipsis(d, head, f_main, x1 - tx - rw - 12 * s),
               font=f_main, fill=TEXT_MAIN)
        if right:
            d.text((x1 - 12 * s - rw, dcy - 8 * s), right, font=f_main, fill=pal["dot"])
        row_y = dcy + 8 * s
        for i, t in enumerate(snap.tasks):
            _task_row(d, s, x0, x1, row_y, t, f_small, label=tlabel(i, t))
            row_y += 18 * s
        return img.resize((WIN_W, ch), Image.LANCZOS)

    # ── 单任务折叠态 / 详情态标题 ──
    label = pal["label"]
    if n_tasks > 1:
        label = f"{n_tasks} 个任务进行中"
    elif snap.state == "tools" and snap.tool_names:
        label = f"执行 {snap.tool_names[0]}"
    elif snap.state == "idle" and snap.idle_hint:
        label = snap.idle_hint
    elif snap.state == "offline":
        label = "未发现 DSH 会话"

    right = _fmt_ms(snap.elapsed_ms) if snap.elapsed_ms is not None else ""
    rw = d.textlength(right, font=f_main) if right else 0
    d.text((tx, dcy - 8 * s), _ellipsis(d, label, f_main, x1 - tx - rw - 12 * s),
           font=f_main, fill=TEXT_MAIN)
    if right:
        d.text((x1 - 12 * s - rw, dcy - 8 * s), right, font=f_main, fill=pal["dot"])

    row_y = dcy + 12 * s
    if not detail:
        if sub_text:
            # 气泡（她说的话）用强调色，跟普通状态副行区分开
            d.text((tx, row_y), _ellipsis(d, sub_text, f_sub, x1 - tx - 12 * s),
                   font=f_sub, fill=pal["dot"] if snap.bubble else TEXT_DIM)
        return img.resize((WIN_W, ch), Image.LANCZOS)

    # ── 详情展开 ──
    # 多任务才逐行列出；单任务的信息已经在标题行，再列一遍是重复
    if n_tasks > 1:
        shown = snap.tasks[:3]
        for i, t in enumerate(shown):
            _task_row(d, s, x0, x1, row_y, t, f_small, label=tlabel(i, t))
            row_y += 16 * s
        if n_tasks > len(shown):
            d.text((x0 + 26 * s, row_y), f"… 另有 {n_tasks - len(shown)} 个任务",
                   font=f_small, fill=TEXT_DIM)
            row_y += 16 * s
    elif sub_text:
        d.text((tx, row_y), _ellipsis(d, sub_text, f_sub, x1 - tx - 12 * s),
               font=f_sub, fill=TEXT_DIM)
        row_y += 16 * s

    d.line([(x0 + 10 * s, row_y + 2 * s), (x1 - 10 * s, row_y + 2 * s)],
           fill=CARD_LINE, width=1 * s)
    row_y += 9 * s

    rows = [
        ("模型", f"{snap.model}" + (f" · {snap.provider}" if snap.provider else "")),
        ("进度", f"第 {snap.turn} 轮 · 第 {snap.step} 步" if snap.turn else "—"),
        ("输出", f"{snap.tokens_out/1000:.1f}K tokens · 上下文 {snap.context_pct:.0f}%"),
    ]
    for k, v in rows:
        d.text((tx, row_y), k, font=f_small, fill=TEXT_DIM)
        kw = d.textlength(k, font=f_small)
        d.text((tx + max(kw, 30 * s) + 8 * s, row_y),
               _ellipsis(d, str(v), f_small, x1 - tx - max(kw, 30 * s) - 22 * s),
               font=f_small, fill=TEXT_MAIN)
        row_y += 14 * s

    bar_y = row_y + 2 * s
    bar_w = x1 - tx - 12 * s
    d.rounded_rectangle([tx, bar_y, tx + bar_w, bar_y + 4 * s], radius=2 * s,
                        fill=(52, 56, 66, 255))
    fill_w = max(2 * s, bar_w * min(snap.context_pct, 100.0) / 100.0)
    bar_col = (240, 120, 110, 255) if snap.context_pct > 80 else pal["dot"]
    d.rounded_rectangle([tx, bar_y, tx + fill_w, bar_y + 4 * s], radius=2 * s, fill=bar_col)

    # 最慢的几个工具（来自事件日志的 tool/call → tool/result 计时）
    if snap.slow_tools:
        row_y = bar_y + 10 * s
        d.text((tx, row_y), "最慢", font=f_small, fill=TEXT_DIM)
        row_y += 13 * s
        for name, ms in snap.slow_tools[:3]:
            d.text((tx + 8 * s, row_y), _ellipsis(d, name, f_small, 60 * s),
                   font=f_small, fill=TEXT_MAIN)
            val = f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"
            vw = d.textlength(val, font=f_small)
            d.text((x1 - 12 * s - vw, row_y), val, font=f_small, fill=pal["dot"])
            row_y += 13 * s

    return img.resize((WIN_W, ch), Image.LANCZOS)


# ─────────────────────────────────────────────────────────────────────────────
# 会话状态读取
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TaskInfo:
    """一个正在进行的任务 —— 一个 DSH 会话算一个。"""
    session_id: str = ""
    state: str = "idle"
    title: str = ""
    provider: str = ""
    model: str = ""
    turn: int = 0
    step: int = 0
    started_ms: Optional[float] = None
    tool_names: list = field(default_factory=list)
    prompt: str = ""
    context_pct: float = 0.0
    surface_tokens: float = 0.0
    context_window: float = 0.0
    tokens_out: float = 0.0
    mtime: float = 0.0
    goal: str = ""                                    # 该会话的长期目标（若有）
    queued: bool = False                              # 有排队中的消息
    plan_active: bool = False                         # 处于计划模式
    turn_open: bool = False                           # 回合尚未结束
    slow_tools: list = field(default_factory=list)    # 本次会话最慢的几个工具 [(名字, 毫秒)]

    @property
    def elapsed_ms(self) -> Optional[float]:
        if self.started_ms is None:
            return None
        return max(0.0, time.time() * 1000 - self.started_ms)

    @property
    def label(self) -> str:
        return self.title or (self.session_id[:8] if self.session_id else "未命名会话")

    @property
    def short_state(self) -> str:
        if self.state == "tools":
            return f"执行 {self.tool_names[0]}" if self.tool_names else "执行中"
        return {"thinking": "思考中", "waiting": "等你回答", "failed": "出错重试",
                "celebrating": "已完成", "idle": "空闲",
                "offline": "离线"}.get(self.state, self.state)


@dataclass
class Snapshot:
    state: str = "offline"                            # 聚合状态（最忙的那个任务）
    tasks: list = field(default_factory=list)         # 活跃任务列表
    session_id: str = ""                              # 以下均为主任务（tasks[0]）的字段
    title: str = ""
    provider: str = ""
    model: str = ""
    turn: int = 0
    step: int = 0
    started_ms: Optional[float] = None
    tool_names: list = field(default_factory=list)
    context_pct: float = 0.0
    surface_tokens: float = 0.0
    context_window: float = 0.0
    tokens_out: float = 0.0
    prompt: str = ""
    phase: float = 0.0
    idle_hint: str = ""
    detail_line: str = ""
    forced: bool = False
    bubble: bool = False                              # detail_line 是她说的话，用强调色画
    mirror: bool = False                              # 水平翻转（看向左边时用）
    wander: float = 0.0                               # 窗口水平目标位置（绝对坐标，0 = 不动）
    slow_tools: list = field(default_factory=list)    # 主任务最慢的工具 [(名字, 毫秒)]
    focus_x: float = 0.0                              # 非 0 时把窗口移到这个 x（等待提醒用）

    @property
    def elapsed_ms(self) -> Optional[float]:
        if self.started_ms is None:
            return None
        return max(0.0, time.time() * 1000 - self.started_ms)


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                    # noqa: BLE001
        return None


def list_session_files() -> list:
    """列出全部 projcache 会话文件（路径, mtime, 大小）。"""
    out = []
    try:
        names = os.listdir(PROJ_DIR)
    except OSError:
        return out
    for name in names:
        if not (name.startswith("session-") and name.endswith(".json")):
            continue
        path = os.path.join(PROJ_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append((path, name[len("session-"):-len(".json")], st.st_mtime, st.st_size))
    out.sort(key=lambda x: -x[2])
    return out


class LogTail:
    """增量读取会话 zstd 事件日志，提取 callId -> 工具名。"""

    MAGIC = b"\x28\xb5\x2f\xfd"

    def __init__(self, path: Optional[str]):
        self.path = path
        self.offset = 0
        self.partial = ""
        self.calls: dict = {}
        self.last_tool = ""
        # 工具耗时统计：按【事件 seq】记发起时刻与工具名，结果事件通过
        # sourceEventSeqs 指回来。不能用 callId 配对 —— 它里面的 call_00_
        # 是每回合重置的序号，跨回合重复，会算出几十天的耗时。
        self.call_at: dict = {}
        self.call_name: dict = {}
        self.tool_times: dict = {}
        self._dctx = None
        if path:
            self._seed()

    def _decompressor(self):
        if self._dctx is None:
            import zstandard
            self._dctx = zstandard.ZstdDecompressor()
        return self._dctx

    def _decode_frame(self, blob: bytes) -> Optional[str]:
        import io
        try:
            with self._decompressor().stream_reader(io.BytesIO(blob)) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:                                # noqa: BLE001
            return None

    def _seed(self, tail_bytes: int = 3_000_000, max_frames: int = 40) -> None:
        """启动时只解最近的若干帧，避免长会话拖慢启动。"""
        try:
            size = os.path.getsize(self.path)
            with open(self.path, "rb") as fh:
                if size > tail_bytes:
                    fh.seek(size - tail_bytes)
                buf = fh.read()
        except OSError:
            return
        base = size - len(buf)
        offs, i = [], buf.find(self.MAGIC)
        while i != -1:
            offs.append(i)
            i = buf.find(self.MAGIC, i + 1)
        if not offs:
            return
        for n, start in enumerate(offs[-(max_frames + 1):-1] if len(offs) > 1 else offs):
            end = offs[offs.index(start) + 1] if offs.index(start) + 1 < len(offs) else len(buf)
            txt = self._decode_frame(buf[start:end])
            if txt:
                self._consume(txt)
        # 全部已解帧之后的偏移作为续读起点
        self.offset = base + (offs[-max_frames - 1] if len(offs) > max_frames + 1 else offs[0])
        if self.offset == base + offs[0]:
            self.offset = base + (offs[-max_frames] if len(offs) > max_frames else offs[0])

    def _consume(self, text: str) -> None:
        text = self.partial + text
        lines = text.split("\n")
        self.partial = lines.pop()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:                            # noqa: BLE001
                continue
            et = ev.get("type")
            data = ev.get("data") or {}
            if et == "tool/call":
                cid, name = data.get("callId"), data.get("name")
                if cid and name:
                    self.calls[cid] = name
                    self.last_tool = name
                    # 计时按 seq 记，不按 callId —— callId 里的 call_00_ 是
                    # 每回合重置的序号，跨回合会重复，用它配对会算出几十天的耗时。
                    self.call_at[ev.get("seq")] = ev.get("time")
                    self.call_name[ev.get("seq")] = name
                    if len(self.calls) > 800:
                        for k in list(self.calls)[:400]:
                            self.calls.pop(k, None)
                    if len(self.call_at) > 800:
                        for k in list(self.call_at)[:400]:
                            self.call_at.pop(k, None)
                            self.call_name.pop(k, None)
            elif et == "tool/result":
                end = ev.get("time")
                for sq in (ev.get("sourceEventSeqs") or []):
                    started = self.call_at.pop(sq, None)
                    name = self.call_name.pop(sq, None)
                    if name and started and end:
                        dur = max(0.0, end - started)
                        bucket = self.tool_times.setdefault(name, [])
                        bucket.append(dur)
                        if len(bucket) > 40:                 # 只留最近 40 次
                            del bucket[:20]
                    break

    def slowest(self, n: int = 3) -> list:
        """本次会话最慢的 n 个工具（按各自最慢一次排）。"""
        ranked = [(name, max(durs)) for name, durs in self.tool_times.items() if durs]
        ranked.sort(key=lambda x: -x[1])
        return ranked[:n]

    def poll(self) -> None:
        if not self.path:
            return
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:                           # 日志被轮转/重建
            self.offset, self.calls, self.partial = 0, {}, ""
            return
        if size == self.offset:
            return
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                buf = fh.read()
        except OSError:
            return

        offs, i = [], buf.find(self.MAGIC)
        while i != -1:
            offs.append(i)
            i = buf.find(self.MAGIC, i + 1)
        if not offs:
            return

        consumed = 0
        for n, start in enumerate(offs):
            end = offs[n + 1] if n + 1 < len(offs) else len(buf)
            txt = self._decode_frame(buf[start:end])
            if txt is None:                              # 帧未写完，留给下一次
                break
            self._consume(txt)
            consumed = end
        if consumed:
            self.offset += consumed


def find_log_path(session_id: str) -> Optional[str]:
    pat = os.path.join(SESS_DIR, "*", f"session-{session_id}", "session*.jsonl.zstd")
    cands = glob.glob(pat)
    if not cands:
        pat2 = os.path.join(SESS_DIR, "**", f"session-{session_id}", "session*.jsonl.zstd")
        cands = glob.glob(pat2, recursive=True)
    if not cands:
        return None
    try:
        return max(cands, key=os.path.getmtime)
    except OSError:
        return cands[0]


def snapshot_from_rows(rows: dict) -> Optional[dict]:
    """把投影行折成中间状态。找不到会话统计就返回 None。"""
    stats = (rows.get("sessionStats") or {}).get("val")
    if not isinstance(stats, dict):
        return None
    open_step = stats.get("openStep")
    pending = stats.get("pendingCalls") or {}
    if not isinstance(pending, dict):
        pending = {}

    # 三个此前没用上的信号：等你回答 / 出错重试 / 排队中
    qs = ((rows.get("userQuestions") or {}).get("val") or {}).get("questions") or {}
    waiting = bool(qs.get("active"))
    retry = (rows.get("llmRetry") or {}).get("val")
    failed = bool(retry) if isinstance(retry, dict) else False
    inbox = (rows.get("inbox") or {}).get("val") or {}
    queued = bool(inbox.get("next-turn") or inbox.get("next-step"))
    plan_active = bool(((rows.get("plan") or {}).get("val") or {}).get("active"))
    goal_val = ((rows.get("goal") or {}).get("val") or {}).get("current") or {}
    goal = ((goal_val.get("goal") or {}).get("objective") or "").strip()
    tb = (rows.get("turnBoundary") or {}).get("val") or {}
    turn_open = tb.get("openTurnStartSeq") is not None

    # 优先级：卡在用户这里 > 出错 > 跑工具 > 生成中
    if waiting:
        state = "waiting"
    elif failed:
        state = "failed"
    elif pending:
        state = "tools"
    elif isinstance(open_step, dict):
        state = "thinking"
    else:
        state = "idle"

    started = None
    if pending:
        vals = [v for v in pending.values() if isinstance(v, (int, float))]
        if vals:
            started = min(vals)
    if started is None and isinstance(open_step, dict):
        started = open_step.get("startTime")

    model_sel = (rows.get("modelSelection") or {}).get("val") or {}
    last_used = model_sel.get("lastUsed") or {}
    ctx = (rows.get("contextPressure") or {}).get("val") or {}
    usage = ((rows.get("tokenUsage") or {}).get("val") or {}).get("totals") or {}
    outline = ((rows.get("turnOutline") or {}).get("val") or {}).get("turns") or []
    prompt = ""
    if outline:
        prompt = (outline[-1].get("prompt") or "").strip()

    return dict(
        state=state,
        turn=(open_step or {}).get("turn") or stats.get("lastTurn") or 0,
        step=(open_step or {}).get("step") or stats.get("steps") or 0,
        started_ms=started,
        pending_ids=list(pending.keys()),
        title=((rows.get("title") or {}).get("val") or "") or "",
        provider=last_used.get("provider") or "",
        model=last_used.get("model") or "",
        context_pct=(ctx.get("pressureTokens", 0) / ctx["contextWindow"] * 100.0)
        if ctx.get("contextWindow") else 0.0,
        surface_tokens=float(ctx.get("surfaceTokens") or 0),
        context_window=float(ctx.get("contextWindow") or 0),
        tokens_out=float(usage.get("outputTokens") or 0),
        prompt=prompt,
        goal=goal,
        queued=queued,
        plan_active=plan_active,
        turn_open=turn_open,
    )


class StatusWatcher:
    """扫描全部 DSH 会话，汇总出「正在进行的任务」列表。

    一个会话算一个任务。投影缓存按 mtime+size 缓存，文件没变就不重复解析；
    工具名只为活跃会话建增量日志读取器 —— 只有它们用得上。
    """

    def __init__(self, pinned: Optional[str] = None, max_tasks: int = 4):
        self.pinned = pinned
        self.max_tasks = max_tasks
        self._cache: dict = {}                        # path -> (mtime, size, info|None)
        self._tails: dict = {}                        # session_id -> LogTail
        self._idle_since: Optional[float] = None
        self._turn_open: dict = {}                    # session_id -> 上次看到的回合开闭
        self._celebrate_until: dict = {}              # session_id -> 庆祝截止时间
        self._new_completion = False                  # 本轮刚有任务完成（供提示音）
        self._completed: list = []                    # 最近完成的会话 [{title, tools}]

    # -- 会话扫描 -----------------------------------------------------------
    def _scan(self) -> list:
        infos, alive = [], set()
        for path, sid, mtime, size in list_session_files():
            alive.add(path)
            hit = self._cache.get(path)
            if hit and hit[0] == mtime and hit[1] == size:
                info = hit[2]
            else:
                rows = (_read_json(path) or {}).get("record", {}).get("rows") or {}
                info = snapshot_from_rows(rows) if rows else None
                self._cache[path] = (mtime, size, info)
            if info:
                item = dict(info)
                item["session_id"] = sid
                item["mtime"] = mtime
                infos.append(item)
        for gone in [p for p in self._cache if p not in alive]:
            self._cache.pop(gone, None)
        return infos

    def _tail(self, sid: str) -> LogTail:
        tail = self._tails.get(sid)
        if tail is None:
            tail = LogTail(find_log_path(sid))
            self._tails[sid] = tail
            while len(self._tails) > 6:               # 只留最近用到的几个
                for k in list(self._tails):
                    if k != sid:
                        self._tails.pop(k, None)
                        break
        return tail

    def _make_task(self, info: dict, with_tools: bool) -> TaskInfo:
        names = []
        slow = []
        if with_tools:
            tail = self._tail(info["session_id"])
            tail.poll()
            if info["pending_ids"]:
                for cid in info["pending_ids"]:
                    nm = tail.calls.get(cid)
                    if nm and nm not in names:
                        names.append(nm)
                if not names and tail.last_tool:
                    names = [tail.last_tool]
            slow = tail.slowest(3)
        return TaskInfo(
            session_id=info["session_id"], state=info["state"], title=info["title"],
            provider=info["provider"], model=info["model"], turn=info["turn"],
            step=info["step"], started_ms=info["started_ms"], tool_names=names,
            prompt=info["prompt"], context_pct=info["context_pct"],
            surface_tokens=info["surface_tokens"], context_window=info["context_window"],
            tokens_out=info["tokens_out"], mtime=info["mtime"], goal=info["goal"],
            queued=info["queued"], plan_active=info["plan_active"],
            turn_open=info["turn_open"], slow_tools=slow)

    def _track_turns(self, infos: list) -> set:
        """检测「回合由开转闭」= 任务完成，返回正在庆祝的会话 id 集合。"""
        now = time.time()
        celebrating = set()
        for i in infos:
            sid, open_now = i["session_id"], i["turn_open"]
            was = self._turn_open.get(sid)
            if was is True and open_now is False:      # 刚刚跑完一个回合
                self._celebrate_until[sid] = now + CELEBRATE_S
                self._new_completion = True
                # 记下是谁跑完了 —— 通知气泡要说得出名字，而不是笼统的"任务完成"
                self._completed.append({
                    "title": (i.get("title") or sid[:8]).strip(),
                    "tools": list(i.get("tool_names") or [])[:2],
                })
                if len(self._completed) > 8:
                    del self._completed[:-8]
            self._turn_open[sid] = open_now
            if self._celebrate_until.get(sid, 0) > now:
                celebrating.add(sid)
        for sid in [s for s, t in self._celebrate_until.items() if t <= now]:
            self._celebrate_until.pop(sid, None)
        return celebrating

    # -- 采样 ---------------------------------------------------------------
    def poll(self) -> Snapshot:
        infos = self._scan()
        if self.pinned:
            infos = [i for i in infos if i["session_id"] == self.pinned]
        if not infos:
            return Snapshot(state="offline", detail_line="未发现 DSH 会话")

        celebrating = self._track_turns(infos)

        active = []
        for i in infos:
            if i["state"] in ACTIVE_STATES:
                active.append(self._make_task(i, with_tools=(i["state"] == "tools")))
            elif i["session_id"] in celebrating:
                t = self._make_task(i, with_tools=False)
                t.state = "celebrating"
                t.started_ms = (self._celebrate_until[i["session_id"]] - CELEBRATE_S) * 1000
                active.append(t)

        # 排序：按状态优先级；同类按开始时间，早的在前 —— 顺序稳定，不会来回跳
        active.sort(key=lambda t: (STATE_RANK.get(t.state, 9), t.started_ms or 0))

        if active:
            primary = active[0]
            state = primary.state
        else:
            state = "idle"
            primary = self._make_task(max(infos, key=lambda i: i["mtime"]), with_tools=False)

        snap = Snapshot(
            state=state, tasks=active[:self.max_tasks], session_id=primary.session_id,
            title=primary.title, provider=primary.provider, model=primary.model,
            turn=primary.turn, step=primary.step, started_ms=primary.started_ms,
            tool_names=primary.tool_names, context_pct=primary.context_pct,
            surface_tokens=primary.surface_tokens, context_window=primary.context_window,
            tokens_out=primary.tokens_out, prompt=primary.prompt,
            slow_tools=primary.slow_tools)

        # 完成通知：说得出是哪个会话跑完了，而不是笼统的"任务已完成"。
        # 只在庆祝刚开始的那几秒里说，不然会一直挂着。
        if state == "celebrating" and self._completed:
            item = self._completed[-1]
            name = item["title"][:18]
            tools = item["tools"]
            if tools:
                snap.detail_line = f"「{name}」跑完了 · {tools[0]}"
            else:
                snap.detail_line = f"「{name}」跑完了"
            snap.bubble = True
        elif state == "waiting":
            snap.detail_line = "在等你回答"
        elif state == "failed":
            snap.detail_line = "请求出错，正在重试"
        elif state == "celebrating":
            snap.detail_line = "任务已完成"
        elif state == "tools":
            if len(active) > 1:
                snap.detail_line = f"{len(active)} 个任务进行中"
            elif len(primary.tool_names) > 1:
                snap.detail_line = f"{len(primary.tool_names)} 个工具调用进行中"
            else:
                snap.detail_line = primary.prompt[:40] or "工具调用进行中"
        elif state == "thinking":
            snap.detail_line = primary.prompt[:44] if primary.prompt else "模型正在生成"
        else:
            if self._idle_since is None:
                self._idle_since = time.time()
            snap.idle_hint = "空闲" if time.time() - self._idle_since < 90 else "空闲（睡着了）"
            snap.detail_line = primary.title[:44] if primary.title else "等待你的下一条指令"
        if state != "idle":
            self._idle_since = None

        return snap


# ─────────────────────────────────────────────────────────────────────────────
# 陪伴模式：DSH 那边一个活跃会话都没有时，她自己过日子
# ─────────────────────────────────────────────────────────────────────────────
# 输出和 StatusWatcher 一样的 Snapshot，所以 PetApp 不需要知道状态是谁给的。
#
# 行为规则：
#   · 深夜（23:00–07:00）或精力耗尽 → 直接睡
#   · 一段时间没碰键盘鼠标 → 犯困 → 打盹 → 睡着
#   · 一有输入 → 醒来 → 回到空闲
#   · 白天精力足的时候会自己溜达、发呆
#   · 被戳/被拖/被喂 → 精神一点，亲密度涨

MOOD_PATH = os.path.join(data_dir(), "pet_mood.json")

# 闲置多久进入下一档（秒）
IDLE_YAWN = 45.0
IDLE_DOZE = 90.0
IDLE_SLEEP = 240.0
# 深夜只要安静这么久就睡
NIGHT_IDLE_SLEEP = 30.0
NIGHT_HOURS = (23, 7)            # [23:00, 07:00)


def cursor_pos() -> tuple:
    """当前鼠标的屏幕坐标。

    用 GetCursorPos 而不是 Tk 的 <Motion> —— 鼠标在窗口外时收不到 Motion 事件，
    而她要看的是整个桌面上的鼠标。
    """
    pt = wintypes.POINT()
    if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return int(pt.x), int(pt.y)
    return (0, 0)


def idle_seconds() -> float:
    """距上次键盘/鼠标输入过了多久（秒）。取不到就返回 0。"""
    class _LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = _LastInput()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    # GetTickCount 是 32 位、约 49.7 天回绕，用掩码算差值即可
    delta = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return delta / 1000.0


@dataclass
class Mood:
    """持久化的心情与相处记录，存在 pet_mood.json。"""

    affection: float = 0.5          # 亲密度 0~1
    energy: float = 1.0             # 精力 0~1
    pokes: int = 0                  # 被戳次数
    drags: int = 0                  # 被拖次数
    feeds: int = 0                  # 被喂次数
    first_seen: float = 0.0         # 第一次见面
    last_seen: float = 0.0          # 上次运行
    last_tick: float = 0.0          # 上次结算时间（算精力增减用）
    greeted: str = ""               # 已经问候过的时段（morning / night），避免重复
    unlocked: int = 0               # 已解锁的相处天数里程碑

    @property
    def days_together(self) -> int:
        if not self.first_seen:
            return 1
        return max(1, int((time.time() - self.first_seen) / 86400) + 1)

    @property
    def mood_label(self) -> str:
        """按亲密度给个说法，给气泡和详情卡用。"""
        a = self.affection
        if a >= 0.85:
            return "特别黏你"
        if a >= 0.65:
            return "挺开心"
        if a >= 0.4:
            return "还不错"
        if a >= 0.2:
            return "有点蔫"
        return "好久没理我了"


class CompanionSource:
    """自主陪伴行为。接口和 StatusWatcher 一样，只有 poll()。"""

    def __init__(self, path: str = MOOD_PATH):
        self.path = path
        self.mood = self._load()
        self.state = "idle"
        self.state_until = 0.0          # 当前状态至少保持到什么时候
        self.next_roam = time.time() + random.uniform(25, 70)
        self.next_peek = time.time() + random.uniform(120, 300)
        self.next_hungry = time.time() + random.uniform(180, 420)
        self.next_lonely = time.time() + random.uniform(1800, 3600)
        self.bubble = ""                # 当前要显示的气泡文案
        self.bubble_until = 0.0
        self._entered = time.time()
        self.last_touch = time.time()   # 上次被互动的时刻（判断"太久没人理"）
        # 看向鼠标：PetApp 每次 poll 前把这些填进来
        self.mouse_x = 0                # 鼠标屏幕 x
        self.mouse_at = 0.0             # 鼠标最后活动时刻
        self.pet_x = 0                  # 桌宠窗口左边 x
        self.gaze_dir = 1               # 1 向右看，-1 向左看
        self.wander_target = 0.0        # 溜达的窗口目标 x（0 = 不动）

    # -- 持久化 -------------------------------------------------------------
    def _load(self) -> Mood:
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:                                # noqa: BLE001
            raw = {}
        fields = {f.name for f in dataclasses.fields(Mood)}
        mood = Mood(**{k: v for k, v in raw.items() if k in fields})
        now = time.time()
        if not mood.first_seen:
            mood.first_seen = now
        mood.last_seen = now
        mood.last_tick = now
        return mood

    def save(self) -> None:
        try:
            self.mood.last_seen = time.time()
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(dataclasses.asdict(self.mood), fh, ensure_ascii=False, indent=2)
        except Exception:                                # noqa: BLE001
            pass

    # -- 互动 ---------------------------------------------------------------
    def touch(self, kind: str) -> None:
        """用户互动时调用：poke / drag / feed。会把她弄精神，亲密度上涨。"""
        self.last_touch = time.time()
        if kind == "poke":
            self.mood.pokes += 1
            self.mood.affection = min(1.0, self.mood.affection + 0.02)
            self.mood.energy = min(1.0, self.mood.energy + 0.05)
        elif kind == "drag":
            self.mood.drags += 1
            self.mood.affection = min(1.0, self.mood.affection + 0.01)
        elif kind == "feed":
            self.mood.feeds += 1
            self.mood.affection = min(1.0, self.mood.affection + 0.06)
            self.mood.energy = min(1.0, self.mood.energy + 0.25)
        # 被碰了就醒
        if self.state in ("dozing", "sleeping"):
            self._enter("waking", 2.6)
        self.save()

    def wake(self) -> None:
        """DSH 那边开工了 —— 她也该醒了（不碰统计，只是醒过来）。"""
        if self.state in ("dozing", "sleeping", "yawning"):
            self._enter("waking", 2.6)

    def say(self, text: str, secs: float = 4.0) -> None:
        self.bubble = text
        self.bubble_until = time.time() + secs

    # -- 状态机 -------------------------------------------------------------
    def _enter(self, state: str, hold: float = 0.0) -> None:
        self.state = state
        self.state_until = time.time() + hold
        self._entered = time.time()

    def _night(self) -> bool:
        hour = time.localtime().tm_hour
        lo, hi = NIGHT_HOURS
        return hour >= lo or hour < hi

    def _settle_energy(self, now: float, dt: float) -> None:
        """醒着掉精力，睡着回精力。"""
        if self.state == "sleeping":
            self.mood.energy = min(1.0, self.mood.energy + dt * 0.0025)
        elif self.state in ("dozing", "idle", "roaming", "yawning"):
            self.mood.energy = max(0.0, self.mood.energy - dt * 0.00035)
        self.mood.last_tick = now

    def poll(self) -> Snapshot:
        now = time.time()
        dt = max(0.0, min(now - (self.mood.last_tick or now), 5.0))
        self._settle_energy(now, dt)

        idle = idle_seconds()
        night = self._night()
        tired = self.mood.energy < 0.25
        held = now < self.state_until

        # ── 状态转换 ──
        if not held:
            if self.state in ("sleeping", "dozing"):
                # 有人动键盘鼠标，或者白天睡饱了，就醒
                if idle < 3.0 or (not night and self.mood.energy > 0.85):
                    self._enter("waking", 2.6)
                    if idle < 3.0 and random.random() < 0.35:
                        self.say(say_line("WAKE_LINES"))
            elif self.state == "waking":
                self._enter("idle")
            elif self.state == "yawning":
                self._enter("dozing", 0.0)
            elif self.state in ("roaming", "gazing", "peeking", "petted"):
                self._enter("idle")
                self.wander_target = 0.0                     # 停下就别再挪窗口
            elif self.state == "idle":
                sleep_after = NIGHT_IDLE_SLEEP if (night or tired) else IDLE_SLEEP
                if idle > sleep_after or (tired and idle > 20):
                    self._enter("sleeping")
                    if random.random() < 0.3:
                        self.say(say_line("SLEEP_LINES"))
                elif idle > IDLE_DOZE:
                    self._enter("dozing")
                elif idle > IDLE_YAWN:
                    self._enter("yawning", 2.8)
                    if random.random() < 0.4:
                        self.say(say_line("YAWN_LINES"))
                elif self._try_gaze(now, night):
                    pass                                     # 看向鼠标
                elif not night and self.mood.energy > 0.35 and now > self.next_roam:
                    self._enter("roaming", random.uniform(4.0, 8.0))
                    self.next_roam = now + random.uniform(35, 90)
                    self.wander_target = self._pick_wander_target()
                    if random.random() < 0.25:
                        self.say(say_line("ROAM_LINES"))
                elif now > self.next_peek:
                    self._enter("peeking", random.uniform(3.0, 6.0))
                    self.next_peek = now + random.uniform(120, 300)

        self._situational(now, idle, night)

        # ── 组装 Snapshot ──
        m = self.mood
        snap = Snapshot(state=self.state)
        snap.idle_hint = PALETTE[self.state]["label"]
        snap.title = f"陪你第 {m.days_together} 天"
        snap.detail_line = m.mood_label
        snap.tokens_out = float(m.pokes + m.drags + m.feeds)
        snap.phase = random.random()
        snap.forced = True
        # 看向哪边 / 溜达到哪 —— 交给 PetApp 去动窗口
        snap.mirror = self.gaze_dir < 0
        snap.wander = self.wander_target
        # 气泡：她说的话占用副行文案，但用强调色画出来，并进 _card_cache 的键
        if now < self.bubble_until and self.bubble:
            snap.detail_line = self.bubble
            snap.bubble = True
        return snap

    # -- 溜达目标 -----------------------------------------------------------
    def _pick_wander_target(self) -> float:
        """挑一个溜达目的地。

        多数时候就在附近走两步；偶尔（1/8）换一块显示器 —— 多屏时她会跑过去。
        """
        vx, vy, vw, vh = virtual_screen()
        if vw <= 0:
            return 0.0
        cur = float(self.pet_x)
        if random.random() < 0.125 and vw > WIN_W * 3:
            return float(random.randint(vx, max(vx, vx + vw - WIN_W)))
        step = random.uniform(-200, 200)
        return float(max(vx, min(vx + vw - WIN_W, cur + step)))

    # -- 看向鼠标 -----------------------------------------------------------
    def _try_gaze(self, now: float, night: bool) -> bool:
        """鼠标在附近动过就转头看他。返回是否接管了本帧。"""
        if night or now - self.mouse_at > GAZE_FRESH:
            return False
        dx = self.mouse_x - self.pet_x
        if abs(dx) < GAZE_MIN_DX:
            return False
        self.gaze_dir = 1 if dx > 0 else -1
        self._enter("gazing", GAZE_HOLD)
        self.next_roam = now + random.uniform(30, 80)        # 别马上又去溜达
        return True

    # -- 情境互动 -----------------------------------------------------------
    def _situational(self, now: float, idle: float, night: bool) -> None:
        """饿了、太久没人理、早晚问候、陪满天数解锁。都是低频触发。"""
        m = self.mood

        # 饿了她会主动要吃的
        if (m.energy < 0.22 and self.state in ("idle", "roaming", "gazing")
                and now > self.next_hungry):
            self.next_hungry = now + random.uniform(180, 420)
            self.say(say_line("HUNGRY_LINES"), 5.0)

        # 太久没人理：叹气 + 亲密度缓慢下降
        lonely_after = 7200.0
        if (now - self.last_touch > lonely_after and self.state in ("idle", "gazing")
                and now > self.next_lonely):
            self.next_lonely = now + random.uniform(1800, 3600)
            self.say(say_line("LONELY_LINES"), 5.0)
            m.affection = max(0.0, m.affection - 0.01)

        # 早晚问候：每个时段只问候一次
        hour = time.localtime().tm_hour
        slot = "morning" if 5 <= hour < 11 else ("night" if hour >= 22 or hour < 2 else "")
        if slot and self.greeted != slot and self.state == "idle":
            self.greeted = slot
            self.save()
            self.say(random.choice(MORNING_LINES if slot == "morning" else NIGHT_LINES), 5.0)

        # 陪满整周解锁新台词
        days = m.days_together
        if days >= 7 and self.unlocked < 7:
            self.unlocked = 7
            self.save()
            self.say(f"我们认识 {days} 天啦", 5.0)


# 陪伴模式的文案表。想改直接改这里。
WAKE_LINES = ["唔…醒了醒了", "你回来啦", "别晃我…", "再睡五分钟…"]
SLEEP_LINES = ["我先眯一会儿", "撑不住了…", "晚安", "困死了"]
YAWN_LINES = ["啊——欠", "好无聊啊", "你在忙什么", "有点困了"]
ROAM_LINES = ["溜达一圈", "活动活动", "看看窗外", "嗯…走两步"]
PET_LINES = ["嘿嘿", "好舒服", "再摸摸", "最喜欢你了"]
FEED_LINES = ["好吃！", "谢谢投喂", "还有吗", "满足了"]
ANNOY_LINES = ["别摸了…", "够了够了", "手拿开", "烦不烦"]
DIZZY_LINES = ["晕…", "别甩了！", "天旋地转", "放我下来"]
GRAB_LINES = ["喂！", "放开我", "干嘛呀", "别拎着我"]
HUNGRY_LINES = ["有点饿了…", "想吃东西", "喂我一口嘛"]
LONELY_LINES = ["好久没理我了", "在忙吗", "唉…"]
MORNING_LINES = ["早啊", "今天也加油", "睡醒啦"]
NIGHT_LINES = ["还不睡？", "该休息了", "夜深了"]
EDGE_LINES = ["抓到了", "不松手", "救命…"]
POMO_DONE_LINES = ["时间到！", "该休息了", "这一轮结束了", "起来走两步吧"]

# ── 台词表 ──────────────────────────────────────────────────────────────────
# 想换成自己的一套（毒舌版、温柔版…）：把同名键写进 <程序目录>/pet_lines.json，
# 比如 {"PET_LINES": ["嘿嘿", "再来"], "FEED_LINES": ["谢谢"]}。
# 没写的键继续用内置默认，所以只覆盖想改的那几条就行。
LINES_PATH = os.path.join(data_dir(), "pet_lines.json")

_LINE_DEFAULTS = {
    "WAKE_LINES": WAKE_LINES, "SLEEP_LINES": SLEEP_LINES, "YAWN_LINES": YAWN_LINES,
    "ROAM_LINES": ROAM_LINES, "PET_LINES": PET_LINES, "FEED_LINES": FEED_LINES,
    "ANNOY_LINES": ANNOY_LINES, "DIZZY_LINES": DIZZY_LINES, "GRAB_LINES": GRAB_LINES,
    "HUNGRY_LINES": HUNGRY_LINES, "LONELY_LINES": LONELY_LINES,
    "MORNING_LINES": MORNING_LINES, "NIGHT_LINES": NIGHT_LINES,
    "EDGE_LINES": EDGE_LINES, "POMO_DONE_LINES": POMO_DONE_LINES,
}


def load_lines() -> dict:
    """取台词表：内置默认 + pet_lines.json 的覆盖。

    只接受「非空字符串列表」的值 —— 写错了就当没写，不让一个坏配置文件
    把桌宠弄成一句话都不说。
    """
    table = dict(_LINE_DEFAULTS)
    try:
        with open(LINES_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:                                    # noqa: BLE001
        return table
    if not isinstance(raw, dict):
        return table
    for key, val in raw.items():
        if key not in table:
            continue
        if isinstance(val, list) and val and all(isinstance(x, str) and x for x in val):
            table[key] = val
    return table


LINES = load_lines()


def say_line(key: str) -> str:
    """随机取一句台词。表里没有这个键就返回空串（调用方自己兜底）。"""
    pool = LINES.get(key) or []
    return random.choice(pool) if pool else ""


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
# RegisterHotKey 的 hwnd 传 NULL 时，WM_HOTKEY 会投递到【线程】的消息队列，
# 所以必须自己起一个线程跑 GetMessage —— Tk 的消息循环拿不到我们的消息。
# 回调不直接碰界面（Tk 只能在主线程操作），而是把动作名塞进队列，
# 由主线程的 _drain_tray 消费 —— 和托盘回调走同一条路。

MOD_ALT, MOD_CONTROL, MOD_SHIFT = 0x0001, 0x0002, 0x0004
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
VK_P, VK_F, VK_S = 0x50, 0x46, 0x53
VK_H, VK_D = 0x48, 0x44

HOTKEYS = {
    1: (MOD_CONTROL | MOD_ALT, VK_P, "toggle"),      # 显示 / 隐藏
    2: (MOD_CONTROL | MOD_ALT, VK_F, "feed"),        # 喂一口
    3: (MOD_CONTROL | MOD_ALT, VK_S, "settings"),    # 打开设置
    4: (MOD_CONTROL | MOD_ALT, VK_H, "ghost"),       # 截图模式：整个隐藏（连托盘也不显示）
    5: (MOD_CONTROL | MOD_ALT, VK_D, "focus"),       # 专注模式：她安静待机不冒泡
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
APP_VERSION = "1.1.0"
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


def autostart_command() -> str:
    """开机自启要跑的命令行。打包成 exe 之后就是 exe 自己。"""
    if getattr(sys, "frozen", False):                    # PyInstaller 打的包
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
        return True
    except Exception:                                    # noqa: BLE001
        return False


def set_autostart(on: bool) -> bool:
    """开/关开机自启。返回是否成功。"""
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            if on:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:                             # noqa: BLE001
        print(f"[warn] 设置开机自启失败：{exc}", file=sys.stderr)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 桌宠窗口
# ─────────────────────────────────────────────────────────────────────────────

class PetApp:
    def __init__(self, args):
        import tkinter as tk
        self.tk = tk
        self.args = args
        self.cfg = self._load_config()

        self.forced_state: Optional[str] = None
        self.demo_i = 0
        self.last_demo = 0.0

        self.root = tk.Tk()
        self.root.title("DSH Pet")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))
        self.root.resizable(False, False)

        dx, dy = default_position()
        x = int(self.cfg.get("x", dx))
        y = int(self.cfg.get("y", dy))
        # 缩放要在几何之前算好 —— 窗口尺寸由它决定
        self.scale = max(SCALE_MIN, min(SCALE_MAX, float(self.cfg.get("scale", 1.0))))
        self.win_w = max(60, int(round(WIN_W * self.scale)))
        self.win_h = max(80, int(round(WIN_H * self.scale)))
        x, y = clamp_position(x, y)                      # 拔掉副屏后可能已经在屏幕外
        self.root.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")

        self.display, self.backend = make_display(self.root, self.win_w, self.win_h)

        self.watcher = None if (args.demo or args.force_state) else \
            StatusWatcher(pinned=args.session)
        # 陪伴模式：DSH 没活干时接管。--demo/--force-state 下不用，那是纯预览。
        self.companion = None if (args.demo or args.force_state or args.no_companion) \
            else CompanionSource()
        self.in_companion = False

        self.detail = bool(self.cfg.get("show_detail", False))
        self.privacy = bool(self.cfg.get("privacy", False))
        self.sound = bool(self.cfg.get("sound", True))
        self.frame_i = 0
        self.snap = Snapshot(state=args.force_state or "idle")
        self._card_cache = (None, None)
        self._drag = None
        self._moved = False
        self.poke_until = 0.0
        self._tray_actions: list = []
        self._last_sound = 0.0

        # ── 交互状态 ──
        # 缩放（scale / win_w / win_h）已在几何之前初始化，这里只补其余交互状态。
        self.hover_since = 0.0        # 鼠标停在她身上的起始时刻
        self.hover_until = 0.0        # 悬停反应持续到什么时候
        self.dizzy_until = 0.0        # 被甩晕
        self.annoyed_until = 0.0      # 被摸烦了
        self.petted_until = 0.0       # 摸得开心
        self.pet_streak = 0           # 连续摸的次数（超了就烦）
        self.pet_streak_at = 0.0
        self.grabbed = False          # 长按"抱起来"
        self.grab_edge = None         # 抓住屏幕边缘时贴的是哪一边
        self._press_at = 0.0
        self._press_xy = (0, 0)
        self._shake = []              # 甩动检测：最近几次拖动的方向与时刻
        self._mouse = (0, 0)          # 最近一次鼠标位置（用来看向鼠标）
        self._mouse_at = 0.0
        self._cursor_prev = (0, 0)    # 全局鼠标位置的上一次采样（判断"刚动过"）
        self._cursor_at = 0.0
        # 番茄钟 / 专注模式 / 截图模式
        self.pomo_until = 0.0         # 计时结束时刻（0 = 没在计时）
        self.pomo_total = 0
        self.focus_mode = bool(self.cfg.get("focus", False))
        self._ghost = False           # 截图模式：连托盘一起藏
        self._autohide = bool(self.cfg.get("autohide", False))
        self._peek_until = 0.0        # 贴边隐藏后探出来的时刻
        self._attention_at = 0.0      # 上次"等待提醒"挪窗口的时刻
        self._long_press_job = None
        self.wander_x = 0             # 溜达时窗口的水平偏移

        self.skin = load_skin(getattr(args, "skin", None) or self.cfg.get("skin", "dafeiyu"))
        self._build_sprites()
        self._bind_events()
        self._start_tray()

        self.root.after(60, self._poll)
        self.root.after(60, self._tick)

    # -- 配置 ---------------------------------------------------------------
    def _load_config(self) -> dict:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:                                # noqa: BLE001
            return {}

    def _save_config(self) -> None:
        cfg = dict(self.cfg)
        cfg["x"], cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        cfg["show_detail"] = self.detail
        cfg["privacy"] = self.privacy
        cfg["sound"] = self.sound
        cfg["skin"] = self.skin.name
        cfg["scale"] = round(self.scale, 3)
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
        except Exception:                                # noqa: BLE001
            pass

    # -- 精灵缓存 -----------------------------------------------------------
    def _ensure_state(self, st: str) -> None:
        """按需渲染某个状态的帧。

        不能启动时全建：whale-chan 七个状态就要 6.4 秒，现在状态涨到 13 个，
        全建得十几秒，那段时间窗口是死的。改成用到哪个建哪个，
        启动只等 idle。
        """
        if st in self.sprites:
            return
        try:
            items = self.skin.frames(st)
        except Exception as exc:                         # noqa: BLE001
            print(f"[warn] 状态 {st} 渲染失败：{exc}", file=sys.stderr)
            items = []
        self.sprites[st] = [im for im, _ in items]
        self.durations[st] = [d for _, d in items]
        self._loop_ms[st] = sum(self.durations[st])

    def _build_sprites(self) -> None:
        """预渲染 idle，其余状态等用到再建（见 _ensure_state）。"""
        self.sprites: dict = {}
        self.durations: dict = {}
        self._loop_ms: dict = {}
        t0 = time.time()
        self._ensure_state("idle")
        # 交互反应素材（clawd 主题自带；内置皮肤程序化生成）
        self.reaction_sprites: dict = {}
        self.reaction_durs: dict = {}
        for kind in REACTION_KINDS:
            try:
                items = self.skin.reaction_frames(kind)
            except Exception as exc:                     # noqa: BLE001
                print(f"[warn] 反应素材 {kind} 加载失败：{exc}", file=sys.stderr)
                items = []
            self.reaction_sprites[kind] = [im for im, _ in items]
            self.reaction_durs[kind] = [d for _, d in items]
        have = [k for k in REACTION_KINDS if self.reaction_sprites[k]]
        print(f"[skin] {self.skin.display}: 启动预渲染 "
              f"{len(self.sprites['idle'])} 帧，{time.time() - t0:.1f}s"
              f"；其余状态按需加载；交互反应 {', '.join(have) if have else '无'}")

    # -- 事件 ---------------------------------------------------------------
    def _bind_events(self) -> None:
        r = self.root
        r.bind("<ButtonPress-1>", self._on_press)
        r.bind("<B1-Motion>", self._on_drag)
        r.bind("<ButtonRelease-1>", self._on_release)
        r.bind("<Button-3>", self._on_menu)
        r.bind("<Motion>", self._on_motion)               # 悬停反应 + 看向鼠标
        r.bind("<Leave>", self._on_leave)
        r.bind("<MouseWheel>", self._on_wheel)            # 滚轮缩放
        r.bind("<Double-Button-1>", self._on_double)      # 双击开设置

        self.menu = self.tk.Menu(r, tearoff=0)
        self.menu.add_command(label="喂一口 🍡", command=self._feed)
        self.menu.add_command(label="展开/收起详情", command=self._toggle_detail)
        self.menu.add_separator()
        self.menu.add_command(label="自动跟随活跃会话", command=self._follow_auto)
        self.menu.add_command(label="复制会话 ID", command=self._copy_sid)
        self.menu.add_separator()
        self.var_privacy = self.tk.BooleanVar(value=self.privacy)
        self.var_sound = self.tk.BooleanVar(value=self.sound)
        self.var_companion = self.tk.BooleanVar(
            value=self.companion is not None and bool(self.cfg.get("companion", True)))
        self.menu.add_checkbutton(label="隐私模式（隐藏标题与提示词）",
                                  variable=self.var_privacy, command=self._toggle_privacy)
        self.menu.add_checkbutton(label="任务完成时提示音",
                                  variable=self.var_sound, command=self._toggle_sound)
        self.menu.add_checkbutton(label="陪伴模式（没任务时自己过日子）",
                                  variable=self.var_companion, command=self._toggle_companion)
        self.var_focus = self.tk.BooleanVar(value=self.focus_mode)
        self.menu.add_checkbutton(label="专注模式（安静待机不打扰）",
                                  variable=self.var_focus, command=self._toggle_focus)
        self.menu.add_separator()
        self._pomo_menu = self.tk.Menu(self.menu, tearoff=0)
        for mins in POMODORO_CHOICES:
            self._pomo_menu.add_command(label=f"{mins} 分钟",
                                        command=lambda m=mins: self._start_pomodoro(m))
        self._pomo_menu.add_separator()
        self._pomo_menu.add_command(label="取消计时", command=self._cancel_pomodoro)
        self.menu.add_cascade(label="番茄钟 🍅", menu=self._pomo_menu)
        self.menu.add_separator()
        self.menu.add_cascade(label="皮肤", menu=self._skin_menu)
        self._rebuild_skin_menu()
        self.menu.add_separator()
        self.menu.add_command(label="设置…", command=self._open_settings)
        self.menu.add_command(label="导出相处记录…", command=self._export_record)
        self.menu.add_command(label="检查更新…", command=self._check_update_async)
        self.menu.add_command(label="退出", command=self._quit)

    def _rebuild_skin_menu(self) -> None:
        self._skin_menu.delete(0, "end")
        self.var_skin = self.tk.StringVar(value=self.skin.name)
        for s in discover_skins():
            self._skin_menu.add_radiobutton(
                label=s["display"], value=s["id"], variable=self.var_skin,
                command=lambda sid=s["id"]: self._switch_skin(sid))

    def _switch_skin(self, skin_id: str) -> None:
        new = load_skin(skin_id)
        if new.name == self.skin.name and new.kind == self.skin.kind:
            return
        self.skin = new
        self._build_sprites()
        self._card_cache = (None, None)
        self._save_config()

    def _toggle_privacy(self) -> None:
        self.privacy = bool(self.var_privacy.get())
        self._card_cache = (None, None)
        self._save_config()

    def _toggle_sound(self) -> None:
        self.sound = bool(self.var_sound.get())
        self._save_config()

    def _toggle_companion(self) -> None:
        on = bool(self.var_companion.get())
        self.cfg["companion"] = on
        if on and self.companion is None:
            self.companion = CompanionSource()
        elif not on:
            if self.companion is not None:
                self.companion.save()
            self.companion = None
            self.in_companion = False
        self._save_config()

    # -- 设置窗口 -----------------------------------------------------------
    def _open_settings(self) -> None:
        tk = self.tk
        if getattr(self, "_settings_win", None) is not None:
            try:
                self._settings_win.deiconify()
                self._settings_win.lift()
                return
            except Exception:                            # noqa: BLE001
                self._settings_win = None

        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title("DSH 桌宠 · 设置")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        pad = dict(padx=14, pady=6, anchor="w")

        row = 0
        tk.Label(win, text="行为", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 2))
        row += 1

        v_comp = tk.BooleanVar(value=self.companion is not None)
        v_priv = tk.BooleanVar(value=self.privacy)
        v_sound = tk.BooleanVar(value=self.sound)
        v_auto = tk.BooleanVar(value=autostart_enabled())

        def apply_companion():
            self.var_companion.set(v_comp.get())
            self._toggle_companion()

        def apply_priv():
            self.var_privacy.set(v_priv.get())
            self._toggle_privacy()

        def apply_sound():
            self.var_sound.set(v_sound.get())
            self._toggle_sound()

        def apply_auto():
            ok = set_autostart(v_auto.get())
            if not ok:
                v_auto.set(autostart_enabled())
                status.config(text="设置开机自启失败（见控制台）", fg="#B93B37")

        tk.Checkbutton(win, text="陪伴模式（DSH 没任务时自己过日子）", variable=v_comp,
                       command=apply_companion).grid(row=row, column=0, columnspan=2, **pad)
        row += 1
        tk.Checkbutton(win, text="开机自启", variable=v_auto,
                       command=apply_auto).grid(row=row, column=0, columnspan=2, **pad)
        row += 1
        tk.Checkbutton(win, text="隐私模式（隐藏标题与提示词）", variable=v_priv,
                       command=apply_priv).grid(row=row, column=0, columnspan=2, **pad)
        row += 1
        tk.Checkbutton(win, text="任务完成时提示音", variable=v_sound,
                       command=apply_sound).grid(row=row, column=0, columnspan=2, **pad)
        row += 1

        tk.Frame(win, height=1, bg="#DDD").grid(row=row, column=0, columnspan=2,
                                                sticky="ew", padx=14, pady=8)
        row += 1
        tk.Label(win, text="皮肤", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 2))
        row += 1

        v_skin = tk.StringVar(value=self.skin.name)
        labels = {s["id"]: s["display"] for s in discover_skins()}
        om = tk.OptionMenu(win, v_skin, *labels.keys(),
                           command=lambda sid: self._switch_skin(sid))
        om.grid(row=row, column=0, columnspan=2, sticky="ew", padx=14, pady=4)
        row += 1

        tk.Frame(win, height=1, bg="#DDD").grid(row=row, column=0, columnspan=2,
                                                sticky="ew", padx=14, pady=8)
        row += 1
        tk.Label(win, text="相处记录", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 2))
        row += 1

        m = self.companion.mood if self.companion else Mood()
        info = (f"陪你第 {m.days_together} 天 · {m.mood_label}\n"
                f"被戳 {m.pokes} 次 · 被拖 {m.drags} 次 · 被喂 {m.feeds} 次\n"
                f"亲密度 {m.affection * 100:.0f}% · 精力 {m.energy * 100:.0f}%")
        tk.Label(win, text=info, justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 6))
        row += 1

        def reset_pos():
            dx, dy = default_position()
            self.root.geometry(f"{WIN_W}x{WIN_H}+{dx}+{dy}")
            self._save_config()
            status.config(text="位置已复位", fg="#2E9E63")

        def forget_me():
            if self.companion is not None:
                self.companion.mood = Mood()
                self.companion.mood.first_seen = time.time()
                self.companion.save()
            win.destroy()
            self._settings_win = None
            self._open_settings()

        btns = tk.Frame(win)
        btns.grid(row=row, column=0, columnspan=2, sticky="ew", padx=14, pady=(2, 6))
        tk.Button(btns, text="位置复位", command=reset_pos).pack(side="left")
        tk.Button(btns, text="重置相处记录", command=forget_me).pack(side="left", padx=6)
        row += 1

        status = tk.Label(win, text="", fg="#666")
        status.grid(row=row, column=0, columnspan=2, sticky="w", padx=14)
        row += 1
        tk.Label(win, text="热键：Ctrl+Alt+P 显示/隐藏 · +F 喂食 · +S 设置\n"
                           "滚轮缩放 · 双击开这个窗口 · 甩动/长按有惊喜",
                 justify="left", fg="#777").grid(
            row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 0))
        row += 1
        tk.Button(win, text="关闭", width=10, command=lambda: (
            win.destroy(), setattr(self, "_settings_win", None))).grid(
            row=row, column=0, columnspan=2, pady=(6, 14))

    # -- 托盘图标 -----------------------------------------------------------
    def _start_tray(self) -> None:
        self.tray = None
        self._tray_key = None
        self._hidden = False
        # 全局热键：和托盘共用动作队列，都在主线程消费
        self.hotkeys = None
        if not getattr(self.args, "no_hotkeys", False):
            try:
                self.hotkeys = HotKeys(sink=self._tray_actions.append)
                self.hotkeys.start()
            except Exception as exc:                     # noqa: BLE001
                print(f"[warn] 全局热键注册失败：{exc}", file=sys.stderr)
                self.hotkeys = None
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("显示 / 隐藏桌宠",
                                 lambda: self._tray_actions.append("toggle"), default=True),
                pystray.MenuItem("展开 / 收起详情",
                                 lambda: self._tray_actions.append("detail")),
                pystray.MenuItem("隐私模式", lambda: self._tray_actions.append("privacy"),
                                 checked=lambda _i: self.privacy),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", lambda: self._tray_actions.append("quit")),
            )
            self.tray = pystray.Icon("dsh_pet", self._tray_image(), "DSH 桌宠", menu)
            # pystray 自带消息循环，放后台线程；菜单回调只投递动作，主线程执行
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception as exc:                          # noqa: BLE001
            print(f"[warn] 托盘图标不可用（{exc}）", file=sys.stderr)
            self.tray = None

    def _tray_image(self) -> Image.Image:
        """托盘图标 = 当前状态的首帧缩到 64px。"""
        st = self.snap.state
        frames = self.sprites.get(st) or self.sprites.get("idle") or []
        canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        if not frames:
            return canvas
        im = frames[0].copy()
        bb = im.getbbox()
        if bb:
            im = im.crop(bb)
        im.thumbnail((60, 60), Image.LANCZOS)
        canvas.alpha_composite(im, ((64 - im.width) // 2, (64 - im.height) // 2))
        return canvas

    def _tray_sync(self) -> None:
        if self.tray is None:
            return
        key = (self.snap.state, len(self.snap.tasks), self.privacy)
        if key == self._tray_key:
            return
        self._tray_key = key
        try:
            self.tray.icon = self._tray_image()
            self.tray.title = f"DSH 桌宠 · {PALETTE[self.snap.state]['label']}"
            self.tray.update_menu()
        except Exception:                                 # noqa: BLE001
            pass

    def _drain_tray(self) -> None:
        """托盘回调在别的线程，必须回到主线程操作界面。"""
        while self._tray_actions:
            act = self._tray_actions.pop(0)
            if act == "toggle":
                self._hidden = not self._hidden
                if self._hidden:
                    self.root.withdraw()
                else:
                    self.root.deiconify()
            elif act == "detail":
                self._toggle_detail()
            elif act == "privacy":
                self.var_privacy.set(not self.privacy)
                self._toggle_privacy()
            elif act == "feed":
                self._feed()
            elif act == "settings":
                self._open_settings()
            elif act == "ghost":
                self._toggle_ghost()
            elif act == "focus":
                self.var_focus.set(not self.focus_mode)
                self._toggle_focus()
            elif act == "quit":
                self._quit()
            elif isinstance(act, str) and act.startswith("update:"):
                tag = act.split(":", 1)[1]
                if self.companion is not None:
                    self.companion.say(f"有新版本 {tag}", 6.0)
                print(f"[update] 有新版本 {tag} -> {RELEASES_URL}")

    # -- 番茄钟 / 专注 / 截图模式 -------------------------------------------
    def _toggle_focus(self) -> None:
        """专注模式：她安静待机，不冒泡、不溜达、不主动说话。"""
        self.focus_mode = bool(self.var_focus.get())
        if self.companion is not None and self.focus_mode:
            self.companion.bubble_until = 0.0
            self.companion.bubble = ""
        self._save_config()

    def _toggle_ghost(self) -> None:
        """截图模式：连窗口带托盘一起藏掉，录屏时完全不碍事。"""
        self._ghost = not getattr(self, "_ghost", False)
        if self._ghost:
            self.root.withdraw()
            if self.tray is not None:
                try:
                    self.tray.visible = False
                except Exception:                        # noqa: BLE001
                    pass
        else:
            self.root.deiconify()
            if self.tray is not None:
                try:
                    self.tray.visible = True
                except Exception:                        # noqa: BLE001
                    pass

    def _start_pomodoro(self, minutes: int) -> None:
        self.pomo_until = time.time() + minutes * 60
        self.pomo_total = minutes * 60
        if self.companion is not None:
            self.companion.say(f"{minutes} 分钟，开始专注", 3.0)
        self._save_config()

    def _cancel_pomodoro(self) -> None:
        self.pomo_until = 0.0
        if self.companion is not None:
            self.companion.say("番茄钟取消了", 2.5)

    # -- 相处记录导出 / 更新检查 -------------------------------------------
    def _export_record(self) -> None:
        """把相处记录画成一张图，方便分享。"""
        m = self.companion.mood if self.companion else Mood()
        W, H = 420, 260
        img = Image.new("RGBA", (W, H), (26, 29, 38, 255))
        d = ImageDraw.Draw(img)
        try:
            f_big = load_font(26, bold=True)
            f_mid = load_font(15)
            f_small = load_font(13)
        except Exception:                                # noqa: BLE001
            f_big = f_mid = f_small = None
        d.rounded_rectangle([10, 10, W - 10, H - 10], radius=14,
                            outline=(70, 78, 96, 255), width=2)
        d.text((28, 26), f"陪你第 {m.days_together} 天", font=f_big, fill=(240, 243, 248, 255))
        d.text((28, 62), m.mood_label, font=f_mid, fill=(150, 200, 255, 255))
        # 亲密度条
        d.rounded_rectangle([28, 92, W - 28, 100], radius=4, fill=(52, 56, 66, 255))
        fw = max(4, int((W - 56) * max(0.0, min(1.0, m.affection))))
        d.rounded_rectangle([28, 92, 28 + fw, 100], radius=4, fill=(255, 128, 168, 255))
        d.text((28, 108), f"亲密度 {m.affection * 100:.0f}%   精力 {m.energy * 100:.0f}%",
               font=f_small, fill=(152, 160, 176, 255))
        rows = [
            ("被摸", f"{m.pokes} 次"),
            ("被拖", f"{m.drags} 次"),
            ("被喂", f"{m.feeds} 次"),
        ]
        y = 140
        for k, v in rows:
            d.text((28, y), k, font=f_small, fill=(152, 160, 176, 255))
            vw = d.textlength(v, font=f_small)
            d.text((W - 28 - vw, y), v, font=f_small, fill=(240, 243, 248, 255))
            y += 22
        d.text((28, H - 40), "DSH 桌宠", font=f_small, fill=(110, 118, 134, 255))
        out = os.path.join(data_dir(), "相处记录.png")
        try:
            img.convert("RGB").save(out)
            print(f"[export] 相处记录 -> {out}")
            if self.companion is not None:
                self.companion.say("记好啦", 2.5)
        except Exception as exc:                         # noqa: BLE001
            print(f"[warn] 导出失败：{exc}", file=sys.stderr)

    def _check_update_async(self) -> None:
        """后台查一次新版本，结果塞进动作队列 —— 不阻塞界面。"""
        def work():
            tag = check_update()
            if tag:
                self._tray_actions.append(f"update:{tag}")
        try:
            threading.Thread(target=work, name="dsh-pet-update", daemon=True).start()
        except Exception:                                # noqa: BLE001
            pass

    def _attention(self) -> None:
        """卡在「等你回答」时，把她挪到屏幕中间晃一下 —— 别让她安静地举着手被忽略。

        只在状态刚变成 waiting 时挪一次，之后就待在中间，不来回跳。
        """
        now = time.time()
        if now - self._attention_at < 20.0:              # 20 秒内不重复
            return
        self._attention_at = now
        if self._drag is not None or self._drop_job is not None:
            return
        vx, vy, vw, vh = virtual_screen()
        if vw <= 0:
            return
        cx = vx + (vw - self.win_w) // 2
        cy = vy + int(vh * 0.28)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{cx}+{cy}")
        self.root.attributes("-topmost", True)
        self.root.lift()

    def _pomodoro_tick(self, now: float) -> None:
        """番茄钟计时与到点提醒。"""
        if not self.pomo_until:
            return
        if now < self.pomo_until:
            remain = int(self.pomo_until - now)
            self.snap.detail_line = f"🍅 {remain // 60:02d}:{remain % 60:02d}"
            self.snap.bubble = True                      # 用强调色，跟状态文案区分
            return
        # 到点了
        self.pomo_until = 0.0
        self._play_sound()
        if self.companion is not None:
            self.companion.say(say_line("POMO_DONE_LINES"), 6.0)

    def _play_sound(self) -> None:
        if not self.sound:
            return
        now = time.time()
        if now - self._last_sound < 3.0:                  # 多个任务同时完成只响一次
            return
        self._last_sound = now
        try:
            import winsound
            winsound.PlaySound("SystemAsterisk",
                               winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception:                                 # noqa: BLE001
            pass

    # -- 缩放 ---------------------------------------------------------------
    def _overlay_kind(self, now: float) -> str:
        """当前该画哪个交互装饰。交互优先于状态 —— 你碰她的时候先回应你。"""
        if now < self.dizzy_until:
            return "stars"
        if self.grabbed:
            return "sweat"
        if now < self.annoyed_until:
            return "angry"
        if now < self.petted_until:
            return "heart"
        if self.hover_since and now - self.hover_since > HOVER_DELAY:
            return "question"
        if self.snap.state == "sleeping":
            return "zzz"
        if self.snap.state == "celebrating":
            return "note"
        return ""

    def _apply_scale(self, new_scale: float) -> None:
        """改变缩放：重建显示层与窗口几何，以窗口中心为锚点。"""
        new_scale = max(SCALE_MIN, min(SCALE_MAX, new_scale))
        if abs(new_scale - self.scale) < 0.005:
            return
        old_w, old_h = self.win_w, self.win_h
        self.scale = new_scale
        self.win_w = max(60, int(round(WIN_W * self.scale)))
        self.win_h = max(80, int(round(WIN_H * self.scale)))
        x = self.root.winfo_x() + (old_w - self.win_w) // 2
        y = self.root.winfo_y() + (old_h - self.win_h) // 2
        self.display, self.backend = make_display(self.root, self.win_w, self.win_h)
        x, y = clamp_position(x, y)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")
        self._card_cache = (None, None)
        self._save_config()

    # -- 鼠标交互 -----------------------------------------------------------
    def _on_motion(self, e):
        """鼠标在窗口内移动：记录位置（她会看向鼠标），并开始计悬停。"""
        self._mouse = (e.x_root, e.y_root)
        self._mouse_at = time.time()
        if self.hover_since == 0.0:
            self.hover_since = time.time()

    def _on_leave(self, _e):
        self.hover_since = 0.0

    def _on_wheel(self, e):
        """滚轮缩放。Windows 上 delta 是 120 的整数倍。"""
        self._apply_scale(self.scale + (0.1 if e.delta > 0 else -0.1))

    def _on_double(self, _e):
        """双击打开设置。标记 _moved 免得 release 又当成一次「摸」。"""
        self._moved = True
        self._cancel_long_press()
        self._open_settings()

    def _cancel_long_press(self) -> None:
        if self._long_press_job is not None:
            try:
                self.root.after_cancel(self._long_press_job)
            except Exception:                            # noqa: BLE001
                pass
            self._long_press_job = None

    def _long_press_fired(self) -> None:
        """按住不动够久 —— 把她「抱起来」，她会挣扎。"""
        self._long_press_job = None
        if self._drag is None or self._moved:
            return
        self.grabbed = True

    def _drop(self) -> None:
        """松手：她往下掉，落到屏幕底部。"""
        self.grabbed = False
        if getattr(self, "_drop_job", None) is not None:
            try:
                self.root.after_cancel(self._drop_job)
            except Exception:                            # noqa: BLE001
                pass
        floor = self.root.winfo_screenheight() - self.win_h - TASKBAR_MARGIN
        vel = 0.0

        def step():
            nonlocal vel
            x, y = self.root.winfo_x(), self.root.winfo_y()
            if y >= floor:
                self.root.geometry(f"+{x}+{floor}")
                self._drop_job = None
                self._save_config()
                return
            vel += GRAVITY
            ny = min(floor, y + int(vel))
            self.root.geometry(f"+{x}+{ny}")
            self._drop_job = self.root.after(16, step)

        step()

    def _on_press(self, e):
        self._cancel_long_press()
        self._drag = (e.x_root, e.y_root,
                      self.root.winfo_x(), self.root.winfo_y())
        self._moved = False
        self.grabbed = False
        self._shake = []
        self._press_at = time.time()
        self._long_press_job = self.root.after(LONG_PRESS_MS, self._long_press_fired)

    def _on_drag(self, e):
        if not self._drag:
            return
        sx, sy, wx, wy = self._drag
        dx, dy = e.x_root - sx, e.y_root - sy
        if abs(dx) > 3 or abs(dy) > 3:
            if not self._moved:
                self._cancel_long_press()                # 动了就不算长按
                if self.companion is not None:
                    self.companion.touch("drag")         # 拖动也记一笔
            self._moved = True
        # 甩动检测：记最近几次水平方向，来回换够多次就算甩
        now = time.time()
        self._shake = [(t, d) for t, d in self._shake if now - t < SHAKE_WINDOW]
        if abs(dx) > 12:
            self._shake.append((now, 1 if dx > 0 else -1))
        dirs = [d for _, d in self._shake]
        flips = sum(1 for a, b in zip(dirs, dirs[1:]) if a != b)
        if flips >= SHAKE_DIRS:
            self._shake = []
            self.dizzy_until = now + SHAKE_DIZZY_S
            self.poke_until = 0.0
        self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def _on_release(self, e):
        was_grab = self.grabbed
        self._cancel_long_press()
        self._drag = None
        if was_grab:
            self._drop()                                 # 抱起来后松手 → 掉下去
            return
        if not self._moved:
            now = time.time()
            # 单击 = 戳一下：播一次反应动画（皮肤没提供就只切详情卡）
            durs = self.reaction_durs.get("poke") or []
            if durs:
                self.poke_until = now + sum(durs) / 1000.0
            # 陪伴模式记一笔：亲密度和精力都会涨，睡着的话会醒
            if self.companion is not None:
                self.companion.touch("poke")
                # 连着摸太多次她会烦
                if now - self.pet_streak_at > ANNOY_WINDOW:
                    self.pet_streak = 0
                self.pet_streak_at = now
                self.pet_streak += 1
                if self.pet_streak >= ANNOY_AFTER:
                    self.annoyed_until = now + ANNOY_S
                    self.pet_streak = 0
                    self.companion.say(say_line("ANNOY_LINES"), 2.4)
                else:
                    self.petted_until = now + 1.8
                    if self.in_companion and random.random() < 0.3:
                        self.companion.say(say_line("PET_LINES"))
            self._toggle_detail()
        else:
            # 拖到屏幕边缘附近 —— 她会抓住不放
            x, y = self.root.winfo_x(), self.root.winfo_y()
            vx, vy, vw, vh = virtual_screen()
            edge = None
            if x <= vx + EDGE_SNAP:
                edge, x = "left", vx
            elif x + self.win_w >= vx + vw - EDGE_SNAP:
                edge, x = "right", vx + vw - self.win_w
            elif y <= vy + EDGE_SNAP:
                edge, y = "top", vy
            self.grab_edge = edge
            if edge is not None:
                self.root.geometry(f"+{x}+{y}")
        self._save_config()

    def _feed(self):
        """喂一口：精力大涨，亲密度涨得最多。"""
        if self.companion is None:
            return
        self.companion.touch("feed")
        self.companion.say(say_line("FEED_LINES"), 3.5)
        durs = self.reaction_durs.get("poke") or []
        if durs:
            self.poke_until = time.time() + sum(durs) / 1000.0

    def _toggle_detail(self):
        self.detail = not self.detail
        self._card_cache = (None, None)
        self._save_config()

    def _on_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _follow_auto(self):
        if self.watcher:
            self.watcher.pinned = None

    def _copy_sid(self):
        sid = self.snap.session_id
        if not sid:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(sid)
        except Exception:                                # noqa: BLE001
            pass

    def _quit(self):
        self._save_config()
        if self.companion is not None:
            self.companion.save()                        # 记下这一程
        if getattr(self, "hotkeys", None) is not None:
            try:
                self.hotkeys.stop()
            except Exception:                            # noqa: BLE001
                pass
        try:
            self.root.destroy()
        except Exception:                                # noqa: BLE001
            pass

    # -- 循环 ---------------------------------------------------------------
    def _poll(self):
        try:
            self._drain_tray()
            if self.args.demo:
                now = time.time()
                if now - self.last_demo > 3.0:
                    self.last_demo = now
                    self.forced_state = STATE_ORDER[self.demo_i % len(STATE_ORDER)]
                    self.demo_i += 1
                self.snap = self._demo_snapshot(self.forced_state)
            elif self.args.force_state:
                self.snap = self._demo_snapshot(self.args.force_state)
            elif self.watcher:
                # 全局鼠标位置：她要看的是整个桌面上的鼠标，不只是窗口内的
                cx, cy = cursor_pos()
                if (cx, cy) != self._cursor_prev:
                    self._cursor_prev = (cx, cy)
                    self._cursor_at = time.time()
                if self.companion is not None:
                    self.companion.mouse_x = cx
                    self.companion.mouse_at = self._cursor_at
                    self.companion.pet_x = self.root.winfo_x()

                snap = self.watcher.poll()
                if self.watcher._new_completion:          # 有任务刚跑完
                    self.watcher._new_completion = False
                    self._play_sound()
                # DSH 那边没活干（空闲或压根没会话）→ 交给陪伴模式，她自己过日子。
                # 有活在跑时反过来把她叫醒，免得 DSH 都开工了她还在睡。
                if snap.state in ("idle", "offline") and self.companion is not None:
                    snap = self.companion.poll()
                    self.in_companion = True
                else:
                    self.in_companion = False
                    if self.companion is not None:
                        self.companion.wake()
                self.snap = snap
                # 番茄钟 / 专注模式 / 等待提醒
                self._pomodoro_tick(time.time())
                if self.focus_mode:
                    self.snap.bubble = False                 # 专注时不冒泡
                    if self.companion is not None:
                        self.companion.bubble_until = 0.0
                if self.snap.state == "waiting" and not self.focus_mode:
                    self._attention()                        # 卡在你这儿就走到屏幕中间
                # 溜达：把窗口朝目标位置挪一点。拖动中不抢方向盘。
                if (self.in_companion and self.snap.wander and self._drag is None
                        and getattr(self, "_drop_job", None) is None):
                    target = int(self.snap.wander)
                    wx = self.root.winfo_x()
                    if abs(wx - target) > WANDER_STEP:
                        wx += WANDER_STEP if target > wx else -WANDER_STEP
                        self.root.geometry(f"+{wx}+{self.root.winfo_y()}")
        except Exception as exc:                         # noqa: BLE001
            print(f"[warn] poll: {exc}", file=sys.stderr)
        self.root.after(POLL_MS, self._poll)

    def _demo_snapshot(self, state: str) -> Snapshot:
        import random
        base = dict(
            state=state, session_id="demo-0000-0000", title="生成一个桌面宠物",
            provider="opencode-go", model="deepseek-v4.1-flash",
            turn=38, step=8, context_pct=13.6, surface_tokens=136054,
            context_window=1000000, tokens_out=70695,
            prompt="你帮我生成一个桌面宠物，宠物的状态是显示你的任务运行状态",
        )
        if state == "tools":
            base.update(started_ms=time.time() * 1000 - 3200, tool_names=["pwsh"],
                        detail_line="1 个工具调用进行中")
        elif state == "thinking":
            base.update(started_ms=time.time() * 1000 - 1400, detail_line=base["prompt"][:44])
        elif state == "idle":
            base.update(idle_hint="空闲", detail_line="生成一个桌面宠物")
        else:
            base.update(detail_line="等待你的下一条指令")
        snap = Snapshot(**base)
        snap.phase = random.random()
        return snap

    def _tick(self):
        try:
            st = self.snap.state
            self._ensure_state(st)                       # 用到才渲染（见 _ensure_state）
            frames = self.sprites.get(st) or self.sprites.get("idle") or []
            durs = self.durations.get(st) or [TICK_MS]
            if not frames:
                self.root.after(TICK_MS, self._tick)
                return

            # 逐帧时长不同（精灵图契约给了每帧毫秒），所以按时间选帧而不是按计数
            now = time.time()
            # 交互反应优先于任务状态：拖动中显示拖动姿态，刚被戳则播一次反应。
            # 反应素材皮肤没提供就自动跳过，不影响原来的动画。
            react = "drag" if self._drag else ("poke" if now < self.poke_until else None)
            if react and self.reaction_sprites.get(react):
                frames = self.reaction_sprites[react]
                durs = self.reaction_durs[react]
                loop = sum(durs) or TICK_MS
                if react == "poke":
                    # 一次性：按「剩余时间」倒推播放位置，播完自然回落到状态动画
                    t = max(0.0, min(loop - (self.poke_until - now) * 1000, loop - 1))
                else:
                    t = (now * 1000) % loop          # 拖动姿态循环播
            else:
                loop = self._loop_ms.get(st) or sum(durs) or TICK_MS
                t = (now * 1000) % loop

            acc, idx = 0.0, 0
            for i, dur in enumerate(durs):
                acc += dur
                if t < acc:
                    idx = i
                    break
            else:
                idx = len(frames) - 1
            sprite = frames[idx]
            self.snap.phase = t / loop
            self.frame_i += 1

            # 交互装饰画在所有皮肤之上 —— 契约/clawd 的帧是预渲染的，
            # 画在 render_pet 里就只有内置皮肤能用了。
            ov_kind = self._overlay_kind(now)
            if ov_kind:
                ov = overlay_for(ov_kind, self.snap.phase)
                if ov is not None:
                    sprite = Image.alpha_composite(sprite, ov)
            if self.snap.mirror:                         # 看向左边：水平翻转
                sprite = ImageOps.mirror(sprite)
            dots = session_dots(self.snap.tasks)         # 多任务时脚边排一排圆点
            if dots is not None:
                sprite = Image.alpha_composite(sprite, dots)

            card_key = (st, self.snap.detail_line, self.snap.idle_hint, self.detail,
                        self.privacy,
                        self.snap.tool_names and tuple(self.snap.tool_names),
                        int((self.snap.elapsed_ms or 0) / 100),
                        tuple((t_.session_id, t_.state, t_.short_state,
                               int((t_.elapsed_ms or 0) / 1000)) for t_ in self.snap.tasks))
            if self._card_cache[0] != card_key:
                self._card_cache = (card_key,
                                    render_card(self.snap, self.detail, self.privacy))
            card = self._card_cache[1]

            canvas = Image.new("RGBA", (WIN_W, WIN_H), (0, 0, 0, 0))
            canvas.alpha_composite(sprite, (0, 0))
            canvas.alpha_composite(card, (0, PET_AREA_H))
            if self.scale != 1.0:                        # 滚轮缩放：整幅一起缩
                canvas = canvas.resize((self.win_w, self.win_h), Image.LANCZOS)
            self.display.push(canvas)
            self._tray_sync()
        except Exception as exc:                         # noqa: BLE001
            print(f"[warn] tick: {exc}", file=sys.stderr)
        # 刷新间隔跟着当前皮肤的帧时长走：帧短就刷快点（whale-chan 是 17ms/帧），
        # 帧长就慢下来省 CPU（内置皮肤是 95ms）。范围夹在 33~100ms。
        try:
            step = min(self.durations.get(self.snap.state) or [TICK_MS])
        except Exception:                                # noqa: BLE001
            step = TICK_MS
        self.root.after(int(max(33, min(step, 100))), self._tick)

    def run(self):
        self.root.mainloop()


def demo_tasks() -> list:
    """用于预览与回归检查的多任务样例。"""
    now = time.time() * 1000
    return [
        TaskInfo(session_id="a1", state="tools", title="电商商品复制软件",
                 tool_names=["pwsh"], started_ms=now - 8100),
        TaskInfo(session_id="b2", state="thinking", title="介绍一下你",
                 started_ms=now - 4200),
        TaskInfo(session_id="c3", state="thinking", title="网站视觉分析",
                 started_ms=now - 900),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────

def do_shot(prefix: str, skin_id: str = "dafeiyu", privacy: bool = False) -> None:
    """不建窗口，把各状态渲染成 PNG，便于预览与回归检查。"""
    skin = load_skin(skin_id)
    print(f"皮肤: {skin.display}")

    def compose(st: str, snap: "Snapshot", detail: bool) -> Image.Image:
        canvas = Image.new("RGBA", (WIN_W, WIN_H), (0, 0, 0, 0))
        items = skin.frames(st)
        if items:
            canvas.alpha_composite(items[0][0], (0, 0))
        canvas.alpha_composite(render_card(snap, detail, privacy), (0, PET_AREA_H))
        return canvas

    for st in STATE_ORDER:
        snap = Snapshot(state=st, title="生成一个桌面宠物",
                        provider="opencode-go", model="deepseek-v4.1-flash",
                        turn=38, step=8, context_pct=13.6, surface_tokens=136054,
                        context_window=1000000, tokens_out=70695,
                        prompt="你帮我生成一个桌面宠物，宠物的状态是显示你的任务运行状态")
        if st == "tools":
            snap.tool_names = ["pwsh"]
            snap.detail_line = "1 个工具调用进行中"
            snap.started_ms = time.time() * 1000 - 3200
        elif st == "thinking":
            snap.detail_line = snap.prompt[:44]
            snap.started_ms = time.time() * 1000 - 1400
        elif st == "idle":
            snap.idle_hint, snap.detail_line = "空闲", "生成一个桌面宠物"
        elif st == "celebrating":
            snap.detail_line = "任务已完成"
            snap.started_ms = time.time() * 1000 - 2000
        elif st == "waiting":
            snap.detail_line = "在等你回答"
            snap.started_ms = time.time() * 1000 - 5100
        elif st == "failed":
            snap.detail_line = "请求出错，正在重试"
            snap.started_ms = time.time() * 1000 - 2400
        else:
            snap.detail_line = "等待你的下一条指令"
        compose(st, snap, False).save(f"{prefix}-{st}.png")
        compose(st, snap, True).save(f"{prefix}-{st}-detail.png")
        print(f"  {st:12s} -> {prefix}-{st}.png")

    multi = Snapshot(state="tools", tasks=demo_tasks(), title="电商商品复制软件",
                     provider="opencode-go", model="deepseek-v4.1-flash",
                     turn=161, step=4, context_pct=13.6, surface_tokens=136054,
                     context_window=1000000, tokens_out=70695, tool_names=["pwsh"],
                     started_ms=time.time() * 1000 - 8100)
    compose("tools", multi, False).save(f"{prefix}-multi.png")
    compose("tools", multi, True).save(f"{prefix}-multi-detail.png")
    print(f"  multi        -> {prefix}-multi.png  ({len(multi.tasks)} 个任务)")


def main() -> int:
    ap = argparse.ArgumentParser(description="DSH 桌面宠物")
    ap.add_argument("--demo", action="store_true", help="每 3 秒轮换状态预览")
    ap.add_argument("--force-state", choices=STATE_ORDER, help="固定显示某个状态")
    ap.add_argument("--session", help="固定监视指定会话 ID")
    ap.add_argument("--shot", metavar="PREFIX", help="渲染各状态到 PNG 后退出")
    ap.add_argument("--skin", default=None, help="指定皮肤 id（默认用配置里的）")
    ap.add_argument("--list-skins", action="store_true", help="列出可用皮肤后退出")
    ap.add_argument("--privacy", action="store_true", help="隐私模式渲染")
    ap.add_argument("--no-companion", action="store_true",
                    help="关掉陪伴模式（DSH 没任务时就显示空闲，不自己过日子）")
    ap.add_argument("--no-hotkeys", action="store_true",
                    help="不注册全局热键（Ctrl+Alt+P 显示隐藏 / +F 喂食 / +S 设置）")
    args = ap.parse_args()

    if args.list_skins:
        for s in discover_skins():
            print(f"  {s['id']:<16} {s['display']:<24} {s['kind']}")
        return 0

    if args.shot:
        do_shot(args.shot, args.skin or "dafeiyu", args.privacy)
        return 0

    if sys.platform != "win32":
        print("本程序仅支持 Windows（依赖分层窗口）。", file=sys.stderr)
        return 1

    # 单实例锁：绑一个本地端口，进程活着就占着，退出时系统自动释放。
    # 比 PID 文件干净 —— 崩溃也不会留下需要手动清的残留。
    # 插件、快捷方式、手动启动都走这里，所以不会叠出第二只桌宠。
    #
    # 注意：进程收到终止信号后，端口大约还要 2 秒才真正释放。这期间起来的
    # 新实例会 bind 失败 —— 所以这里必须【报错并非零退出】，不能静默 return 0，
    # 否则调用方（DSH 插件）分不清「已经在跑」和「起来又死了」。
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        lock.listen(1)
    except OSError as exc:
        print(f"桌宠启动失败：单实例锁端口 {SINGLE_INSTANCE_PORT} 被占用"
              f"（上一只可能还在退出中，等两秒再试）。{exc}", file=sys.stderr)
        return 2

    PetApp(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
