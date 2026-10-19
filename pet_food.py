"""食物表。

素材来自 [microsoft/fluentui-emoji](https://github.com/microsoft/fluentui-emoji)，
**MIT 许可**（Copyright Microsoft）—— 干净，没有 SA/NC 的传染性。
拉取和光栅化见 `tools/fetch_food.py`。

## 为什么把「饱足」拆出来

原来只有 `energy` 一个值兼着当精力和饥饱 —— 于是「喂饱了」和「精神好」
是同一件事，逻辑上说不通：她可以又累又饱，也可以又饿又精神。

现在两个独立：
  · **energy** —— 精力。会自然回落，睡觉回，喂食也回一点。
  · **satiety** —— 饱足。只靠吃涨，随时间掉。**满了就吃不下。**

## 数值怎么定的

不追求"平衡"，追求**行为上说得通**：
  · 正餐（拉面/饭团/米饭）顶饱，零食（糖/巧克力/茶）几乎不顶饱
  · 甜点亲密涨得多一点（她喜欢）
  · 茶几乎只是个"陪她坐一会儿"的动作 —— 数值最低，但也能喂
"""

from __future__ import annotations

import os
from typing import Optional

# ── 食物表 ──────────────────────────────────────────────────────────────────
# energy/satiety/affection 都是 0~1 的增量
FOODS = {
    "dango": {
        "label": "团子", "energy": 0.25, "satiety": 0.20, "affection": 0.060,
        "line": ["团子！", "这个我喜欢", "糯糯的"],
    },
    "fish": {
        "label": "小鱼", "energy": 0.30, "satiety": 0.30, "affection": 0.100,
        "line": ["鱼！", "同类……不对，好吃", "再来一条"],
    },
    "ramen": {
        "label": "拉面", "energy": 0.35, "satiety": 0.45, "affection": 0.050,
        "line": ["好烫好烫", "这个顶饱", "汤也喝掉"],
    },
    "riceball": {
        "label": "饭团", "energy": 0.28, "satiety": 0.38, "affection": 0.050,
        "line": ["饭团", "扎实", "海苔好吃"],
    },
    "onigiri": {
        "label": "米饭", "energy": 0.25, "satiety": 0.35, "affection": 0.040,
        "line": ["白米饭", "香", "吃饱了"],
    },
    "shortcake": {
        "label": "蛋糕", "energy": 0.18, "satiety": 0.25, "affection": 0.080,
        "line": ["蛋糕！", "草莓留最后吃", "甜甜的"],
    },
    "cookie": {
        "label": "饼干", "energy": 0.10, "satiety": 0.12, "affection": 0.040,
        "line": ["咔嚓", "饼干屑掉了", "还有吗"],
    },
    "candy": {
        "label": "糖果", "energy": 0.12, "satiety": 0.05, "affection": 0.060,
        "line": ["糖！", "含着慢慢化", "甜的"],
    },
    "chocolate": {
        "label": "巧克力", "energy": 0.14, "satiety": 0.08, "affection": 0.070,
        "line": ["巧克力", "掰一块给你", "苦的那种更好"],
    },
    "bubbletea": {
        "label": "奶茶", "energy": 0.15, "satiety": 0.10, "affection": 0.050,
        "line": ["奶茶！", "珍珠要嚼", "半糖就好"],
    },
    "strawberry": {
        "label": "草莓", "energy": 0.08, "satiety": 0.06, "affection": 0.040,
        "line": ["草莓", "酸酸甜甜", "小小的"],
    },
    "teacup": {
        "label": "茶", "energy": 0.05, "satiety": 0.02, "affection": 0.030,
        "line": ["喝茶", "陪你坐会儿", "烫，吹吹"],
    },
}

FOOD_IDS = tuple(FOODS)

# 吃不下 / 不喜欢 / 最爱 的台词
FULL_LINES = ["吃不下啦", "撑了…", "等会儿再吃", "肚子圆了"]
DISLIKE_LINES = ["这个……不太行", "唔", "下次换个吧", "勉勉强强"]
FAVORITE_LINES = ["最爱你懂我", "这个最棒", "就等你喂这个"]

# 饱足到这个程度就吃不下了
FULL_AT = 0.92
# 超过这个量算"吃撑"，她会不高兴
STUFFED_AT = 1.0


def food_label(fid: str) -> str:
    return FOODS.get(fid, {}).get("label", fid)


def food_path(fid: str, assets_dir: str) -> Optional[str]:
    """食物图的路径。没有这个文件返回 None（调用方退回 emoji 文字）。"""
    p = os.path.join(assets_dir, "food", f"{fid}.png")
    return p if os.path.exists(p) else None


def pick_favorites(rng, count: int = 2) -> list:
    """给一只新宠物随机挑几个最爱。传 random.Random 以便复现。"""
    pool = list(FOOD_IDS)
    rng.shuffle(pool)
    return pool[:count]


def pick_dislike(rng, exclude) -> str:
    """挑一个不喜欢的。要和最爱错开。"""
    pool = [f for f in FOOD_IDS if f not in set(exclude)]
    return rng.choice(pool) if pool else FOOD_IDS[0]


def apply(fid: str, mood, favorites, dislike) -> dict:
    """喂一口，改 mood，返回这次发生了什么。

    返回值给调用方决定说什么话、放什么音效：
        {"ok": bool, "favorite": bool, "dislike": bool,
         "energy": float, "satiety": float, "affection": float, "reason": str}
    """
    food = FOODS.get(fid)
    if food is None:
        return {"ok": False, "reason": "unknown", "favorite": False, "dislike": False,
                "energy": 0.0, "satiety": 0.0, "affection": 0.0}

    # 吃太饱就拒绝 —— 这是这个机制唯一的"边界"，没有它喂食是无脑点
    if getattr(mood, "satiety", 0.0) >= FULL_AT:
        return {"ok": False, "reason": "full", "favorite": False, "dislike": False,
                "energy": 0.0, "satiety": 0.0, "affection": 0.0}

    fav = fid in set(favorites or ())
    bad = fid == dislike

    # 三种情况分开算，别用一个 mult 套所有维度 ——
    # 第一版就是那么写的，结果「最爱」的 satiety 乘了 1.5：
    # 爱吃的反而更顶饱，说不通。
    #
    #   最爱：更开心（亲密 ×2），也更有精神（精力 ×1.2），
    #         但**饱足不变** —— 爱吃不代表更管饱
    #   不爱吃：什么都打折，但还是能吃，只是不情愿
    if bad:
        d_energy = food["energy"] * 0.4
        d_sat = food["satiety"] * 0.4
        d_aff = food["affection"] * 0.4
    elif fav:
        d_energy = food["energy"] * 1.2
        d_sat = food["satiety"]
        d_aff = food["affection"] * 2.0
    else:
        d_energy = food["energy"]
        d_sat = food["satiety"]
        d_aff = food["affection"]

    mood.energy = min(1.0, max(0.0, getattr(mood, "energy", 0.5) + d_energy))
    mood.satiety = min(STUFFED_AT, max(0.0, getattr(mood, "satiety", 0.0) + d_sat))
    if hasattr(mood, "affection"):
        mood.affection = min(1.0, mood.affection + d_aff)
    # **不在这里数 feeds** —— 计数是 CompanionSource.touch("feed") 的活。
    # 两边都加会数两次（踩过：喂一口 feeds 从 0 直接变 2）。
    # 这个函数只管"食物的效果"，不管"记一笔互动"。

    return {"ok": True, "favorite": fav, "dislike": bad, "reason": "",
            "energy": d_energy, "satiety": d_sat, "affection": d_aff}
