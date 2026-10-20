"""git 感知的测试 —— **而且这次要测菜单那条路。**

## 为什么单独强调「菜单那条路」

上一版 `_open_lint_report()` 的测试直接调方法，全绿；但**菜单里根本
没有那一项** —— 加菜单的脚本用「文件里有没有这个字符串」判重，而那个
字符串已经在 docstring 里，于是被判成"已有"跳过。

功能做完了，用户永远打不开。

AGENTS.md 规则 1：「对内部函数跑通的测试，证明的是那个函数，不是那个功能。」

所以这里有 `TestMenuReachesTheReport`：**从菜单标签找，模拟点它。**
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
    _TMP = tempfile.mkdtemp(prefix="dsh-pet-git-test-")
    B.data_dir = lambda: _TMP
    import pet_i18n as I
    I.set_language("zh")


import pet_git as G  # noqa: E402
import pet_i18n as I  # noqa: E402


class TestInsights(unittest.TestCase):
    """把形状变成人话。**每条都必须是"你自己看不出来的那种"。**"""

    def test_hot_file_becomes_a_line(self):
        st = {"name": "proj", "commits": 40,
              "hot": [("src/a.py", 31)], "same_day": [], "words": {}, "night": 0}
        lines = G.insights(st)
        self.assertTrue(any("src/a.py" in ln and "31" in ln for ln in lines), lines)

    def test_same_day_repeat_mentions_the_date(self):
        st = {"name": "proj", "commits": 40, "hot": [],
              "same_day": [{"date": "2026-10-19", "file": "t.py", "n": 12}],
              "words": {}, "night": 0}
        lines = G.insights(st)
        self.assertTrue(any("2026-10-19" in ln and "12" in ln for ln in lines), lines)

    def test_fix_ratio_only_when_enough_commits(self):
        """次数太少不算比例 —— 3 次提交里 1 次 fix 说明不了什么。"""
        few = {"name": "p", "commits": 3, "hot": [], "same_day": [],
               "words": {"fix": 1}, "night": 0}
        self.assertFalse(any("fix" in ln for ln in G.insights(few)))
        many = {"name": "p", "commits": 62, "hot": [], "same_day": [],
                "words": {"fix": 19}, "night": 0}
        self.assertTrue(any("fix" in ln and "31%" in ln for ln in G.insights(many)))

    def test_night_commits_only_when_worth_mentioning(self):
        st = {"name": "p", "commits": 30, "hot": [], "same_day": [],
              "words": {}, "night": 1}
        self.assertFalse(any("凌晨" in ln for ln in G.insights(st)))
        st["night"] = 5
        self.assertTrue(any("凌晨" in ln for ln in G.insights(st)))

    def test_empty_stats_give_nothing(self):
        self.assertEqual(G.insights({"name": "p", "commits": 0, "hot": [],
                                     "same_day": [], "words": {}, "night": 0}), [])


class TestReport(unittest.TestCase):
    def test_no_git_says_so(self):
        gw = G.GitWatcher(os.path.join(_TMP, "x.json"))
        # 把有 git 的判断换掉，测文案
        orig = G.git_available
        G.git_available = lambda: False
        try:
            self.assertIn("没有 git", G.format_report(gw))
        finally:
            G.git_available = orig

    def test_no_repos_still_explains(self):
        gw = G.GitWatcher(os.path.join(_TMP, "y.json"))
        orig = G.git_available
        G.git_available = lambda: True
        try:
            self.assertIn("还没找到", G.format_report(gw))
        finally:
            G.git_available = orig


class TestFindRepos(unittest.TestCase):
    def test_finds_a_nested_repo(self):
        d = tempfile.mkdtemp()
        repo = os.path.join(d, "a", "b", "myproj")
        os.makedirs(os.path.join(repo, ".git"))
        found = G.find_repos([d], max_depth=4)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith("myproj"))

    def test_skips_node_modules_and_hidden(self):
        """**这些目录进去又慢又没用** —— 第一次跑就栽在这上面：
        找到的是 `~/.codex` 那种垃圾，真代码一个没找到。"""
        d = tempfile.mkdtemp()
        for junk in ("node_modules/x", ".cache/y", "AppData/z"):
            os.makedirs(os.path.join(d, junk, ".git"), exist_ok=True)
        self.assertEqual(G.find_repos([d], max_depth=5), [])

    def test_respects_max_repos(self):
        d = tempfile.mkdtemp()
        for i in range(6):
            os.makedirs(os.path.join(d, f"r{i}", ".git"))
        self.assertEqual(len(G.find_repos([d], max_repos=3, max_depth=2)), 3)

    def test_missing_root_is_fine(self):
        self.assertEqual(G.find_repos([os.path.join(_TMP, "nope")]), [])


class TestGitWatcher(unittest.TestCase):
    def test_empty_repo_gives_no_stats(self):
        """空仓库（0 次提交）算出来全是零，报告里一条洞察都没有。"""
        import subprocess
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", d], capture_output=True)
        self.assertIsNone(G.repo_stats(d))

    def test_scan_skips_unchanged_repo(self):
        """按 HEAD 判要不要重算 —— 不能每轮都跑一遍 git log。"""
        gw = G.GitWatcher(os.path.join(_TMP, "w.json"))
        real = G.repo_stats
        calls = []

        def fake_stats(repo):
            calls.append(repo)
            return {"name": "x", "commits": 5, "hot": [], "same_day": [],
                    "words": {}, "night": 0}

        G.repo_stats = fake_stats
        G._git = lambda *a, **k: "deadbeef"        # 假的 HEAD
        try:
            gw._candidates = ["/fake/repo"]
            self.assertTrue(gw.refresh(budget_s=5))
            self.assertEqual(len(calls), 1)
            gw._candidates = ["/fake/repo"]
            self.assertFalse(gw.refresh(budget_s=5), "同一个 HEAD 又算了一遍")
            self.assertEqual(len(calls), 1)
        finally:
            G.repo_stats = real

    def test_state_roundtrips(self):
        gw = G.GitWatcher(os.path.join(_TMP, "z.json"))
        gw.repos = {"/r": {"name": "r", "commits": 7, "hot": [["a.py", 3]],
                           "same_day": [], "words": {}, "night": 0}}
        gw._dirty = True
        gw.save()
        again = G.GitWatcher(gw.path).load()
        self.assertEqual(again.repos["/r"]["commits"], 7)


# ── 端到端 ──────────────────────────────────────────────────────────────


class GitE2EBase(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("pystray", None)
        sys.modules["pystray"] = None
        self.tmp = tempfile.mkdtemp(prefix="pet-git-e2e-")
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
        # 别真扫盘：慢，而且依赖机器上有多少仓库（规则 6）
        app._git_no_scan = True
        app._lint_no_scan = True
        app.lint = pet_lint_stub(self.tmp)
        return app


def pet_lint_stub(tmp):
    import pet_lint as L
    return L.Linter(os.path.join(tmp, "lint.json"))


class TestMenuReachesTheReport(GitE2EBase):
    """**这一条是这次新加的，加它的原因就是上一版漏了菜单。**"""

    def test_menu_has_the_entry(self):
        app = self._app()
        labels = app._all_menu_labels()
        self.assertTrue(any(I.t("她都看到了什么…") in x for x in labels),
                        f"菜单里没有报告入口：{labels}")

    def test_clicking_it_opens_the_window(self):
        """模拟**从菜单点** —— 不是直接调方法。"""
        app = self._app()
        target = None
        for m, i, kind in app._menu_entries(app.menu):
            if kind != "command":
                continue
            if I.t("她都看到了什么…") in (m.entrycget(i, "label") or ""):
                target = (m, i)
                break
        self.assertIsNotNone(target, "菜单里找不到那一项")
        m, i = target
        before = len([w for w in app.root.winfo_children()
                      if w.winfo_class() == "Toplevel"])
        m.invoke(i)
        app.root.update()
        after = len([w for w in app.root.winfo_children()
                     if w.winfo_class() == "Toplevel"])
        self.assertEqual(after, before + 1, "点了菜单没开窗口")


class TestReportWindow(GitE2EBase):
    def test_two_tabs(self):
        app = self._app()
        app._open_lint_report()
        app.root.update()
        wins = [w for w in app.root.winfo_children()
                if w.winfo_class() == "Toplevel"]
        self.assertTrue(wins)

        def walk(w, cls, out=None):
            out = [] if out is None else out
            for c in w.winfo_children():
                if c.winfo_class() == cls:
                    out.append(c)
                walk(c, cls, out)
            return out

        nb = walk(wins[-1], "TNotebook")
        self.assertTrue(nb)
        names = [nb[0].tab(i, "text").strip() for i in range(nb[0].index("end"))]
        self.assertEqual(names, [I.t("踩过的坑"), I.t("工作形状")], names)

    def test_git_section_shows_insights(self):
        """**不装 DSH 的人也看得到东西** —— 这是这个功能的全部意义。"""
        app = self._app()
        app.git.repos = {"/r": {"name": "myproj", "commits": 62,
                                "hot": [["dsh_pet.py", 31]], "same_day": [],
                                "words": {"fix": 19}, "night": 0}}
        app._open_lint_report()
        app.root.update()
        wins = [w for w in app.root.winfo_children()
                if w.winfo_class() == "Toplevel"]
        import tkinter as tk
        texts = []

        def walk(w):
            for c in w.winfo_children():
                if c.winfo_class() == "Text":
                    texts.append(c.get("1.0", "end"))
                walk(c)

        walk(wins[-1])
        joined = "\n".join(texts)
        self.assertIn("dsh_pet.py", joined, "工作形状那页是空的")
        self.assertIn("19", joined)


class TestWorksWithoutDSH(GitE2EBase):
    def test_git_tick_runs_without_dsh(self):
        """没有 DSH 时 `_poll` 照样要跑 git 那一半。"""
        app = self._app()
        app._git_no_scan = False
        app.git._candidates = []
        real_find = G.find_repos
        G.find_repos = lambda *a, **k: []
        try:
            for _ in range(3):
                app._poll()
        finally:
            G.find_repos = real_find
        self.assertEqual(app.snap.state is not None, True)

    def test_lint_empty_but_git_has_data(self):
        """**换句话说：没装 DSH，报告里第一页空、第二页有货。**"""
        app = self._app()
        app.git.repos = {"/r": {"name": "proj", "commits": 20,
                                "hot": [["main.py", 9]], "same_day": [],
                                "words": {}, "night": 0}}
        self.assertIn("proj", G.format_report(app.git))
        self.assertEqual(app.lint.top(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
