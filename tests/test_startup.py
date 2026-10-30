"""启动路径的测试 —— 开机时最容易出问题的那几段。

## 这个文件拦的三个真 bug

**1. `P.TaskInfo` —— 单文件重构的残留。**
「右键 → 会话」在没有活跃任务时抛 `NameError`，被 Tk 回调的异常处理器
接住写日志 —— 窗口不弹，用户觉得"点了没反应"。

**2. 单实例锁失败 = 静默死亡。**
`bind` 失败就 `return 2`，只 print 到 stderr。**pythonw.exe 下那是黑洞**：
开机自启撞上「上一只还没退干净」时，用户看到「桌宠没起来」，
日志里一个字都没有。

**3. `repair_autostart` 每天误报一次。**
Windows 路径不区分大小写，字符串 `==` 区分 —— `DeepSeekWork` 和
`DeepSeekwork` 被当成"指错了"，每次启动都重写一遍还记一条日志。
"""

import io
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import pet_base as B  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="dsh-pet-startup-test-")
B.data_dir = lambda: _TMP

import pet_system as S  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestSingleInstance(unittest.TestCase):
    def setUp(self):
        import dsh_pet as P
        self.P = P
        self.port = _free_port()

    def test_acquires_when_free(self):
        lock, err = self.P.acquire_single_instance(self.port, timeout_s=0.5)
        self.assertIsNotNone(lock, f"空端口也拿不到：{err}")
        self.assertIsNone(err)
        self.addCleanup(lock.close)

    def test_fails_when_held(self):
        """端口被占时**报错**，不能静默 return 0 ——
        调用方（DSH 插件）要能分清「已经在跑」和「起来又死了」。"""
        holder = socket.socket()
        holder.bind(("127.0.0.1", self.port))
        holder.listen(1)
        self.addCleanup(holder.close)

        t0 = time.time()
        lock, err = self.P.acquire_single_instance(self.port, timeout_s=0.6)
        spent = time.time() - t0
        self.assertIsNone(lock, "端口被占居然拿到了")
        self.assertIsInstance(err, OSError)
        self.assertGreaterEqual(spent, 0.5, "没等够就放弃了 —— 开机抢跑会失败")

    def test_waits_for_a_slow_release(self):
        """**这是重试存在的理由。**

        开机自启时上一只还要约 2 秒才真正释放端口。第一版 bind 一次就
        `return 2` —— 于是新实例死了，用户看到「桌宠没起来」。
        """
        holder = socket.socket()
        holder.bind(("127.0.0.1", self.port))
        holder.listen(1)

        def release_soon():
            time.sleep(0.4)
            holder.close()

        import threading
        t = threading.Thread(target=release_soon, daemon=True)
        t.start()

        lock, err = self.P.acquire_single_instance(self.port, timeout_s=3.0)
        self.assertIsNotNone(lock, f"等不到释放：{err}")
        self.addCleanup(lock.close)


class TestSessionWindowWithoutTasks(unittest.TestCase):
    """`P.TaskInfo` 那条 —— 走真实路径：`_open_session_actions()`。"""

    def setUp(self):
        sys.modules.pop("pystray", None)
        sys.modules["pystray"] = None
        self.tmp = tempfile.mkdtemp(prefix="sess-")
        self._apps = []

    def tearDown(self):
        for a in self._apps:
            try:
                a._quit()
            except Exception:                            # noqa: BLE001
                pass

    def _app(self):
        import argparse
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
        app.lint = None
        app.git = None
        return app

    def _open(self, app):
        app._open_session_actions()
        app.root.update()
        wins = [w for w in app.root.winfo_children()
                if w.winfo_class() == "Toplevel"]
        for w in wins:
            w.destroy()
        return wins

    def test_opens_without_any_task(self):
        """**没有活跃任务**时最容易炸 —— 原来这里抛 NameError。"""
        import dsh_pet as P
        app = self._app()
        app.snap = P.Snapshot(state="idle", session_id="", title="", tasks=[])
        self.assertTrue(self._open(app), "没有任务时窗口没弹")

    def test_opens_with_a_task(self):
        import dsh_pet as P
        app = self._app()
        app.snap = P.Snapshot(
            state="tools", session_id="abc", title="修 bug",
            tasks=[P.TaskInfo(session_id="abc", state="tools", title="修 bug")])
        self.assertTrue(self._open(app), "有任务时窗口没弹")

    def test_no_bare_P_references_in_source(self):
        """扫一遍源码，别再留下 `P.XXX` 这种单文件时代的引用。

        `P` 是拆模块之前的模块自身别名。拆完之后它不存在了，任何
        `P.Something` 都会在**运行时**才炸（而且被 Tk 的异常处理器吞掉）。
        """
        import re
        src = io.open(os.path.join(ROOT, "dsh_pet.py"), encoding="utf-8").read()
        # 只看大写开头的属性：`p.get(...)` 是小写局部变量，合法
        bad = re.findall(r"(?<![\w.])P\.[A-Z]\w*", src)
        self.assertEqual(bad, [], f"还有单文件时代的 P. 引用：{sorted(set(bad))}")


class TestAutostartRepairCase(unittest.TestCase):
    """`repair_autostart` 不能因为大小写就每天重写一遍。"""

    def setUp(self):
        self.store = {}
        key = mock.MagicMock()
        key.__enter__ = lambda s: self.store
        key.__exit__ = lambda *a: False
        wr = mock.MagicMock()
        wr.HKEY_CURRENT_USER = "HKCU"
        wr.REG_SZ = 1
        wr.CreateKey = lambda *a, **k: key
        wr.OpenKey = lambda *a, **k: key
        wr.SetValueEx = lambda _k, n, _r, _t, v: self.store.__setitem__(n, v)

        def _q(_k, n):
            if n not in self.store:
                raise FileNotFoundError(n)
            return self.store[n], 1

        wr.QueryValueEx = _q
        sys.modules["winreg"] = wr
        self.addCleanup(sys.modules.pop, "winreg", None)

    def test_case_only_difference_is_not_a_repair(self):
        """**这条是回归测试。** 实测日志里每天一条
        「自启命令指错了，已改回：A -> A」，两个 A 看着一模一样。"""
        want = S.autostart_command()
        variant = want.replace("DeepSeekWork", "DeepSeekwork")
        if variant == want:                              # 本机路径没这个大小写
            variant = want.swapcase()
        self.assertNotEqual(variant, want, "造不出大小写变体，这条测不到东西")
        self.store[S.AUTOSTART_NAME] = variant
        with mock.patch.object(S.pet_platform, "IS_WINDOWS", True):
            self.assertEqual(S.repair_autostart(), "ok",
                             "只差大小写却当成指错了 —— 会每天重写一遍")

    def test_real_difference_is_still_repaired(self):
        """大小写宽容了，但真指错了还是得修。"""
        self.store[S.AUTOSTART_NAME] = '"pythonw.exe" "C:\\old\\pet_system.py"'
        with mock.patch.object(S.pet_platform, "IS_WINDOWS", True):
            self.assertEqual(S.repair_autostart(), "fixed")
        self.assertEqual(self.store[S.AUTOSTART_NAME], S.autostart_command())


class TestDependencyCheck(unittest.TestCase):
    """缺依赖会让功能**静默消失** —— 所以启动时查一遍并写日志。

    踩过：DSH 重装运行时之后这台机器丢了 zstandard 和 pywin32。表现是
    「读不了会话日志 → 任务状态和踩过的坑全没了」，而日志里一个字都没有。
    用户只说得出「桌宠又不能用了」。
    """

    def test_reports_missing_and_logs_how_to_fix(self):
        import dsh_pet as P
        d = tempfile.mkdtemp()

        def _dd():
            return d
        orig = B.data_dir
        B.data_dir = _dd
        self.addCleanup(lambda: setattr(B, "data_dir", orig))
        orig_req = P._REQUIRED
        P._REQUIRED = (("PIL", "Pillow", "画图"),
                       ("surely_not_installed_xyz", "no-such-pkg", "测试"))
        self.addCleanup(lambda: setattr(P, "_REQUIRED", orig_req))

        missing = P.check_dependencies()
        self.assertEqual([m[0] for m in missing], ["no-such-pkg"])
        log = os.path.join(d, "pet_error.log")
        self.assertTrue(os.path.exists(log), "缺依赖居然没写日志")
        text = io.open(log, encoding="utf-8").read()
        self.assertIn("no-such-pkg", text)
        self.assertIn("pip install", text, "没告诉用户怎么修")

    def test_only_missing_ones_are_reported(self):
        """**不要断言"列表为空"** —— 那是拿机器状态当断言（规则 6）。

        第一版写的就是 `assertEqual(check_dependencies(), [])`，
        在本地全绿、CI 全挂：CI 只装 `pillow zstandard`，没装 `pystray`。

        正确的契约是：**缺的报，有的不报。**
        """
        import dsh_pet as P
        orig = P._REQUIRED
        P._REQUIRED = (("PIL", "Pillow", "画图"),
                       ("surely_not_installed_xyz", "no-such-pkg", "测试"))
        self.addCleanup(lambda: setattr(P, "_REQUIRED", orig))
        missing = [m[0] for m in P.check_dependencies()]
        self.assertEqual(missing, ["no-such-pkg"], "把装好的也报成缺了")

    def test_pillow_is_required(self):
        """Pillow 是真必需 —— 没有它连一帧都画不出来。"""
        import dsh_pet as P
        mods = [m for m, _p, _w in P._REQUIRED]
        self.assertIn("PIL", mods)


if __name__ == "__main__":
    unittest.main(verbosity=2)
