"""把桌宠打包成单文件 exe。

    python build_exe.py

产物在 dist/DSH-Pet.exe，双击就能跑，不需要装 Python。

为什么要先 `git archive` 导出：仓库工作区里可能有**没有提交**的素材
（比如本地自用的 clawd 主题，因为授权原因被 .gitignore 排除了）。
直接用工作区打包会把它们塞进 exe，等于分发受限素材。
所以这里只拿**已提交**的文件来构建，保证产物和仓库内容一致。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
STAGE = os.path.join(tempfile.gettempdir(), "dsh-pet-build")


def run(cmd: list, **kw) -> None:
    print("  $ " + " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, **kw)


def export_committed(dst: str) -> None:
    """把 HEAD 里已提交的文件导出到 dst（等价于干净 clone 的内容）。"""
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    archive = os.path.join(tempfile.gettempdir(), "dsh-pet-src.tar")
    run(["git", "-C", ROOT, "archive", "-o", archive, "HEAD"])
    run(["tar", "-xf", archive, "-C", dst])
    os.remove(archive)


def make_icon(stage: str) -> str | None:
    """icon.png -> icon.ico（PyInstaller 在 Windows 上只认 .ico）。"""
    png = os.path.join(stage, "assets", "icon.png")
    if not os.path.exists(png):
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    ico = os.path.join(stage, "assets", "icon.ico")
    img = Image.open(png).convert("RGBA")
    img.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"  图标: {ico}")
    return ico


def main() -> int:
    print("[1/4] 从 git 导出已提交的文件")
    export_committed(STAGE)
    n = sum(len(f) for _, _, f in os.walk(STAGE))
    print(f"      {n} 个文件 -> {STAGE}")

    print("[2/4] 准备图标")
    icon = make_icon(STAGE)

    print("[3/4] 打包（首次要几分钟）")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",                      # 单文件
        "--windowed",                     # 不弹控制台
        "--name", "DSH-Pet",
        "--add-data", f"{os.path.join(STAGE, 'assets')}{os.pathsep}assets",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(STAGE, "_build"),
        "--specpath", STAGE,
    ]
    if icon:
        cmd += ["--icon", icon]
    cmd.append(os.path.join(STAGE, "dsh_pet.py"))
    run(cmd, cwd=STAGE)

    print("[4/4] 完成")
    exe = os.path.join(ROOT, "dist", "DSH-Pet.exe")
    if os.path.exists(exe):
        print(f"      {exe}  ({os.path.getsize(exe) / 1048576:.1f} MB)")
        print("      双击即可运行；配置和相处记录会写在 exe 旁边。")
    else:
        print("      ❌ 没找到产物")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
