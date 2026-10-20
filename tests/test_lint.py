"""「反复踩的坑」的单元测试。

## 这个功能最怕的两件事

1. **编**（把不存在的规律报出来）—— 所以测：只出现 1 次的坑**不许**报
2. **泄**（把路径、文件名、你的代码存下来）—— 所以测：签名里不许有路径

以及**别烦人**：同一条在一次运行里只提一次。
"""

import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pet_base as B  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="dsh-pet-lint-test-")
B.data_dir = lambda: _TMP

import pet_lint as L  # noqa: E402


class TestSignature(unittest.TestCase):
    """归一化 —— **规则是对着 274 条真报错调出来的**。"""

    def test_strips_error_prefix(self):
        self.assertEqual(L.signature("Error: boom"), "boom")
        self.assertEqual(L.signature("错误：炸了"), "炸了")

    def test_windows_path_becomes_placeholder(self):
        sig = L.signature('cannot edit "G:\\a\\b\\c.vue": file changed')
        self.assertNotIn("G:\\", sig)
        self.assertNotIn("c.vue", sig)
        self.assertIn("<str>", sig)

    def test_unquoted_path_becomes_placeholder(self):
        sig = L.signature(r"ReplaceFileW EIO (Win32 1175): G:\a\b\c.tsx")
        self.assertNotIn("G:\\", sig)
        self.assertNotIn("c.tsx", sig)

    def test_numbers_become_placeholder(self):
        self.assertIn("<n>", L.signature("matched 2 times in foo"))

    def test_hex_becomes_placeholder(self):
        self.assertIn("<hex>", L.signature("deadbeef12345678 failed"))

    def test_drops_the_advice_tail(self):
        """破折号后面是「该怎么办」，不是形状。"""
        sig = L.signature('edit requires reading "x.js" first — read the file, then retry')
        self.assertNotIn("read the file", sig)
        self.assertEqual(sig, "edit requires reading <str> first")

    def test_same_shape_different_details_collapses(self):
        """**这是整个功能的地基**：换个文件名、换个路径，还得是同一个签名。"""
        a = L.signature('Error: cannot edit "G:\\a\\one.vue": file changed since it was read')
        b = L.signature('Error: cannot edit "C:\\b\\two.vue": file changed since it was read')
        self.assertEqual(a, b)

    def test_empty_is_empty(self):
        self.assertEqual(L.signature(""), "")
        self.assertEqual(L.signature(None), "")


class TestLinter(unittest.TestCase):
    def setUp(self):
        self.lint = L.Linter(os.path.join(tempfile.mkdtemp(), "lint.json"))

    def test_one_occurrence_is_not_recurring(self):
        """**只出现一次的不许报。** 偶然的手滑不是「反复踩的坑」。"""
        self.lint.note("Error: thing broke")
        self.assertIsNone(self.lint.recall("Error: thing broke"))
        self.assertEqual(self.lint.top(), [])

    def test_three_occurrences_is_recurring(self):
        for _ in range(L.MIN_RECUR):
            self.lint.note("Error: thing broke")
        hit = self.lint.recall("Error: thing broke")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["n"], L.MIN_RECUR)

    def test_nags_only_once_per_run(self):
        """**别烦人。** 一个循环里连报 20 次，她不能变复读机。"""
        for _ in range(5):
            self.lint.note("Error: thing broke")
        self.assertIsNotNone(self.lint.recall("Error: thing broke"))
        for _ in range(5):
            self.assertIsNone(self.lint.recall("Error: thing broke"))

    def test_matching_is_by_shape_not_exact_text(self):
        """换个文件名还得认得出来 —— 这才是它有用的原因。"""
        for p in ("a.vue", "b.vue", "c.vue"):
            self.lint.note(f'Error: cannot edit "G:\\x\\{p}": file changed since it was read')
        hit = self.lint.recall('Error: cannot edit "Z:\\other\\zzz.vue": file changed since it was read')
        self.assertIsNotNone(hit, "换个文件名就不认了，那这功能没用")
        self.assertEqual(hit["n"], 3)

    def test_top_is_ranked_and_capped(self):
        # **用词区分，不用数字** —— 归一化故意把数字换成 `<n>`，
        # 所以 "kind 1" / "kind 2" 是同一个签名（那是对的）。
        for name, n in (("alpha", 5), ("beta", 2), ("gamma", 9), ("delta", 3)):
            for _ in range(n):
                self.lint.note(f"Error: {name} broke")
        rows = self.lint.top(2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], 9)
        self.assertIn("gamma", rows[0][0])
        self.assertGreaterEqual(rows[1][1], 5)

    def test_numbers_collapse_on_purpose(self):
        """`kind 1` 和 `kind 2` **该**是同一个签名 —— 这是特性不是 bug。

        （第一版测试就是拿数字当区分，然后被自己的归一化打败了。）
        """
        self.assertEqual(L.signature("Error: kind 1"), L.signature("Error: kind 2"))

    def test_state_roundtrips(self):
        for _ in range(4):
            self.lint.note("Error: thing broke")
        self.lint.save()
        again = L.Linter(self.lint.path).load()
        self.assertEqual(again.count("Error: thing broke"), 4)

    def test_saves_no_head_text(self):
        """**隐私：状态文件里不许出现原文。**"""
        self.lint.note('Error: cannot edit "G:\\secret\\path\\client.ts": file changed')
        self.lint.save()
        raw = io.open(self.lint.path, encoding="utf-8").read()
        self.assertNotIn("secret", raw, "路径被写进状态文件了")
        self.assertNotIn("client.ts", raw, "文件名被写进状态文件了")
        self.assertIn("<str>", raw)

    def test_report_says_when_there_is_nothing(self):
        self.assertIn("还没攒够", L.format_report(self.lint))

    def test_report_lists_counts(self):
        for _ in range(7):
            self.lint.note("Error: thing broke")
        text = L.format_report(self.lint)
        self.assertIn("7 次", text)
        self.assertIn("thing broke", text)


class TestScanFiles(unittest.TestCase):
    """从**真事件日志**里抽 —— 接错文件源是这里最容易犯的错。"""

    def _write_logs(self, dirpath, events):
        os.makedirs(dirpath, exist_ok=True)
        p = os.path.join(dirpath, "session.v4.jsonl.zstd")
        body = "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"
        import zstandard
        with open(p, "wb") as fh:
            fh.write(zstandard.ZstdCompressor().compress(body.encode("utf-8")))
        return p

    @staticmethod
    def _err(head):
        return {"type": "tool/result",
                "data": {"message": {"isError": True,
                                     "content": [{"type": "text", "text": head}]}}}

    def test_extracts_only_errors(self):
        d = tempfile.mkdtemp()
        p = self._write_logs(d, [
            self._err("Error: one"),
            {"type": "tool/result", "data": {"message": {"isError": False,
                                                         "content": [{"text": "ok"}]}}},
            self._err("Error: two"),
        ])
        self.assertEqual(L.errors_in_file(p), ["Error: one", "Error: two"])

    def test_scan_counts_and_is_incremental(self):
        d = tempfile.mkdtemp()
        p = self._write_logs(d, [self._err("Error: same"), self._err("Error: same")])
        lint = L.Linter(os.path.join(d, "lint.json"))
        files = L.list_event_logs(d)
        self.assertTrue(files, "没找到刚写的日志")
        self.assertEqual(lint.scan(files), 2)
        # 同样的文件再扫一遍**不许重复计数**
        self.assertEqual(lint.scan(files), 0)
        self.assertEqual(lint.count("Error: same"), 2)

    def test_list_event_logs_finds_nested_sessions(self):
        d = tempfile.mkdtemp()
        self._write_logs(os.path.join(d, "proj", "session-abc"), [self._err("Error: x")])
        found = L.list_event_logs(d)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0][0].endswith("session.v4.jsonl.zstd"))
        self.assertEqual(len(found[0]), 3, "应该给 (path, mtime, size)")

    def test_pointed_at_the_wrong_dir_gives_nothing(self):
        """**这是第一版真的犯过的错。**

        `pet_sources.list_session_files()` 给的是 projcache 的**摘要 json**，
        里面一条 `tool/result` 都没有。接错文件源的表现是「扫了 13 个文件、
        0 条报错」—— 看着像"没坑"，其实是看错了地方。
        """
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "storages"), exist_ok=True)
        with open(os.path.join(d, "storages", "session-abc.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": 1, "record": {}}, fh)
        self.assertEqual(L.list_event_logs(d), [])


class TestShortSig(unittest.TestCase):
    """气泡里的签名 —— 不许切出半截词（AGENTS.md 规则 8）。"""

    def test_short_enough_passes_through(self):
        import dsh_pet as P
        self.assertEqual(P.PetApp._short_sig("short"), "short")

    def test_cuts_at_placeholder_not_mid_word(self):
        import dsh_pet as P
        got = P.PetApp._short_sig(
            "old_string matched <n> times in <str>; provide a more specific old_string")
        self.assertIn("…", got)
        self.assertNotIn("<st", got.replace("<str>", ""),
                         f"切出半截占位符了：{got}")

    def test_no_hard_cut_mid_word(self):
        import dsh_pet as P
        got = P.PetApp._short_sig("aaa bbb ccc ddd eee fff ggg hhh iii jjj kkk lll mmm")
        self.assertTrue(got.endswith("…"))
        self.assertNotIn("mm…", got, f"在词中间切了：{got}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
