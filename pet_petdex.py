"""从 petdex 找宠物、下载、装成皮肤。

## 为什么是"客户端"而不是"内置一批"

[petdex](https://github.com/crafter-station/petdex) 有 5000+ 只 Codex 宠物。
但它**不暴露单只宠物的许可** —— 查过：

  · `/api/manifest` 的字段只有 `slug, displayName, kind, submittedBy,
    spritesheetUrl, petJsonUrl, zipUrl, spriteVersionNumber`
  · `/api/pets/<slug>` 要鉴权（401）
  · 下载下来的 `pet.json` 只有 4 个必填字段，没有许可
  · 宠物页面上出现的 "License" 是**提交表单的 i18n 文案**，不是这只的值

而它接受的许可有五种（从 about 页面的选项里挖出来的）：

    cc0                  公共领域，不用署名
    cc-by                可用，要署名
    cc-by-sa             可用，要署名，**相同方式共享**
    cc-by-nc             **仅非商用**
    all-rights-reserved  **要问作者**

**后三种和本项目的 MIT 冲突**（`cc-by-sa` 是病毒式传染，`cc-by-nc` 禁商用，
`all-rights-reserved` 根本没授权）。

所以：**我们不打包、不镜像、不内置任何 petdex 宠物。** 这个模块只做客户端 ——
用户自己搜、自己选、自己下载到本地。许可由**用户**判断，界面上给出宠物页
链接让人自己去看。

这和当初排除 Hash Sage / Whale-chan 是同一条线：**权利说不清的东西不进仓库。**
"""

from __future__ import annotations

import io
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

MANIFEST_URL = "https://petdex.dev/api/manifest"
PAGE_URL = "https://petdex.dev/pets/"
TIMEOUT = 25
# 图集最大允许多少字节。petdex 上大的约 2-4 MB；
# 给到 24 MB 是防"下到一个几百 MB 的东西把内存吃爆"，不是防攻击。
MAX_SHEET_BYTES = 24 * 1024 * 1024

# 契约要求的图集尺寸
ATLAS_COLS, ATLAS_CW, ATLAS_CH = 8, 192, 208
ATLAS_MIN_ROWS = 9


class PetdexError(RuntimeError):
    pass


def _get(url: str, timeout: int = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "dsh-pet/petdex-client",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _get_json(url: str, timeout: int = TIMEOUT):
    return json.loads(_get(url, timeout).decode("utf-8", "replace"))


def fetch_manifest(force: bool = False) -> list:
    """拿全部宠物的清单。**不缓存到磁盘** —— 一次几 MB，缓存管理不值当。

    调用方（界面）自己决定什么时候再拉一次。
    """
    got = _get_json(MANIFEST_URL)
    pets = got.get("pets") if isinstance(got, dict) else None
    if not isinstance(pets, list):
        raise PetdexError("manifest 结构不对")
    return pets


def search(pets: list, query: str = "", kind: str = "", version: int = 0) -> list:
    """在清单里筛。**纯函数** —— 好测，也不重复请求。"""
    q = (query or "").strip().lower()
    out = []
    for p in pets:
        if not isinstance(p, dict):
            continue
        if kind and p.get("kind") != kind:
            continue
        # 没写版本的当 v1 —— 显示那行也是 `or 1`，两边要一致。
        # （第一版直接 `!= version`，于是 `None != 1` 把没写版本的滤掉了。）
        if version and (p.get("spriteVersionNumber") or 1) != version:
            continue
        if q:
            hay = " ".join(str(p.get(k, "")) for k in
                           ("slug", "displayName", "kind", "submittedBy")).lower()
            if q not in hay:
                continue
        out.append(p)
    return out


def page_url(slug: str) -> str:
    """宠物页 —— 界面上要给人这个链接去看许可。"""
    return PAGE_URL + urllib.parse.quote(str(slug or ""))


def install(pet: dict, dest_root: str, name_prefix: str = "petdex-") -> dict:
    """下载一只宠物，装成 `dest_root/<prefix><slug>/`。

    **先下到临时目录再原子搬过去** —— 下到一半失败的话，不能留一个
    半截的皮肤目录让 `discover_skins()` 认出来。

    返回 `{ok, id, display, path, error}`。**不抛** —— 调用方是界面。
    """
    slug = str(pet.get("slug") or "").strip()
    if not slug:
        return {"ok": False, "error": "这只宠物没有 slug"}
    # slug 会变成目录名，挡掉路径穿越。petdex 的 slug 是 [a-z0-9-]，
    # 但这里是**用户输入之外唯一一个进路径的字符串**，判一下成本为零。
    safe = "".join(c for c in slug if c.isalnum() or c in "-_")
    if not safe or safe != slug:
        return {"ok": False, "error": f"slug 里有非法字符：{slug!r}"}

    skin_id = name_prefix + safe
    dest = os.path.join(dest_root, skin_id)
    if os.path.exists(dest):
        return {"ok": False, "error": "已经装过了", "id": skin_id,
                "display": pet.get("displayName") or safe, "path": dest}

    tmp = dest + ".part"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        os.makedirs(tmp, exist_ok=True)
        sheet_name = _fetch_sheet(pet, tmp)
        _write_manifest(pet, tmp, sheet_name)
        ok, why = validate_pet_dir(tmp)
        if not ok:
            shutil.rmtree(tmp, ignore_errors=True)
            return {"ok": False, "error": why}
        os.makedirs(dest_root, exist_ok=True)
        os.replace(tmp, dest)
    except Exception as exc:                             # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": True, "id": skin_id,
            "display": pet.get("displayName") or safe, "path": dest}


def _fetch_sheet(pet: dict, tmp: str) -> str:
    """把精灵图下下来。优先 zip（一次拿全），退回落单张图。

    返回落盘的文件名。
    """
    zurl = pet.get("zipUrl")
    if zurl:
        try:
            blob = _get(zurl, timeout=60)
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                names = [n for n in zf.namelist()
                         if os.path.basename(n).lower() in
                         ("spritesheet.webp", "spritesheet.png", "spritesheet.svg")]
                if names:
                    name = "spritesheet" + os.path.splitext(names[0])[1].lower()
                    with zf.open(names[0]) as src, open(os.path.join(tmp, name), "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    # zip 里可能还有 pet.json，一并拿出来（里面有 gazeAnchor）
                    for n in zf.namelist():
                        if os.path.basename(n) == "pet.json":
                            with zf.open(n) as src, \
                                    open(os.path.join(tmp, "pet.json"), "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            break
                    return name
        except Exception:                                # noqa: BLE001
            pass                                         # 落到下面单张下

    surl = pet.get("spritesheetUrl")
    if not surl:
        raise PetdexError("这只宠物既没有 zipUrl 也没有 spritesheetUrl")
    ext = os.path.splitext(urllib.parse.urlparse(surl).path)[1].lower() or ".webp"
    name = "spritesheet" + ext
    blob = _get(surl, timeout=60)
    if len(blob) > MAX_SHEET_BYTES:
        raise PetdexError(f"精灵图太大（{len(blob) // 1048576} MB）")
    with open(os.path.join(tmp, name), "wb") as fh:
        fh.write(blob)
    return name


def _write_manifest(pet: dict, tmp: str, sheet_name: str) -> None:
    """补一份 pet.json。zip 里带的那份不覆盖 —— 它可能有 gazeAnchor。"""
    path = os.path.join(tmp, "pet.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                got = json.load(fh)
            if isinstance(got, dict) and got.get("spritesheetPath"):
                return
        except Exception:                                # noqa: BLE001
            pass
    meta = {
        "id": os.path.basename(tmp).split(".part")[0],
        "displayName": pet.get("displayName") or pet.get("slug") or "petdex pet",
        "description": f"来自 petdex（{pet.get('kind') or 'pet'}）"
                       f"，作者 {pet.get('submittedBy') or '未知'}。"
                       f"许可请到 {page_url(pet.get('slug'))} 确认。",
        "spritesheetPath": sheet_name,
        "sourceUrl": page_url(pet.get("slug")),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)


def validate_pet_dir(path: str) -> tuple:
    """装之前验一下尺寸 —— 不合格的图装进去只会让皮肤列表里多一个坏项。

    返回 `(ok, 说明)`。**只看尺寸**，不解码整张图（大图解码很贵）。
    """
    sheet = None
    for n in ("spritesheet.webp", "spritesheet.png"):
        p = os.path.join(path, n)
        if os.path.exists(p):
            sheet = p
            break
    if sheet is None:
        svg = os.path.join(path, "spritesheet.svg")
        if os.path.exists(svg):
            # 本项目只认位图图集，SVG 要走 OpenPets 那套校验，这里不做
            return False, "这只是 SVG 图集，本项目不支持"
        return False, "没找到精灵图"
    try:
        from PIL import Image
        with Image.open(sheet) as im:
            w, h = im.size
    except Exception as exc:                             # noqa: BLE001
        return False, f"精灵图读不出来：{exc}"
    if w != ATLAS_COLS * ATLAS_CW:
        return False, f"宽 {w}，契约要 {ATLAS_COLS * ATLAS_CW}"
    if h % ATLAS_CH or h < ATLAS_MIN_ROWS * ATLAS_CH:
        return False, f"高 {h}，契约要 {ATLAS_CH} 的整数倍且不少于 {ATLAS_MIN_ROWS * ATLAS_CH}"
    return True, f"{w}x{h}（{w // ATLAS_CW} 列 x {h // ATLAS_CH} 行）"


def installed(dest_root: str) -> list:
    """本地已经装了哪些 petdex 宠物。"""
    try:
        names = sorted(os.listdir(dest_root))
    except OSError:
        return []
    return [n for n in names if n.startswith("petdex-")
            and os.path.isdir(os.path.join(dest_root, n))]


def uninstall(skin_id: str, dest_root: str) -> bool:
    """删掉一只装过的宠物。只认 `petdex-` 前缀 —— 手滑传错名字不会删别的。"""
    if not str(skin_id).startswith("petdex-"):
        return False
    target = os.path.join(dest_root, os.path.basename(skin_id))
    if not os.path.isdir(target):
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True
