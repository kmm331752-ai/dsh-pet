"""DSH 桌宠的回归测试。

    python -m pytest tests/ -q          （需要 pytest）
    python tests/test_pet.py            （不装 pytest 也能跑，自己当脚本执行）

只测**不依赖窗口**的部分：状态机、渲染、解析、交互参数。
开窗口、托盘、热键那些得手工看，这里不碰。

为什么值得有这些：状态机和渲染是改一处就可能悄悄坏掉的地方 ——
比如「看向鼠标」用了镜像翻转，如果哪天 sprite 变了类型，
只有真正渲染一帧才会发现。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dsh_pet as P  # noqa: E402


class TestStates(unittest.TestCase):
    def test_state_order_covers_every_palette_entry(self):
        """每个状态都要有配色 —— _glow_layer 会直接索引 PALETTE[state]。"""
        for st in P.STATE_ORDER:
            self.assertIn(st, P.PALETTE, f"状态 {st} 没有配色")

    def test_companion_states_are_in_state_order(self):
        for st in P.COMPANION_STATES:
            self.assertIn(st, P.STATE_ORDER)

    def test_every_skin_can_render_companion_states(self):
        """三种素材格式都要能渲染陪伴状态，不然切皮肤就崩。"""
        for sid in ("dafeiyu", "kun-like", "matcha-sunny"):
            skin = P.load_skin(sid)
            for st in P.COMPANION_STATES:
                with self.subTest(skin=sid, state=st):
                    self.assertTrue(skin.frames(st), f"{sid} 渲染不出 {st}")

    def test_view_map_covers_all_states(self):
        for st in P.STATE_ORDER:
            self.assertIn(st, P.VIEW_FOR_STATE, f"{st} 没有视图映射")


class TestOverlays(unittest.TestCase):
    KINDS = ("stars", "zzz", "question", "angry", "heart", "sweat", "note")

    def test_all_overlays_render(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                img = P.overlay_for(kind, 0.4)
                self.assertIsNotNone(img)
                self.assertEqual(img.size, (P.WIN_W, P.PET_AREA_H))

    def test_empty_kind_is_none(self):
        self.assertIsNone(P.overlay_for("", 0.4))

    def test_overlay_actually_draws_something(self):
        """空图和真图形要能区分 —— 防止哪天坐标算错画到画布外。"""
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                img = P.overlay_for(kind, 0.3)
                self.assertIsNotNone(img.getbbox(), f"{kind} 什么都没画出来")


class TestSessionDots(unittest.TestCase):
    def test_needs_two_tasks(self):
        self.assertIsNone(P.session_dots([]))
        self.assertIsNone(P.session_dots([P.TaskInfo()]))

    def test_draws_for_multiple(self):
        dots = P.session_dots([P.TaskInfo(state="tools"), P.TaskInfo(state="waiting")])
        self.assertIsNotNone(dots)
        self.assertIsNotNone(dots.getbbox())

    def test_overflow_is_capped(self):
        many = [P.TaskInfo(state="tools") for _ in range(15)]
        self.assertIsNotNone(P.session_dots(many))


class TestCompanionStateMachine(unittest.TestCase):
    def setUp(self):
        self.c = P.CompanionSource()

    def _poll(self, idle, night=False, energy=1.0, mouse_at=None, mouse_x=None):
        self.c.state = "idle"
        self.c.state_until = 0
        self.c.mood.energy = energy
        self.c.next_roam = self.c.next_peek = time.time() + 9999
        # 默认假装鼠标很久没动过（不触发"看向鼠标"）；
        # 要测 gaze 就显式传 mouse_at。
        self.c.mouse_at = 0.0 if mouse_at is None else mouse_at
        if mouse_x is not None:
            self.c.mouse_x = mouse_x
        with mock.patch.object(P, "idle_seconds", lambda v=idle: v), \
             mock.patch.object(P.CompanionSource, "_night", lambda _s, n=night: n):
            return self.c.poll()

    def test_idle_when_active(self):
        self.assertEqual(self._poll(1.0).state, "idle")

    def test_yawn_doze_sleep_ladder(self):
        self.assertEqual(self._poll(P.IDLE_YAWN + 5).state, "yawning")
        self.assertEqual(self._poll(P.IDLE_DOZE + 5).state, "dozing")
        self.assertEqual(self._poll(P.IDLE_SLEEP + 5).state, "sleeping")

    def test_night_sleeps_sooner(self):
        self.assertEqual(self._poll(P.NIGHT_IDLE_SLEEP + 5, night=True).state, "sleeping")

    def test_gaze_mirrors_toward_mouse(self):
        now = time.time()
        self.c.pet_x = 100
        # 鼠标在右边：转头看，不镜像
        snap = self._poll(1.0, mouse_at=now, mouse_x=1200)
        self.assertEqual(snap.state, "gazing")
        self.assertFalse(snap.mirror)
        # 鼠标在左边：同样转头，但要镜像
        snap = self._poll(1.0, mouse_at=now, mouse_x=-500)
        self.assertEqual(snap.state, "gazing")
        self.assertTrue(snap.mirror, "鼠标在左边时应该镜像")

    def test_gaze_ignored_when_mouse_stale(self):
        """鼠标很久没动就不该转头 —— 否则她会一直盯着一个不动的光标。"""
        snap = self._poll(1.0, mouse_at=time.time() - 60, mouse_x=1200)
        self.assertEqual(snap.state, "idle")

    def test_gaze_ignored_when_mouse_too_close(self):
        snap = self._poll(1.0, mouse_at=time.time(), mouse_x=self.c.pet_x + 10)
        self.assertEqual(snap.state, "idle")

    def test_wander_target_inside_virtual_screen(self):
        vx, _vy, vw, _vh = P.virtual_screen()
        self.c.pet_x = 800
        for _ in range(20):
            t = self.c._pick_wander_target()
            self.assertGreaterEqual(t, vx)
            self.assertLessEqual(t, vx + vw - P.WIN_W)

    def test_situational_runs_in_every_hour(self):
        """把 24 小时全跑一遍。

        这条是 CI 逼出来的：早晚问候只在 5~11 点或 22 点后触发，
        本地下午跑测试永远走不到那行 —— 而那里真的写着 self.greeted
        （应该读 self.mood.greeted），在 CI 的 UTC 早晨直接 AttributeError。
        时间相关的分支必须把时间穷举掉，不能靠"我这儿跑过了"。
        """
        for hour in range(24):
            with self.subTest(hour=hour):
                fake = time.struct_time((2026, 1, 1, hour, 0, 0, 3, 1, -1))
                with mock.patch.object(P.time, "localtime", lambda _f=fake: _f):
                    self._poll(1.0)
                    self._poll(1.0, night=(hour >= 22 or hour < 2))

    def test_greeting_is_recorded_on_mood_not_source(self):
        """问候标记必须落在 Mood 上 —— 否则重启就忘了，每天重复问候。"""
        fake = time.struct_time((2026, 1, 1, 8, 0, 0, 3, 1, -1))
        self.c.mood.greeted = ""
        self.c.state = "idle"
        self.c.state_until = 0
        with mock.patch.object(P.time, "localtime", lambda: fake):
            self.c._situational(time.time(), 1.0, False)
        self.assertEqual(self.c.mood.greeted, "morning")
        self.assertFalse(hasattr(self.c, "greeted"), "不该在 CompanionSource 上另开一个属性")


class TestMood(unittest.TestCase):
    def test_mood_round_trips(self):
        import dataclasses
        names = {f.name for f in dataclasses.fields(P.Mood)}
        self.assertTrue({"affection", "energy", "greeted", "unlocked"} <= names)

    def test_mood_label_bands(self):
        m = P.Mood()
        for val, expect in ((0.9, "特别黏你"), (0.5, "还不错"), (0.05, "好久没理我了")):
            m.affection = val
            self.assertEqual(m.mood_label, expect)

    def test_touch_raises_affection_and_wakes(self):
        c = P.CompanionSource()
        c.mood.affection = 0.5
        c.state = "sleeping"
        c.state_until = 0
        c.touch("feed")
        self.assertGreater(c.mood.affection, 0.5)
        self.assertEqual(c.state, "waking")


class TestLogTail(unittest.TestCase):
    """用真实日志跑；机器上没有 DSH 会话就跳过。"""

    def _newest_log(self):
        import glob
        files = glob.glob(os.path.join(P.SESS_DIR, "*", "session-*", "session*.jsonl.zstd"))
        if not files:
            self.skipTest("没有 DSH 会话日志")
        return max(files, key=os.path.getmtime)

    def test_parses_tools_from_real_log(self):
        tail = P.LogTail(self._newest_log())
        tail.poll()
        self.assertTrue(tail.calls, "一个工具调用都没解析出来")

    def test_tool_durations_are_sane(self):
        """耗时必须是正的、且不是几十天。

        这里曾经真的错过：早期按 callId 配对，而 callId 里的 call_00_
        是每回合重置的序号，跨回合重复，配错就算出 61 天的耗时。
        改成按 sourceEventSeqs 关联才对。
        """
        tail = P.LogTail(self._newest_log())
        tail.poll()
        for name, durs in tail.tool_times.items():
            for d in durs:
                with self.subTest(tool=name):
                    self.assertGreater(d, 0)
                    self.assertLess(d, 3_600_000, f"{name} 耗时超过 1 小时，八成配错了")

    def test_slowest_is_sorted(self):
        tail = P.LogTail(self._newest_log())
        tail.poll()
        ranked = tail.slowest(3)
        self.assertLessEqual(len(ranked), 3)
        vals = [ms for _n, ms in ranked]
        self.assertEqual(vals, sorted(vals, reverse=True))


class TestRendering(unittest.TestCase):
    def test_render_pet_all_states(self):
        for st in P.STATE_ORDER:
            with self.subTest(state=st):
                img = P.render_pet(st, 0.3)
                self.assertEqual(img.size, (P.WIN_W, P.PET_AREA_H))

    def test_card_with_and_without_detail(self):
        snap = P.Snapshot(state="tools", title="测试", model="m", provider="p",
                          slow_tools=[("job_output", 600_000), ("grep", 160)])
        for detail in (False, True):
            with self.subTest(detail=detail):
                card = P.render_card(snap, detail, False)
                self.assertEqual(card.size[0], P.WIN_W)
                self.assertGreater(card.size[1], 0)

    def test_card_privacy_mode(self):
        snap = P.Snapshot(state="tools", title="机密标题", prompt="机密提示词")
        card = P.render_card(snap, True, True)
        self.assertEqual(card.size[0], P.WIN_W)


class TestGeometry(unittest.TestCase):
    def test_clamp_pulls_back_onto_screen(self):
        vx, vy, vw, vh = P.virtual_screen()
        x, y = P.clamp_position(vx - 99999, vy - 99999)
        self.assertGreaterEqual(y, vy)
        x2, y2 = P.clamp_position(vx + vw + 99999, vy + vh + 99999)
        self.assertLessEqual(x2, vx + vw)
        self.assertLessEqual(y2, vy + vh)

    def test_default_position_is_on_screen(self):
        x, y = P.default_position()
        self.assertGreater(x, 0)
        self.assertGreater(y, 0)


class TestHotkeys(unittest.TestCase):
    def test_hotkey_table(self):
        self.assertGreaterEqual(len(P.HOTKEYS), 5)
        ids = set()
        for hid, (mods, vk, action) in P.HOTKEYS.items():
            self.assertNotIn(hid, ids, "热键 id 重复")
            ids.add(hid)
            self.assertIsInstance(mods, int)
            self.assertIsInstance(vk, int)
            self.assertIsInstance(action, str)

    def test_cursor_pos_returns_pair(self):
        pos = P.cursor_pos()
        self.assertEqual(len(pos), 2)


class TestSourceSanity(unittest.TestCase):
    """只读源码的检查，不跑代码。

    这里放的是「加功能时手滑删了一行」那类错误 —— 跑起来才发现，
    而且往往被别的报错盖住。静态检查能提前拦住。
    """

    @classmethod
    def setUpClass(cls):
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "dsh_pet.py")
        with open(path, encoding="utf-8") as fh:
            cls.src = fh.read()
        cls.re = re

    def test_every_cascade_menu_is_created(self):
        """add_cascade 引用的每个 self._x_menu 都要有对应的创建语句。

        这条是踩出来的：加番茄钟菜单时把
            self._skin_menu = self.tk.Menu(self.menu, tearoff=0)
        那行删掉了，于是 __init__ 里的 add_cascade 直接 AttributeError ——
        桌宠每次启动都崩。更糟的是当时同时有端口锁冲突，报错被盖住，
        我误以为只是锁的问题就放过了。
        """
        used = set(self.re.findall(r"add_cascade\([^)]*menu=self\.(\w+_menu)", self.src))
        made = set(self.re.findall(r"self\.(\w+_menu)\s*=\s*self\.tk\.Menu", self.src))
        self.assertTrue(used, "一个 cascade 菜单都没找到，检查方式可能失效了")
        self.assertEqual(used - made, set(),
                         f"这些菜单被引用但没有创建：{sorted(used - made)}")

    def test_mood_fields_are_not_read_from_source(self):
        """只定义在 Mood 上的字段，不该被写成 self.<字段>。

        greeted / unlocked 属于 Mood（要跨次启动记住）。曾经写成
        self.greeted，早上或深夜启动就 AttributeError —— 也是 CI 抓到的。
        """
        import dataclasses
        mood_fields = {f.name for f in dataclasses.fields(P.Mood)}
        for field in ("greeted", "unlocked"):
            self.assertIn(field, mood_fields)
            with self.subTest(field=field):
                self.assertNotIn(f"self.{field}", self.src,
                                 f"应该写 self.mood.{field}，不是 self.{field}")

    def test_all_line_tables_are_wired(self):
        """台词表里的每一组都要真被用到，避免加了表却忘了接线。

        允许条件形式（say_line("A" if x else "B")），所以匹配的是
        「这个键作为字符串出现在某次 say_line 调用里」而不是死板的
        say_line("KEY")。第一版写死了，把 MORNING/NIGHT 误报成没接线 ——
        而它同时也漏掉了 DIZZY/GRAB/EDGE 这三组真的没接的。
        """
        for key in P.LINES:
            with self.subTest(key=key):
                self.assertRegex(
                    self.src, rf'say_line\([^)]*"{key}"',
                    f"{key} 定义了但没有任何地方 say_line 它")


class TestErrorLog(unittest.TestCase):
    """出错日志：打包成 exe 之后 stderr 是黑洞，异常必须落盘。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-pet-err-")
        self._orig = P.ERROR_LOG
        P.ERROR_LOG = os.path.join(self.tmp, "pet_error.log")
        P._last_stderr.clear()

    def tearDown(self):
        P.ERROR_LOG = self._orig
        P._last_stderr.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_log_reads_empty(self):
        self.assertEqual(P.read_error_log(), "")

    def test_records_where_message_and_traceback(self):
        try:
            raise ValueError("测试异常消息")
        except ValueError as exc:
            P.log_error("测试位置", exc)
        text = P.read_error_log()
        self.assertIn("测试位置", text)
        self.assertIn("测试异常消息", text)
        self.assertIn("Traceback", text)

    def test_stderr_is_throttled_but_file_is_not(self):
        """同一个位置连续报错：控制台只出一行，文件一条不漏。

        _tick 是 30ms 一次的循环，它要是坏了而没节流，终端会被瞬间刷爆。
        """
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            for _ in range(50):
                try:
                    raise RuntimeError("重复错误")
                except RuntimeError as exc:
                    P.log_error("同一个位置", exc)
        self.assertEqual(len([ln for ln in buf.getvalue().splitlines() if ln.strip()]), 1)
        with open(P.ERROR_LOG, encoding="utf-8") as fh:
            self.assertEqual(fh.read().count("同一个位置"), 50)

    def test_log_is_truncated_instead_of_growing_forever(self):
        for i in range(300):
            try:
                raise RuntimeError("x" * 400)
            except RuntimeError as exc:
                P.log_error(f"填充 {i}", exc)
        size = os.path.getsize(P.ERROR_LOG)
        self.assertLess(size, P.ERROR_LOG_MAX * 1.5,
                        "日志没有截断，长期运行会无限增长")

    def test_log_error_never_raises(self):
        """日志函数本身绝不能抛 —— 它已经在处理异常了。"""
        class Bad(Exception):
            def __str__(self):
                raise RuntimeError("连 __str__ 都坏了")
        try:
            P.log_error("坏异常", Bad())
        except Exception as exc:                         # noqa: BLE001
            self.fail(f"log_error 抛了：{exc}")

    def test_excepthook_is_installed(self):
        before = sys.excepthook
        P.install_excepthook()
        self.assertIsNot(sys.excepthook, before)
        # 钩子本身要能吞下异常并落盘
        sys.excepthook(ValueError, ValueError("钩子测试"), None)
        self.assertIn("钩子测试", P.read_error_log())
        sys.excepthook = before


class TestConfig(unittest.TestCase):
    def test_paths_are_absolute(self):
        for p in (P.CONFIG_PATH, P.MOOD_PATH, P.ASSETS):
            self.assertTrue(os.path.isabs(p), p)

    def test_assets_exist(self):
        self.assertTrue(os.path.isdir(P.ASSETS))
        for view in ("front.png", "side.png", "back.png"):
            self.assertTrue(os.path.exists(os.path.join(P.ASSETS, view)), view)

    def test_discover_skins_finds_builtin(self):
        ids = {s["id"] for s in P.discover_skins()}
        self.assertIn("dafeiyu", ids)
        self.assertIn("kun-like", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
