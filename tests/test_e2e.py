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
import pet_food  # noqa: E402
from pet_render import overlay_for  # noqa: E402

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

    def read_config(self) -> dict:
        """读回 pet_config.json —— 端到端测试的**输出**在这里。

        设置界面改的东西最后都落到这个文件上，所以"用户改了之后存下来了
        吗"要从这里断言，不是从内存里的 `app.cfg`。
        """
        with open(B.config_path(), encoding="utf-8") as fh:
            return json.load(fh)

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
        # 别真扫会话历史 —— 慢，而且依赖机器上有多少文件（规则 6）
        app._lint_no_scan = True
        # git 也一样 —— 否则每条测试都去跑 `git log`，
        # 又慢又依赖机器上有哪些仓库（规则 6）。
        app._git_no_scan = True
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
        # 和真 MusicSource 一样：只看 available 就排队，**不看有没有曲目**
        # （真源就是这么写的 —— 所以不能拿它的返回值当"有没有在放"）
        if not self.available:
            return False
        self.commands.append("toggle")
        return True

    def next_track(self):
        if not self.available:
            return False
        self.commands.append("next")
        return True

    def prev_track(self):
        if not self.available:
            return False
        self.commands.append("prev")
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

    # ── 反向控制：接口在那儿，用户得碰得到 ──
    def test_music_commands_reach_the_source(self):
        """三个控制命令要真的发到音乐源。

        **这条测的是一个"接口写了但没人调用"的缺口**：
        `MusicSource.toggle_play()` / `next_track()` / `prev_track()`
        早就实现好了，但 `dsh_pet.py` 里一次都没调用过 ——
        "反向控制"这条目标等于只做了一半。
        """
        app, fm, _ = self._app_with(fake_track("歌", "人"))
        app._poll()
        for cmd in ("toggle", "next", "prev"):
            app._music_cmd(cmd)
        self.assertEqual(fm.commands, ["toggle", "next", "prev"])
        self.assertGreater(app.poke_until, time.time(), "控制后该播反应动画")

    def test_music_command_is_gated_by_now_playing(self):
        """没在放歌时**不发命令**，但要说一声。

        判据必须和菜单启用/禁用用的是同一个 —— 否则会出现
        "菜单说在放，按下去却什么都不发生"。
        真 MusicSource 的 toggle_play() 只看 available 就排队，
        所以不能拿它的返回值当"有没有在放"。
        """
        app, fm, _ = self._app_with(fake_track("歌", "人"))
        app._poll()
        fm.np = None
        fm.commands.clear()
        app.companion.bubble = ""
        app._music_cmd("toggle")
        self.assertEqual(fm.commands, [], "没在放歌却发了命令")
        self.assertTrue(app.companion.bubble, "该说一声")

    def test_music_command_without_a_source(self):
        app = self.make_app(force_state=None, no_companion=False)
        app.music = None
        app.companion.music = None
        app.companion.bubble = ""
        app._music_cmd("toggle")                     # 不崩就算过
        self.assertTrue(app.companion.bubble)

    def test_music_ready_matches_what_the_menu_uses(self):
        app, fm, _ = self._app_with(fake_track("歌", "人"))
        self.assertTrue(app._music_ready())
        fm.np = None
        self.assertFalse(app._music_ready())
        app.music = None
        self.assertFalse(app._music_ready())

    def test_music_menu_items_exist(self):
        """音乐那几项要在菜单里 —— **递归找**。

        菜单是分组的（音乐/番茄钟/陪伴/外观/会话五个子菜单），
        只看顶层会找不到。测试**不该关心菜单是平铺还是嵌套的**，
        所以用 `_menu_entries` 走整棵树。
        """
        app, _, _ = self._app_with(None)
        joined = " ".join(app._all_menu_labels())
        for want in ("⏯", "⏭", "⏮", I.t("现在在放什么…")):
            self.assertIn(want, joined, f"菜单里少了 {want}")

    def test_menu_is_grouped_not_flat(self):
        """**30 项平铺 → 11 个可见项 + 5 个子菜单。**

        原来右键一下弹出一整屏文字，得读完 30 项才能找到要的那个。

        **数的是可见项，不含分隔线** —— 分隔线是排版不是内容，
        算进去的话"缩短菜单"就变成了"少画几条线"。
        """
        app, _, _ = self._app_with(None)
        n = app.menu.index("end") + 1
        visible = [i for i in range(n) if app.menu.type(i) != "separator"]
        self.assertLessEqual(len(visible), 12,
                             f"顶层还有 {len(visible)} 个可见项，太长了")
        cascades = [app.menu.entrycget(i, "label") or ""
                    for i in range(n)
                    if app.menu.type(i) == "cascade"]
        self.assertGreaterEqual(len(cascades), 5, f"子菜单太少：{cascades}")

    def test_music_items_get_greyed_out_by_the_recursive_walk(self):
        """**`_on_menu` 要能灰掉子菜单里的项。**

        第一版只遍历顶层 `command`，而音乐项搬进子菜单之后就再也
        灰不掉了 —— 没在放歌时那几项还是亮的，点了没反应。
        """
        app, fm, _ = self._app_with(fake_track("歌", "人"))
        fm.np = None
        app.music = fm
        # **调抽出来的那个，不要调 `_on_menu`** —— 后者最后会
        # `tk_popup`，那是**阻塞等用户输入**的。测试里没人点，
        # 于是卡到超时（实测 175 秒，把整条 e2e 从 30 秒拖到 170 秒）。
        app._refresh_music_menu_state()
        states = {}
        for m, i, kind in app._menu_entries(app.menu):
            if kind != "command":
                continue
            label = m.entrycget(i, "label") or ""
            if "⏯" in label or "⏭" in label or "⏮" in label:
                states[label] = str(m.entrycget(i, "state"))
        self.assertTrue(states, "没找到音乐项")
        for label, st in states.items():
            self.assertEqual(st, "disabled", f"{label} 没被灰掉（{st}）")

    def test_track_change_plays_a_reaction(self):
        """切歌时她要有反应 —— 走 _poll，不是直接调 _music_react。"""
        app, fm, _ = self._app_with(fake_track("第一首", "人"))
        app._poll()
        app.poke_until = 0.0
        fm.np = fake_track("第二首", "人")
        app._poll()
        self.assertGreater(app.poke_until, time.time(), "切歌没反应")

    def test_health_reminder_survives_quiet(self):
        """**CI 抓到的回归测试。** quiet 是「别闲聊」，不是「闭嘴」。

        语义：开会/写代码时不该说"这首好听"，但**该提醒你起来动动**。

        ## 这条飘过好几次，最后那句断言本来就是错的

        `decorate()` 里是：

            if now < self._quiet_until and not self.bubble_important:
                snap.quiet = True

        **important 气泡在的时候 quiet 本来就不该为真。** 原来最后那句
        `assertTrue(app.snap.quiet)` 之所以通常能过，只是因为
        `_health_tick()` 跑在 `decorate()` **之后** —— 纯属时序巧合，
        换个顺序或者前面留了条重要气泡就挂。

        （也试过把 `_quiet_until` 显式钉住，不行：`_activity_tick()`
        在 `_poll()` 里跑在 `decorate()` **之前**，会把它清成 0。
        所以还是走活动源，让 coding 那个分类自己把 quiet 设上。）
        """
        app, _, _ = self._app_with(
            None, AC.Activity(process="Code.exe", category="coding"))
        app._poll()
        self.assertTrue(app.snap.quiet, "动手前先确认真的进了 quiet")

        # 造一个"久坐到点"
        app.companion.sitting_since = time.time() - 99999
        app.companion.next_health = 0.0
        app.companion.bubble_until = 0.0
        # idle_seconds 要 patch **两个模块** —— dsh_pet 里是
        # `from pet_sources import *`，那是另一份引用（AGENTS.md 规则 6）。
        with mock.patch.object(S, "idle_seconds", lambda: 1.0), \
             mock.patch.object(P, "idle_seconds", lambda: 1.0):
            app._poll()
            app.root.update()
        self.assertTrue(app.companion.bubble, "久坐提醒被 quiet 压没了")
        self.assertTrue(app.companion.bubble_important, "久坐提醒该标 important")
        self.assertTrue(app.snap.bubble, "重要气泡没贴到 snapshot 上")
        # **不再断言 snap.quiet** —— 见 docstring，那条本来就是错的。

    def test_chitchat_is_suppressed_by_quiet(self):
        """**另一半语义**：不重要的话，quiet 期间确实压掉。

        和上一条配成一对 —— 只有一条的话，"quiet 太狠"和"quiet 没用"
        看起来是一样的。
        """
        app, _, _ = self._app_with(
            None, AC.Activity(process="Code.exe", category="coding"))
        app.companion.bubble_until = 0.0
        app.companion.bubble_important = False
        # 一句闲聊：不重要。放在 poll **之前**，让 decorate 有机会压它。
        app.companion.say("这首好听", 10.0, important=False)
        app._poll()
        self.assertTrue(app.snap.quiet, "quiet 期间应该是安静的")
        self.assertFalse(app.snap.bubble, "闲聊不该穿过 quiet")

    def test_music_wakes_her_up(self):
        """放歌说明有人在 —— 她不该睡着。

        CI 抓到的：测试期望装饰是 music，拿到 zzz（她睡了，
        而 sleeping 的检查在 music 之前）。
        """
        app, _, _ = self._app_with(fake_track("歌", "人"))
        app.companion.state = "sleeping"
        app.companion.state_until = time.time() + 999
        with mock.patch.object(S, "idle_seconds", lambda: 9999.0):
            for _ in range(5):
                app._poll()
                app.root.update()
        self.assertNotEqual(app.snap.state, "sleeping", "放歌时她睡着了")
        self.assertNotEqual(app._overlay_kind(time.time()), "zzz")

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


class TestWorksWithoutDSH(E2EBase):
    """**目标验收**：没有 DSH 时功能不残缺。

    DSH 只是她的数据源之一，不是前提。这一组把 DSH 指到空目录
    （等价于"这台机器没装 DSH"），然后**逐个功能走一遍** ——
    不是看代码在不在，是看它能不能用。

    写这一组的时候抓到两次"看着像 bug、其实是测试写错了"：
      · 60 轮紧循环里没有时间流逝，`state_until` 永远不过 →
        她一直停在 gazing，误判成"状态机坏了"
      · 配置是在 `_quit()` 里写的，检查放在了 `_quit()` 之前 →
        误判成"配置没存下来"
    """

    def _no_dsh(self):
        return mock.patch.object(S, "list_session_files", lambda: [])

    def test_she_lives_her_own_life(self):
        """没 DSH 时她自己过日子，不是卡在 offline，也不卡在一个状态。

        **时间要自己推，不能靠 sleep**（前两版都栽在这儿）：

          · 第一版 sleep 0.05×30 = 1.5 秒 —— `gazing` 的保持期比这长，
            只看到 gazing，误判成"状态机坏了"
          · 第二版干脆不 sleep —— 墙钟没走，`state_until` 永远不过，
            **她当然一直停在 gazing**

        正确做法：钉一个**可控时钟**，每轮往前推 3 秒。既确定又快，
        而且真的在测状态机（而不是在测我 sleep 了多久）。

        **时钟要从"现在"起步，不能随便挑个常数**：`next_roam` /
        `next_peek` 是 `CompanionSource.__init__` 里用**真实时间**算的，
        假时钟要是比它早（比如 1_700_000_000），`now > next_roam`
        永远不成立 —— 她会安安静静 idle 两分钟，看着像"卡住了"。
        第三版就是这么挂的。
        """
        clock = [time.time()]

        def tick_time():
            clock[0] += 3.0
            return clock[0]

        with self._no_dsh():
            app = self.make_app(force_state=None, no_companion=False)
            self.assertEqual(app.watcher.poll().state, "offline")

            # idle_seconds 也要钉死（规则 6）—— CI runner 上没人动键鼠，
            # 真实 idle 很大，她会一直睡（实测：只看到 ['sleeping']）。
            # 两处都要 patch：dsh_pet 里是 `from pet_sources import *`。
            with mock.patch.object(S.time, "time", tick_time), \
                 mock.patch.object(P.time, "time", tick_time), \
                 mock.patch.object(S, "idle_seconds", lambda: 1.0), \
                 mock.patch.object(P, "idle_seconds", lambda: 1.0), \
                 mock.patch.object(P, "cursor_pos", lambda: (99999, 99999)):
                app._poll()
                self.assertTrue(app.in_companion, f"state={app.snap.state}")
                self.assertNotEqual(app.snap.state, "offline")

                seen = set()
                for _ in range(40):
                    app.companion.mouse_at = 0.0   # 鼠标"很久没动"，她不会去看
                    app._poll()
                    app.root.update()
                    seen.add(app.snap.state)

            # 40 轮 × 3 秒 = 2 分钟，够她换好几次状态了
            self.assertGreaterEqual(
                len(seen), 2,
                f"两分钟里只出现了一个状态，她卡住了：{sorted(seen)}")
            self.assertNotIn("offline", seen, "不该退回 offline")

    def test_feeding_works(self):
        with self._no_dsh():
            app = self.make_app(force_state=None, no_companion=False)
            m = app.companion.mood
            m.satiety = 0.0
            before = m.feeds
            app._do_feed("ramen")
            self.assertEqual(m.feeds, before + 1)
            self.assertGreater(m.satiety, 0.0)
            self.assertTrue(m.favorites and m.dislike)

    def test_music_works(self):
        with self._no_dsh():
            app = self.make_app(force_state=None, no_companion=False)
            fm = FakeMusic(fake_track("歌", "人"))
            app.music, app.companion.music = fm, fm
            # idle 钉死：CI 上没人动键鼠，她会睡，装饰就是 zzz 不是 music
            with mock.patch.object(S, "idle_seconds", lambda: 1.0), \
                 mock.patch.object(P, "idle_seconds", lambda: 1.0):
                app._poll()
            self.assertEqual(app.snap.music, "人 - 歌")
            self.assertEqual(app._overlay_kind(time.time()), "music")
            app._music_cmd("toggle")
            self.assertEqual(fm.commands, ["toggle"])

    def test_activity_awareness_works(self):
        with self._no_dsh():
            app = self.make_app(force_state=None, no_companion=False)
            fa = FakeActivity(AC.Activity(process="wemeetapp.exe",
                                          category="meeting"))
            app.activity, app.companion.activity = fa, fa
            app._poll()
            self.assertTrue(app.snap.quiet)

    def test_health_reminder_works(self):
        """**没 DSH 时最容易漏的一个** —— 它当年就是挂在 poll() 里废掉的。"""
        with self._no_dsh():
            app = self.make_app(force_state=None, no_companion=False)
            app.companion.sitting_since = 0
            app.companion.next_health = 0
            app.companion.bubble = ""
            app.companion.bubble_until = 0
            with mock.patch.object(S, "HEALTH_FIRST", 0), \
                 mock.patch.object(S, "HEALTH_EVERY", 0), \
                 mock.patch.object(S, "idle_seconds", lambda: 1.0), \
                 mock.patch.object(P, "idle_seconds", lambda: 1.0):
                app._poll()
            self.assertTrue(app.snap.bubble, "没 DSH 时久坐提醒不出来了")

    def test_windows_open(self):
        with self._no_dsh():
            app = self.make_app(force_state=None, no_companion=False)
            for name, opener in (("设置", app._open_settings),
                                 ("皮肤预览", app._open_skin_preview),
                                 ("刚才错在哪", app._show_last_error)):
                with self.subTest(window=name):
                    opener()
                    app.root.update()

    def test_weekly_report_exports(self):
        with self._no_dsh():
            app = self.make_app(force_state=None, no_companion=False)
            app.companion.mood.note_completion(ok=True)
            app._export_week()
            self.assertTrue(os.path.exists(B.week_path()))

    def test_survives_a_restart(self):
        """喂过、有口味 —— 重启后还记得。"""
        with self._no_dsh():
            app = self.make_app(force_state=None, no_companion=False)
            m = app.companion.mood
            m.satiety = 0.0
            app._do_feed("ramen")
            favs, feeds = list(m.favorites), m.feeds
            app._quit()

            app2 = self.make_app(force_state=None, no_companion=False)
            self.assertEqual(app2.companion.mood.favorites, favs)
            self.assertEqual(app2.companion.mood.feeds, feeds)


class TestGazeEndToEnd(E2EBase):
    """16 方向视线：走 _tick，**断言像素**。

    素材本来就在图里 —— v2 图集的第 9、10 行是 16 个视线方向格子，
    但代码只认 9 行，那两行从来没播过。所以这一组测的是
    "鼠标动一下，推给屏幕的画面真的变了"。
    """

    def _app(self, skin="openpets"):
        app = self.make_app(force_state=None, no_companion=False, skin=skin)
        return app

    @staticmethod
    def _put_cursor(app, dx, dy):
        """把光标放到锚点偏移 (dx, dy)，并假装刚动过。"""
        ax, ay = app._gaze_anchor_px()
        app._cursor_prev = (int(ax + dx), int(ay + dy))
        app._cursor_at = time.time()

    def _shot(self, app):
        """跑一帧，拿推给屏幕的画布。"""
        grabbed = []
        orig = app.display.push
        app.display.push = lambda img: (grabbed.append(img.copy()), orig(img))[1]
        try:
            app._tick()
        finally:
            app.display.push = orig
        return grabbed[-1] if grabbed else None

    def test_different_directions_reach_the_screen(self):
        """四个方向推给屏幕的画布必须两两不同。

        断言 `_gaze_sprite()` 返回不同对象只能证明取到了不同格子，
        证明不了**画上去了** —— 中间还可能被镜像、配饰、装饰覆盖。
        """
        app = self._app()
        self.assertTrue(app.skin.supports_gaze, "openpets 皮肤该支持视线")
        shots = {}
        for name, dx, dy in (("上", 0, -200), ("右", 200, 0),
                             ("下", 0, 200), ("左", -200, 0)):
            self._put_cursor(app, dx, dy)
            shots[name] = self._shot(app)
            self.assertIsNotNone(shots[name], f"{name} 没画面")
        for a, b in (("上", "下"), ("左", "右"), ("上", "左")):
            with self.subTest(pair=f"{a}/{b}"):
                box = self.diff_box(shots[a], shots[b])
                self.assertIsNotNone(box, f"{a} 和 {b} 画面一样 —— 视线没生效")

    def test_stopping_the_cursor_returns_to_neutral(self):
        """鼠标停 1.2 秒以上就回中性 —— 一直盯着看像卡住了。"""
        app = self._app()
        self._put_cursor(app, 200, 0)
        moving = self._shot(app)
        app._cursor_at = time.time() - 5.0
        still = self._shot(app)
        self.assertIsNotNone(self.diff_box(moving, still),
                             "停下来后画面没变")

    def test_dead_zone_uses_neutral(self):
        app = self._app()
        self._put_cursor(app, 3, 3)                 # 死区内
        inside = self._shot(app)
        self._put_cursor(app, 0, -200)              # 正上
        up = self._shot(app)
        self.assertIsNotNone(self.diff_box(inside, up))

    def test_not_gazing_while_dragging(self):
        """拖动中不看她自己 —— 手在哪儿她跟着看很怪。"""
        app = self._app()
        self._put_cursor(app, 200, 0)
        gazing = self._shot(app)
        app._drag = (0, 0, 0)
        try:
            dragging = self._shot(app)
        finally:
            app._drag = None
        self.assertIsNotNone(self.diff_box(gazing, dragging))

    def test_skin_without_gaze_is_unaffected(self):
        """内置皮肤没有视线格子 —— 不能因此崩，也不能画错。"""
        app = self._app(skin="dafeiyu")
        self.assertFalse(getattr(app.skin, "supports_gaze", False))
        self._put_cursor(app, 200, 0)
        self.assertIsNone(app._gaze_sprite(time.time()))
        self.assertIsNotNone(self._shot(app))

    def test_mirror_compensation(self):
        """图被翻转时取的是镜像格子 —— 否则翻过来方向是反的。"""
        app = self._app()
        self._put_cursor(app, 200, 0)
        app.snap.mirror = True
        flipped = app._gaze_sprite(time.time())
        app.snap.mirror = False
        normal = app._gaze_sprite(time.time())
        self.assertIsNotNone(flipped)
        self.assertIsNotNone(normal)
        self.assertIsNot(flipped, normal, "翻转和没翻转取到同一张缓存图 = 没补偿")

    def test_anchor_comes_from_the_manifest(self):
        app = self._app()
        self.assertEqual(app._gaze_anchor, (0.5, 0.32),
                         "没从 pet.json 读到 gazeAnchor")


class TestMappingWindow(E2EBase):
    """映射编辑界面 —— **从设置窗口那个按钮进，断言配置和皮肤状态**。

    规则 1 说"测路径不测零件"：所以这里不直接调 `_open_mapping_window()`，
    而是先找到设置窗口上那个按钮 `invoke()` 它 —— 用户就是这么进来的。

    **语言要钉死（规则 6）。** 按钮文字是 `t("动画映射…")`，而 CI runner
    是英文环境 —— 搜 `"映射"` 搜不到，6 条全挂，本地却全过。
    AGENTS.md 规则 6 的表格里正好记着这一条（"冒烟测试断言 '安装' in label，
    CI runner 是英文环境就挂"），我还是又踩了一遍。
    """

    def setUp(self):
        super().setUp()
        self._lang_before = I.current()
        # **配置里就要写 lang。** 只调 `I.set_language("zh")` 没用 ——
        # `PetApp.__init__` 会自己按 `cfg["lang"]` 再设一遍，配置里没有就
        # 回落 "auto"，CI runner 上就是英文。
        # （第一版只设了 I，本地过、CI 6 条全挂。）
        self.write_config(lang="zh", skin="openpets")

    def tearDown(self):
        I.set_language(self._lang_before)
        super().tearDown()

    def _open_via_settings(self, app):
        """走用户那条路：设置窗口 → 点按钮 → 映射窗口。"""
        app._open_settings()
        app.root.update()
        sw = app._settings_win
        self.assertIsNotNone(sw, "设置窗口没建起来")
        hits = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.cget("text") and "映射" in str(c.cget("text")):
                        hits.append(c)
                except Exception:                        # noqa: BLE001
                    pass
                walk(c)

        walk(sw)
        self.assertTrue(hits, "设置窗口里没有「动画映射」按钮")
        hits[0].invoke()
        app.root.update()
        return app._mapping_win

    @staticmethod
    def _buttons(win):
        """所有按钮的 (文字, 控件)。

        **`cget("text")` 要包在 try 里** —— ttk 的 `TSeparator` 没有
        `text` 选项，直接取会抛 TclError。
        """
        out = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.winfo_class() not in ("Button", "TButton"):
                        walk(c)
                        continue
                    text = str(c.cget("text"))
                    # **过滤掉已经销毁的** —— 重开过窗口之后，
                    # 树里可能还留着失效引用，`invoke()` 会抛
                    # `invalid command name`。
                    if c.winfo_exists():
                        out.append((text, c))
                except Exception:                        # noqa: BLE001
                    pass
                walk(c)

        walk(win)
        return out

    @staticmethod
    def _widgets(w, cls):
        """按控件类找。**ttk 与否都认。**

        `ttk.Button` 的 `winfo_class()` 是 `TButton`，`tk.Button` 是
        `Button`。测试不该关心用哪套 —— 换了 ttk 之后十几处断言会全挂，
        而它们本来测的是"有几个按钮"，不是"按钮是哪个实现的"。
        """
        want = {cls, "T" + cls}
        out = []

        def walk(x):
            for c in x.winfo_children():
                try:
                    if c.winfo_class() in want:
                        out.append(c)
                except Exception:                        # noqa: BLE001
                    pass
                walk(c)

        walk(w)
        return out

    def _set_state(self, win, want_from, to):
        """把当前值是 `want_from` 的那个下拉框改成 `to`。返回改了几个。"""
        n = 0

        def walk(w):
            nonlocal n
            for c in w.winfo_children():
                try:
                    if c.winfo_class() in ("Menubutton", "TMenubutton"):
                        vn = str(c.cget("textvariable"))
                        if vn and win.getvar(vn) == want_from:
                            win.setvar(vn, to)
                            n += 1
                except Exception:                        # noqa: BLE001
                    pass
                walk(c)

        walk(win)
        return n

    def test_the_button_opens_the_window(self):
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        win = self._open_via_settings(app)
        self.assertIsNotNone(win, "点了按钮窗口没建起来")
        texts = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.cget("text"):
                        texts.append(str(c.cget("text")))
                except Exception:                        # noqa: BLE001
                    pass
                walk(c)

        walk(win)
        self.assertTrue(any("思考中" in x for x in texts), "没列出状态")
        self.assertTrue(any("openpets" in x for x in texts), "没显示皮肤名")

    def test_saving_a_change_reaches_the_skin_and_the_config(self):
        """**这条是核心。** 改一项 → 保存 → 皮肤变了 + 配置写对了。"""
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        self.assertEqual(app.skin.mapping.row_for("thinking"), 8, "基线不对")
        win = self._open_via_settings(app)

        self.assertEqual(self._set_state(win, "review", "idle"), 1,
                         "没找到 thinking 那个下拉框")
        save = dict(self._buttons(win)).get("保存")
        self.assertIsNotNone(save, "没有保存按钮")
        save.invoke()
        app.root.update()

        self.assertEqual(app.skin.mapping.row_for("thinking"), 0, "皮肤没跟着变")
        self.assertEqual(app.skin.mapping.row_for("tools"), 7, "没碰的被改了")
        cfg = self.read_config()
        self.assertEqual(cfg.get("mapping", {}).get("states"), {"thinking": "idle"},
                         "配置写错了")

    def test_only_changed_states_are_stored(self):
        """只存改过的 —— 15 个状态全写进配置的话，以后改默认值就传不下去了。"""
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        win = self._open_via_settings(app)
        self._set_state(win, "review", "idle")
        dict(self._buttons(win))["保存"].invoke()
        app.root.update()
        states = self.read_config().get("mapping", {}).get("states", {})
        self.assertEqual(set(states), {"thinking"}, f"存多了：{set(states)}")

    def test_change_survives_a_restart(self):
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        win = self._open_via_settings(app)
        self._set_state(win, "review", "idle")
        dict(self._buttons(win))["保存"].invoke()
        app.root.update()

        app2 = self.make_app(force_state=None, no_companion=False, skin="openpets")
        self.assertEqual(app2.skin.mapping.row_for("thinking"), 0, "重启就丢了")
        self.assertEqual(app2.skin.mapping.row_for("tools"), 7)

    def test_restore_defaults_really_restores(self):
        """「恢复默认」要回得去。

        踩过：如果保存时只改 `skin.mapping.states`，那"恢复默认"就没有
        干净的起点可比 —— 必须在保存时从皮肤的**原始清单**重新解析。
        """
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        win = self._open_via_settings(app)
        self._set_state(win, "review", "idle")
        dict(self._buttons(win))["保存"].invoke()
        app.root.update()
        self.assertEqual(app.skin.mapping.row_for("thinking"), 0)

        app._open_mapping_window()                       # 重开一个（旧的已经关了）
        app.root.update()
        win2 = app._mapping_win
        self.assertIsNotNone(win2)
        btns = dict(self._buttons(win2))
        btns["恢复默认"].invoke()
        app.root.update()
        btns["保存"].invoke()
        app.root.update()
        self.assertEqual(app.skin.mapping.row_for("thinking"), 8, "没恢复回去")
        self.assertNotIn("states", self.read_config().get("mapping") or {},
                         "配置里还留着 states")

    @staticmethod
    def _tabs(win):
        """Notebook 的两个 Tab 页。"""
        out = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.winfo_class() == "TNotebook":
                        out.extend(c.winfo_children())
                except Exception:                        # noqa: BLE001
                    pass
                walk(c)

        walk(win)
        return out

    def test_animation_tab_exists(self):
        """**两个 Tab** —— 状态 15 行 + 动画 9 行排一起会高到放不下。"""
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        win = self._open_via_settings(app)
        tabs = self._tabs(win)
        self.assertEqual(len(tabs), 2, f"Tab 数不对：{len(tabs)}")
        self.assertEqual(len(self._widgets(tabs[0], "Menubutton")), 16,
                         "状态那页该有 16 个下拉框（加了 chasing）")
        self.assertEqual(len(self._widgets(tabs[1], "Spinbox")), 9,
                         "动画那页该有 9 个行号框")
        self.assertEqual(len(self._widgets(tabs[1], "Entry")), 9,
                         "动画那页该有 9 个时长框")

    def test_making_idle_slower(self):
        """**「让待机慢一点」就是为这个加的。**

        改之前：只能在 pet_config.json 里手写 `animations.idle.durationMs`。
        """
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        self.assertEqual(app.skin.mapping.animations["idle"]["duration_ms"], 5500)
        win = self._open_via_settings(app)
        tabs = self._tabs(win)

        hit = None
        for e in self._widgets(tabs[1], "Entry"):
            vn = str(e.cget("textvariable"))
            if vn and win.getvar(vn) == "5500":
                hit = vn
                break
        self.assertIsNotNone(hit, "没找到 idle 的时长框")
        win.setvar(hit, "11000")
        app.root.update()
        dict(self._buttons(win))["保存"].invoke()
        app.root.update()

        self.assertEqual(app.skin.mapping.animations["idle"]["duration_ms"], 11000)
        cfg = self.read_config()
        self.assertEqual(
            cfg["mapping"]["animations"]["idle"]["durationMs"], 11000)
        self.assertEqual(set(cfg["mapping"]["animations"]), {"idle"},
                         "只该存改过的")
        # 帧时长真的变慢了
        fr = app.skin.frames("idle")
        self.assertGreater(fr[0][1], 1500, f"{fr[0][1]} ms/帧，没变慢")

    def test_both_tabs_save_together(self):
        """两个 Tab 各改一处，保存后配置里两块都在。"""
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        win = self._open_via_settings(app)
        tabs = self._tabs(win)
        self._set_state(tabs[0], "review", "idle")
        for sp in self._widgets(tabs[1], "Spinbox"):
            vn = str(sp.cget("textvariable"))
            if vn and win.getvar(vn) == "5":
                win.setvar(vn, "7")
                break
        app.root.update()
        dict(self._buttons(win))["保存"].invoke()
        app.root.update()
        m = self.read_config().get("mapping", {})
        self.assertIn("states", m)
        self.assertIn("animations", m)
        self.assertEqual(m["states"]["thinking"], "idle")
        self.assertTrue(any(v.get("row") == 7 for v in m["animations"].values()),
                        str(m["animations"]))

    def test_restore_defaults_covers_both_tabs(self):
        """「恢复默认」要把两个 Tab 都还原，配置里两块都清掉。"""
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        win = self._open_via_settings(app)
        tabs = self._tabs(win)
        self._set_state(tabs[0], "review", "idle")
        for e in self._widgets(tabs[1], "Entry"):
            vn = str(e.cget("textvariable"))
            if vn and win.getvar(vn) == "5500":
                win.setvar(vn, "9999")
                break
        app.root.update()
        dict(self._buttons(win))["保存"].invoke()
        app.root.update()
        self.assertEqual(app.skin.mapping.animations["idle"]["duration_ms"], 9999)

        app._open_mapping_window()
        app.root.update()
        win2 = app._mapping_win
        btns = dict(self._buttons(win2))
        btns["恢复默认"].invoke()
        app.root.update()
        btns["保存"].invoke()
        app.root.update()
        self.assertEqual(app.skin.mapping.animations["idle"]["duration_ms"], 5500)
        self.assertEqual(app.skin.mapping.row_for("thinking"), 8)
        m = self.read_config().get("mapping") or {}
        self.assertNotIn("states", m)
        self.assertNotIn("animations", m)

    def test_half_typed_number_does_not_crash(self):
        """输入框里是半截数字（用户正在打字）时点保存 —— 不能炸。"""
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        win = self._open_via_settings(app)
        tabs = self._tabs(win)
        for e in self._widgets(tabs[1], "Entry"):
            vn = str(e.cget("textvariable"))
            if vn and win.getvar(vn) == "5500":
                win.setvar(vn, "abc")
                break
        app.root.update()
        dict(self._buttons(win))["保存"].invoke()
        app.root.update()
        self.assertEqual(app.skin.mapping.animations["idle"]["duration_ms"], 5500,
                         "坏输入该被跳过，用默认")

    def test_animation_tab_is_reachable_from_settings(self):
        """整条路：设置窗口 → 按钮 → 窗口 → 两个 Tab。"""
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        win = self._open_via_settings(app)
        self.assertIsNotNone(win)
        self.assertEqual(len(self._tabs(win)), 2)

    def test_builtin_skin_explains_instead_of_crashing(self):
        """内置皮肤没有可配置的行 —— 要给说明，不是报错。"""
        app = self.make_app(force_state=None, no_companion=False, skin="dafeiyu")
        shown = []
        app.messagebox = type("M", (), {"showinfo": staticmethod(
            lambda *a, **k: shown.append(a))})()
        app._open_mapping_window()
        app.root.update()
        self.assertEqual(len(shown), 1, "没给说明")
        self.assertIsNone(app._mapping_win, "不该建窗口")

    def test_bad_config_shows_a_warning(self):
        """被忽略的配置项要在界面上看得见。"""
        # lang 要跟着写 —— 这里再 write_config 一次会把 setUp 那份冲掉
        self.write_config(lang="zh", skin="openpets",
                          mapping={"states": {"thinking": "不存在的动画"}})
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        self.assertTrue(app.mapping_warnings(), "没记下警告")
        win = self._open_via_settings(app)
        texts = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.cget("text"):
                        texts.append(str(c.cget("text")))
                except Exception:                        # noqa: BLE001
                    pass
                walk(c)

        walk(win)
        self.assertTrue(any("忽略" in x for x in texts), f"界面没提示：{texts[-6:]}")


class TestNewInteractions(E2EBase):
    """补上的三个"完全没有反馈"的情况。

    | 情况 | 原来 |
    |---|---|
    | 滚轮缩放 | `_apply_scale` 直接改大小，**零反馈** |
    | 睡着时被戳 | 和醒着被戳走同一条路（同动画同台词） |
    | 喂食 | 借的是「被戳」动画，看不出在吃 |
    """

    def _app(self):
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        return app

    def _wheel(self, app, delta=120):
        app._on_wheel(type("E", (), {"delta": delta})())

    def test_wheel_stretch_reaches_the_screen(self):
        """滚轮缩放要有拉扯装饰 —— 断言像素。"""
        app = self._app()
        before = self.capture(app, poll=False)
        self._wheel(app)
        after = self.capture(app, poll=False)
        self.assertIsNotNone(self.diff_box(before, after),
                             "滚轮之后画面没变")
        self.assertEqual(app._overlay_kind(time.time()), "stretch")

    def test_wheel_at_the_limit_gives_no_reaction(self):
        """已经到最大了再滚，她不该有反应。

        不然会变成"滚一下抖一下" —— 用户滚到底是常见动作。
        """
        app = self._app()
        app._apply_scale(P.SCALE_MAX)
        app.stretch_until = 0.0
        self._wheel(app)
        self.assertNotEqual(app._overlay_kind(time.time()), "stretch",
                            "到顶了还在抖")

    def test_feeding_shows_crumbs(self):
        """喂食要有"在吃"的装饰，不是借「被戳」那套。"""
        app = self._app()
        app._do_feed(pet_food.FOOD_IDS[0])
        self.assertEqual(app._overlay_kind(time.time()), "munch")
        canvas = self.capture(app, poll=False)
        self.assertIsNotNone(canvas)

    def test_waking_up_is_different_from_being_petted(self):
        """睡着被戳 = 惊醒，和醒着被戳**不是一回事**。

        原来两条走同一条路（同一套动画、同一句话），完全看不出区别。

        **断言的是翻译后的表**（`I.t(x) for x in S.STARTLE_LINES`），
        不是中文原表 —— `say_line()` 返回的是当前语言那句，
        CI runner 是英文环境，直接比中文表会挂（规则 6）。
        这样写任何语言下都对，不用钉语言。
        """
        app = self._app()
        app.snap.state = "sleeping"
        app._moved = False
        app._drag = (0, 0, 0, 0)
        app._on_release(type("E", (), {"y": 10, "x": 10, "x_root": 0, "y_root": 0})())
        want = [I.t(x) for x in S.STARTLE_LINES]
        self.assertIn(app.companion.bubble, want,
                      f"说的不是惊醒的话：{app.companion.bubble!r}")

    def test_awake_poke_still_uses_the_normal_lines(self):
        """醒着被戳不能变成惊醒的话。"""
        app = self._app()
        app.snap.state = "idle"
        app._moved = False
        app._drag = (0, 0, 0, 0)
        startle = [I.t(x) for x in S.STARTLE_LINES]
        # 随机性：多戳几次，一次都不该是惊醒的话
        for _ in range(12):
            app.pet_streak = 0
            app.annoyed_until = 0.0
            app.companion.bubble = ""
            app._moved = False
            app._drag = (0, 0, 0, 0)
            app._on_release(type("E", (), {"y": 10, "x": 10,
                                           "x_root": 0, "y_root": 0})())
            self.assertNotIn(app.companion.bubble, startle,
                             "醒着被戳说了惊醒的话")

    def test_all_three_overlays_render(self):
        """三个新装饰都要画得出来（相位 0 最容易崩 —— `stretch` 就崩过）。"""
        for kind in ("stretch", "exclaim", "munch"):
            for ph in (0.0, 0.25, 0.5, 0.75, 1.0):
                with self.subTest(kind=kind, phase=ph):
                    im = overlay_for(kind, ph)
                    self.assertIsNotNone(im)

    def test_stretch_has_content_at_phase_zero(self):
        """相位 0 曾经抛 `ValueError: y1 must be >= y0` —— 弧的包围盒写反了。"""
        im = overlay_for("stretch", 0.0)
        self.assertIsNotNone(im.getbbox(), "相位 0 什么都没画")


class TestSettingsLayout(E2EBase):
    """设置窗口的排布 —— **四个 Tab**，而且小屏放得下。

    原来是扁平长列表：**891px = 屏高的 82%**，而且**没有滚动** ——
    768px 的笔记本上底部的「关闭」和热键说明**点不到**。
    """

    def setUp(self):
        super().setUp()
        self._lang_before = I.current()
        self.write_config(lang="zh", skin="openpets")

    def tearDown(self):
        I.set_language(self._lang_before)
        super().tearDown()

    @staticmethod
    def _widgets(w, cls):
        """按控件类找。**ttk 与否都认。**

        `ttk.Button` 的 `winfo_class()` 是 `TButton`，`tk.Button` 是
        `Button`。测试不该关心用哪套 —— 换了 ttk 之后十几处断言会全挂，
        而它们本来测的是"有几个按钮"，不是"按钮是哪个实现的"。
        """
        want = {cls, "T" + cls}
        out = []

        def walk(x):
            for c in x.winfo_children():
                try:
                    if c.winfo_class() in want:
                        out.append(c)
                except Exception:                        # noqa: BLE001
                    pass
                walk(c)

        walk(w)
        return out

    def _open(self):
        app = self.make_app(force_state=None, no_companion=False, skin="openpets")
        app._open_settings()
        app.root.update()
        win = app._settings_win
        self.assertIsNotNone(win, "设置窗口没建起来")
        nbs = self._widgets(win, "TNotebook")
        self.assertEqual(len(nbs), 1, "没有 Notebook")
        return app, win, nbs[0]

    def test_four_tabs_with_the_right_names(self):
        _app, _win, nb = self._open()
        # **strip** —— Tab 文字两边有填充空格（为了让 Tab 好看），
        # 但测试断言的是"有哪些 Tab"，不是"两边有没有空格"。
        names = [nb.tab(i, "text").strip() for i in range(nb.index("end"))]
        self.assertEqual(names, ["行为", "外观", "对话", "关于"], str(names))

    def test_window_fits_a_small_screen(self):
        """**这是重排的主要理由。**

        原来 891px —— 768px 的笔记本上放不下，而且没有滚动。
        """
        app, win, _nb = self._open()
        win.update_idletasks()
        h = win.winfo_height()
        self.assertLess(h, 891, f"没变矮：{h}px")
        self.assertLessEqual(h, 768, f"{h}px，768px 的屏幕放不下")

    def test_language_dropdown_shows_labels_not_keys(self):
        """语言下拉框要显示 **label**，不是 `LANGS` 里的 key。

        和宠物库那个是**一模一样的 bug**：定义了 `LANG_LABELS` 却把
        `LANGS` 的 key 传给了 OptionMenu，界面上显示 `auto`。

        **不能断言具体是哪个 label** —— `setUp` 里写了 `lang="zh"`，
        正确显示就是「中文」。要断言的是"它在 label 集合里、不在 key 集合里"。
        （第一版写死断言「跟随系统」，于是挂在一个**正确**的行为上。）
        """
        _app, win, nb = self._open()
        beh = nb.winfo_children()[0]
        oms = self._widgets(beh, "Menubutton")
        self.assertEqual(len(oms), 1, f"行为页有 {len(oms)} 个下拉框")
        vn = str(oms[0].cget("textvariable"))
        val = win.getvar(vn)
        self.assertIn(val, set(I.LANG_LABELS.values()),
                      f"显示的是 {val!r} —— 那是 key 吧")
        self.assertNotIn(val, set(I.LANGS), f"显示的是 key {val!r}，该是 label")

    def test_appearance_tab_has_the_skin_buttons(self):
        """**按钮归位** —— 它们属于「皮肤」，原来挂在最下面隔了两个区块。"""
        _app, _win, nb = self._open()
        look = nb.winfo_children()[1]
        texts = [str(b.cget("text")) for b in self._widgets(look, "Button")]
        for want in ("动画映射", "宠物库", "皮肤预览"):
            with self.subTest(button=want):
                self.assertTrue(any(want in x for x in texts), str(texts))
        self.assertTrue(self._widgets(look, "Menubutton"), "外观页没有皮肤下拉框")

    def test_about_tab_has_the_records_buttons(self):
        _app, _win, nb = self._open()
        about = nb.winfo_children()[3]
        texts = [str(b.cget("text")) for b in self._widgets(about, "Button")]
        for want in ("位置复位", "重置相处记录", "出错日志"):
            with self.subTest(button=want):
                self.assertTrue(any(want in x for x in texts), str(texts))

    def test_status_and_close_live_outside_the_tabs(self):
        """状态提示和「关闭」在**底部条**上 —— 任何 Tab 下都看得见。

        判据是"**不在 Notebook 的页里**"。

        两次写错这条：
          · 第一版遍历顶层子控件再断言"每个都不是 Notebook"，
            而 Notebook 自己就在顶层 —— 必然失败。
          · 第二版只看 `win` 的直接子控件，而「关闭」现在在一个底部
            `bar` Frame 里 —— 扫不到。

        **断言的是"在不在 Tab 页里"，不是"在第几层父控件下面"。**
        """
        _app, win, nb = self._open()
        inside = set(nb.winfo_children())                # 四个 Tab 页

        def outside_widgets(w):
            """不在 Notebook 页里的控件（递归）。"""
            out = []
            for c in w.winfo_children():
                if c in inside or c is nb:
                    continue
                out.append(c)
                out.extend(outside_widgets(c))
            return out

        outer = outside_widgets(win)
        btns = []
        for c in outer:
            try:
                if c.winfo_class() in ("Button", "TButton"):
                    btns.append(str(c.cget("text")))
            except Exception:                            # noqa: BLE001
                pass
        self.assertTrue(any("关闭" in x for x in btns), f"关闭不在底部：{btns}")
        labels = [c for c in outer
                  if c.winfo_class() in ("Label", "TLabel")]
        self.assertTrue(labels, "底部没有状态标签")

    def test_nothing_was_lost(self):
        """重排不能弄丢功能。"""
        _app, _win, nb = self._open()
        beh, look, chat, about = nb.winfo_children()[:4]
        self.assertEqual(len(self._widgets(beh, "Checkbutton")), 4,
                         "行为页该有 4 个复选框")
        self.assertEqual(len(self._widgets(chat, "Entry")), 2,
                         "对话页该有 2 个输入框")
        chat_btns = [str(b.cget("text")) for b in self._widgets(chat, "Button")]
        self.assertEqual(len(chat_btns), 3, str(chat_btns))
        # 相处记录还在
        texts = [str(x.cget("text")) for x in self._widgets(about, "Label")]
        self.assertTrue(any("相处记录" in x for x in texts), str(texts[:6]))
        # 热键表**在行为页** —— 有意从「关于」挪过来的：行为页原来只有
        # 四个复选框 + 一个下拉框，底下空一大片，热键放这儿正好填上。
        beh_texts = [str(x.cget("text")) for x in self._widgets(beh, "Label")]
        self.assertTrue(any("热键" in x for x in beh_texts), str(beh_texts[:8]))
        self.assertTrue(any("Ctrl+Alt+P" in x for x in beh_texts), str(beh_texts[:12]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
