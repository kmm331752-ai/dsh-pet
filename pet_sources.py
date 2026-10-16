"""状态来源：读 DSH 会话日志 + 陪伴行为。"""

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

from pet_types import Snapshot, TaskInfo, read_json
# Mood 也定义在本模块（它属于陪伴行为），不额外从 pet_types 引


def list_session_files() -> list:
    """列出全部 projcache 会话文件（路径, mtime, 大小）。"""
    out = []
    try:
        names = os.listdir(PROJ_DIR)
    except OSError:
        return out
    for name in names:
        if not (name.startswith("session-") and name.endswith(".json")):
            continue
        path = os.path.join(PROJ_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append((path, name[len("session-"):-len(".json")], st.st_mtime, st.st_size))
    out.sort(key=lambda x: -x[2])
    return out


class LogTail:
    """增量读取会话 zstd 事件日志，提取 callId -> 工具名。"""

    MAGIC = b"\x28\xb5\x2f\xfd"

    def __init__(self, path: Optional[str]):
        self.path = path
        self.offset = 0
        self.partial = ""
        self.calls: dict = {}
        self.last_tool = ""
        # 工具耗时统计：按【事件 seq】记发起时刻与工具名，结果事件通过
        # sourceEventSeqs 指回来。不能用 callId 配对 —— 它里面的 call_00_
        # 是每回合重置的序号，跨回合重复，会算出几十天的耗时。
        self.call_at: dict = {}
        self.call_name: dict = {}
        self.tool_times: dict = {}
        self._dctx = None
        if path:
            self._seed()

    def _decompressor(self):
        if self._dctx is None:
            import zstandard
            self._dctx = zstandard.ZstdDecompressor()
        return self._dctx

    def _decode_frame(self, blob: bytes) -> Optional[str]:
        import io
        try:
            with self._decompressor().stream_reader(io.BytesIO(blob)) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:                                # noqa: BLE001
            return None

    def _seed(self, tail_bytes: int = 3_000_000, max_frames: int = 40) -> None:
        """启动时只解最近的若干帧，避免长会话拖慢启动。"""
        try:
            size = os.path.getsize(self.path)
            with open(self.path, "rb") as fh:
                if size > tail_bytes:
                    fh.seek(size - tail_bytes)
                buf = fh.read()
        except OSError:
            return
        base = size - len(buf)
        offs, i = [], buf.find(self.MAGIC)
        while i != -1:
            offs.append(i)
            i = buf.find(self.MAGIC, i + 1)
        if not offs:
            return
        for n, start in enumerate(offs[-(max_frames + 1):-1] if len(offs) > 1 else offs):
            end = offs[offs.index(start) + 1] if offs.index(start) + 1 < len(offs) else len(buf)
            txt = self._decode_frame(buf[start:end])
            if txt:
                self._consume(txt)
        # 全部已解帧之后的偏移作为续读起点
        self.offset = base + (offs[-max_frames - 1] if len(offs) > max_frames + 1 else offs[0])
        if self.offset == base + offs[0]:
            self.offset = base + (offs[-max_frames] if len(offs) > max_frames else offs[0])

    def _consume(self, text: str) -> None:
        text = self.partial + text
        lines = text.split("\n")
        self.partial = lines.pop()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:                            # noqa: BLE001
                continue
            et = ev.get("type")
            data = ev.get("data") or {}
            if et == "tool/call":
                cid, name = data.get("callId"), data.get("name")
                if cid and name:
                    self.calls[cid] = name
                    self.last_tool = name
                    # 计时按 seq 记，不按 callId —— callId 里的 call_00_ 是
                    # 每回合重置的序号，跨回合会重复，用它配对会算出几十天的耗时。
                    self.call_at[ev.get("seq")] = ev.get("time")
                    self.call_name[ev.get("seq")] = name
                    if len(self.calls) > 800:
                        for k in list(self.calls)[:400]:
                            self.calls.pop(k, None)
                    if len(self.call_at) > 800:
                        for k in list(self.call_at)[:400]:
                            self.call_at.pop(k, None)
                            self.call_name.pop(k, None)
            elif et == "tool/result":
                end = ev.get("time")
                for sq in (ev.get("sourceEventSeqs") or []):
                    started = self.call_at.pop(sq, None)
                    name = self.call_name.pop(sq, None)
                    if name and started and end:
                        dur = max(0.0, end - started)
                        bucket = self.tool_times.setdefault(name, [])
                        bucket.append(dur)
                        if len(bucket) > 40:                 # 只留最近 40 次
                            del bucket[:20]
                    break

    def slowest(self, n: int = 3) -> list:
        """本次会话最慢的 n 个工具（按各自最慢一次排）。"""
        ranked = [(name, max(durs)) for name, durs in self.tool_times.items() if durs]
        ranked.sort(key=lambda x: -x[1])
        return ranked[:n]

    def poll(self) -> None:
        if not self.path:
            return
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:                           # 日志被轮转/重建
            self.offset, self.calls, self.partial = 0, {}, ""
            return
        if size == self.offset:
            return
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                buf = fh.read()
        except OSError:
            return

        offs, i = [], buf.find(self.MAGIC)
        while i != -1:
            offs.append(i)
            i = buf.find(self.MAGIC, i + 1)
        if not offs:
            return

        consumed = 0
        for n, start in enumerate(offs):
            end = offs[n + 1] if n + 1 < len(offs) else len(buf)
            txt = self._decode_frame(buf[start:end])
            if txt is None:                              # 帧未写完，留给下一次
                break
            self._consume(txt)
            consumed = end
        if consumed:
            self.offset += consumed


def find_log_path(session_id: str) -> Optional[str]:
    pat = os.path.join(SESS_DIR, "*", f"session-{session_id}", "session*.jsonl.zstd")
    cands = glob.glob(pat)
    if not cands:
        pat2 = os.path.join(SESS_DIR, "**", f"session-{session_id}", "session*.jsonl.zstd")
        cands = glob.glob(pat2, recursive=True)
    if not cands:
        return None
    try:
        return max(cands, key=os.path.getmtime)
    except OSError:
        return cands[0]


def snapshot_from_rows(rows: dict) -> Optional[dict]:
    """把投影行折成中间状态。找不到会话统计就返回 None。"""
    stats = (rows.get("sessionStats") or {}).get("val")
    if not isinstance(stats, dict):
        return None
    open_step = stats.get("openStep")
    pending = stats.get("pendingCalls") or {}
    if not isinstance(pending, dict):
        pending = {}

    # 三个此前没用上的信号：等你回答 / 出错重试 / 排队中
    qs = ((rows.get("userQuestions") or {}).get("val") or {}).get("questions") or {}
    waiting = bool(qs.get("active"))
    retry = (rows.get("llmRetry") or {}).get("val")
    failed = bool(retry) if isinstance(retry, dict) else False
    inbox = (rows.get("inbox") or {}).get("val") or {}
    queued = bool(inbox.get("next-turn") or inbox.get("next-step"))
    plan_active = bool(((rows.get("plan") or {}).get("val") or {}).get("active"))
    goal_val = ((rows.get("goal") or {}).get("val") or {}).get("current") or {}
    goal = ((goal_val.get("goal") or {}).get("objective") or "").strip()
    tb = (rows.get("turnBoundary") or {}).get("val") or {}
    turn_open = tb.get("openTurnStartSeq") is not None

    # 优先级：卡在用户这里 > 出错 > 跑工具 > 生成中
    if waiting:
        state = "waiting"
    elif failed:
        state = "failed"
    elif pending:
        state = "tools"
    elif isinstance(open_step, dict):
        state = "thinking"
    else:
        state = "idle"

    started = None
    if pending:
        vals = [v for v in pending.values() if isinstance(v, (int, float))]
        if vals:
            started = min(vals)
    if started is None and isinstance(open_step, dict):
        started = open_step.get("startTime")

    model_sel = (rows.get("modelSelection") or {}).get("val") or {}
    last_used = model_sel.get("lastUsed") or {}
    ctx = (rows.get("contextPressure") or {}).get("val") or {}
    usage = ((rows.get("tokenUsage") or {}).get("val") or {}).get("totals") or {}
    outline = ((rows.get("turnOutline") or {}).get("val") or {}).get("turns") or []
    prompt = ""
    if outline:
        prompt = (outline[-1].get("prompt") or "").strip()

    return dict(
        state=state,
        turn=(open_step or {}).get("turn") or stats.get("lastTurn") or 0,
        step=(open_step or {}).get("step") or stats.get("steps") or 0,
        started_ms=started,
        pending_ids=list(pending.keys()),
        title=((rows.get("title") or {}).get("val") or "") or "",
        provider=last_used.get("provider") or "",
        model=last_used.get("model") or "",
        context_pct=(ctx.get("pressureTokens", 0) / ctx["contextWindow"] * 100.0)
        if ctx.get("contextWindow") else 0.0,
        surface_tokens=float(ctx.get("surfaceTokens") or 0),
        context_window=float(ctx.get("contextWindow") or 0),
        tokens_out=float(usage.get("outputTokens") or 0),
        prompt=prompt,
        goal=goal,
        queued=queued,
        plan_active=plan_active,
        turn_open=turn_open,
    )


class StatusWatcher:
    """扫描全部 DSH 会话，汇总出「正在进行的任务」列表。

    一个会话算一个任务。投影缓存按 mtime+size 缓存，文件没变就不重复解析；
    工具名只为活跃会话建增量日志读取器 —— 只有它们用得上。
    """

    def __init__(self, pinned: Optional[str] = None, max_tasks: int = 4):
        self.pinned = pinned
        self.max_tasks = max_tasks
        self._cache: dict = {}                        # path -> (mtime, size, info|None)
        self._tails: dict = {}                        # session_id -> LogTail
        self._idle_since: Optional[float] = None
        self._turn_open: dict = {}                    # session_id -> 上次看到的回合开闭
        self._celebrate_until: dict = {}              # session_id -> 庆祝截止时间
        self._new_completion = False                  # 本轮刚有任务完成（供提示音）
        self._completed: list = []                    # 最近完成的会话 [{title, tools}]

    # -- 会话扫描 -----------------------------------------------------------
    def _scan(self) -> list:
        infos, alive = [], set()
        for path, sid, mtime, size in list_session_files():
            alive.add(path)
            hit = self._cache.get(path)
            if hit and hit[0] == mtime and hit[1] == size:
                info = hit[2]
            else:
                rows = (read_json(path) or {}).get("record", {}).get("rows") or {}
                info = snapshot_from_rows(rows) if rows else None
                self._cache[path] = (mtime, size, info)
            if info:
                item = dict(info)
                item["session_id"] = sid
                item["mtime"] = mtime
                infos.append(item)
        for gone in [p for p in self._cache if p not in alive]:
            self._cache.pop(gone, None)
        return infos

    def _tail(self, sid: str) -> LogTail:
        tail = self._tails.get(sid)
        if tail is None:
            tail = LogTail(find_log_path(sid))
            self._tails[sid] = tail
            while len(self._tails) > 6:               # 只留最近用到的几个
                for k in list(self._tails):
                    if k != sid:
                        self._tails.pop(k, None)
                        break
        return tail

    def _make_task(self, info: dict, with_tools: bool) -> TaskInfo:
        names = []
        slow = []
        if with_tools:
            tail = self._tail(info["session_id"])
            tail.poll()
            if info["pending_ids"]:
                for cid in info["pending_ids"]:
                    nm = tail.calls.get(cid)
                    if nm and nm not in names:
                        names.append(nm)
                if not names and tail.last_tool:
                    names = [tail.last_tool]
            slow = tail.slowest(3)
        return TaskInfo(
            session_id=info["session_id"], state=info["state"], title=info["title"],
            provider=info["provider"], model=info["model"], turn=info["turn"],
            step=info["step"], started_ms=info["started_ms"], tool_names=names,
            prompt=info["prompt"], context_pct=info["context_pct"],
            surface_tokens=info["surface_tokens"], context_window=info["context_window"],
            tokens_out=info["tokens_out"], mtime=info["mtime"], goal=info["goal"],
            queued=info["queued"], plan_active=info["plan_active"],
            turn_open=info["turn_open"], slow_tools=slow)

    def _track_turns(self, infos: list) -> set:
        """检测「回合由开转闭」= 任务完成，返回正在庆祝的会话 id 集合。"""
        now = time.time()
        celebrating = set()
        for i in infos:
            sid, open_now = i["session_id"], i["turn_open"]
            was = self._turn_open.get(sid)
            if was is True and open_now is False:      # 刚刚跑完一个回合
                self._celebrate_until[sid] = now + CELEBRATE_S
                self._new_completion = True
                # 记下是谁跑完了 —— 通知气泡要说得出名字，而不是笼统的"任务完成"
                self._completed.append({
                    "title": (i.get("title") or sid[:8]).strip(),
                    "tools": list(i.get("tool_names") or [])[:2],
                })
                if len(self._completed) > 8:
                    del self._completed[:-8]
            self._turn_open[sid] = open_now
            if self._celebrate_until.get(sid, 0) > now:
                celebrating.add(sid)
        for sid in [s for s, t in self._celebrate_until.items() if t <= now]:
            self._celebrate_until.pop(sid, None)
        return celebrating

    # -- 采样 ---------------------------------------------------------------
    def poll(self) -> Snapshot:
        infos = self._scan()
        if self.pinned:
            infos = [i for i in infos if i["session_id"] == self.pinned]
        if not infos:
            return Snapshot(state="offline", detail_line="未发现 DSH 会话")

        celebrating = self._track_turns(infos)

        active = []
        for i in infos:
            if i["state"] in ACTIVE_STATES:
                active.append(self._make_task(i, with_tools=(i["state"] == "tools")))
            elif i["session_id"] in celebrating:
                t = self._make_task(i, with_tools=False)
                t.state = "celebrating"
                t.started_ms = (self._celebrate_until[i["session_id"]] - CELEBRATE_S) * 1000
                active.append(t)

        # 排序：按状态优先级；同类按开始时间，早的在前 —— 顺序稳定，不会来回跳
        active.sort(key=lambda t: (STATE_RANK.get(t.state, 9), t.started_ms or 0))

        if active:
            primary = active[0]
            state = primary.state
        else:
            state = "idle"
            primary = self._make_task(max(infos, key=lambda i: i["mtime"]), with_tools=False)

        snap = Snapshot(
            state=state, tasks=active[:self.max_tasks], session_id=primary.session_id,
            title=primary.title, provider=primary.provider, model=primary.model,
            turn=primary.turn, step=primary.step, started_ms=primary.started_ms,
            tool_names=primary.tool_names, context_pct=primary.context_pct,
            surface_tokens=primary.surface_tokens, context_window=primary.context_window,
            tokens_out=primary.tokens_out, prompt=primary.prompt,
            slow_tools=primary.slow_tools)

        # 完成通知：说得出是哪个会话跑完了，而不是笼统的"任务已完成"。
        # 只在庆祝刚开始的那几秒里说，不然会一直挂着。
        if state == "celebrating" and self._completed:
            item = self._completed[-1]
            name = item["title"][:18]
            tools = item["tools"]
            if tools:
                snap.detail_line = f"「{name}」跑完了 · {tools[0]}"
            else:
                snap.detail_line = f"「{name}」跑完了"
            snap.bubble = True
        elif state == "waiting":
            snap.detail_line = "在等你回答"
        elif state == "failed":
            snap.detail_line = "请求出错，正在重试"
        elif state == "celebrating":
            snap.detail_line = "任务已完成"
        elif state == "tools":
            if len(active) > 1:
                snap.detail_line = f"{len(active)} 个任务进行中"
            elif len(primary.tool_names) > 1:
                snap.detail_line = f"{len(primary.tool_names)} 个工具调用进行中"
            else:
                snap.detail_line = primary.prompt[:40] or "工具调用进行中"
        elif state == "thinking":
            snap.detail_line = primary.prompt[:44] if primary.prompt else "模型正在生成"
        else:
            if self._idle_since is None:
                self._idle_since = time.time()
            snap.idle_hint = "空闲" if time.time() - self._idle_since < 90 else "空闲（睡着了）"
            snap.detail_line = primary.title[:44] if primary.title else "等待你的下一条指令"
        if state != "idle":
            self._idle_since = None

        return snap


# ─────────────────────────────────────────────────────────────────────────────
# 陪伴模式：DSH 那边一个活跃会话都没有时，她自己过日子


# ─────────────────────────────────────────────────────────────────────────────
# 输出和 StatusWatcher 一样的 Snapshot，所以 PetApp 不需要知道状态是谁给的。
#
# 行为规则：
#   · 深夜（23:00–07:00）或精力耗尽 → 直接睡
#   · 一段时间没碰键盘鼠标 → 犯困 → 打盹 → 睡着
#   · 一有输入 → 醒来 → 回到空闲
#   · 白天精力足的时候会自己溜达、发呆
#   · 被戳/被拖/被喂 → 精神一点，亲密度涨

# 相处记录的路径见 pet_base.mood_path() —— 做成函数，方便测试换目录

# 闲置多久进入下一档（秒）
IDLE_YAWN = 45.0
IDLE_DOZE = 90.0
IDLE_SLEEP = 240.0
# 深夜只要安静这么久就睡
NIGHT_IDLE_SLEEP = 30.0
NIGHT_HOURS = (23, 7)            # [23:00, 07:00)


def cursor_pos() -> tuple:
    """当前鼠标的屏幕坐标。

    用 GetCursorPos 而不是 Tk 的 <Motion> —— 鼠标在窗口外时收不到 Motion 事件，
    而她要看的是整个桌面上的鼠标。
    """
    pt = wintypes.POINT()
    if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return int(pt.x), int(pt.y)
    return (0, 0)


def idle_seconds() -> float:
    """距上次键盘/鼠标输入过了多久（秒）。取不到就返回 0。"""
    class _LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = _LastInput()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    # GetTickCount 是 32 位、约 49.7 天回绕，用掩码算差值即可
    delta = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return delta / 1000.0


@dataclass
class Mood:
    """持久化的心情与相处记录，存在 pet_mood.json。"""

    affection: float = 0.5          # 亲密度 0~1
    energy: float = 1.0             # 精力 0~1
    pokes: int = 0                  # 被戳次数
    drags: int = 0                  # 被拖次数
    feeds: int = 0                  # 被喂次数
    first_seen: float = 0.0         # 第一次见面
    last_seen: float = 0.0          # 上次运行
    last_tick: float = 0.0          # 上次结算时间（算精力增减用）
    greeted: str = ""               # 已经问候过的时段（morning / night），避免重复
    unlocked: int = 0               # 已解锁的相处天数里程碑
    # 作息统计：24 个格子，每个格子记「这个小时你有多活跃」。
    # 由 PetApp 每次轮询时喂进来（DSH 有活在跑 = 活跃）。
    # 目的是让她学到「你几点在、几点不在」，而不是死按 23:00~07:00 判断夜晚。
    busy_slots: list = field(default_factory=lambda: [0] * 24)
    seen_slots: list = field(default_factory=lambda: [0] * 24)

    # -- 作息统计 -----------------------------------------------------------
    # 样本不够时不下结论 —— 刚开始用的一两个小时不该让她误以为你半夜不睡。
    SCHEDULE_MIN_SAMPLES = 30
    SCHEDULE_IDLE_RATIO = 0.25

    def note_activity(self, hour: int, busy: bool) -> None:
        """记一次观察。hour 是 0~23。"""
        if not 0 <= hour < 24:
            return
        # 老版本的 json 里没有这两个字段，_load 会用默认值补上；
        # 但如果被人手改成了长度不对的列表，这里兜一下。
        if len(self.busy_slots) != 24:
            self.busy_slots = [0] * 24
        if len(self.seen_slots) != 24:
            self.seen_slots = [0] * 24
        self.seen_slots[hour] += 1
        if busy:
            self.busy_slots[hour] += 1

    def active_ratio(self, hour: int) -> float:
        """这个小时你有多活跃（0~1）。样本不够时返回 -1 表示「不知道」。"""
        if not 0 <= hour < 24 or len(self.seen_slots) != 24:
            return -1.0
        seen = self.seen_slots[hour]
        if seen < self.SCHEDULE_MIN_SAMPLES:
            return -1.0
        return self.busy_slots[hour] / seen

    def is_usually_idle(self, hour: int) -> bool:
        """统计上你这个时候通常不在。样本不够一律返回 False（保守：按在算）。"""
        r = self.active_ratio(hour)
        return r >= 0.0 and r < self.SCHEDULE_IDLE_RATIO

    def schedule_known(self) -> bool:
        """够不够画一张作息图 —— 设置窗口用它决定要不要显示。

        要求至少 6 个钟点各自攒够样本。第一版写成「总样本 >= 30*4」，
        那是拍脑袋的数：轮询是 350ms 一次，120 个样本 42 秒就攒到了，
        而摊到 24 个格子里每小时才 5 个 —— 什么都说明不了。
        按钟点分别把关才有意义。
        """
        if len(self.seen_slots) != 24:
            return False
        ready = sum(1 for h in range(24)
                    if self.seen_slots[h] >= self.SCHEDULE_MIN_SAMPLES)
        return ready >= 6

    def schedule_text(self) -> str:
        """把作息画成一行，给设置窗口看。"""
        if len(self.seen_slots) != 24:
            self.seen_slots = [0] * 24
        ready = sum(1 for h in range(24)
                    if self.seen_slots[h] >= self.SCHEDULE_MIN_SAMPLES)
        if ready < 6:
            return f"还在观察你的作息（{ready}/6 个时段攒够样本）"
        blocks = []
        for h in range(24):
            r = self.active_ratio(h)
            if r < 0:
                blocks.append("·")                       # 样本不够
            elif r >= 0.5:
                blocks.append("█")                       # 常在
            elif r >= self.SCHEDULE_IDLE_RATIO:
                blocks.append("▒")                       # 有时在
            else:
                blocks.append("░")                       # 通常不在
        return "0" + "".join(blocks) + "23"

    @property
    def days_together(self) -> int:
        if not self.first_seen:
            return 1
        return max(1, int((time.time() - self.first_seen) / 86400) + 1)

    @property
    def mood_label(self) -> str:
        """按亲密度给个说法，给气泡和详情卡用。"""
        a = self.affection
        if a >= 0.85:
            return "特别黏你"
        if a >= 0.65:
            return "挺开心"
        if a >= 0.4:
            return "还不错"
        if a >= 0.2:
            return "有点蔫"
        return "好久没理我了"


class CompanionSource:
    """自主陪伴行为。接口和 StatusWatcher 一样，只有 poll()。"""

    def __init__(self, path: Optional[str] = None):
        # 默认值用 None 而不是 mood_path() —— 写成默认参数会在 import 时求值，
        # 之后测试改 data_dir 就换不掉目录了。
        self.path = path or mood_path()
        self.mood = self._load()
        self.state = "idle"
        self.state_until = 0.0          # 当前状态至少保持到什么时候
        self.next_roam = time.time() + random.uniform(25, 70)
        self.next_peek = time.time() + random.uniform(120, 300)
        self.next_hungry = time.time() + random.uniform(180, 420)
        self.next_lonely = time.time() + random.uniform(1800, 3600)
        self.bubble = ""                # 当前要显示的气泡文案
        self.bubble_until = 0.0
        self._entered = time.time()
        self.last_touch = time.time()   # 上次被互动的时刻（判断"太久没人理"）
        # 看向鼠标：PetApp 每次 poll 前把这些填进来
        self.mouse_x = 0                # 鼠标屏幕 x
        self.mouse_at = 0.0             # 鼠标最后活动时刻
        self.pet_x = 0                  # 桌宠窗口左边 x
        self.gaze_dir = 1               # 1 向右看，-1 向左看
        self.wander_target = 0.0        # 溜达的窗口目标 x（0 = 不动）

    # -- 持久化 -------------------------------------------------------------
    def _load(self) -> Mood:
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:                                # noqa: BLE001
            raw = {}
        fields = {f.name for f in dataclasses.fields(Mood)}
        mood = Mood(**{k: v for k, v in raw.items() if k in fields})
        now = time.time()
        if not mood.first_seen:
            mood.first_seen = now
        mood.last_seen = now
        mood.last_tick = now
        return mood

    def save(self) -> None:
        try:
            self.mood.last_seen = time.time()
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(dataclasses.asdict(self.mood), fh, ensure_ascii=False, indent=2)
        except Exception:                                # noqa: BLE001
            pass

    # -- 互动 ---------------------------------------------------------------
    def touch(self, kind: str) -> None:
        """用户互动时调用：poke / drag / feed。会把她弄精神，亲密度上涨。"""
        self.last_touch = time.time()
        if kind == "poke":
            self.mood.pokes += 1
            self.mood.affection = min(1.0, self.mood.affection + 0.02)
            self.mood.energy = min(1.0, self.mood.energy + 0.05)
        elif kind == "drag":
            self.mood.drags += 1
            self.mood.affection = min(1.0, self.mood.affection + 0.01)
        elif kind == "feed":
            self.mood.feeds += 1
            self.mood.affection = min(1.0, self.mood.affection + 0.06)
            self.mood.energy = min(1.0, self.mood.energy + 0.25)
        # 被碰了就醒
        if self.state in ("dozing", "sleeping"):
            self._enter("waking", 2.6)
        self.save()

    def wake(self) -> None:
        """DSH 那边开工了 —— 她也该醒了（不碰统计，只是醒过来）。"""
        if self.state in ("dozing", "sleeping", "yawning"):
            self._enter("waking", 2.6)

    def say(self, text: str, secs: float = 4.0) -> None:
        self.bubble = text
        self.bubble_until = time.time() + secs

    # -- 状态机 -------------------------------------------------------------
    def _enter(self, state: str, hold: float = 0.0) -> None:
        self.state = state
        self.state_until = time.time() + hold
        self._entered = time.time()

    def _night(self) -> bool:
        """现在是「她该睡」的时候吗。

        两个判据取或：
          · 钟点落在 NIGHT_HOURS 里（兜底，没有统计数据时也成立）
          · 学到的作息说你这个点通常不在 —— 夜班的人 03:00 还在干活，
            那对她来说就不是深夜

        第二条要样本够才生效（is_usually_idle 自己会把关）。
        """
        hour = time.localtime().tm_hour
        lo, hi = NIGHT_HOURS
        if hour >= lo or hour < hi:
            return True
        return self.mood.is_usually_idle(hour)

    def observe(self, busy: bool) -> None:
        """PetApp 每次轮询时喂进来：DSH 现在有没有活在跑。

        这是「学习作息」的输入 —— 不需要读键盘记录，DSH 忙就说明你在。
        """
        self.mood.note_activity(time.localtime().tm_hour, busy)

    def _settle_energy(self, now: float, dt: float) -> None:
        """醒着掉精力，睡着回精力。"""
        if self.state == "sleeping":
            self.mood.energy = min(1.0, self.mood.energy + dt * 0.0025)
        elif self.state in ("dozing", "idle", "roaming", "yawning"):
            self.mood.energy = max(0.0, self.mood.energy - dt * 0.00035)
        self.mood.last_tick = now

    def poll(self) -> Snapshot:
        now = time.time()
        dt = max(0.0, min(now - (self.mood.last_tick or now), 5.0))
        self._settle_energy(now, dt)

        idle = idle_seconds()
        night = self._night()
        tired = self.mood.energy < 0.25
        held = now < self.state_until

        # ── 状态转换 ──
        if not held:
            if self.state in ("sleeping", "dozing"):
                # 有人动键盘鼠标，或者白天睡饱了，就醒
                if idle < 3.0 or (not night and self.mood.energy > 0.85):
                    self._enter("waking", 2.6)
                    if idle < 3.0 and random.random() < 0.35:
                        self.say(say_line("WAKE_LINES"))
            elif self.state == "waking":
                self._enter("idle")
            elif self.state == "yawning":
                self._enter("dozing", 0.0)
            elif self.state in ("roaming", "gazing", "peeking", "petted"):
                self._enter("idle")
                self.wander_target = 0.0                     # 停下就别再挪窗口
            elif self.state == "idle":
                sleep_after = NIGHT_IDLE_SLEEP if (night or tired) else IDLE_SLEEP
                if idle > sleep_after or (tired and idle > 20):
                    self._enter("sleeping")
                    if random.random() < 0.3:
                        self.say(say_line("SLEEP_LINES"))
                elif idle > IDLE_DOZE:
                    self._enter("dozing")
                elif idle > IDLE_YAWN:
                    self._enter("yawning", 2.8)
                    if random.random() < 0.4:
                        self.say(say_line("YAWN_LINES"))
                elif self._try_gaze(now, night):
                    pass                                     # 看向鼠标
                elif not night and self.mood.energy > 0.35 and now > self.next_roam:
                    self._enter("roaming", random.uniform(4.0, 8.0))
                    self.next_roam = now + random.uniform(35, 90)
                    self.wander_target = self._pick_wander_target()
                    if random.random() < 0.25:
                        self.say(say_line("ROAM_LINES"))
                elif now > self.next_peek:
                    self._enter("peeking", random.uniform(3.0, 6.0))
                    self.next_peek = now + random.uniform(120, 300)

        self._situational(now, idle, night)

        # ── 组装 Snapshot ──
        m = self.mood
        snap = Snapshot(state=self.state)
        snap.idle_hint = PALETTE[self.state]["label"]
        snap.title = f"陪你第 {m.days_together} 天"
        snap.detail_line = m.mood_label
        snap.tokens_out = float(m.pokes + m.drags + m.feeds)
        snap.phase = random.random()
        snap.forced = True
        # 看向哪边 / 溜达到哪 —— 交给 PetApp 去动窗口
        snap.mirror = self.gaze_dir < 0
        snap.wander = self.wander_target
        # 气泡：她说的话占用副行文案，但用强调色画出来，并进 _card_cache 的键
        if now < self.bubble_until and self.bubble:
            snap.detail_line = self.bubble
            snap.bubble = True
        return snap

    # -- 溜达目标 -----------------------------------------------------------
    def _pick_wander_target(self) -> float:
        """挑一个溜达目的地。

        多数时候就在附近走两步；偶尔（1/8）换一块显示器 —— 多屏时她会跑过去。
        """
        vx, vy, vw, vh = virtual_screen()
        if vw <= 0:
            return 0.0
        cur = float(self.pet_x)
        if random.random() < 0.125 and vw > WIN_W * 3:
            return float(random.randint(vx, max(vx, vx + vw - WIN_W)))
        step = random.uniform(-200, 200)
        return float(max(vx, min(vx + vw - WIN_W, cur + step)))

    # -- 看向鼠标 -----------------------------------------------------------
    def _try_gaze(self, now: float, night: bool) -> bool:
        """鼠标在附近动过就转头看他。返回是否接管了本帧。"""
        if night or now - self.mouse_at > GAZE_FRESH:
            return False
        dx = self.mouse_x - self.pet_x
        if abs(dx) < GAZE_MIN_DX:
            return False
        self.gaze_dir = 1 if dx > 0 else -1
        self._enter("gazing", GAZE_HOLD)
        self.next_roam = now + random.uniform(30, 80)        # 别马上又去溜达
        return True

    # -- 情境互动 -----------------------------------------------------------
    def _situational(self, now: float, idle: float, night: bool) -> None:
        """饿了、太久没人理、早晚问候、陪满天数解锁。都是低频触发。"""
        m = self.mood

        # 饿了她会主动要吃的
        if (m.energy < 0.22 and self.state in ("idle", "roaming", "gazing")
                and now > self.next_hungry):
            self.next_hungry = now + random.uniform(180, 420)
            self.say(say_line("HUNGRY_LINES"), 5.0)

        # 太久没人理：叹气 + 亲密度缓慢下降
        lonely_after = 7200.0
        if (now - self.last_touch > lonely_after and self.state in ("idle", "gazing")
                and now > self.next_lonely):
            self.next_lonely = now + random.uniform(1800, 3600)
            self.say(say_line("LONELY_LINES"), 5.0)
            m.affection = max(0.0, m.affection - 0.01)

        # 早晚问候：每个时段只问候一次。
        # 这两个标记存在 Mood 上（要跨次启动记住），不是 CompanionSource 的属性。
        hour = time.localtime().tm_hour
        slot = "morning" if 5 <= hour < 11 else ("night" if hour >= 22 or hour < 2 else "")
        if slot and m.greeted != slot and self.state == "idle":
            m.greeted = slot
            self.save()
            self.say(say_line("MORNING_LINES" if slot == "morning" else "NIGHT_LINES"), 5.0)

        # 陪满整周解锁新台词
        days = m.days_together
        if days >= 7 and m.unlocked < 7:
            m.unlocked = 7
            self.save()
            self.say(f"我们认识 {days} 天啦", 5.0)


# 陪伴模式的文案表。想改直接改这里。
WAKE_LINES = ["唔…醒了醒了", "你回来啦", "别晃我…", "再睡五分钟…"]
SLEEP_LINES = ["我先眯一会儿", "撑不住了…", "晚安", "困死了"]
YAWN_LINES = ["啊——欠", "好无聊啊", "你在忙什么", "有点困了"]
ROAM_LINES = ["溜达一圈", "活动活动", "看看窗外", "嗯…走两步"]
PET_LINES = ["嘿嘿", "好舒服", "再摸摸", "最喜欢你了"]
FEED_LINES = ["好吃！", "谢谢投喂", "还有吗", "满足了"]
ANNOY_LINES = ["别摸了…", "够了够了", "手拿开", "烦不烦"]
DIZZY_LINES = ["晕…", "别甩了！", "天旋地转", "放我下来"]
GRAB_LINES = ["喂！", "放开我", "干嘛呀", "别拎着我"]
HUNGRY_LINES = ["有点饿了…", "想吃东西", "喂我一口嘛"]
LONELY_LINES = ["好久没理我了", "在忙吗", "唉…"]
MORNING_LINES = ["早啊", "今天也加油", "睡醒啦"]
NIGHT_LINES = ["还不睡？", "该休息了", "夜深了"]
EDGE_LINES = ["抓到了", "不松手", "救命…"]
POMO_DONE_LINES = ["时间到！", "该休息了", "这一轮结束了", "起来走两步吧"]

# ── 台词表 ──────────────────────────────────────────────────────────────────
# 想换成自己的一套（毒舌版、温柔版…）：把同名键写进 <程序目录>/pet_lines.json，
# 比如 {"PET_LINES": ["嘿嘿", "再来"], "FEED_LINES": ["谢谢"]}。
# 没写的键继续用内置默认，所以只覆盖想改的那几条就行。
# 文件路径见 pet_base.lines_path()

_LINE_DEFAULTS = {
    "WAKE_LINES": WAKE_LINES, "SLEEP_LINES": SLEEP_LINES, "YAWN_LINES": YAWN_LINES,
    "ROAM_LINES": ROAM_LINES, "PET_LINES": PET_LINES, "FEED_LINES": FEED_LINES,
    "ANNOY_LINES": ANNOY_LINES, "DIZZY_LINES": DIZZY_LINES, "GRAB_LINES": GRAB_LINES,
    "HUNGRY_LINES": HUNGRY_LINES, "LONELY_LINES": LONELY_LINES,
    "MORNING_LINES": MORNING_LINES, "NIGHT_LINES": NIGHT_LINES,
    "EDGE_LINES": EDGE_LINES, "POMO_DONE_LINES": POMO_DONE_LINES,
}


def load_lines() -> dict:
    """取台词表：内置默认 + pet_lines.json 的覆盖。

    只接受「非空字符串列表」的值 —— 写错了就当没写，不让一个坏配置文件
    把桌宠弄成一句话都不说。
    """
    table = dict(_LINE_DEFAULTS)
    try:
        with open(lines_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:                                    # noqa: BLE001
        return table
    if not isinstance(raw, dict):
        return table
    for key, val in raw.items():
        if key not in table:
            continue
        if isinstance(val, list) and val and all(isinstance(x, str) and x for x in val):
            table[key] = val
    return table


LINES = load_lines()


def say_line(key: str) -> str:
    """随机取一句台词。表里没有这个键就返回空串（调用方自己兜底）。"""
    pool = LINES.get(key) or []
    return random.choice(pool) if pool else ""
