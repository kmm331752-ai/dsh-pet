"""配饰：帽子、围巾、蝴蝶结这些独立图层。

为什么用程序画而不是图片素材：
  · 不增加仓库和 exe 体积（现在 51MB 已经不小了）
  · 自动跟着角色缩放走 —— 图片素材换皮肤/改缩放就得重做一套
  · 可以按状态微调（睡觉时帽子歪一点）

代价是只能做简单形状。想加复杂配饰还是得走皮肤包那条路。

坐标系：所有绘制都拿到角色的 (cx, body_top, body_h, s)，自己算位置，
不要去猜 WIN_W/PET_AREA_H —— 那样换皮肤尺寸变了就错位。
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from pet_base import INK, SS


def _rgba(color, alpha: int = 255):
    from pet_base import _rgba as f
    return f(color, alpha)


# ── 各个配饰 ────────────────────────────────────────────────────────────────
# 约定：函数签名都是 (d, cx, top, bh, s, phase)
#   d      ImageDraw
#   cx     角色水平中心
#   top    角色**本体**头顶 y（由 character_box 量出来，不是 getbbox）
#   bh     角色本体高度
#   s      缩放系数（帧已是最终分辨率时传 1.0）
#   phase  0~1 的动画相位（做轻微摆动）
#
# 比例全部相对 bh，而且刻意画得小：
# 画布只有 182px 高，角色头顶到画布顶只剩 26px，配饰太高就会被切掉
# （第一版帽子用了 bh*0.34 ≈ 52px，直接顶出画面）。

def _hat(d, cx, top, bh, s, phase):
    """尖顶小帽。往一侧歪一点，比正着戴自然。"""
    w = bh * 0.175
    h = bh * 0.115
    lean = math.sin(phase * 2 * math.pi) * 1.2 * s
    tip = (cx + w * 0.30 + lean, top - h)
    left = (cx - w * 0.52, top + bh * 0.015)
    right = (cx + w * 0.52, top + bh * 0.015)
    d.polygon([tip, right, left], fill=(214, 84, 92, 250), outline=INK)
    d.rounded_rectangle(
        [cx - w * 0.66, top - bh * 0.004, cx + w * 0.66, top + bh * 0.040],
        radius=2 * s, fill=(180, 62, 72, 250), outline=INK)
    r = 2.6 * s
    d.ellipse([tip[0] - r, tip[1] - r, tip[0] + r, tip[1] + r],
              fill=(246, 226, 160, 255), outline=INK)


def _cap(d, cx, top, bh, s, phase):
    """鸭舌帽。"""
    w = bh * 0.21
    d.rounded_rectangle(
        [cx - w * 0.5, top - bh * 0.085, cx + w * 0.5, top + bh * 0.045],
        radius=4 * s, fill=(64, 108, 176, 250), outline=INK)
    # 帽舌朝右（角色多数时候朝右走）
    d.polygon([(cx + w * 0.30, top - bh * 0.012),
               (cx + w * 0.92, top + bh * 0.012),
               (cx + w * 0.88, top + bh * 0.048),
               (cx + w * 0.28, top + bh * 0.038)],
              fill=(48, 86, 148, 250), outline=INK)


def _crown(d, cx, top, bh, s, phase):
    """小皇冠。"""
    w = bh * 0.155
    base_y = top + bh * 0.018
    pts = [(cx - w, base_y), (cx + w, base_y)]
    for i in range(3):
        x = cx - w + (2 * w) * (i / 2)
        pts.append((x, base_y - bh * 0.105))
        pts.append((x + w * 0.34, base_y - bh * 0.035))
    d.polygon(pts, fill=(238, 198, 74, 252), outline=INK)
    for i in range(3):
        x = cx - w + (2 * w) * (i / 2)
        r = 1.9 * s
        d.ellipse([x - r, base_y - bh * 0.105 - r, x + r, base_y - bh * 0.105 + r],
                  fill=(250, 240, 190, 255), outline=INK)


def _scarf(d, cx, top, bh, s, phase):
    """围巾：脖子上绕一圈 + 一条垂下来的尾巴（会飘）。

    脖子位置 = 头顶往下 0.47 个身高 —— 实测内置角色最窄处（脖子）
    在 y≈100，头顶 26、身高 153，(100-26)/153 = 0.48。
    第一版写的 0.50 配错的身高，结果横在脸上。
    """
    y = top + bh * 0.50
    w = bh * 0.20
    d.rounded_rectangle([cx - w, y, cx + w, y + bh * 0.060],
                        radius=4 * s, fill=(206, 88, 76, 250), outline=INK)
    sway = math.sin(phase * 2 * math.pi) * 4 * s
    tail_x = cx + w * 0.50
    d.polygon([(tail_x, y + bh * 0.02),
               (tail_x + w * 0.34 + sway, y + bh * 0.24),
               (tail_x + w * 0.14 + sway, y + bh * 0.26),
               (tail_x - w * 0.08, y + bh * 0.04)],
              fill=(188, 72, 62, 250), outline=INK)


def _bow(d, cx, top, bh, s, phase):
    """蝴蝶结，戴在头顶偏一侧。"""
    bx = cx + bh * 0.085
    by = top - bh * 0.006
    w = bh * 0.055
    h = bh * 0.038
    d.polygon([(bx, by), (bx - w, by - h), (bx - w, by + h)],
              fill=(238, 122, 158, 252), outline=INK)
    d.polygon([(bx, by), (bx + w, by - h), (bx + w, by + h)],
              fill=(238, 122, 158, 252), outline=INK)
    r = 2.2 * s
    d.ellipse([bx - r, by - r, bx + r, by + r], fill=(250, 176, 200, 255), outline=INK)


def _glasses(d, cx, top, bh, s, phase):
    """圆框眼镜。眼睛大约在头顶往下 0.32 个身高。"""
    y = top + bh * 0.37
    r = bh * 0.050
    gap = bh * 0.062
    for sx in (-1, 1):
        ex = cx + sx * gap
        d.ellipse([ex - r, y - r, ex + r, y + r],
                  fill=(150, 200, 240, 80), outline=INK,
                  width=max(1, int(1.4 * s)))
    d.line([(cx - gap + r, y), (cx + gap - r, y)],
           fill=INK, width=max(1, int(1.4 * s)))


def _headphones(d, cx, top, bh, s, phase):
    """头戴耳机。横梁跨过头顶，两个耳罩在头两侧。"""
    y = top + bh * 0.25                       # 头部中心（实测 top+0.24）
    r = bh * 0.19
    d.arc([cx - r, y - r, cx + r, y + r], start=185, end=355,
          fill=(72, 78, 92, 250), width=max(2, int(3.6 * s)))
    for sx in (-1, 1):
        ex = cx + sx * r * 0.99
        d.rounded_rectangle([ex - 5 * s, y - 8 * s, ex + 5 * s, y + 8 * s],
                            radius=4 * s, fill=(56, 62, 76, 252), outline=INK)


# id -> (显示名, 绘制函数)
ACCESSORIES = {
    "none": ("不戴", None),
    "hat": ("尖顶帽", _hat),
    "cap": ("鸭舌帽", _cap),
    "crown": ("小皇冠", _crown),
    "scarf": ("围巾", _scarf),
    "bow": ("蝴蝶结", _bow),
    "glasses": ("眼镜", _glasses),
    "headphones": ("耳机", _headphones),
}

DEFAULT_ACCESSORY = "none"

# 判定「角色本体」的 alpha 阈值。
# getbbox() 会把半透明的光晕和落影算进去 —— 实测光晕比头顶高 10px，
# 照它定位的话所有配饰都会飘在头上（第一版就是这样，预览图一眼看出）。
BODY_ALPHA = 200


def character_box(frame: Image.Image):
    """量出角色本体的 (x0, y0, x1, y1)，不含光晕/落影。

    用 alpha 阈值卡出来，比 getbbox() 准。不用 numpy —— 它是可选依赖，
    而这里只是取个包围盒，PIL 的 point + getbbox 足够快。
    """
    try:
        alpha = frame.getchannel("A").point(
            lambda v: 255 if v > BODY_ALPHA else 0)
        box = alpha.getbbox()
        if box:
            return box
    except Exception:                                    # noqa: BLE001
        pass
    return frame.getbbox()


def accessory_ids() -> list:
    return list(ACCESSORIES.keys())


def accessory_label(aid: str) -> str:
    return ACCESSORIES.get(aid, ACCESSORIES[DEFAULT_ACCESSORY])[0]


def draw_accessory(img: Image.Image, aid: str, cx: float, top: float,
                   bh: float, s: float = SS, phase: float = 0.0) -> bool:
    """把一个配饰画到 img 上。返回是否真的画了。

    坐标全部相对角色算，所以换皮肤、改缩放都不会错位。
    """
    entry = ACCESSORIES.get(aid)
    if not entry or entry[1] is None:
        return False
    try:
        d = ImageDraw.Draw(img)
        entry[1](d, cx, top, bh, s, phase)
        return True
    except Exception:                                    # noqa: BLE001
        # 配饰画不出来不该让整帧渲染失败 —— 顶多是这次没戴
        return False
