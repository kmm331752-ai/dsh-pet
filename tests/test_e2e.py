"""端到端测试：从配置一路走到屏幕像素。

和另外两套的区别：

    test_pet.py     测零件（函数输入输出）
    test_smoke.py   测能不能起来、窗口和菜单在不在
    test_e2e.py     测**整条路**：写配置 → 建 PetApp → 跑一轮 → 看推给屏幕的像素

为什么需要这一层：配饰那个 bug 最能说明问题。当时的测试直接调
`draw_accessory()` 验证它画得出来 —— 通过。但真实路径上有两个 bug
（配置被 BOM 吞掉、绘制污染缓存），**一个都没测到**，最后是用户发现的。

零件都对、路走不通，是这类桌面程序最常见的坏法。

核心工具是 capture(app)：截获 display.push 收到的画布，
那就是用户真正看到的东西。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dsh_pet as P  # noqa: E402
import pet_accessories as A  # noqa: E402
import pet_base as B  # noqa: E402
import pet_music as MU  # noqa: E402
import pet_activity as AC  # noqa: E402
import pet_i18n as I  # noqa: E402
import pet_sources as S  # noqa: E402

TK_OK = True
try:
    import tkinter
    _r = tkinter.Tk()
    _r.destroy()
except Exception:                                        # noqa: BLE001
    TK_OK = False


@unittest.skipUnless(TK_OK, "没有可用的 Tk（无头环境）")
class E2EBase(unittest.TestCase):
    """公共装置：隔离目录、固定语言、干掉托盘。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-pet-e2e-")
        self._orig_data_dir = B.data_dir
        B.data_dir = lambda: self.tmp
        self._orig_lang = I.current()
        I.set_language("zh")
        self._saved_pystray = sys.modules.pop("pystray", None)
        sys.modules["pystray"] = None                    # type: ignore[assignment]
        self._apps: list = []

    def tearDown(self):
        for app in self._apps:
            try:
                app._quit()
            except Exception:                            # noqa: BLE001
                pass
        B.data_dir = self._orig_data_dir
        I.set_language(self._orig_lang)
        sys.modules.pop("pystray", None)
        if self._saved_pystray is not None:
            sys.modules["pystray"] = self._saved_pystray
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 装置 ──
    def write_config(self, **kw) -> None:
        """写 pet_config.json —— 端到端测试的**输入**就在这里。"""
        cfg = {"skin": "dafeiyu", "companion": False, "scale": 1.0}
        cfg.update(kw)
        with open(B.config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)

    def write_config_bom(self, **kw) -> None:
        """带 BOM 地写 —— 模拟记事本存 UTF-8。"""
        import io as _io
        cfg = {"skin": "dafeiyu", "companion": False, "scale": 1.0}
        cfg.update(kw)
        with _io.open(B.config_path(), "w", encoding="utf-8-sig") as fh:
            json.dump(cfg, fh, ensure_ascii=False)

    def make_app(self, **kw):
        """按配置建一个 PetApp。"""
        args = argparse.Namespace(
            demo=False, force_state=kw.pop("force_state", "idle"), session=None,
            shot=None, skin=kw.pop("skin", None), list_skins=False,
            privacy=kw.pop("privacy", False), no_companion=kw.pop("no_companion", True),
            no_hotkeys=True)
        app = P.PetApp(args)
        self._apps.append(app)
        app.root.update()
        return app

    def capture(self, app, ticks: int = 1, poll: bool = True):
        """跑几轮 _tick，返回**推给屏幕的那张画布**。

        这是这一层的关键：不看中间变量，看最终像素。display.push 是最后一站。

        两个必须注意的地方（第一版都踩了）：

        · **时间要冻住**。动画帧是按 time.time() 选的，两次截图之间
          墙钟在走，画面对不上 —— 会误判成"配饰没关掉"。
        · **poll 要能关**。_poll 会用 DSH 状态覆盖 self.snap，
          测试手工塞的 snapshot 会被冲掉。
        """
        grabbed = []
        orig = app.display.push
        app.display.push = lambda img: (grabbed.append(img.copy()), orig(img))[1]
        try:
            with mock.patch.object(P.time, "time", lambda: self.FROZEN):
                if poll:
                    app._poll()
                for _ in range(ticks):
                    app._tick()
        finally:
            app.display.push = orig
        return grabbed[-1] if grabbed else None

    # 冻住的时间点。随便挑的，只要两次截图用的是同一个值。
    FROZEN = 1_700_000_000.0

    @staticmethod
    def head_pixels(canvas):
        """角色区里头顶那一带的像素（配饰/帽子会出现在这里）。"""
        return canvas.crop((0, 0, P.WIN_W, 40))

    @staticmethod
    def diff_box(a, b):
        from PIL import ImageChops
        return ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox()



class FakeMusic:
    """假的音乐源。想让它放什么就放什么。"""

    def __init__(self, np=None):
        self.np = np
        self.available = True
        self.stopped = False
        self.commands = []

    def poll(self):
        return self.np

    def sessions(self):
        return [self.np.app] if self.np else []

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def toggle_play(self):
        self.commands.append("toggle")
        return True


class FakeActivity:
    def __init__(self, act=None):
        self.act = act
        self.available = True

    def poll(self):
        return self.act


def fake_track(title="歌", artist="人", playing=True):
    return MU.NowPlaying(app="cloudmusic.exe", title=title, artist=artist,
                         playing=playing, duration=200, position=10,
                         changed_at=time.time())


class TestHealthReminderEndToEnd(E2EBase):
    """久坐提醒要能从**真实的轮询路径**触发。

    这里踩过一个坑，而且是"测试全绿但功能等于没做"的那种：
    _health_check 原来挂在 CompanionSource.poll() 里，而 poll() 只在
    DSH **空闲**时才被调用（dsh_pet.py 里 `if snap.state in ("idle","offline")`）。
    久坐恰恰发生在你干活的时候 —— 结果是「你越忙她越不提醒」。

    实测过：DSH 状态 tools / waiting 时 _health_check 被调用 **0 次**。
    当时的单元测试直接调 _health_check，所以完全没发现。

    这条测试走 _poll()，断言的是"气泡出现在最终 snapshot 上"。
    """

    def _stub_watcher(self, app, state: str, **kw):
        """塞一个假 watcher。

        注意 make_app 默认 force_state="idle" —— 那样 _poll 会走
        _demo_snapshot 分支，根本到不了 watcher。所以这几条测试要
        force_state=None（那样 PetApp 会建真的 watcher 和 companion，
        再把 watcher 换掉）。
        """
        snap = P.Snapshot(state=state, session_id="x", title="t", **kw)
        app.watcher = type("W", (), {
            "poll": staticmethod(lambda: snap),
            "_new_completion": False,
        })()
        return snap

    def _patch_health(self, first=0, every=0, idle=1.0):
        """把健康提醒的三个输入都钉死。

        **idle_seconds 要同时 patch 两个模块** —— `dsh_pet` 里写的是
        `from pet_sources import *`，那是**另一份引用**，patch
        pet_sources.idle_seconds 对它无效。

        踩过：只 patch 了 pet_sources，于是真实系统空闲时间说了算 ——
        刚敲完命令时测试过，机器闲了 60 秒后就挂（idle > 60 会提前 return）。
        典型的 flaky：本地怎么跑都是绿的，隔一会儿再跑就红。
        """
        return [
            mock.patch.object(S, "HEALTH_FIRST", first),
            mock.patch.object(S, "HEALTH_EVERY", every),
            mock.patch.object(S, "idle_seconds", lambda: idle),
            mock.patch.object(P, "idle_seconds", lambda: idle),
        ]

    def test_fires_while_dsh_is_busy(self):
        app = self.make_app(force_state=None, no_companion=False)
        app.root.update()
        self.assertIsNotNone(app.watcher, "force_state=None 时应该有 watcher")
        self.assertIsNotNone(app.companion, "应该有 companion")

        patches = self._patch_health()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for state in ("tools", "thinking", "waiting", "idle"):
            with self.subTest(state=state):
                app.companion.sitting_since = 0
                app.companion.next_health = 0
                app.companion.bubble = ""
                app.companion.bubble_until = 0
                self._stub_watcher(app, state)
                app._poll()
                self.assertTrue(app.snap.bubble,
                                f"DSH 是 {state} 时没提醒（气泡没出现）")
                self.assertTrue(app.snap.detail_line)

    def test_busy_snapshot_still_keeps_its_own_text_without_a_bubble(self):
        """没有气泡时，忙碌状态的文案不能被改掉。"""
        app = self.make_app(force_state=None, no_companion=False)
        app.root.update()
        app.companion.bubble = ""
        app.companion.bubble_until = 0
        self._stub_watcher(app, "tools", detail_line="原来的文案")
        patches = self._patch_health(idle=1.0)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        app._poll()
        self.assertEqual(app.snap.detail_line, "原来的文案")

    def test_quiet_when_the_machine_is_idle(self):
        """人离开电脑（空闲很久）时不该提醒 —— 她可能在看手机。"""
        app = self.make_app(force_state=None, no_companion=False)
        app.root.update()
        app.companion.sitting_since = 0
        app.companion.next_health = 0
        app.companion.bubble = ""
        self._stub_watcher(app, "tools")
        patches = self._patch_health(idle=S.HEALTH_BREAK + 60)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        app._poll()
        self.assertFalse(app.snap.bubble, "人不在还提醒了")


class TestLazyAttributes(E2EBase):
    """「用到才建」的属性要不得。

    _drop_job 原来是这么写的，而 _attention() 直接读 self._drop_job ——
    全新启动、还没拖过她的时候 AttributeError，被 _poll 的 except 吞掉。
    后果：「卡在你这儿就走到屏幕中间」**从来没生效过**。

    这类 bug 不会崩、不会报错，只是某个功能安静地不工作。
    """

    def test_attention_works_on_a_fresh_app(self):
        app = self.make_app()
        app.root.update()
        app._attention_at = 0
        try:
            app._attention()
        except AttributeError as exc:
            self.fail(f"全新启动时 _attention() 抛了 AttributeError：{exc}")

    def test_no_attribute_is_created_only_on_use(self):
        """扫一遍：代码直接读的 self._xxx，__init__ 里都得先建出来。

        判定「作者知道它可能不存在」的方式是**全文件搜** getattr(self, "xxx" ——
        不能只看用到的那一行，因为守卫常写在上一行：

            if getattr(self, "_settings_win", None) is not None:
                self._settings_win.deiconify()      # ← 这行没有 getattr

        第一版按行找，把这几个误报成问题。
        """
        app = self.make_app()
        app.root.update()
        src_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "dsh_pet.py")
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()

        missing = []
        for m in re.finditer(r"self\.(_[a-z][a-z_]*)\b(?!\s*=[^=])", src):
            name = m.group(1)
            if f'getattr(self, "{name}"' in src:
                continue                                 # 明确处理过"可能没有"
            if not hasattr(app, name) and name not in missing:
                line = src[:m.start()].count("\n") + 1
                missing.append(f"{name} (L{line})")
        self.assertEqual(missing, [],
                         f"这些属性 __init__ 里没建、代码却直接读：{missing}")


class TestAutoHideEndToEnd(E2EBase):
    """贴边自动隐藏。

    这个配置项原来是**死代码** —— 键读进来了，字段建好了，全文没人读它。
    功能从来没实现过。现在补上了，这里守着它真的动。

    另一个坑：Tk 的 geometry() 会把窗口**夹在可见区域内**，
    贴边隐藏要把窗口大部分挪到屏幕外，Tk 做不到（想放 -236 实际停在 0）。
    所以走 Win32 的 SetWindowPos。
    """

    def _placed(self, app, x, y):
        """摆好窗口并让 Tk 真正应用 —— geometry() 之后不 update()，
        winfo_x() 读到的还是旧值（第一版就这么误判的）。"""
        app.root.geometry(f"+{x}+{y}")
        app.root.update()

    def _park_at(self, app, edge):
        """贴到某条边上并让鼠标走开，返回 (vx, vy, vw, vh)。"""
        app._autohide = True
        app.grab_edge = edge
        vx, vy, vw, vh = P.virtual_screen()
        if edge == "left":
            self._placed(app, vx, vy + 200)
            far = (vx + vw - 50, vy + vh - 50)
        else:
            self._placed(app, vx + vw - app.win_w, vy + 200)
            far = (vx + 50, vy + 50)
        with mock.patch.object(P, "cursor_pos", lambda: far):
            app._autohide_tick()
        return vx, vy, vw, vh

    def test_off_by_default_does_nothing(self):
        app = self.make_app()
        app.root.update()
        app._autohide = False
        app.grab_edge = "left"
        self._placed(app, 0, 200)
        app._autohide_tick()
        self.assertIsNone(app._hidden_at, "关着的时候不该隐藏")

    def test_does_not_hide_when_not_parked_at_an_edge(self):
        """她自己溜达到屏幕中间时不该突然消失。"""
        app = self.make_app()
        app.root.update()
        app._autohide = True
        app.grab_edge = None
        self._placed(app, 600, 300)
        with mock.patch.object(P, "cursor_pos", lambda: (1900, 1000)):
            app._autohide_tick()
        self.assertIsNone(app._hidden_at)

    def test_hides_at_left_edge_when_the_mouse_is_far(self):
        app = self.make_app()
        app.root.update()
        vx, _vy, _vw, _vh = self._park_at(app, "left")
        self.assertEqual(app._hidden_at, "left")
        self.assertEqual(app.root.winfo_x(), vx - app.win_w + P.AUTOHIDE_PEEK_PX,
                         "藏起来后没停在正确位置（Tk 会夹住窗口，要用 SetWindowPos）")

    def test_hides_at_right_edge(self):
        app = self.make_app()
        app.root.update()
        vx, _vy, vw, _vh = self._park_at(app, "right")
        self.assertEqual(app._hidden_at, "right")
        self.assertEqual(app.root.winfo_x(), vx + vw - P.AUTOHIDE_PEEK_PX)

    def test_comes_back_when_the_mouse_approaches(self):
        app = self.make_app()
        app.root.update()
        vx, vy, _vw, _vh = self._park_at(app, "left")
        self.assertEqual(app._hidden_at, "left")

        with mock.patch.object(P, "cursor_pos", lambda: (vx + 30, vy + 260)):
            app._autohide_tick()
        self.assertIsNone(app._hidden_at, "鼠标靠近了没出来")
        self.assertEqual(app.root.winfo_x(), vx)

    def test_click_peeks(self):
        app = self.make_app()
        app.root.update()
        self._park_at(app, "left")
        self.assertEqual(app._hidden_at, "left")

        app._peek()
        self.assertIsNone(app._hidden_at)
        self.assertGreater(app._peek_until, time.time())

    def test_turning_it_off_releases_her(self):
        """关掉开关时要把已经藏起来的放出来，不然窗口留在屏幕外。"""
        app = self.make_app()
        app.root.update()
        vx, _vy, _vw, _vh = self._park_at(app, "left")
        self.assertEqual(app._hidden_at, "left")

        app._autohide = False
        app._autohide_tick()
        self.assertIsNone(app._hidden_at, "关掉开关后她还藏在屏幕外")
        self.assertEqual(app.root.winfo_x(), vx)

    def test_press_while_hidden_peeks_instead_of_dragging(self):
        app = self.make_app()
        app.root.update()
        self._park_at(app, "left")

        class E:
            x_root, y_root, x, y = 100, 100, 5, 5
        app._on_press(E())
        self.assertIsNone(app._hidden_at, "点那 12 像素没把她叫出来")
        self.assertIsNone(app._drag, "藏着的时候点击不该变成拖动")


class TestConfigPersistsEndToEnd(E2EBase):
    """开关类配置要存下来。

    **踩过**：_save_config 只写了 11 个键，而 focus / autohide / companion
    是「读但从不写回」—— 菜单里能勾，重启就变回去，用户只会觉得
    「我开的那个开关自己关了」。

    加配置项时读和写要一起加。
    """

    def test_toggles_survive_a_restart(self):
        app = self.make_app()
        app.root.update()
        app.focus_mode = True
        app._autohide = True
        app._save_config()

        with open(B.config_path(), encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        for key in ("focus", "autohide", "companion"):
            with self.subTest(key=key):
                self.assertIn(key, cfg, f"配置里没写 {key}，重启就丢")

    def test_every_key_that_is_read_is_also_written(self):
        """扫一遍：cfg.get 读的键，_save_config 里都得写。

        autohide / focus / companion 就是这么漏掉的。
        """
        src_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "dsh_pet.py")
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()
        read = set(re.findall(r'cfg\.get\(\s*"([a-z_]+)"', src))
        body = re.search(r"def _save_config\(self\).*?(?=\n    def )", src, re.S)
        self.assertIsNotNone(body, "找不到 _save_config")
        written = set(re.findall(r'cfg\["([a-z_]+)"\]', body.group(0)))
        # always_on_top 是「启动时读一次就够」的窗口样式，不是用户开关
        missing = sorted(read - written - {"always_on_top"})
        self.assertEqual(missing, [],
                         f"这些配置项读了但从不写回，重启就丢：{missing}")


class TestAccessoryEndToEnd(E2EBase):
    """配饰：配置 → 屏幕。两个真实 bug 都出在这条路上。"""

    def test_config_reaches_the_screen(self):
        """配置里写 hat，屏幕上就该有帽子。

        这条能同时抓住那两个 bug：
          · 配置被 BOM 吞掉 → accessory 变 none → 没帽子
          · 绘制污染缓存 → 后面几轮会花掉
        """
        self.write_config(accessory="hat")
        app = self.make_app()
        self.assertEqual(app.accessory, "hat", "配置没传到 PetApp")

        with_hat = self.capture(app)

        # 对照：同一个 app 换成不戴，重新截
        app._switch_accessory("none")
        for _ in range(3):
            app._tick()
        without = self.capture(app)

        self.assertIsNotNone(with_hat)
        self.assertIsNotNone(without)
        box = self.diff_box(self.head_pixels(with_hat), self.head_pixels(without))
        self.assertIsNotNone(box, "配置写了 hat，但屏幕上看不出区别")

    def test_bom_config_still_reaches_the_screen(self):
        """带 BOM 的配置也要生效 —— 记事本存 UTF-8 就是带 BOM 的。"""
        self.write_config_bom(accessory="crown")
        app = self.make_app()
        self.assertEqual(app.accessory, "crown", "带 BOM 的配置被丢弃了")
        canvas = self.capture(app)
        self.assertIsNotNone(canvas)

    def test_switching_off_actually_removes_it(self):
        """关掉配饰，屏幕上要真的没有 —— 不能烧在缓存里。"""
        self.write_config(accessory="hat")
        app = self.make_app()
        first = self.capture(app, ticks=4)

        app._switch_accessory("none")
        after = self.capture(app, ticks=4)

        # 关掉之后再跟"从一开始就没戴"比
        app2 = self.make_app()
        app2._switch_accessory("none")
        clean = self.capture(app2, ticks=4)

        box = self.diff_box(self.head_pixels(after), self.head_pixels(clean))
        self.assertIsNone(box, f"关掉配饰后屏幕上还有残留：{box}")

    def test_every_accessory_survives_a_full_animation(self):
        """每个配饰跑满一轮动画都不该出问题，也不该污染后续。"""
        self.write_config()
        app = self.make_app()
        app._switch_accessory("none")
        clean = self.capture(app, ticks=14)
        for aid in A.accessory_ids():
            if aid == "none":
                continue
            with self.subTest(accessory=aid):
                app._switch_accessory(aid)
                canvas = self.capture(app, ticks=14)
                self.assertIsNotNone(canvas, f"{aid} 渲染挂了")
        app._switch_accessory("none")
        back = self.capture(app, ticks=14)
        self.assertIsNone(self.diff_box(self.head_pixels(clean), self.head_pixels(back)),
                          "换了一圈配饰后画面回不到原样")


class TestSkinEndToEnd(E2EBase):
    def test_config_skin_is_used(self):
        self.write_config(skin="kun-like")
        app = self.make_app()
        self.assertEqual(app.skin.name, "kun-like")

    def test_switching_skin_changes_the_screen(self):
        self.write_config(skin="dafeiyu")
        app = self.make_app()
        a = self.capture(app)
        app._switch_skin("kun-like")
        b = self.capture(app)
        self.assertIsNotNone(self.diff_box(a, b), "换了皮肤但画面没变")

    def test_unknown_skin_falls_back(self):
        self.write_config(skin="不存在的皮肤")
        app = self.make_app()
        self.assertTrue(app.skin.name, "加载失败也要有个能用的皮肤")
        self.assertIsNotNone(self.capture(app))


class TestScaleEndToEnd(E2EBase):
    def test_config_scale_changes_window_and_canvas(self):
        self.write_config(scale=1.0)
        app = self.make_app()
        small = self.capture(app)
        self.assertEqual(small.size, (app.win_w, app.win_h))

        app._apply_scale(1.6)
        big = self.capture(app)
        self.assertGreater(big.width, small.width, "放大后画布没变大")
        self.assertEqual(big.size, (app.win_w, app.win_h))

    def test_scale_is_clamped_from_config(self):
        self.write_config(scale=99.0)
        app = self.make_app()
        self.assertLessEqual(app.scale, P.SCALE_MAX)


class TestLanguageEndToEnd(E2EBase):
    def test_config_language_changes_the_rendered_card(self):
        """配置里写 en，渲染出来的信息卡就该是英文。

        这条是踩出来的：多语言第一版只包了菜单，信息卡一个字符串都没包，
        而当时的测试只查"t() 包住的有没有翻译"，没包的根本不在检查范围。
        """
        self.write_config(lang="zh")
        app = self.make_app()

        def shot():
            # poll=False：_poll 会用 DSH 状态覆盖手工塞的 snapshot
            app.snap = P.Snapshot(state="tools", title="测试任务")
            app._card_cache = (None, None)               # 卡片有缓存，得清
            return self.capture(app, poll=False)

        zh = shot()
        I.set_language("en")
        en = shot()
        I.set_language("zh")

        self.assertIsNotNone(self.diff_box(zh, en), "切了语言但画面没变")

    def test_language_from_config_is_applied(self):
        """配置里写 en，PetApp 建出来就该是英文。"""
        self.write_config(lang="en")
        app = self.make_app()
        self.assertEqual(app.lang, "en")
        self.assertEqual(I.t("设置…"), "Settings…")
        I.set_language("zh")

    def test_auto_language_resolves_to_something_real(self):
        self.write_config(lang="auto")
        app = self.make_app()
        self.assertIn(app.lang, ("zh", "en"))


class TestSoundEndToEnd(E2EBase):
    def test_config_sound_reaches_the_player(self):
        """配置里关掉声音，_play_sound 就该什么都不做。"""
        self.write_config(sound=False)
        app = self.make_app()
        self.assertFalse(app.sound)
        calls = []
        import winsound
        orig = winsound.PlaySound
        winsound.PlaySound = lambda *a, **k: calls.append(a)
        try:
            app._play_sound("done")
        finally:
            winsound.PlaySound = orig
        self.assertEqual(calls, [], "配置关了声音但还是响了")

    def test_enabled_sound_plays(self):
        self.write_config(sound=True)
        app = self.make_app()
        calls = []
        import winsound
        orig = winsound.PlaySound
        winsound.PlaySound = lambda *a, **k: calls.append(a)
        try:
            app._last_sound.clear()
            app._play_sound("done")
        finally:
            winsound.PlaySound = orig
        self.assertEqual(len(calls), 1, "开着声音却没响")


class TestChatEndToEnd(E2EBase):
    def test_config_chat_reaches_the_client(self):
        self.write_config(chat_url="127.0.0.1:11434", chat_model="qwen2.5:3b")
        app = self.make_app()
        self.assertTrue(app.chat.configured, "配置没传到 chat 客户端")
        self.assertEqual(app.chat.base, "http://127.0.0.1:11434/v1")
        self.assertEqual(app.chat.model, "qwen2.5:3b")

    def test_unconfigured_chat_is_off(self):
        self.write_config()
        app = self.make_app()
        self.assertFalse(app.chat.configured)


class TestPrivacyEndToEnd(E2EBase):
    def test_privacy_hides_the_title(self):
        self.write_config(privacy=False)
        app = self.make_app()

        def shot(priv):
            app.privacy = priv
            app.snap = P.Snapshot(state="tools", title="机密项目名称", prompt="机密提示词")
            app._card_cache = (None, None)
            return self.capture(app, poll=False)

        normal = shot(False)
        private = shot(True)
        self.assertIsNotNone(self.diff_box(normal, private), "隐私模式没起作用")


class TestDetailEndToEnd(E2EBase):
    def test_detail_toggle_changes_the_card(self):
        """展开详情后信息卡要变高。

        比的是**卡片**不是画布 —— 画布永远是 WIN_W×WIN_H，
        卡片在里面按需长高（DETAIL_CARD_H 是上限，不是固定值）。
        第一版比画布高度，两边都是 330，看着像"没生效"。
        """
        self.write_config()
        app = self.make_app()

        def shot(detail):
            app.detail = detail
            app.snap = P.Snapshot(state="tools", title="任务", model="m", provider="p")
            app._card_cache = (None, None)
            self.capture(app, poll=False)
            return app._card_cache[1]

        folded = shot(False)
        expanded = shot(True)
        self.assertGreater(expanded.height, folded.height,
                           f"展开详情后卡片没变高：{folded.height} -> {expanded.height}")

    def test_card_height_matches_the_renderer(self):
        """render_card 的高度要和 card_height() 报的一致 —— 不一致会画到画布外。"""
        snap = P.Snapshot(state="tools", title="任务", model="m", provider="p",
                          slow_tools=[("grep", 1200)])
        for detail in (False, True):
            with self.subTest(detail=detail):
                self.assertEqual(P.render_card(snap, detail).height,
                                 P.card_height(snap, detail))


class TestMusicAndActivity(E2EBase):
    """音乐 / 前台应用：走 _poll，**断言推给屏幕的像素**。

    **为什么必须走 _poll 且断言像素**（AGENTS.md 规则 1）：直接调
    `_music_tick()` 只能证明那个函数对。这个功能第一版挂在
    `CompanionSource.poll()` 里，而 DSH 忙的时候走 watcher 分支 ——
    那条路根本不经过它。实测现象：DSH 有任务在跑时，你在开会她照样冒泡。

    **为什么注入假源**（规则 6）：真音乐源读的是这台机器上正在放什么，
    测试依赖它换台机器就挂。假源想让它放什么就放什么。
    """

    def _app_with(self, track=None, activity=None):
        app = self.make_app(force_state=None, no_companion=False)
        fm = FakeMusic(track)
        fa = FakeActivity(activity)
        app.companion.attach_sources(music=fm, activity=fa)
        app.music, app.activity = fm, fa
        return app, fm, fa

    @staticmethod
    def _settle(app):
        """跑一轮把"刚切歌"那次冒泡消费掉，再清干净。

        顺序很重要：先 poll 一次（她这时会说"换歌了"，`_music_seen` 才记上），
        再清气泡。反过来做的话下一轮 poll 仍会当成"切歌"再说一次 ——
        因为 `_music_seen` 还是空的（实测踩到）。
        """
        app._poll()
        app.companion.bubble_until = 0.0
        app.companion.bubble = ""

    # ── 像素层：音符真的画上去了吗 ──
    def test_music_notes_reach_the_screen(self):
        """有歌 / 没歌，推给屏幕的画布必须不一样。

        这是这一层存在的意义：断言 `snap.music` 只能证明数据传到了，
        证明不了音符画出来了。
        """
        app, fm, _ = self._app_with(fake_track("歌", "人"))
        self._settle(app)
        with_music = self.capture(app, ticks=2)

        fm.np = None
        without = self.capture(app, ticks=2)

        self.assertIsNotNone(with_music)
        self.assertIsNotNone(without)
        box = self.diff_box(with_music, without)
        self.assertIsNotNone(box, "有歌和没歌的画面一模一样 —— 音符没画上去")
        # 音符在角色头顶那一带
        self.assertLess(box[1], P.PET_AREA_H, f"差异出现在信息卡里：{box}")

    def test_music_overlay_is_the_music_kind(self):
        app, _, _ = self._app_with(fake_track("歌", "人"))
        self._settle(app)
        app._poll()
        self.assertEqual(app._overlay_kind(time.time()), "music")

    def test_title_shows_when_she_is_not_talking(self):
        app, _, _ = self._app_with(fake_track("歌", "人"))
        self._settle(app)
        app._poll()
        self.assertEqual(app.snap.music, "人 - 歌")
        self.assertTrue(app.snap.detail_line.startswith("♪"), app.snap.detail_line)

    def test_paused_music_is_not_listening(self):
        app, _, _ = self._app_with(fake_track("歌", "人", playing=False))
        app._poll()
        self.assertEqual(app.snap.music, "")
        self.assertNotEqual(app._overlay_kind(time.time()), "music")

    def test_stopping_music_clears_it(self):
        app, fm, _ = self._app_with(fake_track("歌", "人"))
        app._poll()
        fm.np = None
        app._poll()
        self.assertEqual(app.snap.music, "")
        self.assertNotEqual(app._overlay_kind(time.time()), "music")

    # ── 安静：知道你在干什么 ──
    def test_meeting_makes_her_quiet(self):
        app, _, _ = self._app_with(
            fake_track("歌", "人"),
            AC.Activity(process="wemeetapp.exe", category="meeting"))
        app._poll()
        self.assertTrue(app.snap.quiet, "开会该安静")
        self.assertFalse(app.snap.bubble)
        self.assertEqual(app.snap.music, "人 - 歌", "歌名不该丢")

    def test_coding_video_game_are_quiet(self):
        for proc, cat in (("Code.exe", "coding"), ("vlc.exe", "video"),
                          ("steam.exe", "game")):
            with self.subTest(category=cat):
                app, _, _ = self._app_with(
                    fake_track("歌", "人"),
                    AC.Activity(process=proc, category=cat))
                app._poll()
                self.assertTrue(app.snap.quiet, f"{cat} 该安静")

    def test_not_quiet_when_just_browsing(self):
        app, _, _ = self._app_with(
            None, AC.Activity(process="explorer.exe", category="other"))
        app._poll()
        self.assertFalse(app.snap.quiet)

    def test_quiet_applies_even_when_dsh_is_busy(self):
        """**这条是这个 bug 的回归测试。**

        第一版把音乐/活动的 tick 挂在 `CompanionSource.poll()` 里，
        而 DSH 忙时走 watcher 分支 —— 完全失效。
        现在 `tick_sources()` 由 PetApp 无条件调，两条路都经过。
        """
        app, _, _ = self._app_with(
            None, AC.Activity(process="Zoom.exe", category="meeting"))
        # 造一个"DSH 正忙"的 snapshot，确认 quiet 仍然贴得上
        app.snap = P.Snapshot(state="tools", title="正在干活", session_id="x")
        app.companion.tick_sources(time.time())
        app.companion.decorate(app.snap, time.time())
        self.assertTrue(app.snap.quiet, "DSH 忙的时候也该安静")
        self.assertEqual(app.snap.state, "tools", "状态不该被改掉")

    def test_quiet_survives_a_real_poll_cycle(self):
        """走完整 _poll（含 watcher 分支），确认 quiet 没被冲掉。"""
        app, _, _ = self._app_with(
            fake_track("歌", "人"),
            AC.Activity(process="wemeetapp.exe", category="meeting"))
        for _ in range(3):
            app._poll()
            app.root.update()
        self.assertTrue(app.snap.quiet, f"state={app.snap.state} 时该安静")

    # ── 健壮性 ──
    def test_no_sources_does_not_crash(self):
        """没装 winrt 的机器上音乐源不可用 —— 不能崩。"""
        app = self.make_app(force_state=None, no_companion=False)
        app.music, app.activity = None, None
        app.companion.music, app.companion.activity = None, None
        app._poll()
        self.assertIsNotNone(app.snap)
        self.assertEqual(app.snap.music, "")
        self.assertFalse(app.snap.quiet)

    def test_no_companion_does_not_crash(self):
        """--no-companion 时也要能跑（那时没有 companion 可挂源）。"""
        app = self.make_app(force_state=None, no_companion=True)
        app._poll()
        self.assertIsNotNone(app.snap)

    def test_quit_stops_the_music_thread(self):
        app, fm, _ = self._app_with(fake_track("歌", "人"))
        app._poll()
        app._quit()
        self.assertTrue(fm.stopped, "退出时要停后台线程")


if __name__ == "__main__":
    unittest.main(verbosity=2)
