"""桌宠的数据类型：任务、快照、相处记录。"""

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

from pet_base import *          # noqa: F403

import pet_i18n


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
    slow_tools: list = field(default_factory=list)    # 本次会话最慢的几个工具 [(名字, 毫秒)]
    error: str = ""                                   # 失败时错在哪（一行摘要）

    @property
    def elapsed_ms(self) -> Optional[float]:
        if self.started_ms is None:
            return None
        return max(0.0, time.time() * 1000 - self.started_ms)

    @property
    def label(self) -> str:
        return self.title or (self.session_id[:8] if self.session_id
                              else pet_i18n.t("未命名会话"))

    @property
    def short_state(self) -> str:
        """行尾的短状态。这些是显示文案，要过 t()。"""
        if self.state == "tools":
            return (pet_i18n.t("执行 ") + str(self.tool_names[0])
                    if self.tool_names else pet_i18n.t("执行中"))
        table = {"thinking": "思考中", "waiting": "等你回答", "failed": "出错重试",
                 "celebrating": "已完成", "idle": "空闲", "offline": "离线",
                 "tools": "执行中"}
        key = table.get(self.state)
        # 认不出的状态原样显示（可能是新加的），别变成空白
        return pet_i18n.t(key) if key else self.state


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
    bubble: bool = False                              # detail_line 是她说的话，用强调色画
    mirror: bool = False                              # 水平翻转（看向左边时用）
    wander: float = 0.0                               # 窗口水平目标位置（绝对坐标，0 = 不动）
    slow_tools: list = field(default_factory=list)    # 主任务最慢的工具 [(名字, 毫秒)]
    focus_x: float = 0.0                              # 非 0 时把窗口移到这个 x（等待提醒用）
    quiet: bool = False                               # 你在开会/专注时她闭嘴（音乐源和活动源决定）
    music: str = ""                                   # 正在放什么（"歌手 - 歌名"），空 = 没在放
    # 主任务最近一次工具报错（一行摘要）。**一直有这个值，只是原来没抄进
    # Snapshot** —— `Task.error` 填了，`_make_snapshot` 建 Snapshot 时丢了。
    # 「反复踩的坑」那个功能要读它。
    error: str = ""

    @property
    def elapsed_ms(self) -> Optional[float]:
        if self.started_ms is None:
            return None
        return max(0.0, time.time() * 1000 - self.started_ms)


def read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:                                    # noqa: BLE001
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 出错日志
