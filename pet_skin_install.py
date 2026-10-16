"""皮肤安装：从 URL 或本地压缩包装一套皮肤。

设计上只做一件事：把压缩包安全地放进 assets/skins/<id>/，然后交给
discover_skins() 去识别。识别逻辑不在这里重复 —— 装完重新扫一遍就知道
成没成，成不了就回滚删掉，绝不留半个坏皮肤让桌宠加载时崩。

安全上要挡住三件事：
  · 路径穿越（zip 里的 ../../ 把文件写到皮肤目录外面）
  · 压缩炸弹（解压后几百 GB）
  · 装完不是皮肤（随便一个 zip 丢进来）

都是真会被触发的：用户会从各种地方拿素材包。
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import urllib.request
import zipfile
from typing import Optional

from pet_base import data_dir, skins_dir

# 下载上限。皮肤素材一般几 MB，whale-chan 那种 60fps 动图也就 157MB，
# 但那是极端值 —— 超过这个数基本是搞错了或者恶意。
MAX_DOWNLOAD = 256 * 1024 * 1024
# 解压后总大小上限（挡压缩炸弹：10KB 的 zip 能解出 10GB）
MAX_EXTRACTED = 512 * 1024 * 1024
# 单个文件上限
MAX_FILE = 128 * 1024 * 1024
# 最多多少个文件（挡海量小文件）
MAX_FILES = 5000

# 认得出来的皮肤标志文件。
# 必须和 pet_render.discover_skins() 认的**完全一致** —— 第一版我写成
# sprite.png / sheet.png，结果装完自己认不出来，所有安装都被回滚。
SKIN_MARKERS = ("theme.json", "spritesheet.webp", "spritesheet.png")

# 内置皮肤，删不掉
BUILTIN = frozenset({"dafeiyu", "kun-like", "matcha-sunny"})


class SkinInstallError(Exception):
    """装不了。消息直接给用户看，所以写人话。"""


def _safe_members(zf: zipfile.ZipFile, dest: str) -> list:
    """挑出可以安全解压的成员，顺便算总大小。

    路径穿越的判定：解压后的绝对路径必须还在 dest 里面。
    只检查字符串前缀是不够的（`dest_evil` 也以 `dest` 开头），
    要比 commonpath。
    """
    base = os.path.abspath(dest)
    out = []
    total = 0
    for info in zf.infolist():
        name = info.filename
        if name.endswith("/"):
            continue                                  # 目录，跳过
        # 绝对路径 / 盘符 / ..
        if os.path.isabs(name) or ":" in name.split("/")[0]:
            raise SkinInstallError(f"压缩包里有绝对路径：{name}")
        target = os.path.abspath(os.path.join(base, name))
        try:
            inside = os.path.commonpath([base, target]) == base
        except ValueError:                            # 不同盘
            inside = False
        if not inside:
            raise SkinInstallError(f"压缩包试图写到皮肤目录外面：{name}")
        if info.file_size > MAX_FILE:
            raise SkinInstallError(f"单个文件太大：{name}")
        total += info.file_size
        if total > MAX_EXTRACTED:
            raise SkinInstallError("解压后太大（可能是压缩炸弹），已中止")
        if len(out) >= MAX_FILES:
            raise SkinInstallError("文件数太多，已中止")
        out.append(info)
    if not out:
        raise SkinInstallError("压缩包是空的")
    return out


def _extract_zip(path: str, dest: str) -> None:
    with zipfile.ZipFile(path) as zf:
        members = _safe_members(zf, dest)
        os.makedirs(dest, exist_ok=True)
        for info in members:
            target = os.path.join(dest, info.filename)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 256)


def _find_skin_root(root: str) -> str:
    """在解压结果里找皮肤根目录。

    压缩包常见的三种结构：
        skin/theme.json              <- root 本身就是
        myskin/theme.json            <- 外面套了一层
        a/b/theme.json               <- 套了两层
    往下找最多三层，找到含标志文件的目录就返回。
    """
    if any(os.path.exists(os.path.join(root, m)) for m in SKIN_MARKERS):
        return root
    for _ in range(3):
        entries = [e for e in os.listdir(root)
                   if os.path.isdir(os.path.join(root, e))]
        # 只有唯一子目录才往下走，多个就说明结构不认识
        if len(entries) != 1:
            break
        root = os.path.join(root, entries[0])
        if any(os.path.exists(os.path.join(root, m)) for m in SKIN_MARKERS):
            return root
    raise SkinInstallError(
        "压缩包里没找到皮肤（要有 theme.json 或 spritesheet.png / spritesheet.webp）")


def _slug(text: str) -> str:
    """把任意名字变成安全的目录名。"""
    keep = []
    for ch in text.strip().lower():
        if ch.isalnum() and ord(ch) < 128:
            keep.append(ch)
        elif ch in "-_":
            keep.append(ch)
        elif ch in " .":
            keep.append("-")
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:48] or "skin"


def install_skin(src: str, name: Optional[str] = None) -> tuple:
    """从 URL 或本地文件装一套皮肤。

    返回 (皮肤 id, 提示消息)。失败抛 SkinInstallError，消息可直接给用户看。

    装完会**重新扫一遍**确认 discover_skins 认得它 —— 不自己判断格式，
    免得出现"装上了但列表里没有"这种更糟的情况。
    """
    import pet_render                                    # 延迟引，避免循环
    tmpdir = tempfile.mkdtemp(prefix="dsh-pet-skin-")
    try:
        # ── 拿到文件 ──
        if src.startswith(("http://", "https://")):
            local = os.path.join(tmpdir, "download.zip")
            try:
                req = urllib.request.Request(
                    src, headers={"User-Agent": "dsh-pet-skin-installer"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    declared = int(resp.headers.get("Content-Length") or 0)
                    if declared > MAX_DOWNLOAD:
                        raise SkinInstallError(
                            f"文件太大（{declared / 1048576:.0f}MB）")
                    got = 0
                    with open(local, "wb") as fh:
                        while True:
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            got += len(chunk)
                            if got > MAX_DOWNLOAD:
                                raise SkinInstallError("下载超过上限，已中止")
                            fh.write(chunk)
            except SkinInstallError:
                raise
            except Exception as exc:                     # noqa: BLE001
                raise SkinInstallError(f"下载失败：{exc}") from exc
            suggested = os.path.basename(src.split("?")[0]) or "skin"
        else:
            if not os.path.exists(src):
                raise SkinInstallError(f"找不到文件：{src}")
            local = src
            suggested = os.path.basename(src)

        # ── 解压 ──
        stage = os.path.join(tmpdir, "unpacked")
        if zipfile.is_zipfile(local):
            _extract_zip(local, stage)
        else:
            # 不是 zip：当成单个文件（比如直接给一张 sprite.png）
            os.makedirs(stage, exist_ok=True)
            shutil.copy2(local, os.path.join(stage, os.path.basename(local)))
        skin_root = _find_skin_root(stage)

        # ── 定 id ──
        skin_id = _slug(name or os.path.splitext(suggested)[0] or "skin")
        dest = os.path.join(skins_dir(), skin_id)
        if os.path.exists(dest):
            raise SkinInstallError(f"皮肤 {skin_id} 已经装过了，先删掉再装")

        # ── 搬过去 ──
        os.makedirs(skins_dir(), exist_ok=True)
        shutil.copytree(skin_root, dest)

        # ── 验证：真的能被认出来吗 ──
        found = {s["id"] for s in pet_render.discover_skins()}
        if skin_id not in found:
            shutil.rmtree(dest, ignore_errors=True)      # 回滚，别留坏皮肤
            raise SkinInstallError(
                "装上了但识别不出来（格式不对），已经清理掉了")
        return skin_id, f"皮肤 {skin_id} 装好了，在右键菜单的「皮肤」里选"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def remove_skin(skin_id: str) -> tuple:
    """删掉一套自己装的皮肤。内置的三套删不掉。"""
    if skin_id in BUILTIN:
        raise SkinInstallError(f"{skin_id} 是内置皮肤，删不了")
    dest = os.path.join(skins_dir(), skin_id)
    if not os.path.isdir(dest):
        raise SkinInstallError(f"没有这套皮肤：{skin_id}")
    shutil.rmtree(dest)
    return skin_id, f"已删除皮肤 {skin_id}"
