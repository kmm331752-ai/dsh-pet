"""长跑测试：让她连着跑几个模拟小时，看会不会漏、会不会卡。

## 它检测什么（先回答"会测出什么失败"）

| 会检测到 | 真出现了怎么办 |
|---|---|
| **内存涨**（RSS / 对象数持续上升） | 找到持有引用的那个缓存或列表，加淘汰 |
| **数据结构无界增长**（history / seen_slots / 缓存） | 加上限 |
| **线程泄漏**（每轮多一个） | 找没停的那个源 |
| **状态机卡死**（长时间停在同一个状态） | 看 `state_until` 有没有出口 |
| **错误日志暴涨** | 看 `pet_error.log` 里反复出现的那条 |

**这五条都是"跑得久了才看得出来"的** —— 单元测试和 e2e 都是秒级的，
看不到累积效应。

## 怎么跑的（诚实说明）

**直接调 `_tick` / `_poll`，不走 Tk 事件循环。** 两个原因：

  · 实测 `_tick` 直接调是 **0.56 ms**，走事件循环要等 `after` 的 33~100ms
    间隔 —— 直接调快 100 倍，8 个模拟小时从 8 小时压到 8 分钟。
  · 时钟受控，同样的种子跑出来一样，能对比。

**代价**：`after` 那条调度路径没被测到。那条路由 `tests/test_smoke.py`
覆盖（它跑真的 `mainloop`）。**这里测的是"跑得久会不会漏/卡"，
不是"调度对不对"。**

## 用法

    python tools/soak.py                      # 8 个模拟小时，约 8 分钟
    python tools/soak.py --hours 24           # 一整天
    python tools/soak.py --hours 8 --quiet    # 只打结论
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import gc
import io
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pet_base as B  # noqa: E402

# 跑在临时目录里 —— 不能污染用户真实的 pet_mood.json / pet_config.json
_SOAK_DIR = tempfile.mkdtemp(prefix="dsh-pet-soak-")
B.data_dir = lambda: _SOAK_DIR

import dsh_pet as P  # noqa: E402
import pet_activity as AC  # noqa: E402
import pet_i18n as I  # noqa: E402
import pet_music as MU  # noqa: E402
import pet_sources as S  # noqa: E402

FPS = 30                      # 模拟帧率
STEP = 1.0 / FPS              # 每 tick 推进多少模拟秒
SAMPLE_TARGET = 60            # 目标采样点数 —— 少了看不出趋势


def rss_mb() -> float:
    """当前进程的常驻内存。**这是最诚实的"内存"数字** —— 对象数会被
    小对象的复用掩盖，RSS 不会。"""
    try:
        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        # `ctypes.windll.psapi` 拿不到这个函数（第一版返回 0.0 就是这个原因）——
        # psapi 要显式 WinDLL 加载，而且得声明 argtypes。
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize / 1048576.0
    except Exception:                                    # noqa: BLE001
        pass
    return 0.0


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class FakeMusic:
    """假的音乐源 —— 让 `_music_tick` 那条路真的跑到，但不碰 SMTC。

    每 3 个模拟分钟换一首，这样"切歌反应"那条路也会被走到。
    """

    available = True
    last_error = None

    def __init__(self):
        self._t = 0.0
        self._i = 0
        self.np = None

    def start(self):
        pass

    def stop(self):
        pass

    def sessions(self):
        return ["fake.exe"]

    def poll(self):
        return self.np

    def toggle_play(self):
        return True

    def next_track(self):
        return True

    def prev_track(self):
        return True

    def advance(self, now: float):
        """**跟着空闲走** —— "你该困了"的窗口里没人放歌。

        第一版是"每 3 分钟换一首，7 次里 1 次停"，结果状态机里那条
        `if self._music_playing(): pass`（有人在放歌 = 有人在，不睡）
        让她 86% 的时间都睡不着 —— 长跑看到的全是 idle。

        **两个假源各自都对，合起来把要测的那条路堵死了。** 这和
        AGENTS.md 规则 1 记的第 3、4 次事故是同一个形状。

        真实世界也是这样：你离开电脑，音乐自然也停了。
        """
        idle = FakeIdle.seconds(now)
        if idle > 60:                                 # 你不在
            self.np = None
            return
        if now - self._t < 180:
            return
        self._t = now
        self._i += 1
        self.np = MU.NowPlaying(
            app="fake.exe", title=f"曲目 {self._i}", artist="测试",
            playing=True, duration=200, position=10, changed_at=now)


class FakeIdle:
    """按模拟时间造的"你多久没碰键鼠"。

    **这是必须的。** `idle_seconds()` 走 Windows 的 `GetLastInputInfo` ——
    真的系统空闲时间。写脚本时人一直在用电脑，它就一直是个小值，
    于是**她永远不困、永远不溜达**，长跑看到的全是 `idle`。
    陪伴模式状态机一半的输入就是这个值。

    周期 40 个模拟分钟，把每条路都走一遍。
    """

    CYCLE = 40 * 60
    # 相位锚点。**不能用绝对时间戳取模** —— 那样每次跑的相位是随机的，
    # 9 分钟的跑法可能整段落在"夜里"，于是她 95% 的时间在睡，
    # 判定就误报"卡住了"。锚在开始时刻才能让同样的参数跑出一样的结果。
    ANCHOR = 0.0

    @staticmethod
    def seconds(now: float) -> float:
        t = (now - FakeIdle.ANCHOR) % FakeIdle.CYCLE
        if t < 12 * 60:
            return 20.0                    # 醒着
        if t < 22 * 60:
            return 6 * 60.0                # 犯困 → 打盹
        if t < 39 * 60:
            return 25 * 60.0               # 睡着
        return 0.0                         # 你回来了 → 惊醒


class FakeActivity:
    """假的前台应用 —— 每 20 个模拟分钟换一个，把 9 类都轮一遍。"""

    POOL = ["code.exe", "chrome.exe", "potplayer.exe", "cloudmusic.exe",
            "zoom.exe", "steam.exe", "wechat.exe", "photoshop.exe", "winword.exe"]

    def __init__(self):
        self._t = 0.0
        self._i = 0
        self.cur = None

    def start(self):
        pass

    def stop(self):
        pass

    def poll(self):
        return self.cur

    def advance(self, now: float):
        if now - self._t < 1200:
            return
        self._t = now
        self._i += 1
        proc = self.POOL[self._i % len(self.POOL)]
        self.cur = AC.Activity(process=proc, title="", pid=1,
                               category=AC.classify(proc), since=now)


def build_app(skin: str = "openpets"):
    """建一个真的 PetApp，配上假的源。

    **屏蔽真实会话**，逼她进陪伴模式 —— 那才是状态机最复杂的地方
    （15 个状态 + roam 定时器 + 打盹/睡觉阈值 + 久坐提醒 + 音乐反应 +
    前台应用分类）。第一版没屏蔽，跑出来 `tools×35`，等于什么都没测。
    """
    args = argparse.Namespace(
        demo=False, force_state=None, session=None, shot=None, skin=skin,
        list_skins=False, privacy=False, no_companion=False, no_hotkeys=True)
    sys.modules["pystray"] = None
    _real_list = S.list_session_files
    S.list_session_files = lambda: []                    # 当没有 DSH
    try:
        app = P.PetApp(args)
    finally:
        S.list_session_files = _real_list
    app.root.update()
    # 建完之后也要继续屏蔽 —— `_poll` 每一轮都会查
    S.list_session_files = lambda: []
    fm, fa = FakeMusic(), FakeActivity()
    app.music = fm
    app.companion.music = fm
    app.companion.attach_sources(fm, fa)
    return app, fm, fa


def sample(app, fm, fa, now: float) -> dict:
    """采一次样。**都是便宜的读操作** —— 采样本身不能影响被观测的东西。"""
    m = app.companion.mood if app.companion else None
    return {
        "t": now,
        "rss": rss_mb(),
        "objs": len(gc.get_objects()),
        "threads": threading.active_count(),
        "state": app.snap.state,
        "history": len(getattr(m, "history", ()) or ()),
        "seen_slots": len(getattr(m, "seen_slots", ()) or ()),
        "busy_slots": len(getattr(m, "busy_slots", ()) or ()),
        "sprites": sum(len(v) for v in getattr(app, "sprites", {}).values()),
        "durations": len(getattr(app, "durations", {})),
        "tray": len(getattr(app, "_tray_actions", ()) or ()),
        "petdex": len(getattr(app, "_petdex_results", ()) or ()),
        "errlog": file_size(os.path.join(_SOAK_DIR, "pet_error.log")),
        "moodfile": file_size(os.path.join(_SOAK_DIR, "pet_mood.json")),
    }


def verdict(rows: list, warmup: int = 3, states: dict = None,
            covered_s: float = 1e9) -> tuple:
    """判定。**判断，不是评分表** —— 每条对应一个具体的失败。

    前 `warmup` 个样本不算 —— 启动时缓存要预热，那一段涨是正常的。
    """
    bad, warn = [], []
    if len(rows) <= warmup + 1:
        return bad, warn
    use = rows[warmup:]
    first, last = use[0], use[-1]

    # 内存：涨 60% 或 80MB 以上才算 —— 小幅波动是分配器行为
    dr = last["rss"] - first["rss"]
    if first["rss"] > 0 and (dr > 80 or dr / first["rss"] > 0.6):
        bad.append(f"内存涨了 {dr:.0f} MB（{first['rss']:.0f} → {last['rss']:.0f}）")

    do = last["objs"] - first["objs"]
    if first["objs"] > 0 and do / first["objs"] > 0.6:
        bad.append(f"对象数涨了 {do}（{first['objs']} → {last['objs']}）")

    if last["threads"] > first["threads"]:
        bad.append(f"线程数涨了（{first['threads']} → {last['threads']}）")

    # 卡死：某个状态占了 92% 以上。
    #
    # **用全量统计，不用稀疏采样** —— `roaming` 只持续 4~8 秒，
    # 1 分钟采一次根本采不到，会把"状态机在动"看成"只有两个状态"。
    # （第一版就是这样，判定误报。）
    if not states:
        states = {}
        for r in use:
            states[r["state"]] = states.get(r["state"], 0) + 1
    total = sum(states.values()) or 1
    top, n = max(states.items(), key=lambda kv: kv[1])

    # **不满一个作息周期就不判"卡死"。**
    # 一个周期是 40 分钟；不到一个周期，你分不清"她卡住了"和"她正在睡"。
    # CI 那个 9 分钟的跑法只查泄漏 —— 那是它的本职。
    if covered_s < FakeIdle.CYCLE:
        warn.append(f"只跑了 {covered_s / 60:.0f} 个模拟分钟（不足一个作息周期"
                    f" {FakeIdle.CYCLE // 60} 分钟），不判断是否卡死")
    else:
        if n / total > 0.92:
            bad.append(f"她 {n / total * 100:.0f}% 的时间都停在 {top!r} —— 卡住了？")
        # 陪伴模式该**换**状态。只有一个状态说明压根没进陪伴模式，
        # 或者状态机死了 —— 两种都值得报。
        if len(states) < 3:
            bad.append(f"整场只出现了 {len(states)} 个状态（{sorted(states)}）—— "
                       f"状态机没在动？")
    warn.append(f"状态分布：{dict(sorted(states.items(), key=lambda kv: -kv[1]))}")

    # 无界增长
    for key, label in (("history", "心情历史"), ("seen_slots", "见过的时间段"),
                       ("busy_slots", "忙碌时间段"), ("sprites", "精灵缓存"),
                       ("tray", "托盘队列"), ("petdex", "petdex 队列")):
        d = last[key] - first[key]
        if d > 200:
            bad.append(f"{label}涨了 {d}（{first[key]} → {last[key]}）")
        elif d > 50:
            warn.append(f"{label}涨了 {d}")

    if last["errlog"] > first["errlog"] + 4096:
        bad.append(f"错误日志涨了 {last['errlog'] - first['errlog']} 字节")

    return bad, warn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8.0, help="跑几个模拟小时")
    ap.add_argument("--skin", default="openpets")
    ap.add_argument("--quiet", action="store_true", help="只打结论")
    ap.add_argument("--json", default="", help="把样本写成 json 到这儿")
    args = ap.parse_args()

    I.set_language("zh")
    total_s = args.hours * 3600
    app, fm, fa = build_app(args.skin)

    # 受控时钟：真的 time.time() 只用来决定"什么时候采样"，
    # 喂给 app 的是模拟时间。三个模块都要 patch —— `from x import *`
    # 会把 time 模块对象复制进每个命名空间（规则 6/7 踩过）。
    base = time.time()
    clock = [base]
    FakeIdle.ANCHOR = base                            # 相位从 0 开始

    # **只换 `time()`，别的照旧。**
    # 第一版用一个只有 time() 的类把整个 `time` 模块替掉了 ——
    # 而 `pet_sources` 还要用 `time.localtime()` / `time.strftime()`，
    # 于是每轮 poll 都抛 `no attribute 'localtime'`，长跑跑的是个残废的 app。
    class Clock:
        def __getattr__(self, name):
            return getattr(time, name)                   # localtime / strftime / …

        @staticmethod
        def time():
            return clock[0]

    _clock = Clock()
    for mod in (P, S, MU):
        mod.time = _clock

    # 空闲时间也要按模拟时间走 —— 不然她永远不困（见 FakeIdle 的说明）。
    # 两个模块都要 patch：`dsh_pet` 是 `from pet_sources import *`，
    # 那是**另一份引用**（规则 6/7 踩过）。
    for mod in (P, S):
        mod.idle_seconds = lambda: FakeIdle.seconds(clock[0])
    app.root.after = lambda *a, **k: None      # 不让它自己排下一帧

    # 采样间隔跟着总时长走 —— 12 分钟的跑法每 10 分钟采一次只有 2 个点，
    # 看不出任何趋势（第一版就是这样，状态分布只统计到 2 个样本）。
    sample_every = max(30.0, total_s / SAMPLE_TARGET)

    # 状态**每个 tick 都记** —— 字符串比较，比采样便宜得多。
    # RSS / 对象数那些贵的才按间隔采。
    state_counts: dict = {}

    rows = []
    t0 = time.perf_counter()
    sim = 0.0
    nxt_sample = 0.0
    nxt_poll = 0.0
    iters = 0

    if not args.quiet:
        print(f"  跑 {args.hours:g} 个模拟小时（{int(total_s / STEP):,} 帧 @ {FPS}fps）")
        print(f"  皮肤 {args.skin}   临时目录 {_SOAK_DIR}")
        print(f"  {'模拟时间':>10} {'RSS':>8} {'对象':>9} {'线程':>5} {'状态':>10}")
        print("  " + "─" * 52)

    while sim < total_s:
        clock[0] = base + sim
        fm.advance(clock[0])
        fa.advance(clock[0])
        app._tick()
        st = app.snap.state
        state_counts[st] = state_counts.get(st, 0) + 1
        if sim >= nxt_poll:
            nxt_poll = sim + 1.0                 # _poll 是 1 秒一次
            app._poll()
        iters += 1
        sim += STEP

        if sim >= nxt_sample:
            nxt_sample = sim + sample_every
            r = sample(app, fm, fa, sim)
            rows.append(r)
            if not args.quiet:
                hh = sim / 3600
                print(f"  {hh:>9.1f}h {r['rss']:>7.1f}M {r['objs']:>9,} "
                      f"{r['threads']:>5} {r['state']:>10}")

    wall = time.perf_counter() - t0
    bad, warn = verdict(rows, states=state_counts, covered_s=total_s)

    if not args.quiet:
        print()
    print(f"  ── 跑完 {args.hours:g} 个模拟小时 ──")
    # 倍速 = 模拟秒 / 实际秒。第一版多除了一个 60，把 55x 显示成 1x。
    print(f"  {iters:,} 帧，实际耗时 {wall:.0f} 秒"
          f"（{iters / max(wall, 0.001):,.0f} 帧/秒，"
          f"{total_s / max(wall, 0.001):.0f}x 实时）")

    if rows:
        f, l = rows[0], rows[-1]
        print(f"  内存 {f['rss']:.0f} → {l['rss']:.0f} MB    "
              f"对象 {f['objs']:,} → {l['objs']:,}    "
              f"线程 {f['threads']} → {l['threads']}")
        top = sorted(state_counts.items(), key=lambda kv: -kv[1])[:8]
        pct = {k: v / max(1, iters) * 100 for k, v in top}
        print("  状态分布: " + "  ".join(
            f"{k} {pct[k]:.1f}%" for k, _v in top))
        if len(state_counts) < 3:
            print(f"  ⚠ 只出现了 {len(state_counts)} 个状态")

    for w in warn:
        print(f"  ⚠ {w}")
    if bad:
        print()
        for b in bad:
            print(f"  ❌ {b}")
        print("\n  长跑不通过。")
        rc = 1
    else:
        print("\n  ✅ 没看出泄漏或卡死。")
        rc = 0

    if args.json:
        with io.open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print(f"  样本 -> {args.json}")

    try:
        app._quit()
    except Exception:                                    # noqa: BLE001
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
