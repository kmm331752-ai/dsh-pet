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
import pet_i18n as I  # noqa: E402


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
        # 语言固定成中文，别跟着机器走 —— CI 的 runner 是英文环境，
        # 而有些断言（比如菜单里有没有「安装」）是看文案的。
        # 不固定的话同一份代码在本地过、在 CI 挂（踩过）。
        self._orig_lang = I.current()
        I.set_language("zh")
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
        I.set_language(self._orig_lang)
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
        # 别真扫会话历史 —— 慢，而且依赖机器上有多少文件（规则 6）
        app._lint_no_scan = True
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

    # ── 所有对话框都要能打开 ──
    #
    # 这一组是补的：设置窗口一直打不开，因为 pad 字典里写了 anchor="w"，
    # 而 anchor 是 pack 的选项，grid 要用 sticky —— 第一个 Checkbutton
    # 就抛 TclError。而 Tk 会把回调里的异常吞掉（只打到 stderr），
    # 用户看到的就是「右键点设置没反应」。
    #
    # 冒烟测试当时只测了主窗口，没碰任何 Toplevel，所以全绿。
    # 教训：**每个能打开的窗口都要有一条测试**，不是只测主窗口。

    def _open_and_check(self, app, opener, name):
        opener()
        app.root.update()
        wins = [w for w in app.root.winfo_children() if w.winfo_class() == "Toplevel"]
        self.assertTrue(wins, f"{name} 没建出窗口")
        win = wins[-1]
        self.assertTrue(win.winfo_exists(), f"{name} 窗口不存在")
        self.assertGreater(win.winfo_width(), 1, f"{name} 宽度不对")
        self.assertGreater(win.winfo_height(), 1, f"{name} 高度不对")
        win.destroy()
        app.root.update()

    def test_settings_window_opens(self):
        app = self._app()
        app.root.update()
        self._open_and_check(app, app._open_settings, "设置窗口")

    def test_settings_window_is_reusable(self):
        """再点一次应该复用已开的窗口，而不是堆一叠。"""
        app = self._app()
        app.root.update()
        app._open_settings()
        app.root.update()
        first = app._settings_win
        app._open_settings()
        app.root.update()
        self.assertIs(app._settings_win, first, "第二次点击又建了一个窗口")

    @staticmethod
    def _walk(widget) -> list:
        """递归收集一个窗口里的所有控件。

        只数直接子控件是不够的 —— 设置窗口的按钮装在 Frame 里，
        直接数只会数到 1 个（"关闭"），看着像少了但其实是数法不对。
        """
        out = []
        stack = [widget]
        while stack:
            w = stack.pop()
            out.append(w)
            try:
                stack.extend(w.winfo_children())
            except Exception:                            # noqa: BLE001
                pass
        return out

    def test_settings_checkbuttons_all_exist(self):
        """设置窗口里每个复选框都要真的建出来。

        直接数 widget —— pad 参数写错会让某一个 Checkbutton 抛异常，
        而它后面的控件全都不会建。
        """
        app = self._app()
        app.root.update()
        app._open_settings()
        app.root.update()
        win = app._settings_win
        widgets = self._walk(win)
        checks = [w for w in widgets
              if w.winfo_class() in ("Checkbutton", "TCheckbutton")]
        self.assertGreaterEqual(len(checks), 4, f"复选框只有 {len(checks)} 个")
        buttons = [w for w in widgets
               if w.winfo_class() in ("Button", "TButton")]
        # 位置复位 / 重置相处记录 / 出错日志 / 关闭
        self.assertGreaterEqual(len(buttons), 4, f"按钮只有 {len(buttons)} 个")
        win.destroy()
        app._settings_win = None

    def test_error_log_window_opens(self):
        app = self._app()
        app.root.update()
        self._open_and_check(app, app._show_error_log, "出错日志窗口")

    def test_skin_install_dialog_opens(self):
        app = self._app()
        app.root.update()
        self._open_and_check(app, app._install_skin_dialog, "皮肤安装窗口")

    def test_skin_remove_dialog_opens_or_says_so(self):
        """没有可删的皮肤时不弹窗、只说一句 —— 也不该崩。"""
        app = self._app()
        app.root.update()
        app._remove_skin_dialog()                        # 不该抛
        app.root.update()

    def test_skin_menu_has_install_entries(self):
        app = self._app()
        app.root.update()
        labels = []
        for i in range(app._skin_menu.index("end") + 1):
            try:
                if app._skin_menu.type(i) == "command":
                    labels.append(app._skin_menu.entrycget(i, "label"))
            except Exception:                            # noqa: BLE001
                pass
        # 比对翻译后的文案而不是写死中文 —— 这样切到英文也成立
        for key in ("预览…", "从链接安装…", "删除皮肤…"):
            with self.subTest(item=key):
                self.assertIn(I.t(key), labels, labels)

    # ── 新窗口也要能打开 ──
    def test_skin_preview_opens(self):
        app = self._app()
        app.root.update()
        self._open_and_check(app, app._open_skin_preview, "皮肤预览")
        app._preview_win = None

    def test_skin_thumbnails_render(self):
        app = self._app()
        app.root.update()
        for sid in ("dafeiyu", "kun-like", "matcha-sunny"):
            with self.subTest(skin=sid):
                thumb = app._skin_thumb(sid)
                self.assertIsNotNone(thumb, f"{sid} 缩略图生成失败")
                self.assertGreater(thumb.width, 0)

    def test_session_actions_window_opens(self):
        app = self._app()
        app.root.update()
        app.snap = P.Snapshot(state="tools", session_id="s1", title="测试任务",
                              tasks=[P.TaskInfo(session_id="s1", state="tools",
                                                title="测试任务")])
        self._open_and_check(app, app._open_session_actions, "会话操作")
        app._sess_win = None

    def test_session_actions_opens_without_tasks(self):
        """没有活跃会话时也要能开（说明一下就行），不该崩。"""
        app = self._app()
        app.root.update()
        app.snap = P.Snapshot(state="idle")
        app._open_session_actions()
        app.root.update()
        self.assertIsNotNone(app._sess_win)
        app._sess_win.destroy()
        app._sess_win = None

    def _app_with_companion(self):
        """建一个带 companion 的 app。

        force_state / demo 都会把 companion 关掉，而喂食必须有它 ——
        所以直接塞一个（和 e2e 里 TestObservedMemory 的做法一样）。
        """
        import pet_sources as _S
        app = self._app()
        app.root.update()
        app.companion = _S.CompanionSource()
        return app

    def test_feed_window_opens(self):
        """喂食窗口要能开，12 个食物都要有图。"""
        app = self._app_with_companion()
        self._open_and_check(app, app._open_feed_window, "喂食窗口")
        self.assertGreaterEqual(len(app._feed_imgs), 10, "食物图没加载出来")
        app._feed_win = None

    def test_feeding_changes_satiety(self):
        """喂一口，饱足要涨。走 _do_feed，不是直接调 pet_food。"""
        app = self._app_with_companion()
        m = app.companion.mood
        m.satiety = 0.0
        app._do_feed("ramen")
        self.assertGreater(m.satiety, 0.0, "喂了但饱足没涨")
        self.assertEqual(m.feeds, 1)

    def test_feeding_when_full_does_nothing(self):
        app = self._app_with_companion()
        m = app.companion.mood
        m.satiety = 0.99
        app._do_feed("candy")
        self.assertEqual(m.feeds, 0, "吃饱了还喂进去了")

    def test_weekly_report_exports(self):
        app = self._app(no_companion=False)
        app.root.update()
        app._export_week()
        out = os.path.join(self.tmp, "周报.png")
        self.assertTrue(os.path.exists(out), "周报没生成")
        self.assertGreater(os.path.getsize(out), 1000)

    def test_click_on_card_opens_sessions_not_poke(self):
        """点信息卡 = 会话操作；点角色 = 摸她。

        这条守着那次改动 —— 两件事的意图完全不同，混在一起两边都不好用。
        """
        app = self._app()
        app.root.update()

        class E:
            x_root = 100
            y_root = 100
            x = 10
            y = P.PET_AREA_H + 20                        # 卡片区
        app._on_press(E())
        app._on_release(E())
        app.root.update()
        self.assertIsNotNone(getattr(app, "_sess_win", None),
                             "点卡片没开会话窗口")
        app._sess_win.destroy()
        app._sess_win = None

    def test_click_on_character_pokes(self):
        app = self._app()
        app.root.update()
        before = app.companion.mood.pokes if app.companion else 0

        class E:
            x_root = 100
            y_root = 100
            x = 10
            y = 10                                       # 角色区
        app._on_press(E())
        app._on_release(E())
        app.root.update()
        if app.companion is not None:
            self.assertGreater(app.companion.mood.pokes, before, "点角色没记一笔")

    def test_quit_is_clean(self):
        app = P.PetApp(self._args())
        app.root.update()
        app._quit()                                      # 不该抛
        with self.assertRaises(Exception):
            app.root.winfo_exists()                      # 窗口确实销毁了


if __name__ == "__main__":
    unittest.main(verbosity=2)
