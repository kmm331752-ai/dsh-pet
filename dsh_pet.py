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
import glob
import json
import math
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from ctypes import wintypes
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ─────────────────────────────────────────────────────────────────────────────
# 基本配置
# ─────────────────────────────────────────────────────────────────────────────

DSH_HOME = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
PROJ_DIR = os.path.join(DSH_HOME, "storages", "session_projcache", "sessions")
SESS_DIR = os.path.join(DSH_HOME, "sessions")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pet_config.json")
# 单实例锁用的本地端口（只绑 127.0.0.1，不对外）
SINGLE_INSTANCE_PORT = 51937

WIN_W = 248                      # 窗口逻辑宽度
PET_AREA_H = 182                 # 角色区高度
DETAIL_CARD_H = 148              # 详情展开时的信息卡高度
WIN_H = PET_AREA_H + DETAIL_CARD_H
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

STATE_ORDER = ["idle", "thinking", "tools", "waiting", "failed", "celebrating", "offline"]

# 状态优先级：越小越优先成为「聚合状态」——
# 等你回答最要紧（卡在你这），其次出错，再是干活
STATE_RANK = {"waiting": 0, "failed": 1, "tools": 2, "thinking": 3, "celebrating": 4, "idle": 5}
ACTIVE_STATES = ("waiting", "failed", "tools", "thinking")

CELEBRATE_S = 6.0                # 完成庆祝持续秒数

# 角色素材来自 1190fasheqi/dafeiyu-pet（MIT），见 assets/ATTRIBUTION.md
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
SKINS_DIR = os.path.join(ASSETS, "skins")
VIEW_FOR_STATE = {"idle": "front", "thinking": "front", "tools": "side",
                  "waiting": "front", "failed": "front",
                  "celebrating": "front", "offline": "back"}
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

# 状态 -> 图集行
ATLAS_ROW = {"idle": 0, "tools": 7, "thinking": 8, "waiting": 6,
             "failed": 5, "celebrating": 4, "offline": 0}
# 行的 (中间帧, 末帧) 毫秒，取自契约的逐帧时长
ATLAS_TIMING = {
    0: (120, 320), 3: (140, 280), 4: (140, 280),
    5: (140, 240), 6: (150, 260), 7: (120, 220), 8: (150, 280),
}


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
        return [(self._frame(state, r, i), last if k == len(seq) - 1 else mid)
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
            d.text((tx, row_y), _ellipsis(d, sub_text, f_sub, x1 - tx - 12 * s),
                   font=f_sub, fill=TEXT_DIM)
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
                    if len(self.calls) > 800:
                        for k in list(self.calls)[:400]:
                            self.calls.pop(k, None)

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
        if with_tools and info["pending_ids"]:
            tail = self._tail(info["session_id"])
            tail.poll()
            for cid in info["pending_ids"]:
                nm = tail.calls.get(cid)
                if nm and nm not in names:
                    names.append(nm)
            if not names and tail.last_tool:
                names = [tail.last_tool]
        return TaskInfo(
            session_id=info["session_id"], state=info["state"], title=info["title"],
            provider=info["provider"], model=info["model"], turn=info["turn"],
            step=info["step"], started_ms=info["started_ms"], tool_names=names,
            prompt=info["prompt"], context_pct=info["context_pct"],
            surface_tokens=info["surface_tokens"], context_window=info["context_window"],
            tokens_out=info["tokens_out"], mtime=info["mtime"], goal=info["goal"],
            queued=info["queued"], plan_active=info["plan_active"],
            turn_open=info["turn_open"])

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
            tokens_out=primary.tokens_out, prompt=primary.prompt)

        if state == "waiting":
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

        x = int(self.cfg.get("x", self.root.winfo_screenwidth() - WIN_W - 40))
        y = int(self.cfg.get("y", self.root.winfo_screenheight() - WIN_H - 90))
        self.root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

        self.display, self.backend = make_display(self.root, WIN_W, WIN_H)

        self.watcher = None if (args.demo or args.force_state) else \
            StatusWatcher(pinned=args.session)

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
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
        except Exception:                                # noqa: BLE001
            pass

    # -- 精灵缓存 -----------------------------------------------------------
    def _build_sprites(self) -> None:
        """把每个状态的帧与时长预渲染好；动画只做合成与推送。"""
        self.sprites: dict = {}
        self.durations: dict = {}
        total = 0.0
        for st in STATE_ORDER:
            items = self.skin.frames(st)
            self.sprites[st] = [im for im, _ in items]
            self.durations[st] = [d for _, d in items]
            total += sum(self.durations[st])
        self._loop_ms = {st: sum(self.durations[st]) for st in STATE_ORDER}
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
        print(f"[skin] {self.skin.display}: "
              f"{sum(len(v) for v in self.sprites.values())} 帧，共 {total:.0f}ms"
              f"；交互反应 {', '.join(have) if have else '无'}")

    # -- 事件 ---------------------------------------------------------------
    def _bind_events(self) -> None:
        r = self.root
        r.bind("<ButtonPress-1>", self._on_press)
        r.bind("<B1-Motion>", self._on_drag)
        r.bind("<ButtonRelease-1>", self._on_release)
        r.bind("<Button-3>", self._on_menu)

        self.menu = self.tk.Menu(r, tearoff=0)
        self.menu.add_command(label="展开/收起详情", command=self._toggle_detail)
        self.menu.add_separator()
        self.menu.add_command(label="自动跟随活跃会话", command=self._follow_auto)
        self.menu.add_command(label="复制会话 ID", command=self._copy_sid)
        self.menu.add_separator()
        self.var_privacy = self.tk.BooleanVar(value=self.privacy)
        self.var_sound = self.tk.BooleanVar(value=self.sound)
        self.menu.add_checkbutton(label="隐私模式（隐藏标题与提示词）",
                                  variable=self.var_privacy, command=self._toggle_privacy)
        self.menu.add_checkbutton(label="任务完成时提示音",
                                  variable=self.var_sound, command=self._toggle_sound)
        self.menu.add_separator()
        self._skin_menu = self.tk.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label="皮肤", menu=self._skin_menu)
        self._rebuild_skin_menu()
        self.menu.add_separator()
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

    # -- 托盘图标 -----------------------------------------------------------
    def _start_tray(self) -> None:
        self.tray = None
        self._tray_key = None
        self._hidden = False
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
            elif act == "quit":
                self._quit()

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

    def _on_press(self, e):
        self._drag = (e.x_root, e.y_root,
                      self.root.winfo_x(), self.root.winfo_y())
        self._moved = False

    def _on_drag(self, e):
        if not self._drag:
            return
        sx, sy, wx, wy = self._drag
        dx, dy = e.x_root - sx, e.y_root - sy
        if abs(dx) > 3 or abs(dy) > 3:
            self._moved = True
        self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def _on_release(self, e):
        self._drag = None
        if not self._moved:
            # 单击 = 戳一下：播一次反应动画（皮肤没提供就只切详情卡）
            durs = self.reaction_durs.get("poke") or []
            if durs:
                self.poke_until = time.time() + sum(durs) / 1000.0
            self._toggle_detail()
        self._save_config()

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
                self.snap = self.watcher.poll()
                if self.watcher._new_completion:          # 有任务刚跑完
                    self.watcher._new_completion = False
                    self._play_sound()
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
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        lock.listen(1)
    except OSError:
        print("桌宠已在运行，本次启动忽略。")
        return 0

    PetApp(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
