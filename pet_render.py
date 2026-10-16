"""桌宠的绘制：角色、叠加图形、皮肤、信息卡。"""

from __future__ import annotations

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
# `from x import *` 不带下划线开头的名字 —— 这些缓存和辅助函数必须显式引，
# 否则渲染时 NameError（踩过：_GLOW_CACHE / _rgba 缺失，所有状态都渲染不出来）。
from pet_base import _GLOW_CACHE, _VIEW_CACHE, _gear, _rgba

import pet_i18n

from pet_types import Snapshot, TaskInfo


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

    def __init__(self, path=None, name="dafeiyu", display=None, frames: int = 12):
        display = display or pet_i18n.t("大肥鱼（内置）")
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
    os.path.join(skins_dir(), "clawd"),
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
        # utf-8-sig：主题包作者在 Windows 上用记事本编辑过的话会带 BOM，
        # 用 utf-8 读会直接抛异常，整个主题就加载失败了
        with open(os.path.join(path, "theme.json"), encoding="utf-8-sig") as fh:
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
    out = [dict(id="dafeiyu", display=pet_i18n.t("大肥鱼（内置）"),
                kind="dafeiyu", path=None)]
    seen = {"dafeiyu"}
    # Codex 精灵图契约
    for root, tag in [(skins_dir(), ""),
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
                with open(p, encoding="utf-8-sig") as fh:
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
                log_error(f"皮肤 {skin_id} 加载失败，改用内置", exc)
                break
        if s["kind"] == "clawd":
            try:
                return ClawdThemeSkin(s["path"], s["id"], s["display"])
            except Exception as exc:                     # noqa: BLE001
                log_error(f"clawd 主题 {skin_id} 加载失败，改用内置",
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
        return pet_i18n.t("会话 ") + str(idx + 1) if privacy else task.label

    sub_text = GENERIC_DETAIL.get(snap.state, "") if privacy else snap.detail_line

    # ── 折叠态 & 多任务：标题 + 每个任务一行 ──
    if n_tasks > 1 and not detail:
        head = f"{n_tasks} " + pet_i18n.t("个任务进行中")
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
    # PALETTE 里的 label 是中文原文（它同时是翻译表的 key），显示前过一道 t()
    label = pet_i18n.t(pal["label"])
    if n_tasks > 1:
        label = f"{n_tasks} " + pet_i18n.t("个任务进行中")
    elif snap.state == "tools" and snap.tool_names:
        label = pet_i18n.t("执行 ") + str(snap.tool_names[0])
    elif snap.state == "idle" and snap.idle_hint:
        label = snap.idle_hint
    elif snap.state == "offline":
        label = pet_i18n.t("未发现 DSH 会话")

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
            d.text((x0 + 26 * s, row_y),
                   pet_i18n.t("… 另有 ") + f"{n_tasks - len(shown)} " + pet_i18n.t("个任务"),
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
        (pet_i18n.t("模型"),
         f"{snap.model}" + (f" · {snap.provider}" if snap.provider else "")),
        (pet_i18n.t("进度"),
         pet_i18n.t("第 ") + f"{snap.turn}" + pet_i18n.t(" 轮 · 第 ")
         + f"{snap.step}" + pet_i18n.t(" 步") if snap.turn else "—"),
        (pet_i18n.t("输出"),
         f"{snap.tokens_out/1000:.1f}K tokens · " + pet_i18n.t("上下文 ")
         + f"{snap.context_pct:.0f}%"),
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
        d.text((tx, row_y), pet_i18n.t("最慢"), font=f_small, fill=TEXT_DIM)
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
