"""把她的动起来的样子做成 GIF —— 静态图表现不出桌宠的价值。

## 为什么

README 有 805 行、32 张预览图，**全是静态 PNG**。而一个桌宠的全部价值
就是"会动、会反应"：16 方向转头、被戳的反应、溜达、打盹、睡着、醒来、
音符飘 —— **静态图一张都表现不出来**。

仓库现在 1 star、25 个版本总下载 9 次。**瓶颈不是功能**（功能已经很多了），
是**没人知道它长什么样**。

## 做哪几段

按"最能在三秒内说清这是什么"排序：

  1. **16 方向转头** —— 最抓眼的一条，别人家桌宠没有
  2. **被戳的反应** —— 有反馈才叫"活的"
  3. **过自己的日子** —— 溜达 → 打盹 → 睡着 → 醒来
  4. **在听歌** —— 音符飘

帧**全部从真渲染拿**（`skin.frames()` / `reaction_frames()` /
`overlay_for()`），不是画的示意图。
"""

from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

import pet_render as R  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "preview")
BG = (26, 29, 38)

# GIF 的调色板很有限，先降到 128 色并抖动，不然渐变会出色带
GIF_COLORS = 128


def _font():
    """中文字体。**PIL 默认的位图字体渲染不了中文** —— 直接画出来是
    一串方块/问号（第一版就是这样）。"""
    from PIL import ImageFont
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
        try:
            return ImageFont.truetype(name, 13)
        except OSError:
            continue
    return ImageFont.load_default()


_FONT = None


def _label(img: Image.Image, text: str) -> Image.Image:
    """在图下面加一行说明。GIF 里每一段要让人知道在看什么。"""
    global _FONT
    if _FONT is None:
        _FONT = _font()
    out = Image.new("RGB", (img.width, img.height + 24), BG)
    out.paste(img.convert("RGB"), (0, 0))
    d = ImageDraw.Draw(out)
    d.text((6, img.height + 6), text, fill=(190, 200, 215), font=_FONT)
    return out


def _frames(seq, repeat=2):
    """把 (图, 毫秒) 序列展开成 GIF 帧列表，顺便统一尺寸。"""
    out = []
    for im, ms in seq:
        for _ in range(max(1, repeat)):
            out.append((im.convert("RGB"), max(40, int(ms))))
    return out


def build_gaze(skin) -> list:
    """16 方向转头 —— 鼠标绕她转一圈。

    **这一条最抓眼**，而且别人家的桌宠基本没有（要用 v2 图集的
    第 9、10 行，绝大多数人不知道那两行是干什么的）。
    """
    frames = []
    # 绕一圈：0 → 15 → 0，像鼠标从左绕回左
    order = list(range(16)) + list(range(15, -1, -1))
    for i in order:
        f = skin.gaze_frame(i)
        if f is None:
            continue
        frames.append((_label(f, f"看向鼠标 · 方向 {i}/16"), 90))
    # 结尾停在中性姿势
    n = skin.neutral_frame()
    if n is not None:
        for _ in range(8):
            frames.append((_label(n, "鼠标停了 → 回中性"), 120))
    return frames


def build_poke(skin) -> list:
    """被戳一下 —— 有反馈才叫"活的"。"""
    frames = []
    for im, ms in skin.reaction_frames("poke"):
        frames.append((_label(im, "戳她一下"), ms))
    for im, ms in skin.frames("idle")[:4]:
        frames.append((_label(im, "……"), ms))
    return frames


def build_day(skin) -> list:
    """过自己的日子：溜达 → 打盹 → 睡着 → 醒来。"""
    frames = []
    plan = [("roaming", "自己去溜达", 2), ("yawning", "有点困了", 2),
            ("dozing", "打盹", 2), ("sleeping", "睡着了", 2),
            ("waking", "你一动她就醒", 2)]
    for state, text, n in plan:
        got = skin.frames(state)
        if not got:
            continue
        step = max(1, len(got) // n)
        for im, ms in got[::step][:n]:
            frames.append((_label(im, text), ms))
    return frames


def build_music(skin) -> list:
    """在听歌 —— 音符飘。装饰是程序化画的（overlay_for）。"""
    frames = []
    base = skin.frames("idle")
    if not base:
        return frames
    for k in range(12):
        im, _ms = base[k % len(base)]
        ov = R.overlay_for("music", k / 12.0)
        if ov is not None:
            im = Image.alpha_composite(im, ov)
        frames.append((_label(im, "在听歌"), 110))
    return frames


def main() -> int:
    skin = R.load_skin("openpets")
    print(f"  皮肤 {skin.display}")

    parts = [("gaze", build_gaze(skin)), ("poke", build_poke(skin)),
             ("day", build_day(skin)), ("music", build_music(skin))]
    for name, fr in parts:
        print(f"  {name:8} {len(fr):3} 帧")

    # ── 合起来的长版 ──
    allf = [f for _n, fr in parts for f in fr]
    w = max(f.width for f, _ in allf)
    h = max(f.height for f, _ in allf)
    norm = [(f if f.size == (w, h) else _pad(f, w, h), ms) for f, ms in allf]

    out = os.path.join(OUT, "demo.gif")
    _save(norm, out)
    print(f"  -> demo.gif  {len(norm)} 帧  {w}x{h}  "
          f"{os.path.getsize(out) / 1024:.0f} KB")

    # ── 单独一条最抓眼的（README 顶上用）──
    g = parts[0][1]
    gw = max(f.width for f, _ in g)
    gh = max(f.height for f, _ in g)
    gn = [(f if f.size == (gw, gh) else _pad(f, gw, gh), ms) for f, ms in g]
    out2 = os.path.join(OUT, "demo-gaze.gif")
    _save(gn, out2)
    print(f"  -> demo-gaze.gif  {len(gn)} 帧  {gw}x{gh}  "
          f"{os.path.getsize(out2) / 1024:.0f} KB")
    return 0


def _pad(img: Image.Image, w: int, h: int) -> Image.Image:
    out = Image.new("RGB", (w, h), BG)
    out.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
    return out


def _save(frames, path: str) -> None:
    imgs = [f for f, _ in frames]
    durs = [ms for _, ms in frames]
    # 用**第一帧**当调色板基准再统一量化 —— 不这么做的话 GIF 每帧各用各的
    # 调色板，背景色会一闪一闪。
    pal = imgs[0].quantize(colors=GIF_COLORS, method=Image.MEDIANCUT)
    q = [im.quantize(palette=pal, dither=Image.FLOYDSTEINBERG) for im in imgs]
    q[0].save(path, save_all=True, append_images=q[1:], duration=durs,
              loop=0, optimize=True, disposal=2)


if __name__ == "__main__":
    sys.exit(main())
