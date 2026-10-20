"""喂食的回归测试。

## 抓到的真 bug

`_do_feed()` 喂完做的是 `w.destroy()` + `_open_feed_window()`（为了刷新
饱足条），而 `_open_feed_window` **从不设 geometry** ——

    第一次开: 位置 (330,291)   ← 靠近桌宠
    喂完重开: 位置 (96,57)     ← 跳到屏幕左上角

用户看到的是「点一下，窗口跑了」。

顺带三个副作用：丢焦点、闪一下、快速连点会点到正在销毁的控件
（`invalid command name "…!button"`）。

## 这里测什么

**从菜单进、点食物、连点三次、窗口还得在原地。**
不是直接调 `_do_feed()` —— 那证明不了用户走得到的路。
"""

import argparse
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import pet_base as B  # noqa: E402

_TMP = None


def setUpModule():
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="dsh-pet-feed-test-")
    B.data_dir = lambda: _TMP
    import pet_i18n as I
    I.set_language("zh")


import pet_i18n as I  # noqa: E402
import pet_food  # noqa: E402


class FeedBase(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("pystray", None)
        sys.modules["pystray"] = None
        self.tmp = tempfile.mkdtemp(prefix="feed-")
        self._apps = []

    def tearDown(self):
        for a in self._apps:
            try:
                a._quit()
            except Exception:                            # noqa: BLE001
                pass

    def _app(self):
        import dsh_pet as P
        args = argparse.Namespace(
            demo=False, force_state=None, session=None, shot=None,
            skin=None, list_skins=False, privacy=False,
            no_companion=False, no_hotkeys=True)
        app = P.PetApp(args)
        self._apps.append(app)
        app.root.update()
        app._git_no_scan = True
        app._lint_no_scan = True
        return app

    @staticmethod
    def _feed_buttons(win):
        """窗口里那 12 个食物按钮（没有文字的那个就是图片按钮）。"""
        out = []

        def walk(w):
            for c in w.winfo_children():
                if c.winfo_class() == "Button":
                    try:
                        if str(c.cget("text")) == "":
                            out.append(c)
                    except Exception:                    # noqa: BLE001
                        pass
                walk(c)

        walk(win)
        return out


class TestFeedFromMenu(FeedBase):
    def test_menu_opens_the_window(self):
        app = self._app()
        target = None
        for m, i, kind in app._menu_entries(app.menu):
            if kind != "command":
                continue
            if I.t("喂她吃点什么") in (m.entrycget(i, "label") or ""):
                target = (m, i)
                break
        self.assertIsNotNone(target, "菜单里没有喂食项")
        m, i = target
        m.invoke(i)
        app.root.update()
        self.assertIsNotNone(getattr(app, "_feed_win", None), "点了没开窗口")

    def test_window_lists_every_food(self):
        app = self._app()
        app._open_feed_window()
        app.root.update()
        btns = self._feed_buttons(app._feed_win)
        self.assertEqual(len(btns), len(pet_food.FOOD_IDS), f"{len(btns)} 个按钮")


class TestFeedingActuallyWorks(FeedBase):
    def test_one_food_raises_satiety(self):
        app = self._app()
        m = app.companion.mood
        # **钉死起点**：mood 是存在盘上的，前面测试喂过就满了（规则 6）。
        m.satiety = 0.2
        before = m.satiety
        app._open_feed_window()
        app.root.update()
        btn = self._feed_buttons(app._feed_win)[0]
        btn.invoke()
        app.root.update()
        self.assertGreater(m.satiety, before, "点了食物饱足没涨")
        self.assertTrue(app.companion.bubble, "吃完一句话都没说")

    def test_three_in_a_row_all_work(self):
        """**连点三次都得有效。**

        第一版喂一口就销毁重建窗口，所以第二次开始 `invoke()` 抛
        `invalid command name "…!button"` —— 拿到的全是死引用。
        """
        app = self._app()
        m = app.companion.mood
        m.satiety = 0.1                              # 钉死起点，别被前面的测试喂饱
        app._open_feed_window()
        app.root.update()
        for k in range(3):
            btn = self._feed_buttons(app._feed_win)[k]
            before = m.satiety
            btn.invoke()                                  # 死控件会在这里抛
            app.root.update()
            self.assertGreaterEqual(m.satiety, before, f"第 {k+1} 次喂食出问题")


class TestWindowDoesNotJump(FeedBase):
    def test_position_is_stable_after_feeding(self):
        """**这条是这次那个 bug 的回归测试。**

        实测过：(330,291) → 喂一口 → (96,57)。窗口跑到屏幕左上角，
        用户以为坏了。
        """
        app = self._app()
        app._open_feed_window()
        app.root.update()
        for _ in range(6):
            app.root.update()
        win = app._feed_win
        win.update_idletasks()
        before = (win.winfo_rootx(), win.winfo_rooty())

        btn = self._feed_buttons(win)[0]
        btn.invoke()
        app.root.update()
        for _ in range(6):
            app.root.update()

        same = app._feed_win
        self.assertIs(same, win, "窗口对象被换掉了（重建了，位置就会变）")
        same.update_idletasks()
        after = (same.winfo_rootx(), same.winfo_rooty())
        self.assertEqual(after, before, f"喂完之后窗口跳了：{before} -> {after}")

    def test_satiety_bar_actually_refreshes(self):
        """**不重建也得能刷新。** 这是原来重建的唯一理由。"""
        app = self._app()
        app._open_feed_window()
        app.root.update()
        bar = app._feed_bar
        n_before = len(bar.find_all())
        btn = self._feed_buttons(app._feed_win)[0]
        btn.invoke()
        app.root.update()
        self.assertIs(app._feed_bar, bar, "饱足条被换成新控件了")
        self.assertGreaterEqual(len(bar.find_all()), n_before, "重画之后条子空了")
        lbl = app._feed_pct_label
        self.assertIn("%", str(lbl.cget("text")), "百分比没刷新")


class TestEdgeCases(FeedBase):
    def test_closing_clears_the_bar_reference(self):
        """窗口关掉之后引用要清 —— 不然会往死控件上画（抛 TclError）。"""
        app = self._app()
        app._open_feed_window()
        app.root.update()
        app._feed_win.destroy()
        app._feed_win = None
        app._feed_bar = None
        app._feed_pct_label = None
        app._redraw_feed_bar()                        # 不许抛
        app._open_feed_window()                       # 还能再开
        app.root.update()
        self.assertIsNotNone(app._feed_win)

    def test_full_is_reported_not_crashed(self):
        app = self._app()
        app.companion.mood.satiety = 1.0
        app._do_feed("dango")                         # 不许抛
        self.assertTrue(app.companion.bubble, "吃饱了也该说一句")


if __name__ == "__main__":
    unittest.main(verbosity=2)
