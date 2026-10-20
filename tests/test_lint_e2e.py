"""端到端：反复踩的坑要走 `_poll` 才看得见。

## 为什么这一层不能省

`AGENTS.md` 规则 1 记了**四次**同一个事故：功能挂在只走一条分支的路径上。

第一次（配饰）：直接调 `draw_accessory()` —— 通过，真路径上两个 bug 都没测到。
第二次（久坐提醒）：直接调 `_health_check()` —— 通过，但那函数只在 DSH 空闲
时跑，而久坐发生在你**干活**的时候。实测调用 0 次。

这个功能最容易重演第二次：报错恰恰发生在 DSH **正在跑工具**的时候。
所以测试必须：

  · `force_state=None` → 走 watcher 分支（不是 demo 快照）
  · 塞一个 `error=` 的 snapshot → 模拟"你刚踩了个坑"
  · 跑 `app._poll()`
  · **断言最终 snapshot 上的 `bubble`**，不是 `companion.bubble`

规则 1 的第四次事故就是这么漏的：中间变量有值，最后被清掉了。
"""

import argparse
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import pet_base as B  # noqa: E402
import pet_i18n as I  # noqa: E402

_TMP = None


def setUpModule():
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="dsh-pet-lint-e2e-")
    B.data_dir = lambda: _TMP
    # ttk/设置窗口那套会让 P 用到真目录，钉死
    import pet_i18n as I
    I.set_language("zh")


class LintE2EBase(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("pystray", None)
        sys.modules["pystray"] = None
        self.tmp = tempfile.mkdtemp(prefix="pet-lint-")
        self._apps = []

    def tearDown(self):
        for a in self._apps:
            try:
                a._quit()
            except Exception:                            # noqa: BLE001
                pass

    def _preload(self, app, sig_head, n):
        """直接给 linter 灌 n 次 —— 不用真扫盘，好控。"""
        for _ in range(n):
            app.lint.note(sig_head)

    def _app(self, **kw):
        import dsh_pet as P
        import pet_lint as L
        args = argparse.Namespace(
            demo=False, force_state=None, session=None, shot=None,
            skin=kw.pop("skin", None), list_skins=False,
            privacy=kw.pop("privacy", False),
            no_companion=kw.pop("no_companion", False), no_hotkeys=True)
        app = P.PetApp(args)
        self._apps.append(app)
        app.root.update()
        # **别真扫盘** —— 不然每条测试都读机器上的会话文件，又慢又
        # 依赖机器状态（规则 6）。这几条测的是"新报错来了她认不认得"，
        # 不需要真历史：计数由 `_preload` 直接灌。
        app._lint_no_scan = True
        # git 也一样 —— 否则每条测试都去跑 `git log`，
        # 又慢又依赖机器上有哪些仓库（规则 6）。
        app._git_no_scan = True
        # **每个测试一份干净的 linter。**
        # 第一版共用 `data_dir` 下的同一个状态文件，于是前面测试攒的计数
        # 漏到后面 —— `test_oneoff_error_says_nothing` 拿到「第 5 次」。
        # 规则 6：测试不许依赖别的测试跑过什么。
        app.lint = L.Linter(os.path.join(self.tmp, "lint.json"))
        return app

    def _stub(self, app, state="tools", **kw):
        import dsh_pet as P
        snap = P.Snapshot(state=state, session_id="x", title="t", **kw)
        app.watcher = type("W", (), {
            "poll": staticmethod(lambda: snap),
            "_new_completion": False,
        })()
        return snap

    def _bubble_text(self, app) -> str:
        """**最终 snapshot 上的气泡** —— 不是 companion 的中间变量。"""
        app._poll()
        return app.snap.detail_line or ""


class TestRecurringPitfallReachesTheSnapshot(LintE2EBase):
    def test_recurring_error_makes_her_speak(self):
        """走 `_poll`，看**最终 snapshot** 上有没有那句话。"""
        app = self._app()
        app.watcher = None
        self._preload(app, 'Error: cannot edit "G:\\x\\a.vue": file changed since it was read', 3)
        self._stub(app, state="tools",
                   error='cannot edit "G:\\y\\b.vue": file changed since it was read')
        text = self._bubble_text(app)
        self.assertTrue(text, "她一句话都没说")
        # **断言翻译后的值，不钉语言** —— CI 是英文环境，钉死 "又踩到"
        # 这条会挂（规则 6，第四次了）。签名是语言无关的，一起断。
        self.assertIn(I.t("又踩到这个坑："), text)
        self.assertIn("cannot edit", text)

    def test_oneoff_error_says_nothing(self):
        """**只出现过一次的坑不许报** —— 偶然手滑不是规律。"""
        app = self._app()
        app.watcher = None
        self._preload(app, 'Error: cannot edit "G:\\x\\a.vue": file changed since it was read', 1)
        self._stub(app, state="tools",
                   error='cannot edit "G:\\y\\b.vue": file changed since it was read')
        self.assertEqual(self._bubble_text(app), "")

    def test_unknown_error_says_nothing(self):
        app = self._app()
        app.watcher = None
        self._preload(app, 'Error: cannot edit "G:\\x\\a.vue": file changed since it was read', 9)
        self._stub(app, state="tools", error="Error: something brand new")
        self.assertEqual(self._bubble_text(app), "")

    def test_no_error_no_bubble(self):
        app = self._app()
        app.watcher = None
        self._preload(app, 'Error: x', 9)
        self._stub(app, state="tools", error="")
        self.assertEqual(self._bubble_text(app), "")

    def test_same_error_twice_only_speaks_once(self):
        """**别烦人**：同一个错连着来两轮，只说一次。"""
        app = self._app()
        app.watcher = None
        self._preload(app, 'Error: cannot edit "G:\\x\\a.vue": file changed since it was read', 5)
        err = 'cannot edit "G:\\y\\b.vue": file changed since it was read'
        self._stub(app, state="tools", error=err)
        # **断言翻译后的值，不钉语言** —— CI 是英文环境，钉死 "又踩到"
        # 这条会挂（规则 6，这是第四次）。签名是语言无关的，一起断。
        text = self._bubble_text(app)
        self.assertIn(I.t("又踩到这个坑："), text)
        self.assertIn("cannot edit", text)
        # 气泡过期，然后同一个错再来一轮
        app.companion.bubble_until = 0.0
        app.snap.detail_line = ""
        self.assertEqual(self._bubble_text(app), "",
                         "同一个坑重复提醒了 —— 会变成复读机")

    def test_it_works_while_dsh_is_busy(self):
        """**这条是防止重演久坐提醒事故的。**

        报错发生在 DSH **正在跑工具**的时候。如果它挂在
        `CompanionSource.poll()` 上（那里只在 DSH 空闲时跑），
        这条会挂 —— 而且只有这条会挂。
        """
        app = self._app()
        app.watcher = None
        app.in_companion = False                     # 明确：不是陪伴模式
        self._preload(app, 'Error: cannot edit "G:\\x\\a.vue": file changed since it was read', 4)
        self._stub(app, state="tools",
                   error='cannot edit "G:\\y\\b.vue": file changed since it was read')
        text = self._bubble_text(app)
        self.assertIn(I.t("又踩到这个坑："), text,
                      "DSH 忙的时候不提醒 —— 和久坐提醒同一个 bug")
        self.assertIn("cannot edit", text)


class TestPrivacy(LintE2EBase):
    def test_bubble_has_no_path(self):
        """**气泡里不许出现路径/文件名。**"""
        app = self._app()
        app.watcher = None
        self._preload(app, 'Error: cannot edit "G:\\secret\\client.vue": file changed', 6)
        self._stub(app, state="tools",
                   error='cannot edit "G:\\other\\zzz.vue": file changed')
        text = self._bubble_text(app)
        self.assertTrue(text)
        self.assertNotIn("secret", text)
        self.assertNotIn("client", text)
        self.assertNotIn("zzz", text)
        self.assertNotIn("G:\\", text)

    def test_state_file_has_no_path(self):
        app = self._app()
        for _ in range(3):
            app.lint.note('Error: cannot edit "G:\\secret\\client.vue": file changed')
        app.lint.save()
        raw = open(app.lint.path, encoding="utf-8").read()
        self.assertNotIn("secret", raw)
        self.assertNotIn("client.vue", raw)


class TestReportWindow(LintE2EBase):
    def test_opens_and_lists_pitfalls(self):
        app = self._app()
        for _ in range(12):
            app.lint.note("Error: old_string and new_string must differ")
        app._open_lint_report()
        app.root.update()
        wins = [w for w in app.root.winfo_children()
                if w.winfo_class() == "Toplevel"]
        self.assertTrue(wins, "报告窗口没建起来")
        import tkinter as tk
        texts = []

        def walk(w):
            for c in w.winfo_children():
                if c.winfo_class() == "Text":
                    texts.append(c.get("1.0", "end"))
                walk(c)

        walk(wins[-1])
        self.assertTrue(texts, "窗口里没有文字区")
        self.assertIn("12 次", texts[0])
        self.assertIn("old_string and new_string must differ", texts[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
