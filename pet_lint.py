"""她会记得你反复踩的坑 —— 一个读过你全部工作记录的 linter。

## 为什么做这个

这个项目别的功能都是「一个桌宠做 X」—— 摸她、喂她、溜达、看任务状态，
全是 20 年前就有的形状。**唯一别人没有的东西**是：

    她读得到你和 AI 协作的全部历史。

这台机器上有 80 个会话文件，里面 **274 条工具报错**。先把它聚一下：

    74 次  cannot edit <str>: file changed since it was read
    48 次  edit requires reading <str> first
    36 次  cannot modify <str>: file has not been read
    17 次  old_string was not found in <str>
    12 次  ReplaceFileW EIO (Win32 <n>): <path>
     8 次  old_string and new_string must differ

**274 条聚成 45 组，23 组是反复出现的。** 这就是「你反复踩的坑」，
而且是可以数的 —— 不需要 LLM，不会编。

所以她的功能是：**你又一次踩到同一个坑的时候，她认得出来。**

    「又来了 —— `edit` 之前得先 `read`，这周第 48 次了。」

## 隐私：只存签名，不存内容

会话文件里有你的提示词、你的代码、你的路径。**这个模块一个字都不存。**

存下来的只有归一化后的**形状**：

    edit requires reading <str> first

路径变成 `<str>`、数字变成 `<n>`、长尾巴砍掉。原文谁也不给看 ——
连她自己的气泡里也不放，只放签名。

## 归一化规则是**在真数据上验过的**

`signature()` 的第一版是猜的。拿 274 条真报错跑了一遍才定下来：
去掉 `Error: ` 前缀、路径/引号串/十六进制/数字都换成占位符、
砍掉破折号后面的细节。

没验过就上，结果会是一堆「每个签名只出现 1 次」的垃圾。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

try:
    import zstandard
except ImportError:                                       # pragma: no cover
    zstandard = None

# 少于这个次数不算「反复踩的坑」—— 一次两次是偶然，报出来只会让人烦
MIN_RECUR = 3

# 一次最多看多久。首次扫全部历史要读几十 MB，**不能卡住界面线程**
SCAN_BUDGET_S = 6.0

STATE_NAME = "pet_lint.json"


# ── 归一化 ──────────────────────────────────────────────────────────────
#
# 目标：把「每次都不一样的部分」换成占位符，留下**形状**。
# 这些规则是对着 274 条真报错调出来的，不是拍脑袋。

_PATH_WIN = re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+")
_PATH_UNIX = re.compile(r"(?<![\w.])/(?:[\w.\-]+/){1,}[\w.\-]+")
_QUOTED = re.compile(r"\"[^\"]{0,300}\"|'[^']{0,300}'")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_WS = re.compile(r"\s+")
_PREFIX = re.compile(r"^(?:Error|错误)[:：]\s*")


def signature(head: str, maxlen: int = 90) -> str:
    """把一条报错的第一行压成**形状**。纯函数，好测。

    >>> signature('Error: edit requires reading "G:\\\\a\\\\b.js" first — read it')
    'edit requires reading <str> first'
    """
    if not head:
        return ""
    s = _PREFIX.sub("", str(head).strip())
    s = _PATH_WIN.sub("<path>", s)
    s = _PATH_UNIX.sub("<path>", s)
    s = _QUOTED.sub("<str>", s)
    s = _HEX.sub("<hex>", s)
    s = _NUM.sub("<n>", s)
    s = _WS.sub(" ", s).strip()
    # 破折号后面通常是「该怎么办」的详细说明，不是形状的一部分
    for sep in (" — ", " – ", " -- "):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    if len(s) > maxlen:
        s = s[:maxlen].rsplit(" ", 1)[0]
    return s.strip(" :：,，")


# ── 从会话事件日志里抽报错 ──────────────────────────────────────────────
#
# **路径要自己找，不能用 `pet_sources.list_session_files()`。**
# 那个返回的是 `<DSH_HOME>/storages/session_projcache/sessions/session-<id>.json`
# —— projcache 的**摘要**，里面只有 `{version, record}`，**一条 tool/result
# 都没有**。第一版接上去扫了 13 个文件、0 条报错，看着"没坑"，其实是看错了地方。
#
# 真正的事件日志在 `<DSH_HOME>/sessions/<项目>/<会话>/session.v*.jsonl.zstd`。


def list_event_logs(sessions_dir: str = "") -> list:
    """列出会话事件日志，新的在前。返回 `(path, mtime, size)`。"""
    if not sessions_dir:
        try:
            from pet_base import SESS_DIR
            sessions_dir = SESS_DIR
        except Exception:                                # noqa: BLE001
            sessions_dir = os.path.join(
                os.path.expanduser("~"), ".dsh", "sessions")
    out = []
    if not os.path.isdir(sessions_dir):
        return out
    for root, _dirs, names in os.walk(sessions_dir):
        for n in names:
            if not (n.startswith("session") and n.endswith(".jsonl.zstd")):
                continue
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append((p, st.st_mtime, st.st_size))
    out.sort(key=lambda x: -x[1])
    return out


def _iter_events(path: str):
    """按行产出会话文件里的事件。zstd 或明文都行。"""
    try:
        if path.endswith(".zstd"):
            if zstandard is None:
                return
            with open(path, "rb") as fh:
                data = zstandard.ZstdDecompressor().stream_reader(fh).read()
            text = data.decode("utf-8", "replace")
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
    except (OSError, Exception):                          # noqa: BLE001
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def errors_in_file(path: str) -> list:
    """从一个会话文件里抽 (head, tool) 列表。

    **只取第一行** —— 后面的堆栈又长又没用，而且可能带路径。
    """
    out = []
    for ev in _iter_events(path):
        if not isinstance(ev, dict) or ev.get("type") != "tool/result":
            continue
        data = ev.get("data") or {}
        msg = data.get("message")
        if not isinstance(msg, dict) or not msg.get("isError"):
            continue
        text = ""
        content = msg.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("text"):
                    text = str(c["text"])
                    break
        if not text:
            err = data.get("error")
            text = str(err) if err else ""
        head = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if head:
            out.append(head)
    return out


# ── linter ──────────────────────────────────────────────────────────────


def state_path() -> str:
    """**函数，不是常量** —— `from pet_base import *` 会把常量拷贝一份，
    那之后就蹭不到测试里打的补丁了（踩过好几次）。
    """
    try:
        from pet_base import data_dir
        return os.path.join(data_dir(), STATE_NAME)
    except Exception:                                    # noqa: BLE001
        return os.path.join(os.getcwd(), STATE_NAME)


class Linter:
    """统计「反复踩的坑」。

    状态很小：每个签名一条 `{n, tool, first, last}`。
    **不存原文** —— 会话文件里有你的提示词和代码，这里一个字都不留。
    """

    def __init__(self, path: str = ""):
        self.path = path or ""
        self.sigs: dict = {}          # signature -> {n, tool, first, last}
        self.scanned: dict = {}       # 文件路径 -> [mtime, size]
        self.total_seen = 0
        self._dirty = False
        self._said: set = set()       # 这一次运行里已经提醒过的签名
        self.last_scan_s = 0.0

    # -- 持久化 ---------------------------------------------------------
    def load(self) -> "Linter":
        p = self.path or state_path()
        self.path = p
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return self
        if not isinstance(data, dict) or data.get("version") != 1:
            return self
        self.sigs = data.get("sigs") or {}
        self.scanned = data.get("scanned") or {}
        self.total_seen = int(data.get("total_seen") or 0)
        return self

    def save(self) -> None:
        if not self._dirty or not self.path:
            return
        try:
            tmp = self.path + ".part"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "sigs": self.sigs,
                           "scanned": self.scanned,
                           "total_seen": self.total_seen},
                          fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            pass

    # -- 记一条 ---------------------------------------------------------
    def note(self, head: str, tool: str = "") -> int:
        """记一条报错，返回这个签名累计出现几次。"""
        sig = signature(head)
        if not sig:
            return 0
        now = time.time()
        e = self.sigs.get(sig)
        if e is None:
            self.sigs[sig] = {"n": 1, "tool": tool or "", "first": now, "last": now}
            self.total_seen += 1
            self._dirty = True
            return 1
        e["n"] = int(e.get("n") or 0) + 1
        e["last"] = now
        if tool and not e.get("tool"):
            e["tool"] = tool
        self.total_seen += 1
        self._dirty = True
        return e["n"]

    # -- 扫描历史 -------------------------------------------------------
    def scan(self, files, budget_s: float = SCAN_BUDGET_S) -> int:
        """增量扫会话事件日志。返回这次新记的条数。

        `files` 收 `(path, mtime, size)`（`list_event_logs()` 给的就是这个）。
        `(path, sid, mtime, size)` 四元组也认 —— 万一以后换个来源。

        **按 (mtime, size) 判要不要重扫** —— 几十个文件、解压后几十 MB，
        每次都全扫是不行的。

        `budget_s` 是时间上限：首次扫全部历史会很慢，**不能卡住调用方**。
        超时了下次接着扫（扫过的有记录）。
        """
        t0 = time.time()
        added = 0
        for item in files:
            if time.time() - t0 > budget_s:
                break
            try:
                # 尾部两个永远是 (mtime, size)
                path, mtime, size = item[0], item[-2], item[-1]
            except (IndexError, TypeError):
                continue
            rec = self.scanned.get(path)
            if rec and rec[0] == mtime and rec[1] == size:
                continue
            try:
                heads = errors_in_file(path)
            except Exception:                            # noqa: BLE001
                continue
            for h in heads:
                self.note(h)
                added += 1
            self.scanned[path] = [mtime, size]
            self._dirty = True
        self.last_scan_s = time.time() - t0
        return added

    # -- 查询 -----------------------------------------------------------
    def count(self, head: str) -> int:
        e = self.sigs.get(signature(head))
        return int(e.get("n") or 0) if e else 0

    def recall(self, head: str) -> Optional[dict]:
        """**这是整个功能的入口。**

        你刚踩到一个错 → 这个签名以前出现过 ≥ MIN_RECUR 次 →
        返回那条记录（带次数）。否则 None。

        同一次运行里同一个签名只提醒一次 —— 不然一个循环里连报 20 次
        能把她变成复读机。
        """
        sig = signature(head)
        if not sig:
            return None
        e = self.sigs.get(sig)
        if not e:
            return None
        n = int(e.get("n") or 0)
        if n < MIN_RECUR:
            return None
        if sig in self._said:
            return None
        self._said.add(sig)
        return {"sig": sig, "n": n, "tool": e.get("tool") or "",
                "first": e.get("first"), "last": e.get("last")}

    def top(self, n: int = 8) -> list:
        """反复出现最多的几条。只有 ≥ MIN_RECUR 的才算。"""
        rows = [(sig, int(e.get("n") or 0), e.get("tool") or "")
                for sig, e in self.sigs.items()
                if int(e.get("n") or 0) >= MIN_RECUR]
        rows.sort(key=lambda r: -r[1])
        return rows[:n]

    def recurring_count(self) -> int:
        return sum(1 for e in self.sigs.values()
                   if int(e.get("n") or 0) >= MIN_RECUR)

    def report_line(self) -> str:
        """一行摘要，给气泡用。"""
        rows = self.top(1)
        if not rows:
            return ""
        sig, n, _tool = rows[0]
        return f"{sig}（{n} 次）"


def format_report(linter: Linter, limit: int = 8) -> str:
    """给人看的报告。**只有签名，没有原文。**"""
    rows = linter.top(limit)
    if not rows:
        return "还没攒够数据（同一个坑要出现 3 次以上才算）。"
    lines = [f"一共 {linter.total_seen} 条报错，"
             f"聚成 {len(linter.sigs)} 种，其中 {linter.recurring_count()} 种反复出现。", ""]
    for sig, n, tool in rows:
        tag = f"[{tool}] " if tool else ""
        lines.append(f"{n:>4} 次  {tag}{sig}")
    return "\n".join(lines)
