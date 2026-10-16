"""冒烟测试：真的把桌宠建起来。

    python tests/test_smoke.py

和 test_pet.py 的区别：那边测的是纯函数（状态机、渲染、解析），
这边**真的 new 一个 PetApp**，跑一轮事件循环，检查窗口和菜单都在。

为什么必须有这一层：两次发版事故（self.greeted 属性不存在、
_skin_menu 没创建）都是**启动即崩**，而单元测试一个都没拦住 ——
因为它们根本不构造 PetApp。静态扫源码能拦住已知的那一个形状，
拦不住下一个同类。

成本极低（Windows runner 有桌面会话，Tk 能跑），收益是「改完至少还能启动」。

副作用控制：用临时目录当配置目录，不碰你真实的 pet_config.json /
pet_mood.json；托盘和热键都关掉，不弹图标、不占快捷键。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dsh_pet as P  # noqa: E402
import pet_base as B  # noqa: E402


def tk_available() -> bool:
    """能不能建 Tk 窗口。无头环境会失败，那种情况跳过而不是报错。"""
    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:                                    # noqa: BLE001
        return False


TK_OK = tk_available()


@unittest.skipUnless(TK_OK, "没有可用的 Tk（无头环境）")
class TestAppSmoke(unittest.TestCase):
    """PetApp 真的能起来、菜单齐全、能干净退出。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-pet-smoke-")
        # 只改一处：pet_base.data_dir 是所有可变路径的唯一真实来源，
        # 配置、相处记录、出错日志、台词包都从它派生。
        # （拆文件之前是改 CONFIG_PATH/MOOD_PATH 那几个常量 ——
        #   拆开之后常量会被复制进各模块，改一处不影响别处，所以改成了函数。）
        self._orig_data_dir = B.data_dir
        B.data_dir = lambda: self.tmp
        # 托盘：把 pystray 从模块表里摘掉，import 会失败并被 _start_tray 吞掉。
        # 否则测试期间会真的弹一个托盘图标，而且它起线程，退出时不干净。
        self._saved_pystray = sys.modules.pop("pystray", None)
        sys.modules["pystray"] = None                    # type: ignore[assignment]

    def tearDown(self):
        B.data_dir = self._orig_data_dir
        sys.modules.pop("pystray", None)
        if self._saved_pystray is not None:
            sys.modules["pystray"] = self._saved_pystray
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _args(self, **kw) -> argparse.Namespace:
        base = dict(demo=True, force_state=None, session=None, shot=None,
                    skin="dafeiyu", list_skins=False, privacy=False,
                    no_companion=False, no_hotkeys=True)
        base.update(kw)
        return argparse.Namespace(**base)

    def _app(self, **kw):
        """建一个 PetApp，返回 (app, 清理函数)。"""
        app = P.PetApp(self._args(**kw))
        self.addCleanup(lambda: self._safe_quit(app))
        return app

    @staticmethod
    def _safe_quit(app) -> None:
        try:
            app._quit()
        except Exception:                                # noqa: BLE001
            pass

    # ── 核心：能起来 ──
    def test_app_constructs_and_window_exists(self):
        """构造 + 跑一轮事件循环。

        这条就是拦 _skin_menu 那种 bug 的：__init__ 里任何一步抛异常都会
        在这里炸，而不是等用户双击 exe 才发现。
        """
        app = self._app()
        app.root.update()                                # 真跑一轮事件循环
        self.assertTrue(app.root.winfo_exists())
        self.assertEqual(app.root.title(), "DSH Pet")

    def test_window_size_matches_scale(self):
        app = self._app()
        app.root.update()
        self.assertEqual(app.root.winfo_width(), app.win_w)
        self.assertEqual(app.root.winfo_height(), app.win_h)

    def test_menu_is_fully_built(self):
        """菜单每一项都建出来了 —— add_cascade 引用不存在的子菜单会在这里炸。"""
        app = self._app()
        app.root.update()
        self.assertGreater(app.menu.index("end"), 8, "菜单项太少，可能少建了")
        # 逐个 cascade 展开，子菜单必须有内容
        end = app.menu.index("end")
        cascades = 0
        for i in range(end + 1):
            try:
                if app.menu.type(i) != "cascade":
                    continue
                cascades += 1
                sub = app.menu.entrycget(i, "menu")
                self.assertTrue(sub, f"第 {i} 项 cascade 没有子菜单")
            except Exception as exc:                     # noqa: BLE001
                self.fail(f"展开第 {i} 个菜单项失败：{exc}")
        self.assertGreaterEqual(cascades, 2, "皮肤和番茄钟两个 cascade 都该在")

    def test_skin_menu_has_entries(self):
        app = self._app()
        app.root.update()
        self.assertGreater(app._skin_menu.index("end"), 0, "皮肤菜单是空的")

    def test_pomodoro_menu_has_choices(self):
        app = self._app()
        app.root.update()
        # 三个时长 + 分隔线 + 取消 = 至少 4 项
        self.assertGreaterEqual(app._pomo_menu.index("end"), 4)

    def test_render_once_through_tick(self):
        """跑一次 _tick，确认渲染管线真的出图、不抛异常。"""
        app = self._app()
        app._poll()
        app._tick()
        app.root.update()
        self.assertTrue(app.root.winfo_exists())

    # ── 各皮肤都能起来 ──
    def test_every_skin_starts(self):
        for sid in ("dafeiyu", "kun-like"):
            with self.subTest(skin=sid):
                app = self._app(skin=sid)
                app.root.update()
                self.assertTrue(app.root.winfo_exists())
                self.assertEqual(app.skin.name, sid)

    # ── 缩放路径 ──
    def test_scale_rebuilds_display(self):
        app = self._app()
        app.root.update()
        before = (app.win_w, app.win_h)
        app._apply_scale(1.5)
        app.root.update()
        self.assertGreater(app.win_w, before[0])
        self.assertGreater(app.win_h, before[1])
        self.assertEqual(app.root.winfo_width(), app.win_w)
        app._tick()                                      # 缩放后还能渲染

    def test_scale_is_clamped(self):
        app = self._app()
        app._apply_scale(99.0)
        self.assertLessEqual(app.scale, P.SCALE_MAX)
        app._apply_scale(0.01)
        self.assertGreaterEqual(app.scale, P.SCALE_MIN)

    # ── 交互回调不该崩 ──
    def test_interaction_callbacks_survive_without_mouse(self):
        """直接调交互回调，不依赖真实鼠标事件。

        合成鼠标事件送不到 Tk 绑定上（试过 PostMessage 和 SendInput），
        所以这里手工构造事件对象。
        """
        class E:
            x_root = 100
            y_root = 100
            x = 10
            y = 10
            delta = 120

        app = self._app()
        app.root.update()
        for name in ("_on_press", "_on_motion", "_on_drag", "_on_release",
                     "_on_leave", "_on_wheel"):
            with self.subTest(callback=name):
                getattr(app, name)(E())
        app._cancel_long_press()
        app._long_press_fired()                          # 没在拖动，应当直接返回
        app._overlay_kind(0.0)
        app.root.update()

    def test_feed_and_pomodoro_do_not_crash(self):
        app = self._app(no_companion=False)
        app.root.update()
        app._feed()
        app._start_pomodoro(25)
        self.assertTrue(app.pomo_until > 0)
        app._cancel_pomodoro()
        self.assertEqual(app.pomo_until, 0.0)

    def test_ghost_mode_round_trip(self):
        app = self._app()
        app.root.update()
        app._toggle_ghost()
        app.root.update()
        self.assertTrue(app._ghost)
        app._toggle_ghost()
        app.root.update()
        self.assertFalse(app._ghost)

    def test_export_record_writes_a_file(self):
        app = self._app()
        app.root.update()
        app._export_record()
        out = os.path.join(self.tmp, "相处记录.png")
        self.assertTrue(os.path.exists(out), "导出没生成文件")
        self.assertGreater(os.path.getsize(out), 1000)
        # 确认没写到仓库里
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertFalse(os.path.exists(os.path.join(repo, "相处记录.png")),
                         "导出写到了仓库根目录，测试没隔离干净")

    def test_quit_is_clean(self):
        app = P.PetApp(self._args())
        app.root.update()
        app._quit()                                      # 不该抛
        with self.assertRaises(Exception):
            app.root.winfo_exists()                      # 窗口确实销毁了


if __name__ == "__main__":
    unittest.main(verbosity=2)
