"""开机自启的测试。

## 抓到的真 bug

`4d3f645 refactor: 把 3300 行的单文件拆成 6 个模块` 把开机自启这段搬到了
`pet_system.py`，而里面写的是：

    return f'"{exe}" "{os.path.abspath(__file__)}"'

`__file__` 于是从 `dsh_pet.py` 变成了 `pet_system.py` —— 而 `pet_system.py`
只有常量和 winreg 封装，**没有 `main()`**。

用户在设置里勾着「开机自启」，重启之后桌宠不出现，**而且没有任何报错**：
开机跑一个瞬间退出、什么都不发生的进程。

## 所以这里最重要的一条

`test_the_target_is_actually_an_entry_point` —— 它去读那个文件，确认里面有
`if __name__ == "__main__"`。**这条测试如果早存在，那次重构当场就会红。**

写完这条之后我问自己：还有别的地方会踩同样的坑吗？
`启动桌宠.vbs` 也指着一个脚本 —— 一起测了。
"""

import io
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import pet_base as B  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="dsh-pet-autostart-test-")
B.data_dir = lambda: _TMP

import pet_system as S  # noqa: E402


def _script_of(command: str) -> str:
    """从 `"pythonw.exe" "xxx.py"` 里把 xxx.py 抠出来。"""
    parts = [p.strip().strip('"') for p in command.split('" "')]
    parts[0] = parts[0].strip('"')
    for p in parts[1:]:
        if p.lower().endswith(".py"):
            return p
    return parts[-1]


def _is_entry_point(path: str) -> bool:
    """这个文件能不能当入口跑 —— 有没有 `if __name__` 那一段。"""
    try:
        src = io.open(path, encoding="utf-8").read()
    except OSError:
        return False
    return '__name__ == "__main__"' in src or "__name__ == '__main__'" in src


class TestCommand(unittest.TestCase):
    def test_points_at_the_entry_script(self):
        """**不能是 pet_system.py 自己。** 那是常量模块，没有 main()。"""
        cmd = S.autostart_command()
        self.assertNotIn("pet_system.py", cmd,
                         "自启指到了常量模块 —— 开机跑完就退出，什么都不发生")
        self.assertIn("dsh_pet.py", cmd)

    def test_the_target_is_actually_an_entry_point(self):
        """**这条是这次的回归测试。**

        它不检查"命令字符串长什么样"，而是**去读那个文件**，确认里面有
        `if __name__ == "__main__"`。

        为什么这么测：字符串怎么写都可以过，但"那个文件能不能跑起来"
        是这个功能唯一要紧的事。`4d3f645` 那次重构就是字符串看着没问题、
        指向的文件却不是入口。
        """
        target = _script_of(S.autostart_command())
        self.assertTrue(os.path.isfile(target), f"自启指向的文件不存在：{target}")
        self.assertTrue(_is_entry_point(target),
                        f"自启指向的不是入口（没有 __main__）：{target}")

    def test_uses_pythonw_not_python(self):
        """源码跑要用 pythonw —— 用 python 开机会弹一个黑框。"""
        cmd = S.autostart_command()
        exe = cmd.split('"')[1] if cmd.startswith('"') else cmd.split()[0]
        self.assertTrue(exe.lower().endswith("pythonw.exe"),
                        f"用了 {os.path.basename(exe)}，开机可能弹黑框")

    def test_frozen_points_at_the_exe(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", r"C:\apps\DSH-Pet.exe"):
            self.assertEqual(S.autostart_command(), r'"C:\apps\DSH-Pet.exe"')

    def test_entry_script_exists(self):
        self.assertTrue(os.path.isfile(S.entry_script()), S.entry_script())


class TestOtherLaunchers(unittest.TestCase):
    """别的启动入口也指着一个脚本 —— 一起验，别只顾着注册表。"""

    def test_vbs_launcher_points_at_the_entry(self):
        vbs = os.path.join(ROOT, "启动桌宠.vbs")
        if not os.path.isfile(vbs):
            self.skipTest("没有 vbs 启动器")
        src = io.open(vbs, encoding="utf-8", errors="replace").read()
        self.assertIn("dsh_pet.py", src, f"vbs 指错了：{src[:120]}")
        self.assertNotIn("pet_system.py", src)


class TestSetAndRepair(unittest.TestCase):
    """注册表用一个假的 —— 不能真去改测试机上的自启。"""

    def setUp(self):
        self.store = {}
        self.key = mock.MagicMock()
        self.key.__enter__ = lambda s: self.store
        self.key.__exit__ = lambda *a: False

        def _create(*_a, **_k):
            return self.key

        self.winreg = mock.MagicMock()
        self.winreg.HKEY_CURRENT_USER = "HKCU"
        self.winreg.REG_SZ = 1
        self.winreg.CreateKey = _create
        self.winreg.OpenKey = _create

        def _set(_k, name, _r, _t, value):
            self.store[name] = value

        def _query(_k, name):
            if name not in self.store:
                raise FileNotFoundError(name)
            return self.store[name], 1

        def _delete(_k, name):
            if name not in self.store:
                raise FileNotFoundError(name)
            del self.store[name]

        self.winreg.SetValueEx = _set
        self.winreg.QueryValueEx = _query
        self.winreg.DeleteValue = _delete
        sys.modules["winreg"] = self.winreg
        self.addCleanup(sys.modules.pop, "winreg", None)

    def test_set_on_writes_the_right_command(self):
        with mock.patch.object(S.pet_platform, "IS_WINDOWS", True):
            self.assertTrue(S.set_autostart(True))
        self.assertEqual(self.store[S.AUTOSTART_NAME], S.autostart_command())

    def test_set_off_removes_it(self):
        with mock.patch.object(S.pet_platform, "IS_WINDOWS", True):
            S.set_autostart(True)
            self.assertTrue(S.set_autostart(False))
        self.assertNotIn(S.AUTOSTART_NAME, self.store)

    def test_enabled_reads_it_back(self):
        with mock.patch.object(S.pet_platform, "IS_WINDOWS", True):
            self.assertFalse(S.autostart_enabled())
            S.set_autostart(True)
            self.assertTrue(S.autostart_enabled())

    def test_repair_fixes_a_wrong_value(self):
        """**存量修复** —— 已经写进去的错值不会自己好。"""
        self.store[S.AUTOSTART_NAME] = '"pythonw.exe" "C:\\old\\pet_system.py"'
        with mock.patch.object(S.pet_platform, "IS_WINDOWS", True):
            self.assertEqual(S.repair_autostart(), "fixed")
        self.assertEqual(self.store[S.AUTOSTART_NAME], S.autostart_command())

    def test_repair_is_idempotent(self):
        with mock.patch.object(S.pet_platform, "IS_WINDOWS", True):
            S.set_autostart(True)
            self.assertEqual(S.repair_autostart(), "ok")
            self.assertEqual(S.repair_autostart(), "ok")

    def test_repair_does_nothing_when_autostart_is_off(self):
        """没开自启的人不能被我们偷偷加上。"""
        with mock.patch.object(S.pet_platform, "IS_WINDOWS", True):
            self.assertEqual(S.repair_autostart(), "ok")
        self.assertNotIn(S.AUTOSTART_NAME, self.store)

    def test_non_windows_is_a_noop(self):
        with mock.patch.object(S.pet_platform, "IS_WINDOWS", False):
            self.assertFalse(S.set_autostart(True))
            self.assertFalse(S.autostart_enabled())
            self.assertEqual(S.repair_autostart(), "off")
        self.assertEqual(self.store, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
