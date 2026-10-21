"""状态 → 动画的映射，做成配置。

## 为什么要有这个

原来图集皮肤的行号是**写死在 `pet_render.ATLAS_ROW` 里**的：

    ATLAS_ROW = {"idle": 0, "tools": 7, "thinking": 8, ...}

皮肤作者改不了。而 **clawd 主题本来就能自己映射**
（`theme.json` 里 `states: {idle: "xxx.svg", roam: "yyy.apng"}`）——
所以这里是**不对称的**：一种皮肤格式能自己决定，另一种不能。

## 设计（照 OpenPets 的两层）

`apps/desktop/src/reaction-animation-mapping.ts` 是这么分的：

  1. **动画定义** —— 名字 → `{row, frames, durationMs, iterations}`
  2. **反应 → 动画** —— `{thinking: "review", working: "running", ...}`

分开的好处是**两件事可以各自改**：加一个动画不改映射，
改一个状态的对应不动画定义。

我们的 `pet.json` / `theme.json` 里对应写成：

    "mapping": {
      "animations": {
        "idle":    { "row": 0, "durationMs": 5500, "loop": true },
        "running": { "row": 7, "durationMs": 820 }
      },
      "states": {
        "thinking": "review",
        "sleeping": "idle",
        "tools": "running"
      }
    }

**两层都可以只写一部分**，没写的用默认。

## 谁能覆盖谁

    内置默认  <  皮肤清单（pet.json / theme.json）  <  用户配置（pet_config.json）

皮肤作者决定"这只宠物该怎么演"，用户决定"我要它怎么演"。
用户配置里写 `"mapping": {...}` 就盖在皮肤之上。
"""

from __future__ import annotations

import os
from typing import Optional

# ── 第一层：动画定义 ────────────────────────────────────────────────────────
# 名字 -> {row, duration_ms, loop}
#
# 名字沿用契约里的行名（idle / running-right / …），这样认识契约的人
# 一看就懂，也不用另起一套词汇。
#
# `loop=False` 的是**一次性**动画（挥手、跳），播完回 idle ——
# 契约里这两行本来就是 `iterations: 2`。
DEFAULT_ANIMATIONS = {
    "idle":          {"row": 0, "duration_ms": 5500, "loop": True},
    "running-right": {"row": 1, "duration_ms": 1060, "loop": True},
    "running-left":  {"row": 2, "duration_ms": 1060, "loop": True},
    "waving":        {"row": 3, "duration_ms": 700,  "loop": False},
    "jumping":       {"row": 4, "duration_ms": 840,  "loop": False},
    "failed":        {"row": 5, "duration_ms": 1220, "loop": False},
    "waiting":       {"row": 6, "duration_ms": 1050, "loop": True},
    "running":       {"row": 7, "duration_ms": 820,  "loop": True},
    "review":        {"row": 8, "duration_ms": 1030, "loop": True},
}

# ── 第二层：我们的状态 -> 动画名 ────────────────────────────────────────────
# 这就是原来写死的 ATLAS_ROW，只是换成了动画名而不是行号 ——
# 行号是"动画定义"那一层的事，状态这一层不该知道。
DEFAULT_STATE_ANIMATION = {
    "idle": "idle",
    "offline": "idle",
    "peeking": "idle",
    "yawning": "idle",
    "dozing": "idle",
    "sleeping": "idle",
    "thinking": "review",
    "tools": "running",
    "waiting": "waiting",
    "failed": "failed",
    "celebrating": "jumping",
    "waking": "waving",
    "petted": "jumping",
    # 陪伴模式借"跑"那两行当溜达/张望 —— 契约里没有"走"这个状态。
    # **两个都是 running-right（行 1）**，和原来写死的 ATLAS_ROW 一致；
    # 朝向由 mirror 翻转决定，不用换行。
    # （第一版把 gazing 写成 running-left，默认行为就变了 —— 被
    # "默认必须和原来一模一样"那条测试抓到。）
    "roaming": "running-right",
    "gazing": "running-right",
    # 追鼠标用真正的"跑"那一行（行 7）—— 比 running-right 快，
    # 看起来是"赶过来"而不是"散步过来"。
    "chasing": "running",
}

# ── 陪伴模式的放慢倍数 ──────────────────────────────────────────────────────
# 打盹和睡觉不该跟待机一样精神。这是"同一个动画，不同状态播得不一样快"，
# 所以它既不属于动画定义（那是动画本身的时长），也不属于状态映射
# （那是选哪个动画）—— 单列一层。
DEFAULT_SLOWDOWN = {"yawning": 2.4, "dozing": 3.2, "sleeping": 4.5, "waking": 1.6}

# ── 元数据：给界面用 ────────────────────────────────────────────────────────
# 照 OpenPets 的 `selectableAnimationMetadata` / `reactionAnimationMetadata`。
# 有 label 和 description 才能在设置界面里列出来给人选。
ANIMATION_META = [
    {"id": "idle",          "label": "待机",   "desc": "什么都不做，正常呼吸"},
    {"id": "running-right", "label": "向右跑", "desc": "朝右移动"},
    {"id": "running-left",  "label": "向左跑", "desc": "朝左移动"},
    {"id": "waving",        "label": "挥手",   "desc": "打招呼、引起注意"},
    {"id": "jumping",       "label": "跳跃",   "desc": "成功、开心"},
    {"id": "failed",        "label": "失败",   "desc": "出错"},
    {"id": "waiting",       "label": "等待",   "desc": "卡住了、等你回答"},
    {"id": "running",       "label": "忙碌",   "desc": "正在干活"},
    {"id": "review",        "label": "思考",   "desc": "在想、在读"},
]

STATE_META = [
    {"id": "idle",        "label": "空闲",     "default": "idle"},
    {"id": "thinking",    "label": "思考中",   "default": "review"},
    {"id": "tools",       "label": "跑工具",   "default": "running"},
    {"id": "waiting",     "label": "等你回答", "default": "waiting"},
    {"id": "failed",      "label": "出错",     "default": "failed"},
    {"id": "celebrating", "label": "刚完成",   "default": "jumping"},
    {"id": "offline",     "label": "没会话",   "default": "idle"},
    {"id": "roaming",     "label": "溜达",     "default": "running-right"},
    {"id": "gazing",      "label": "看你",     "default": "running-right"},
    {"id": "chasing",     "label": "过来啦",   "default": "running"},
    {"id": "peeking",     "label": "张望",     "default": "idle"},
    {"id": "yawning",     "label": "打哈欠",   "default": "idle"},
    {"id": "dozing",      "label": "犯困",     "default": "idle"},
    {"id": "sleeping",    "label": "睡着",     "default": "idle"},
    {"id": "waking",      "label": "刚醒",     "default": "waving"},
    {"id": "petted",      "label": "被摸",     "default": "jumping"},
]


def animation_choices() -> list:
    """给界面用的动画列表（含 label/desc）。"""
    return [dict(a) for a in ANIMATION_META]


def state_choices() -> list:
    """给界面用的状态列表，`default` 是内置默认。"""
    return [dict(s) for s in STATE_META]


class Mapping:
    """一份解析好的映射。

    `animations` / `states` / `slowdown` 都是**完整**的 —— 没写的部分
    已经用默认补齐，调用方不用判空。

    但 `set_animations` / `set_states` / `set_slowdown` 记着
    **哪些键是显式写过的** —— `merge` 靠它决定盖什么。

    （第一版没记这个，于是 `merge` 把用户那份"补齐过的完整默认值"
    整个盖在皮肤上，皮肤作者配的十几项全被冲掉 —— 正好反了。）
    """

    def __init__(self, animations=None, states=None, slowdown=None,
                 warnings=None, set_animations=None, set_states=None,
                 set_slowdown=None):
        self.animations = dict(animations if animations is not None
                               else DEFAULT_ANIMATIONS)
        self.states = dict(states if states is not None
                           else DEFAULT_STATE_ANIMATION)
        self.slowdown = dict(slowdown if slowdown is not None
                             else DEFAULT_SLOWDOWN)
        self.warnings = list(warnings or ())
        self.set_animations = set(set_animations or ())
        self.set_states = set(set_states or ())
        self.set_slowdown = set(set_slowdown or ())

    def row_for(self, state: str) -> int:
        """状态 -> 图集行号。认不出来就回 idle 那行。"""
        anim = self.states.get(state) or self.states.get("idle") or "idle"
        spec = self.animations.get(anim) or DEFAULT_ANIMATIONS["idle"]
        return int(spec["row"])

    def spec_for(self, state: str) -> dict:
        """状态 -> 完整的动画定义。"""
        anim = self.states.get(state) or "idle"
        return dict(self.animations.get(anim) or DEFAULT_ANIMATIONS["idle"])

    def slow_for(self, state: str) -> float:
        return float(self.slowdown.get(state, 1.0))


def parse(raw, max_rows: Optional[int] = None) -> Mapping:
    """从清单里的一段 `mapping` 解析。**坏了不抛，降级 + 记警告。**

    为什么降级而不是抛：`pet.json` 是用户会手改的文件（记事本存 UTF-8 带 BOM
    这种事已经踩过）。一个拼错的行号不该让整个皮肤加载不出来 ——
    退回默认，记一条警告，界面上能看到。
    """
    warnings: list = []
    if not isinstance(raw, dict):
        return Mapping(warnings=warnings)

    # 第一层：动画定义
    animations = {k: dict(v) for k, v in DEFAULT_ANIMATIONS.items()}
    set_anims: set = set()
    raw_anims = raw.get("animations")
    if raw_anims is not None and not isinstance(raw_anims, dict):
        warnings.append("animations 不是对象，整段忽略")
        raw_anims = None
    for name, spec in (raw_anims or {}).items():
        if not isinstance(spec, dict):
            warnings.append(f"动画 {name} 不是对象，忽略")
            continue
        try:
            row = int(spec["row"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"动画 {name} 没有合法的 row，忽略")
            continue
        if max_rows is not None and not (0 <= row < max_rows):
            warnings.append(f"动画 {name} 的 row={row} 超出图集（{max_rows} 行），忽略")
            continue
        merged = dict(DEFAULT_ANIMATIONS.get(name, {"duration_ms": 1000, "loop": True}))
        merged["row"] = row
        for key, cast in (("duration_ms", int), ("durationMs", int), ("loop", bool)):
            if key in spec:
                try:
                    merged["duration_ms" if key == "durationMs" else key] = cast(spec[key])
                except (TypeError, ValueError):
                    warnings.append(f"动画 {name} 的 {key} 不合法，用默认")
        if merged["duration_ms"] <= 0:
            merged["duration_ms"] = 1000
        animations[name] = merged
        set_anims.add(name)

    # 第二层：状态 -> 动画
    states = dict(DEFAULT_STATE_ANIMATION)
    set_states: set = set()
    raw_states = raw.get("states")
    if raw_states is not None and not isinstance(raw_states, dict):
        warnings.append("states 不是对象，整段忽略")
        raw_states = None
    for state, anim in (raw_states or {}).items():
        if not isinstance(anim, str):
            warnings.append(f"状态 {state} 的映射不是字符串，忽略")
            continue
        if anim not in animations:
            warnings.append(f"状态 {state} 指向不存在的动画 {anim!r}，忽略")
            continue
        states[state] = anim
        set_states.add(state)

    # 放慢倍数
    slowdown = dict(DEFAULT_SLOWDOWN)
    set_slow: set = set()
    raw_slow = raw.get("slowdown")
    if raw_slow is not None and not isinstance(raw_slow, dict):
        warnings.append("slowdown 不是对象，整段忽略")
        raw_slow = None
    for state, factor in (raw_slow or {}).items():
        try:
            f = float(factor)
        except (TypeError, ValueError):
            warnings.append(f"状态 {state} 的放慢倍数不合法，忽略")
            continue
        if f > 0:
            slowdown[state] = f
            set_slow.add(state)

    return Mapping(animations, states, slowdown, warnings,
                   set_anims, set_states, set_slow)


def load_from_manifest(manifest, max_rows: Optional[int] = None) -> Mapping:
    """从皮肤清单里读 `mapping` 那一段。没有就全默认。"""
    if not isinstance(manifest, dict):
        return Mapping()
    return parse(manifest.get("mapping"), max_rows=max_rows)


def merge(user: Mapping, skin: Mapping) -> Mapping:
    """用户配置盖在皮肤之上。

    **只盖用户显式写过的键**，不是整段替换 —— 用户只想改"睡觉播什么"
    的时候，不该把皮肤作者配的另外十几项一起清掉。

    （第一版没记"哪些键是显式写的"，`user.animations` 是补齐过的完整
    默认表，`update` 一下就把皮肤整个冲掉了 —— 正好反了。）
    """
    anims = dict(skin.animations)
    for k in user.set_animations:
        anims[k] = user.animations[k]
    states = dict(skin.states)
    for k in user.set_states:
        states[k] = user.states[k]
    slow = dict(skin.slowdown)
    for k in user.set_slowdown:
        slow[k] = user.slowdown[k]
    return Mapping(anims, states, slow,
                   list(skin.warnings) + list(user.warnings),
                   skin.set_animations | user.set_animations,
                   skin.set_states | user.set_states,
                   skin.set_slowdown | user.set_slowdown)
