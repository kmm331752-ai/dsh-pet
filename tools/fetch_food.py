"""从 FluentUI Emoji 拉食物素材并光栅化。

**为什么用 FluentUI Emoji**（microsoft/fluentui-emoji）：
  · **MIT 许可**，Copyright Microsoft —— 干净，没有 SA/NC 的传染性
  · 10168★，微软维护
  · 平涂风格，和桌宠的 Q 版美术能搭

对比过的其他候选：
  · OpenMoji —— CC-BY-SA-4.0，**SA 有传染性**，和本项目的 MIT 冲突（同理拒过鲸鱼娘）
  · Twemoji —— 代码 MIT 但**美术是 CC-BY 4.0**，要署名且改图有约束
  · Noto Emoji —— OFL-1.1，可用，但风格偏写实

流程：gh api 拉 SVG -> svglib 转 PDF -> pypdfium2 光栅化成 PNG。
（项目里 clawd 主题已经走这条路，依赖都在。）
"""

import io
import os
import subprocess
import sys
import tempfile

ROOT = r"G:\skill\DeepSeekWork\desktop-pet"
OUT_DIR = os.path.join(ROOT, "assets", "food")
GH = r"C:\Program Files\GitHub CLI\gh.exe"
REPO = "microsoft/fluentui-emoji"

# 挑的原则：
#   · 一眼认得出是什么（缩到 40px 还得认得出）
#   · 覆盖不同"效果类型"（甜点 / 正餐 / 饮料 / 零食）
#   · 鲸鱼娘能吃鱼 —— 这个梗值得留
FOODS = {
    "dango":      "Dango",
    "cookie":     "Cookie",
    "shortcake":  "Shortcake",
    "ramen":      "Steaming bowl",
    "riceball":   "Rice ball",
    "candy":      "Candy",
    "bubbletea":  "Bubble tea",
    "fish":       "Fish",
    "chocolate":  "Chocolate bar",
    "strawberry": "Strawberry",
    "onigiri":    "Cooked rice",
    "teacup":     "Teacup without handle",
}


def fetch_svg(name: str) -> bytes:
    """拉一个 emoji 的彩色 SVG。

    文件名规则：目录名保持原样（含空格），文件名是**下划线**连接的小写。
    「Steaming bowl」-> `steaming_bowl_color.svg`。
    （第一版用空格，多词的六个全 404。）
    """
    fname = name.lower().replace(" ", "_") + "_color.svg"
    path = f"assets/{name}/Color/{fname}"
    url = f"repos/{REPO}/contents/{path.replace(' ', '%20')}"
    out = subprocess.run(
        [GH, "api", url, "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"拉不到 {path}: {out.stderr.decode('utf-8', 'replace')[:120]}")
    return out.stdout


def rasterize(svg_bytes: bytes, size: int = 128) -> bytes:
    """SVG -> PNG。走 svglib -> PDF -> pypdfium2（和 clawd 主题同一条路）。"""
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
    import pypdfium2 as pdfium
    from PIL import Image

    drawing = svg2rlg(io.BytesIO(svg_bytes))
    if drawing is None:
        raise RuntimeError("svglib 解析不出这个 SVG")
    # 缩放到目标尺寸
    scale = size / max(drawing.width, drawing.height)
    drawing.scale(scale, scale)
    drawing.width *= scale
    drawing.height *= scale

    pdf_buf = io.BytesIO()
    renderPDF.drawToFile(drawing, pdf_buf)
    pdf_buf.seek(0)

    pdf = pdfium.PdfDocument(pdf_buf)
    page = pdf[0]
    bitmap = page.render(scale=2)          # 2x 再缩，边缘干净些
    img = bitmap.to_pil().convert("RGBA")
    img = img.resize((size, size), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    ok = failed = 0
    for slug, name in FOODS.items():
        dest = os.path.join(OUT_DIR, f"{slug}.png")
        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            print(f"  —  {slug:<12} 已有，跳过")
            ok += 1
            continue
        try:
            svg = fetch_svg(name)
            png = rasterize(svg)
            with open(dest, "wb") as fh:
                fh.write(png)
            print(f"  ✅ {slug:<12} <- {name:<24} {len(png) // 1024} KB")
            ok += 1
        except Exception as exc:                         # noqa: BLE001
            print(f"  ❌ {slug:<12} <- {name:<24} {type(exc).__name__}: {str(exc)[:80]}")
            failed += 1
    print(f"\n  成功 {ok} / 失败 {failed}  ->  {OUT_DIR}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
