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

import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock as mock
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dsh_pet as P  # noqa: E402
import pet_accessories as A  # noqa: E402
import pet_base as B  # noqa: E402
import pet_chat as C  # noqa: E402
import pet_i18n as I  # noqa: E402
import pet_platform as PF  # noqa: E402
import pet_sources as S  # noqa: E402


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
        # 注意补丁打在哪：idle_seconds 和 _night 都是 **pet_sources** 里的名字。
        # 拆文件之前是 P.idle_seconds，拆开后 poll 解析的是 pet_sources 的全局，
        # 打 dsh_pet 那份不起作用（踩过：状态机测试全返回 idle）。
        with mock.patch.object(S, "idle_seconds", lambda v=idle: v), \
             mock.patch.object(S.CompanionSource, "_night", lambda _s, n=night: n):
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
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # 拆文件之后要扫全部模块，不能只看 dsh_pet.py ——
        # 台词表和菜单都在别的模块里（踩过：只扫主文件导致全部误报）。
        cls.src = ""
        for name in ("pet_base.py", "pet_types.py", "pet_render.py",
                     "pet_sources.py", "pet_system.py", "dsh_pet.py"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                cls.src += fh.read() + "\n"
        cls.re = re

    def test_every_cascade_menu_is_created(self):
        """add_cascade 引用的每个 self._x_menu 都要有对应的创建语句。

        这条是踩出来的：加番茄钟菜单时把
            self._skin_menu = self.tk.Menu(self.menu, tearoff=0)
        那行删掉了，于是 __init__ 里的 add_cascade 直接 AttributeError ——
        桌宠每次启动都崩。更糟的是当时同时有端口锁冲突，报错被盖住，
        我误以为只是锁的问题就放过了。
        """
        # menu=self._x_menu 前面可能有 t("...") 这种带括号的东西，
        # 所以不能用 [^)]* 卡（会被 t("皮肤") 的右括号截断）。
        used = set(self.re.findall(r"add_cascade\(.*?menu=self\.(\w+_menu)", self.src))
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
        # 改 data_dir 而不是 ERROR_LOG —— 拆文件之后常量会被复制进各模块，
        # 改 dsh_pet 里的那份不影响 pet_base 里的，会静默失效。
        self._orig_data_dir = B.data_dir
        B.data_dir = lambda: self.tmp
        B._last_stderr.clear()

    def tearDown(self):
        B.data_dir = self._orig_data_dir
        B._last_stderr.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log_path(self) -> str:
        return B.error_log_path()

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
        with open(self._log_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read().count("同一个位置"), 50)

    def test_log_is_truncated_instead_of_growing_forever(self):
        for i in range(300):
            try:
                raise RuntimeError("x" * 400)
            except RuntimeError as exc:
                P.log_error(f"填充 {i}", exc)
        size = os.path.getsize(self._log_path())
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


class TestSkinInstall(unittest.TestCase):
    """皮肤安装器。重点是**安全边界** —— 用户会从各种地方拿素材包。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-pet-skin-")
        import pet_skin_install as SI
        self.SI = SI
        # 改 assets_dir（唯一真实来源），不是 SI.SKINS_DIR 也不是 B.skins_dir ——
        # skins_dir() 的函数体在 pet_base 的命名空间里解析 assets_dir，
        # 所以只要改这一个，pet_render 和 pet_skin_install 看到的都跟着变。
        self._orig = B.assets_dir
        B.assets_dir = lambda: os.path.join(self.tmp, "assets")

    def tearDown(self):
        B.assets_dir = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _zip(self, name, files):
        path = os.path.join(self.tmp, name)
        with zipfile.ZipFile(path, "w") as z:
            for n, d in files.items():
                z.writestr(n, d)
        return path

    def _valid_skin_zip(self, name="good.zip"):
        """造一张符合 Codex 契约的精灵图（8 列 × 192 宽，≥9 行，格子 192×208）。"""
        from PIL import Image as _I
        sheet = os.path.join(self.tmp, "spritesheet.png")
        img = _I.new("RGBA", (8 * 192, 9 * 208), (0, 0, 0, 0))
        for r in range(9):
            for c in range(8):
                img.paste((80, 160, 220, 255),
                          (c * 192 + 40, r * 208 + 40, c * 192 + 152, r * 208 + 168))
        img.save(sheet)
        path = os.path.join(self.tmp, name)
        with zipfile.ZipFile(path, "w") as z:
            z.write(sheet, "myskin/spritesheet.png")
        return path

    # ── 正常路径 ──
    def test_installs_and_is_recognized(self):
        sid, _msg = self.SI.install_skin(self._valid_skin_zip(), "test-skin")
        self.assertEqual(sid, "test-skin")
        self.assertTrue(os.path.isdir(os.path.join(B.skins_dir(), "test-skin")))
        # 装完必须能被 discover_skins 认出来 —— 这是安装器最后一道自检
        self.assertIn("test-skin", {s["id"] for s in P.discover_skins()})

    def test_installed_skin_actually_renders(self):
        self.SI.install_skin(self._valid_skin_zip(), "test-skin")
        skin = P.load_skin("test-skin")
        self.assertTrue(skin.frames("idle"), "装上了但渲染不出帧")

    def test_nested_single_dir_is_unwrapped(self):
        """压缩包里套一层目录是常态，要能找到真正的皮肤根。"""
        sid, _ = self.SI.install_skin(self._valid_skin_zip("nested.zip"), "nested")
        self.assertTrue(os.path.exists(
            os.path.join(B.skins_dir(), sid, "spritesheet.png")))

    def test_duplicate_is_rejected(self):
        z = self._valid_skin_zip()
        self.SI.install_skin(z, "dup")
        with self.assertRaises(self.SI.SkinInstallError) as cm:
            self.SI.install_skin(z, "dup")
        self.assertIn("已经装过", str(cm.exception))

    def test_remove_works_and_builtin_is_protected(self):
        self.SI.install_skin(self._valid_skin_zip(), "gone")
        self.SI.remove_skin("gone")
        self.assertFalse(os.path.isdir(os.path.join(B.skins_dir(), "gone")))
        for builtin in ("dafeiyu", "kun-like", "matcha-sunny"):
            with self.subTest(skin=builtin):
                with self.assertRaises(self.SI.SkinInstallError):
                    self.SI.remove_skin(builtin)

    # ── 安全边界 ──
    def test_zip_slip_is_rejected(self):
        """zip 里写 ../../ 想把文件放到皮肤目录外面。

        这是真会遇到的：素材包来源五花八门，打包工具也可能出错。
        """
        z = self._zip("evil.zip", {"../../evil.txt": "pwned", "theme.json": "{}"})
        with self.assertRaises(self.SI.SkinInstallError):
            self.SI.install_skin(z, "evil")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmp), "evil.txt")))

    def test_absolute_path_is_rejected(self):
        z = self._zip("abs.zip", {"C:/windows/temp/x.txt": "x"})
        with self.assertRaises(self.SI.SkinInstallError):
            self.SI.install_skin(z, "abs")

    def test_non_skin_zip_is_rejected_and_cleaned(self):
        z = self._zip("notskin.zip", {"readme.txt": "hello", "a/b/c.txt": "x"})
        with self.assertRaises(self.SI.SkinInstallError) as cm:
            self.SI.install_skin(z, "notskin")
        self.assertIn("没找到皮肤", str(cm.exception))
        self.assertFalse(os.path.isdir(os.path.join(B.skins_dir(), "notskin")))

    def test_empty_and_missing_are_rejected(self):
        with zipfile.ZipFile(os.path.join(self.tmp, "empty.zip"), "w"):
            pass
        with self.assertRaises(self.SI.SkinInstallError):
            self.SI.install_skin(os.path.join(self.tmp, "empty.zip"), "e")
        with self.assertRaises(self.SI.SkinInstallError) as cm:
            self.SI.install_skin(os.path.join(self.tmp, "nope.zip"), "n")
        self.assertIn("找不到", str(cm.exception))

    def test_oversized_declared_download_is_rejected(self):
        """压缩炸弹：声明解压后超大就直接拒绝，别真解出来。"""
        z = os.path.join(self.tmp, "bomb.zip")
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("spritesheet.png", b"\0" * (self.SI.MAX_EXTRACTED + 1024))
        with self.assertRaises(self.SI.SkinInstallError) as cm:
            self.SI.install_skin(z, "bomb")
        self.assertIn("太大", str(cm.exception))

    def test_slug_makes_safe_dir_names(self):
        self.assertEqual(self.SI._slug("My Cool Skin!! v2"), "my-cool-skin-v2")
        self.assertEqual(self.SI._slug("!!!"), "skin")
        self.assertNotIn("/", self.SI._slug("a/b\\c"))

    def test_markers_match_what_discover_recognizes(self):
        """安装器的标志文件必须和 discover_skins 认的一致。

        踩过：第一版写成 sprite.png / sheet.png，契约皮肤其实是
        spritesheet.png —— 结果每次安装都被自己的自检回滚掉。
        """
        import re
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "pet_render.py"), encoding="utf-8").read()
        for marker in self.SI.SKIN_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(f'"{marker}"', src,
                              f"{marker} 不在 pet_render 里，discover_skins 认不出来")


class TestDpiAndSound(unittest.TestCase):
    def test_dpi_scale_is_sane(self):
        d = B.dpi_scale()
        self.assertGreaterEqual(d, 1.0)
        self.assertLessEqual(d, 4.0)

    def test_set_dpi_awareness_is_safe_to_call(self):
        self.assertIsInstance(B.set_dpi_awareness(), bool)

    def test_sound_aliases_cover_the_events(self):
        for ev in ("done", "failed", "waiting", "pomo", "feed", "pet"):
            self.assertIn(ev, B.SOUND_ALIASES)

    def test_sounds_dir_follows_data_dir(self):
        self.assertTrue(B.sounds_dir().endswith("sounds"))


class TestSchedule(unittest.TestCase):
    """作息学习：从「DSH 忙不忙」推出你几点在、几点不在。"""

    def setUp(self):
        # CompanionSource 会读写 pet_mood.json —— 不隔离的话
        # 测试会污染你真实的相处记录（而且互相干扰：这条一开始就是
        # 读到了别的测试留下的样本才失败的）。
        self.tmp = tempfile.mkdtemp(prefix="dsh-pet-sched-")
        self._orig = B.data_dir
        B.data_dir = lambda: self.tmp

    def tearDown(self):
        B.data_dir = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_starts_unknown(self):
        m = S.Mood()
        self.assertEqual(len(m.busy_slots), 24)
        self.assertEqual(len(m.seen_slots), 24)
        self.assertEqual(m.active_ratio(9), -1.0, "没样本时不该给比例")
        self.assertFalse(m.is_usually_idle(9), "没样本时保守当作你在")
        self.assertFalse(m.schedule_known())

    def test_learns_busy_and_idle_hours(self):
        m = S.Mood()
        for _ in range(40):
            m.note_activity(9, True)
            m.note_activity(3, False)
        self.assertAlmostEqual(m.active_ratio(9), 1.0)
        self.assertAlmostEqual(m.active_ratio(3), 0.0)
        self.assertFalse(m.is_usually_idle(9))
        self.assertTrue(m.is_usually_idle(3))

    def test_bad_input_is_ignored(self):
        m = S.Mood()
        m.note_activity(99, True)
        m.note_activity(-1, True)
        self.assertEqual(sum(m.seen_slots), 0)
        self.assertEqual(m.active_ratio(99), -1.0)

    def test_corrupt_slots_are_repaired(self):
        """手改坏了 json 也不该崩 —— 长度不对就重置。"""
        m = S.Mood()
        m.busy_slots = [1, 2, 3]
        m.seen_slots = "坏了"
        m.note_activity(5, True)
        self.assertEqual(len(m.busy_slots), 24)
        self.assertEqual(len(m.seen_slots), 24)

    def test_schedule_known_needs_six_hours(self):
        """门槛按钟点算，不是按总样本 —— 轮询 350ms 一次，
        总样本 120 个才 42 秒，摊到 24 格里每小时 5 个，什么都说明不了。"""
        m = S.Mood()
        for h in range(5):
            for _ in range(40):
                m.note_activity(h, True)
        self.assertFalse(m.schedule_known(), "只有 5 个时段够样本")
        for _ in range(40):
            m.note_activity(5, True)
        self.assertTrue(m.schedule_known(), "6 个时段够了")

    def test_night_uses_learned_schedule(self):
        """夜班的人凌晨也在 —— 学到的作息要能推翻钟点判据。"""
        c = S.CompanionSource()
        # 中午通常不在 -> 算 night
        m = S.Mood()
        for _ in range(40):
            m.note_activity(12, False)
        c.mood = m
        noon = time.struct_time((2026, 1, 1, 12, 0, 0, 3, 1, -1))
        with mock.patch.object(S.time, "localtime", lambda: noon):
            self.assertTrue(c._night())
        # 中午通常都在 -> 不算 night
        m2 = S.Mood()
        for _ in range(40):
            m2.note_activity(12, True)
        c.mood = m2
        with mock.patch.object(S.time, "localtime", lambda: noon):
            self.assertFalse(c._night())

    def test_schedule_chart(self):
        m = S.Mood()
        for h in range(24):
            for _ in range(35):
                m.note_activity(h, 9 <= h < 19)          # 上班族
        txt = m.schedule_text()
        # "0" + 24 格 + "23"
        self.assertEqual(len(txt), 27, txt)
        self.assertEqual(txt[1 + 9], "█", "9 点该标常在")
        self.assertEqual(txt[1 + 3], "░", "3 点该标不在")
        self.assertIn("观察", S.Mood().schedule_text(), "没样本时该说还在观察")

    def test_round_trips_through_json(self):
        c = S.CompanionSource()
        c.mood.note_activity(9, True)
        c.mood.note_activity(9, True)
        c.save()
        again = S.CompanionSource()
        self.assertEqual(again.mood.seen_slots[9], 2)
        self.assertEqual(again.mood.busy_slots[9], 2)


class TestAccessories(unittest.TestCase):
    def test_table_is_well_formed(self):
        self.assertIn("none", A.ACCESSORIES)
        self.assertIsNone(A.ACCESSORIES["none"][1], "none 不该有绘制函数")
        for aid, (label, fn) in A.ACCESSORIES.items():
            with self.subTest(aid=aid):
                self.assertTrue(label, "每个配饰都要有显示名")
                if aid != "none":
                    self.assertTrue(callable(fn))

    def test_every_accessory_draws_something(self):
        from PIL import ImageChops
        base = P.render_pet("idle", 0.25)
        for aid in A.accessory_ids():
            if aid == "none":
                continue
            with self.subTest(aid=aid):
                img = base.copy()
                bb = A.character_box(img)
                drew = A.draw_accessory(img, aid, (bb[0] + bb[2]) / 2,
                                        float(bb[1]), float(bb[3] - bb[1]), 1.0, 0.25)
                self.assertTrue(drew, f"{aid} 没画上")
                diff = ImageChops.difference(img.convert("RGB"),
                                             base.convert("RGB")).getbbox()
                self.assertIsNotNone(diff, f"{aid} 画了但画面没变")

    def test_accessories_stay_inside_the_canvas(self):
        """配饰不能顶出画布 —— 头顶到画布顶只剩 26px。

        第一版尖顶帽用了 bh*0.34 ≈ 52px，直接被切掉一截。
        """
        base = P.render_pet("idle", 0.25)
        for aid in A.accessory_ids():
            if aid == "none":
                continue
            with self.subTest(aid=aid):
                img = base.copy()
                bb = A.character_box(img)
                A.draw_accessory(img, aid, (bb[0] + bb[2]) / 2,
                                 float(bb[1]), float(bb[3] - bb[1]), 1.0, 0.25)
                nb = A.character_box(img)
                self.assertGreater(nb[1], 0, f"{aid} 被画布顶部切掉了")

    def test_unknown_id_is_a_no_op(self):
        img = P.render_pet("idle", 0.25)
        for bad in ("", "不存在", None):
            with self.subTest(bad=bad):
                self.assertFalse(A.draw_accessory(img, bad, 124, 26, 155))

    def test_character_box_excludes_the_glow(self):
        """getbbox() 会把光晕算进去，比角色本体高 2~10px（各皮肤不同）。
        照它定位的话所有配饰都会飘在头上。"""
        for sid in ("dafeiyu", "kun-like", "matcha-sunny"):
            frames = P.load_skin(sid).frames("idle")
            if not frames:
                continue
            img = frames[0][0]
            with self.subTest(skin=sid):
                raw = img.getbbox()
                body = A.character_box(img)
                self.assertGreaterEqual(body[1], raw[1],
                                        "本体不该比整体 bbox 更高")
                self.assertLess(body[3] - body[1], raw[3] - raw[1],
                                "本体高度该比整体小（排除了光晕/落影）")

    def test_accessory_works_on_every_skin(self):
        from PIL import ImageChops
        for sid in ("dafeiyu", "kun-like", "matcha-sunny"):
            frames = P.load_skin(sid).frames("idle")
            if not frames:
                continue
            with self.subTest(skin=sid):
                img = frames[0][0].copy()
                bb = A.character_box(img)
                self.assertTrue(A.draw_accessory(img, "hat", (bb[0] + bb[2]) / 2,
                                                 float(bb[1]), float(bb[3] - bb[1])))


class TestChat(unittest.TestCase):
    """本地对话。用桩服务器验证协议 —— 本机没模型也能测。"""

    def setUp(self):
        # 对话记忆会落到 data_dir 里。不隔离的话这个测试会去读**真实的
        # pet_chat_memory.json** —— 于是「第二轮该带上第一轮」这种断言
        # 会看到盘上的旧记录，莫名其妙地挂（踩过）。
        self._tmp = tempfile.mkdtemp(prefix="dsh-pet-chat-")
        self._orig_data_dir = B.data_dir
        B.data_dir = lambda: self._tmp

    def tearDown(self):
        B.data_dir = self._orig_data_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    @classmethod
    def setUpClass(cls):
        import http.server
        import json as _json
        import socketserver
        import threading as _th

        cls.seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.endswith("/models"):
                    body = _json.dumps({"data": [{"id": "fake-model"}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                cls.seen["body"] = _json.loads(self.rfile.read(n).decode())
                cls.seen["path"] = self.path
                body = _json.dumps(
                    {"choices": [{"message": {"content": "在呢"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        _th.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_normalize_base_accepts_all_the_forms(self):
        for raw, want in [
            ("127.0.0.1:11434", "http://127.0.0.1:11434/v1"),
            ("http://a:1/", "http://a:1/v1"),
            ("http://a:1/v1/", "http://a:1/v1"),
            ("http://a:1/v1/chat/completions", "http://a:1/v1"),
            ("https://a", "https://a/v1"),
            ("", ""),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(C.normalize_base(raw), want)

    def test_list_models(self):
        self.assertEqual(C.list_models(self.base), ["fake-model"])

    def test_list_models_reports_unreachable(self):
        with self.assertRaises(C.ChatError):
            C.list_models("http://127.0.0.1:1", timeout=1.0)

    def test_ask_sends_the_right_request(self):
        cli = C.ChatClient(self.base, "fake-model")
        self.assertTrue(cli.configured)
        reply = cli.ask("在吗", context="状态是执行中")
        self.assertEqual(reply, "在呢")
        self.assertTrue(self.seen["path"].endswith("/chat/completions"))
        body = self.seen["body"]
        self.assertEqual(body["model"], "fake-model")
        self.assertIs(body["stream"], False, "不做流式")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("执行中", body["messages"][0]["content"])
        self.assertEqual(body["messages"][-1],
                         {"role": "user", "content": "在吗"})

    def test_history_is_kept_and_reset(self):
        cli = C.ChatClient(self.base, "fake-model")
        cli.ask("一")
        cli.ask("二")
        self.assertEqual(len(self.seen["body"]["messages"]), 4,
                         "第二轮该带上第一轮")
        cli.reset()
        self.assertEqual(cli.history, [])

    def test_unconfigured_raises_readable_errors(self):
        for cli, text, want in [
            (C.ChatClient("", "m"), "x", "地址"),
            (C.ChatClient(self.base, ""), "x", "模型"),
            (C.ChatClient(self.base, "m"), "", "说话"),
        ]:
            with self.subTest(want=want):
                with self.assertRaises(C.ChatError) as cm:
                    cli.ask(text)
                self.assertIn(want, str(cm.exception))


class TestI18n(unittest.TestCase):
    """多语言。以中文原文当 key —— 所以「表里的 key 在源码里存在」
    和「源码里 t() 包住的都有翻译」这两个方向都要查。"""

    @classmethod
    def setUpClass(cls):
        import glob
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.root = root
        cls.sources = {}
        for path in glob.glob(os.path.join(root, "*.py")):
            name = os.path.basename(path)
            if name == "pet_i18n.py":
                continue
            with open(path, encoding="utf-8") as fh:
                cls.sources[name] = fh.read()

    def tearDown(self):
        I.set_language("zh")                             # 别把语言漏给别的测试

    def test_language_switch(self):
        I.set_language("zh")
        self.assertEqual(I.t("设置…"), "设置…")
        self.assertEqual(I.t("嘿嘿"), "嘿嘿")
        I.set_language("en")
        self.assertEqual(I.t("设置…"), "Settings…")
        self.assertEqual(I.t("嘿嘿"), "Hehe")
        self.assertEqual(I.t("执行中"), "Working")

    def test_missing_translation_falls_back_to_chinese(self):
        """没翻译的退回原文，不能变空白或漏出 key 名。"""
        I.set_language("en")
        self.assertEqual(I.t("这句没翻译"), "这句没翻译")
        self.assertEqual(I.t(""), "")

    def test_resolve_never_returns_auto(self):
        for pref in ("auto", "zh", "en", "", "klingon", None):
            with self.subTest(pref=pref):
                self.assertIn(I.resolve(pref), ("zh", "en"))

    def test_t_lines(self):
        I.set_language("en")
        self.assertEqual(I.t_lines(["嘿嘿", "好吃！"]), ["Hehe", "Tasty!"])
        I.set_language("zh")
        self.assertEqual(I.t_lines(["嘿嘿"]), ["嘿嘿"])

    @staticmethod
    def _flat(text: str) -> str:
        """比对用：去掉空白、换行转义和引号。

        要处理的三种写法差异：
          · 源码里是 `\\n`（字面量反斜杠 n），翻译表的 key 里是**真换行**
          · 长字符串常写成相邻字面量拼接，源码里两个引号中间夹着空白
          · 排版换行

        归一之后只比"文字内容"，不比怎么写的。第一版只去了空白，
        于是所有相邻字面量拼接的句子都被报成"孤儿 key"。
        """
        return re.sub(r'[\s"\'\\]+', "", text.replace("\\n", ""))

    def test_every_translation_key_exists_in_source(self):
        """翻译表里的 key 必须在源码里出现 —— 否则是改文案时留下的孤儿。

        孤儿 key 本身不报错（t() 查不到就用原文），但它是**漏翻的信号**：
        说明有句话被改过，英文还停在旧版本。
        """
        blob = "\n".join(self.sources.values())
        flat = self._flat(blob)
        orphans = [k for k in I.EN if k not in blob and self._flat(k) not in flat]
        self.assertEqual(orphans, [], f"翻译表里的孤儿 key：{orphans}")

    def test_every_t_call_has_a_translation(self):
        """源码里 t() 包住的中文都要有英文。

        正则要小心：`print("x")` 的尾巴也长得像 `t("x")`，
        所以要加个前置断言（不能是标识符的一部分）。
        """
        missing = []
        for name, src in self.sources.items():
            for m in re.finditer(r'(?<![A-Za-z_])t\("((?:[^"\\]|\\.)*)"\)', src):
                text = m.group(1)
                if not re.search(r"[\u4e00-\u9fff]", text):
                    continue                             # 纯英文/符号不用翻
                # 源码里是 \n 转义，表里是真换行 —— 两边都归一再看
                if text in I.EN or self._flat(text) in {self._flat(k) for k in I.EN}:
                    continue
                missing.append(f"{name}: {text[:40]}")
        self.assertEqual(missing, [], f"这些 t() 没有英文翻译：{missing}")

    def test_state_labels_are_translated(self):
        """PALETTE 的 label 是中文原文，显示前要过 t()。"""
        I.set_language("en")
        for st in P.STATE_ORDER:
            label = P.PALETTE[st]["label"]
            with self.subTest(state=st):
                self.assertNotEqual(I.t(label), label,
                                    f"{st} 的状态标签没翻译：{label}")

    def test_line_tables_are_translated(self):
        """台词表里每一句都要有英文。"""
        I.set_language("en")
        missing = []
        for key, pool in P.LINES.items():
            for line in pool:
                if line not in I.EN:
                    missing.append(f"{key}: {line}")
        self.assertEqual(missing, [], f"这些台词没翻译：{missing[:6]}")

    def test_language_pref_persists(self):
        tmp = tempfile.mkdtemp(prefix="dsh-pet-i18n-")
        orig = B.data_dir
        B.data_dir = lambda: tmp
        try:
            c = S.CompanionSource()
            c.save()
            # 直接写配置再读回来
            with open(os.path.join(tmp, "pet_config.json"), "w", encoding="utf-8") as fh:
                json.dump({"lang": "en"}, fh)
            with open(os.path.join(tmp, "pet_config.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["lang"], "en")
        finally:
            B.data_dir = orig
            shutil.rmtree(tmp, ignore_errors=True)


class TestBomTolerance(unittest.TestCase):
    """带 BOM 的 json 也要能读。

    用户会在记事本里改 pet_config.json / pet_lines.json / theme.json，
    而记事本存「UTF-8」是**带 BOM** 的。用 encoding="utf-8" 读会直接抛
    UnicodeDecodeError，而调用方一律 except 后返回默认值 —— 于是
    **整份配置被静默重置**，用户只会觉得"我改的东西没生效"。

    实测过：带 BOM 的 pet_mood.json 读出来 pokes=0（真值是 42）。
    修法是读的时候用 utf-8-sig（它同时兼容有 BOM 和没 BOM）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-pet-bom-")
        self._orig = B.data_dir
        B.data_dir = lambda: self.tmp

    def tearDown(self):
        B.data_dir = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _write_bom(path: str, data: dict) -> None:
        import io as _io
        with _io.open(path, "w", encoding="utf-8-sig") as fh:
            json.dump(data, fh, ensure_ascii=False)

    def test_mood_with_bom(self):
        self._write_bom(B.mood_path(), {"affection": 0.9, "pokes": 42})
        c = S.CompanionSource()
        self.assertEqual(c.mood.pokes, 42, "带 BOM 的相处记录被丢弃了")
        self.assertAlmostEqual(c.mood.affection, 0.9)

    def test_lines_with_bom(self):
        self._write_bom(B.lines_path(), {"PET_LINES": ["自定义台词"]})
        table = S.load_lines()
        self.assertEqual(table["PET_LINES"], ["自定义台词"],
                         "带 BOM 的台词包被丢弃了")

    def test_read_json_helper_with_bom(self):
        path = os.path.join(self.tmp, "x.json")
        self._write_bom(path, {"a": 1})
        self.assertEqual(P.read_json(path), {"a": 1})

    def test_plain_utf8_still_works(self):
        """没 BOM 的也要照读 —— utf-8-sig 对两者都兼容。"""
        with open(B.mood_path(), "w", encoding="utf-8") as fh:
            json.dump({"pokes": 7}, fh)
        self.assertEqual(S.CompanionSource().mood.pokes, 7)


class TestAccessoryRendering(unittest.TestCase):
    """配饰画在缓存帧上的行为。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-pet-acc-")
        self._orig = B.data_dir
        B.data_dir = lambda: self.tmp
        with open(B.config_path(), "w", encoding="utf-8") as fh:
            json.dump({"accessory": "hat", "skin": "dafeiyu", "companion": False}, fh)
        self._saved = sys.modules.pop("pystray", None)
        sys.modules["pystray"] = None

    def tearDown(self):
        B.data_dir = self._orig
        sys.modules.pop("pystray", None)
        if self._saved is not None:
            sys.modules["pystray"] = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _app(self):
        import argparse
        args = argparse.Namespace(
            demo=True, force_state=None, session=None, shot=None, skin="dafeiyu",
            list_skins=False, privacy=False, no_companion=True, no_hotkeys=True)
        app = P.PetApp(args)
        self.addCleanup(lambda: self._safe_quit(app))
        app.root.update()
        return app

    @staticmethod
    def _safe_quit(app):
        try:
            app._quit()
        except Exception:                                # noqa: BLE001
            pass

    def test_accessory_is_drawn(self):
        app = self._app()
        self.assertEqual(app.accessory, "hat")
        app._tick()
        # 戴帽子之后，屏幕上的画面和"不戴"的差别应该集中在头顶
        from PIL import ImageChops
        clean = P.render_pet("idle", 0.0)
        with_hat = P.render_pet("idle", 0.0)
        bb = A.character_box(with_hat)
        A.draw_accessory(with_hat, "hat", (bb[0] + bb[2]) / 2, float(bb[1]),
                         float(bb[3] - bb[1]), 1.0, 0.0)
        diff = ImageChops.difference(clean.convert("RGB"),
                                     with_hat.convert("RGB")).getbbox()
        self.assertIsNotNone(diff, "帽子没画出来")

    def test_does_not_mutate_the_sprite_cache(self):
        """配饰不能烧进缓存帧。

        frames 是皮肤的缓存，draw_accessory 是原地绘制。不 copy 的话：
        · 关掉配饰帽子还在
        · 换配饰会叠加
        实测踩过 —— 这条守着它。

        **要检查所有帧**，不能只看第 0 帧：_tick 画的是"当前帧"，
        而当前帧是按动画时间算的（idx 一直在变）。第一版只比第 0 帧，
        注入 bug 后照样通过 —— 假绿。
        """
        from PIL import ImageChops
        app = self._app()
        app._switch_accessory("none")
        app._tick()
        clean = [f.copy() for f in app.sprites["idle"]]

        app._switch_accessory("hat")
        for _ in range(12):                              # 跑满一轮动画，覆盖所有帧
            app._tick()
        dirty = []
        for i, (a, b) in enumerate(zip(clean, app.sprites["idle"])):
            if ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox():
                dirty.append(i)
        self.assertEqual(dirty, [], f"配饰画进了缓存帧 {dirty}（应该先 copy()）")

    def test_switching_off_leaves_cache_clean(self):
        from PIL import ImageChops
        app = self._app()
        app._switch_accessory("none")
        app._tick()
        clean = [f.copy() for f in app.sprites["idle"]]
        for aid in ("hat", "scarf", "crown"):
            app._switch_accessory(aid)
            for _ in range(6):
                app._tick()
        app._switch_accessory("none")
        for _ in range(6):
            app._tick()
        dirty = []
        for i, (a, b) in enumerate(zip(clean, app.sprites["idle"])):
            if ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox():
                dirty.append(i)
        self.assertEqual(dirty, [], f"换了几轮配饰后缓存被污染：帧 {dirty}")


class TestHealthReminder(unittest.TestCase):
    """久坐 / 喝水提醒。判据是系统空闲时间，不用窗口就能测。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-pet-health-")
        self._orig = B.data_dir
        B.data_dir = lambda: self.tmp

    def tearDown(self):
        B.data_dir = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lines_are_wired(self):
        self.assertTrue(S.SIT_LINES)
        self.assertTrue(S.WATER_LINES)
        self.assertIn("SIT_LINES", S.LINES)
        self.assertIn("WATER_LINES", S.LINES)

    def test_does_not_fire_immediately(self):
        c = S.CompanionSource()
        c.bubble = ""
        c._health_check(time.time(), idle=1.0)
        self.assertFalse(c.bubble, "刚启动就提醒了")

    def test_break_resets_the_clock(self):
        c = S.CompanionSource()
        old = c.sitting_since
        later = old + 100.0
        c._health_check(later, idle=S.HEALTH_BREAK + 10)
        self.assertEqual(c.sitting_since, later, "离开后没重置计时")

    def test_fires_after_sitting_too_long(self):
        c = S.CompanionSource()
        now = time.time()
        c.next_health = now - 1
        c.sitting_since = now - S.HEALTH_EVERY - 10
        c.bubble = ""
        c._health_check(now, idle=1.0)
        self.assertTrue(c.bubble, "久坐没提醒")

    def test_alternates_between_sit_and_water(self):
        c = S.CompanionSource()
        seen = []
        for _ in range(2):
            now = time.time()
            c.next_health = now - 1
            c.sitting_since = now - S.HEALTH_EVERY - 10
            c.bubble = ""
            c._health_check(now, idle=1.0)
            seen.append(c.bubble)
        self.assertTrue(seen[0] and seen[1])
        self.assertNotEqual(seen[0], seen[1], f"两次提醒一样：{seen}")

    def test_quiet_when_idle(self):
        """已经闲了一会儿就别打扰 —— 人可能在看手机。"""
        c = S.CompanionSource()
        now = time.time()
        c.next_health = now - 1
        c.sitting_since = now - S.HEALTH_EVERY - 10
        c.bubble = ""
        c._health_check(now, idle=120)
        self.assertFalse(c.bubble)


class TestWeekStats(unittest.TestCase):
    """周报统计。"""

    def test_empty_history(self):
        m = S.Mood()
        st = m.week_stats()
        self.assertEqual(len(st["days"]), 7)
        self.assertEqual(st["done"], 0)
        self.assertEqual(st["slowest"], [])
        self.assertIsNone(st["busiest"])

    def test_counts_and_tool_totals(self):
        m = S.Mood()
        for _ in range(5):
            m.note_completion(True)
        m.note_completion(False)
        m.note_tool("grep", 1200)
        m.note_tool("grep", 800)
        m.note_tool("job_output", 60000)
        st = m.week_stats()
        self.assertEqual(st["done"], 5)
        self.assertEqual(st["failed"], 1)
        self.assertEqual(st["slowest"][0][0], "job_output")
        self.assertEqual(dict(st["slowest"])["grep"], 2000)
        self.assertEqual(st["active_days"], 1)
        self.assertIsNotNone(st["busiest"])

    def test_bad_data_self_heals(self):
        m = S.Mood()
        m.history = "坏了"
        m.note_completion(True)
        self.assertIsInstance(m.history, dict)
        m2 = S.Mood()
        m2.history = {"2026-01-01": "坏"}
        m2.note_tool("x", 100)
        self.assertEqual(m2.week_stats()["done"], 0)

    def test_history_is_trimmed(self):
        m = S.Mood()
        for i in range(40):
            m.history[f"2026-01-{i % 28 + 1:02d}"] = {"done": 1, "failed": 0, "tools": {}}
        m.note_completion(True)
        self.assertLessEqual(len(m.history), m.HISTORY_DAYS,
                             "history 没有修剪，会无限长")

    def test_ignores_bad_tool_values(self):
        m = S.Mood()
        m.note_tool("", 100)                             # 空名字
        m.note_tool("x", 0)                              # 0 毫秒
        m.note_tool("y", -5)                             # 负数
        self.assertEqual(m.week_stats()["slowest"], [])


class TestShortName(unittest.TestCase):
    """皮肤名截断。

    踩过：直接用 `name[:14]` 会在词中间切开，界面上出现
    「Hash Sage (cla」「Calico (clawd」这种。
    """

    @staticmethod
    def _f():
        import dsh_pet
        return dsh_pet._short_name

    def test_short_names_pass_through(self):
        f = self._f()
        for s in ("大肥鱼（内置）", "matcha-sunny", "短名"):
            self.assertEqual(f(s), s)

    def test_breaks_at_parenthesis(self):
        f = self._f()
        self.assertEqual(f("Hash Sage (clawd)"), "Hash Sage")
        self.assertEqual(f("Cloudling (clawd)"), "Cloudling")

    def test_breaks_at_space_when_no_paren(self):
        """没有括号就在最后一个空格断 —— 丢掉不完整的那个词。

        "a very long skin name here"[:16] = "a very long skin"，
        最后那个 "skin" 是切了一半的词，丢掉更自然。
        """
        f = self._f()
        self.assertEqual(f("a very long skin name here"), "a very long")

    def test_hard_cut_gets_an_ellipsis(self):
        f = self._f()
        out = f("很长的没有括号没有空格的名字啊啊啊啊啊")
        self.assertLessEqual(len(out), 17)
        self.assertTrue(out.endswith("…"), out)

    def test_never_returns_empty(self):
        f = self._f()
        self.assertEqual(f(""), "")
        self.assertTrue(f("(clawd)"))


class TestDataDir(unittest.TestCase):
    """数据目录要落在**能写**的地方。

    **装完才发现的问题**：安装包默认装到 `C:\\Program Files\\DSH 桌宠`，
    而普通权限写不进去。原来的 data_dir() 直接用 exe 所在目录，后果是
    配置、相处记录、周报、出错日志**全部静默失败**（每处都是 except: pass）——
    用户看到的是「设置改了重启就丢」，而且连出错日志都没有，无从查起。

    实测：装完之后程序旁边一个 pet_*.json 都没有。
    """

    def setUp(self):
        self._orig_cache = B._DATA_DIR_CACHE
        self._orig_exe = getattr(sys, "executable", None)
        self._had_frozen = hasattr(sys, "frozen")
        self._orig_frozen = getattr(sys, "frozen", None)

    def tearDown(self):
        B._DATA_DIR_CACHE = self._orig_cache
        if self._orig_exe is not None:
            sys.executable = self._orig_exe
        if self._had_frozen:
            sys.frozen = self._orig_frozen
        elif hasattr(sys, "frozen"):
            del sys.frozen

    def test_source_mode_uses_the_script_dir(self):
        B._DATA_DIR_CACHE = None
        self.assertEqual(B.data_dir(), os.path.dirname(os.path.abspath(B.__file__)))

    def test_frozen_in_a_writable_dir_stays_put(self):
        """绿色版：解压在能写的地方，配置就跟着 exe 走，拷走不丢。"""
        tmp = tempfile.mkdtemp(prefix="dsh-pet-portable-")
        self.addCleanup(shutil.rmtree, tmp, True)
        B._DATA_DIR_CACHE = None
        sys.frozen = True
        sys.executable = os.path.join(tmp, "DSH-Pet.exe")
        self.assertEqual(B.data_dir(), tmp)

    def test_frozen_in_program_files_falls_back_to_appdata(self):
        """装在 Program Files：那儿写不了，得换到 %APPDATA%。"""
        B._DATA_DIR_CACHE = None
        sys.frozen = True
        sys.executable = r"C:\Program Files\DSH 桌宠\DSH-Pet.exe"
        d = B.data_dir()
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        self.assertTrue(d.startswith(appdata), f"没退到 APPDATA：{d}")
        self.assertTrue(os.path.isdir(d), "退过去的目录没建出来")
        self.assertTrue(B._writable(d), "退过去的地方还是写不了")

    def test_writable_probe_is_honest(self):
        """_writable 是真写一个文件试，不是猜权限位。"""
        tmp = tempfile.mkdtemp(prefix="dsh-pet-w-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertTrue(B._writable(tmp))
        self.assertFalse(B._writable(os.path.join(tmp, "不存在的目录")))
        self.assertEqual([f for f in os.listdir(tmp) if "write-test" in f], [],
                         "探针文件没清掉")


class TestErrorDiagnosis(unittest.TestCase):
    """失败时自动诊断：把「哪个工具、错在哪」挖出来。

    **踩过的形状**：错误标记在 `message.isError` 上，**不是** content 项上；
    正文在 `content[0].text`，另外 `data.error` 里有结构化信息。
    第一版查 content 项的 isError，2257 条 tool/result 一条都没匹配上。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-pet-err-")
        self._orig = B.data_dir
        B.data_dir = lambda: self.tmp

    def tearDown(self):
        B.data_dir = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log(self, events, name="s.v4.jsonl.zstd"):
        """写一个 zstd 会话日志。

        **必须 zstd** —— LogTail._seed 是按 zstd magic 找帧的，
        纯 jsonl 它会直接 return（第一版就这么误判成"没解析出来"）。
        """
        import zstandard as zstd
        p = os.path.join(self.tmp, name)
        raw = ("\n".join(json.dumps(e, ensure_ascii=False) for e in events)
               + "\n").encode("utf-8")
        with open(p, "wb") as fh:
            fh.write(zstd.ZstdCompressor().compress(raw))
        return p

    @staticmethod
    def _err_event(tool="pwsh", cid="c1", turn=3, step=7, text="Error: 命令不存在\n第二行"):
        return [
            {"type": "tool/call", "seq": 1, "time": 1000,
             "data": {"callId": cid, "name": tool}},
            {"type": "tool/result", "seq": 2, "time": 1500, "sourceEventSeqs": [1],
             "data": {"turn": turn, "step": step,
                      "message": {"toolCallId": cid, "isError": True,
                                  "content": [{"type": "text", "text": text}]}}},
        ]

    def test_captures_a_tool_error(self):
        p = self._log(self._err_event())
        lt = S.LogTail(p)
        lt.poll()
        self.assertEqual(len(lt.errors), 1, "没抓到错误")
        e = lt.last_error()
        self.assertEqual(e["tool"], "pwsh")
        self.assertEqual((e["turn"], e["step"]), (3, 7))
        self.assertTrue(e["head"].startswith("Error: 命令不存在"), e["head"])

    def test_brief_is_readable(self):
        lt = S.LogTail(self._log(self._err_event()))
        lt.poll()
        brief = lt.error_brief()
        for piece in ("pwsh", "3-7", "命令不存在"):
            self.assertIn(piece, brief, brief)

    def test_no_error_gives_empty_brief(self):
        p = self._log([{"type": "tool/call", "seq": 1, "time": 1000,
                        "data": {"callId": "c1", "name": "read"}}])
        lt = S.LogTail(p)
        lt.poll()
        self.assertEqual(lt.errors, [])
        self.assertEqual(lt.error_brief(), "")

    def test_seed_does_not_double_consume(self):
        """_seed 之后第一次 poll() 不能把同样的内容再读一遍。

        **踩过**：原来的 offset 在小文件上退化成 0，于是 __init__ 里记一遍、
        poll() 里又记一遍 —— 工具耗时和错误各算两次。
        长会话看不出来（帧多，offset 算对了），**新开的会话必然中招**。
        实测：194 字节的单帧文件，__init__ 后 errors=1，poll() 后 errors=2。
        """
        p = self._log(self._err_event())
        lt = S.LogTail(p)
        self.assertEqual(len(lt.errors), 1, "__init__ 阶段")
        lt.poll()
        self.assertEqual(len(lt.errors), 1, "第一次 poll 重复消费了")
        lt.poll()
        self.assertEqual(len(lt.errors), 1, "第二次 poll 又重复了")
        self.assertEqual(len(lt.tool_times.get("pwsh", [])), 1, "工具耗时也重复了")

    def test_offset_lands_at_end_of_consumed_data(self):
        p = self._log(self._err_event())
        lt = S.LogTail(p)
        self.assertEqual(lt.offset, os.path.getsize(p),
                         "offset 没落在真正消费完的位置")

    def test_error_reaches_the_card_line(self):
        """错误摘要要能出现在信息卡副行上 —— 只写「出错重试」等于没说。"""
        p = self._log(self._err_event())
        lt = S.LogTail(p)
        lt.poll()
        task = P.TaskInfo(session_id="x", state="failed", title="t",
                          error=lt.error_brief())
        self.assertIn("pwsh", task.error)


class TestChatMemory(unittest.TestCase):
    """对话记忆 + 她记得你。

    分两种，可靠程度不同：
      · **观察型** —— 从她本来就看得到的数据里推（常跑什么、几点在、认识多久）
      · **对话型** —— 你说「我叫小明」这种自我披露，模式匹配抠出来
    后者**是尽力而为**：中文没有格标记，模式必然漏也会错，所以只认几个明确句式。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-pet-mem-")
        self._orig = B.data_dir
        B.data_dir = lambda: self.tmp

    def tearDown(self):
        B.data_dir = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 事实抽取 ──
    def test_extracts_self_disclosure(self):
        cases = [
            ("我叫小明", [("名字", "小明")]),
            ("我是做电商的", [("身份", "做电商的")]),
            ("我在做商品复制软件", [("在做", "商品复制软件")]),
            ("我喜欢喝咖啡", [("喜欢", "喝咖啡")]),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(C.extract_facts(text), want)

    def test_does_not_double_match(self):
        """「我在做 X」不该被「我在 X」再匹配一遍。

        踩过：两个模式都命中，同一个句子抽出两条。
        """
        got = C.extract_facts("我在做商品复制软件")
        self.assertEqual(len(got), 1, f"重复匹配了：{got}")

    def test_ignores_chit_chat(self):
        for text in ("今天天气不错", "", "我是谁", "吃了吗"):
            with self.subTest(text=text):
                self.assertEqual(C.extract_facts(text), [], text)

    # ── 存取 ──
    def test_remember_and_dedupe(self):
        c = C.ChatClient("127.0.0.1:11434", "m")
        c.load()
        self.assertTrue(c.remember("名字", "小明"))
        self.assertFalse(c.remember("名字", "小明"), "重复的也记了")
        self.assertTrue(c.remember("喜欢", "喝咖啡"))
        self.assertEqual(len(c.facts), 2)

    def test_survives_a_restart(self):
        c = C.ChatClient("127.0.0.1:11434", "m")
        c.load()
        c.remember("名字", "小明")
        c.history = [{"role": "user", "content": "你好"},
                     {"role": "assistant", "content": "你好呀"}]
        c.save()

        c2 = C.ChatClient("127.0.0.1:11434", "m")
        c2.load()
        self.assertEqual(len(c2.facts), 1)
        self.assertEqual(c2.facts[0]["value"], "小明")
        self.assertEqual(len(c2.history), 2, "对话没存下来")

    def test_bad_file_does_not_crash(self):
        with open(B.chat_history_path(), "w", encoding="utf-8") as fh:
            fh.write("{不是 json")
        c = C.ChatClient("", "")
        c.load()
        self.assertEqual(c.facts, [])
        self.assertEqual(c.history, [])

    def test_bom_file_is_readable(self):
        import io as _io
        with _io.open(B.chat_history_path(), "w", encoding="utf-8-sig") as fh:
            json.dump({"facts": [{"kind": "名字", "value": "小红"}], "history": []},
                      fh, ensure_ascii=False)
        c = C.ChatClient("", "")
        c.load()
        self.assertEqual(len(c.facts), 1)

    def test_facts_are_capped(self):
        c = C.ChatClient("", "")
        for i in range(60):
            c.remember("x", f"v{i}")
        self.assertLessEqual(len(c.facts), C.MAX_FACTS)

    def test_forget(self):
        c = C.ChatClient("", "")
        c.remember("名字", "小明")
        c.remember("喜欢", "咖啡")
        c.forget(0)
        self.assertEqual(len(c.facts), 1)
        self.assertEqual(c.facts[0]["kind"], "喜欢")

    def test_memory_brief_includes_both_kinds(self):
        c = C.ChatClient("", "")
        c.remember("名字", "小明")
        c.notes = ["你常在 14 点干活"]
        brief = c.memory_brief()
        self.assertIn("小明", brief)
        self.assertIn("14 点", brief)


class TestObservedMemory(unittest.TestCase):
    """观察型记忆：从真实数据推出来的那半。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-pet-obs-")
        self._orig = B.data_dir
        B.data_dir = lambda: self.tmp
        # lang 要钉成 zh。不钉的话 PetApp.__init__ 会解析 auto，
        # 而 CI runner 是英文环境 —— 于是 _current 变 en，
        # **后面的 TestSchedule 断言中文就挂**（CI 抓到过）。
        # 自己建的 App 会把语言全局改掉，这是它的副作用。
        with open(B.config_path(), "w", encoding="utf-8") as fh:
            json.dump({"skin": "dafeiyu", "companion": True, "lang": "zh"}, fh)
        self._saved = sys.modules.pop("pystray", None)
        sys.modules["pystray"] = None

    def tearDown(self):
        B.data_dir = self._orig
        I.set_language("zh")                             # 别把语言漏出去
        sys.modules.pop("pystray", None)
        if self._saved is not None:
            sys.modules["pystray"] = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _app(self):
        import argparse
        args = argparse.Namespace(
            demo=False, force_state="idle", session=None, shot=None, skin="dafeiyu",
            list_skins=False, privacy=False, no_companion=True, no_hotkeys=True)
        app = P.PetApp(args)
        self.addCleanup(lambda: self._safe_quit(app))
        app.root.update()
        return app

    @staticmethod
    def _safe_quit(app):
        try:
            app._quit()
        except Exception:                                # noqa: BLE001
            pass

    def test_empty_without_companion(self):
        app = self._app()
        app.companion = None
        self.assertEqual(app._observed_notes(), [])

    def test_derives_from_real_data(self):
        app = self._app()
        app.companion = S.CompanionSource()
        m = app.companion.mood
        m.first_seen = time.time() - 12 * 86400
        for _ in range(7):
            m.note_completion(True)
        m.note_tool("job_output", 62000)
        for h in range(24):
            m.seen_slots[h] = 20
            m.busy_slots[h] = 18 if 14 <= h <= 18 else 2

        notes = " ".join(app._observed_notes())
        self.assertIn("天", notes, notes)
        self.assertIn("7 个任务", notes, notes)
        self.assertIn("job_output", notes, notes)
        self.assertIn("14 点", notes, notes)

    def test_no_notes_from_thin_data(self):
        """数据太少时别硬编 —— 认识一天、没跑过任务，就什么都别说。"""
        app = self._app()
        app.companion = S.CompanionSource()
        self.assertEqual(app._observed_notes(), [])

    def test_broken_mood_does_not_crash(self):
        app = self._app()
        app.companion = S.CompanionSource()
        app.companion.mood.week_stats = lambda: (_ for _ in ()).throw(RuntimeError("坏了"))
        self.assertEqual(app._observed_notes(), [], "坏了应该返回空而不是抛")


class TestPlatformLayer(unittest.TestCase):
    """平台层。

    **诚实的边界**：非 Windows 分支**没有在真机上验证过** —— 开发机是 Windows。
    所以这里测的是：
      · Windows 上快路径没坏（有真值可比）
      · 每个函数**在任何平台都不会抛**（这是 mac/Linux 上"起不来"的直接原因）
      · 模拟非 Windows 时，回退逻辑走的是对的那条

    最后一条用 patch SYSTEM / IS_WINDOWS 来模拟 —— 它能验证分支选择，
    **不能**验证那条分支在真 macOS 上跑得通。
    """

    def test_windows_fast_path_is_intact(self):
        if not PF.IS_WINDOWS:
            self.skipTest("只在 Windows 上比真值")
        self.assertGreaterEqual(PF.dpi_scale(), 1.0)
        vx, vy, vw, vh = PF.virtual_screen()
        self.assertGreater(vw, 0)
        self.assertGreater(vh, 0)
        self.assertIn(PF.system_language(), ("zh", "en", ""))

    def test_nothing_raises_on_any_platform(self):
        """每个函数都不能抛 —— 抛了就是程序起不来。

        非 Windows 上 `ctypes.windll` 根本不存在，这是最容易崩的地方。
        """
        calls = [
            ("dpi_scale", lambda: PF.dpi_scale()),
            ("system_language", lambda: PF.system_language()),
            ("idle_seconds", lambda: PF.idle_seconds()),
            ("cursor_pos", lambda: PF.cursor_pos()),
            ("virtual_screen", lambda: PF.virtual_screen()),
            ("platform_note", lambda: PF.platform_note()),
            ("open_in_file_manager", lambda: PF.open_in_file_manager("")),
            ("play_sound_file", lambda: PF.play_sound_file("")),
            ("foreground_other_window", lambda: PF.foreground_other_window(["x"])),
            ("set_dpi_awareness", lambda: PF.set_dpi_awareness()),
        ]
        for name, fn in calls:
            with self.subTest(fn=name):
                fn()                                     # 不抛就算过

    def test_simulated_non_windows_takes_fallbacks(self):
        """把 IS_WINDOWS 关掉，确认走的是回退而不是 Win32。"""
        with mock.patch.object(PF, "IS_WINDOWS", False), \
             mock.patch.object(PF, "SYSTEM", "Darwin"):
            self.assertEqual(PF.dpi_scale(), 1.0)
            self.assertEqual(PF.idle_seconds(), 0.0)
            self.assertEqual(PF.cursor_pos(), (0, 0))
            self.assertFalse(PF.foreground_other_window(["x"]))
            self.assertTrue(PF.platform_note(), "非 Windows 应该说明哪些功能没有")

    def test_open_and_play_return_false_for_missing_paths(self):
        self.assertFalse(PF.open_in_file_manager("Z:\\不存在的目录"))
        self.assertFalse(PF.play_sound_file("Z:\\不存在.wav"))
        self.assertFalse(PF.open_in_file_manager(""))

    def test_module_functions_delegate(self):
        """各模块的包装函数要转发到平台层。

        不转发的话，改了平台层但模块还用老实现 —— 静默失效。
        """
        with mock.patch.object(PF, "dpi_scale", lambda: 2.5):
            self.assertEqual(B.dpi_scale(), 2.5)
        with mock.patch.object(PF, "virtual_screen", lambda: (1, 2, 3, 4)):
            self.assertEqual(B.virtual_screen(), (1, 2, 3, 4))
        with mock.patch.object(PF, "cursor_pos", lambda: (7, 8)):
            self.assertEqual(S.cursor_pos(), (7, 8))
        with mock.patch.object(PF, "idle_seconds", lambda: 12.5):
            self.assertEqual(S.idle_seconds(), 12.5)
        with mock.patch.object(PF, "system_language", lambda: "en"):
            self.assertEqual(I._system_lang(), "en")

    def test_language_falls_back_when_unknown(self):
        with mock.patch.object(PF, "system_language", lambda: ""):
            self.assertEqual(I._system_lang(), "zh", "认不出来要有默认值")

    def test_display_backend_picks_alpha_off_windows(self):
        """非 Windows 上要挑到 AlphaDisplay，不能抛。

        前两个后端都依赖 Win32，没有第三个的话 make_display 会抛，
        程序直接起不来。
        """
        with mock.patch.object(PF, "IS_WINDOWS", False):
            self.assertTrue(hasattr(B, "AlphaDisplay"),
                            "没有 AlphaDisplay 这个回退后端")
        # 真建一次（在 Windows 上也能建，只是不会走这条路）
        try:
            import tkinter
            root = tkinter.Tk()
            root.withdraw()
            disp = B.AlphaDisplay(root, 64, 48)
            from PIL import Image
            disp.push(Image.new("RGBA", (64, 48), (255, 0, 0, 128)))
            root.destroy()
        except Exception as exc:                         # noqa: BLE001
            self.fail(f"AlphaDisplay 建不起来：{exc}")


class TestConfig(unittest.TestCase):
    def test_paths_are_absolute(self):
        for p in (B.config_path(), B.mood_path(), B.error_log_path(),
                  B.lines_path(), B.ASSETS):
            self.assertTrue(os.path.isabs(p), p)

    def test_paths_follow_data_dir(self):
        """所有可变路径都要从 data_dir 派生 —— 改一处全都跟着变。

        拆文件时踩过：这些原来是模块级常量，拆开后常量被复制进各模块，
        改 dsh_pet 里那份不影响 pet_base 里那份，测试的临时目录隔离
        就静默失效了（表现是测试往仓库里写文件）。改成函数才对。
        """
        orig = B.data_dir
        B.data_dir = lambda: r"X:\probe"
        try:
            for fn in (B.config_path, B.mood_path, B.error_log_path, B.lines_path):
                with self.subTest(fn=fn.__name__):
                    self.assertTrue(fn().startswith(r"X:\probe"), fn())
        finally:
            B.data_dir = orig

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
