"""她读你的 git —— 没有 DSH 也能用的「她懂我的工作」。

## 为什么要这个

「踩过的坑」那个功能是**这个项目唯一别人没有的东西**，但它读的是 DSH
的会话日志 —— **只有装了 DSH 的人才用得到**。

别人从帖子里下载 exe，装上，看到的却是一个"普通的可爱桌宠"，
然后就卸载了。**差异化功能对 99% 的潜在用户不可见。**

## 为什么是 git

开发者机器上遍地都是 git，而且**零配置**：不用你告诉我仓库在哪，
我自己找。

而且 git log 里全是「你的工作形状」：

    62 次提交 / 6 天
    dsh_pet.py 改了 31 次
    62 条 commit 里 19 条含 "fix"
    2026-10-19 那天 tests/test_e2e.py 被改了 12 次      ← 你在跟它搏斗

**最后那条和「踩过的坑」是同一类洞察**：不是"你提交了多少"，
是"你反复回到同一个地方"。

## 隐私

- **纯本地**，不起任何网络请求（这个项目本来就没有遥测）
- 报告里只有**文件名和次数**，没有代码内容、没有 diff、没有 commit message 全文
- 状态文件里只存聚合结果

文件名要不要露出来？**要。** 那是给你自己看的屏幕 —— 说
「某个文件你改了 31 次」而不敢说是哪个，等于没说。

## 没有 git 怎么办

`find_repos` 返回空 → 整个功能安静地不存在，和音乐源在没 SMTC 时一样。
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import time
from typing import Optional

# 一个仓库最多看多少次提交。**大仓库的 --numstat 很慢**，
# 200 次足够看出形状，再多只是浪费时间。
MAX_COMMITS = 200

# 单次 git 调用的超时。超时当没有 —— **不能卡住界面**
GIT_TIMEOUT_S = 4.0

# 最多看几个仓库。机器上可能有一百个，全扫没必要
MAX_REPOS = 12

# 找仓库时最多往下走几层
MAX_DEPTH = 3

STATE_NAME = "pet_git.json"

# 常见代码目录。**用户可以在设置里加** —— 别人把代码放哪我不知道。
DEFAULT_ROOTS = ("~", "~/Desktop", "~/Documents", "~/Projects", "~/code",
                 "~/dev", "~/src", "~/workspace", "~/repos", "~/git",
                 "~/source", "~/work", "~/projects")

# 盘符根下常见的开发目录。Windows 用户经常直接放 `D:\code` 这种
DRIVE_NAMES = ("skill", "code", "dev", "src", "work", "projects", "repos",
               "git", "workspace", "wrk")

# 这些目录进去又慢又没用（`~` 下面尤其要小心）
SKIP_DIRS = frozenset((
    "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".git", "target", "vendor", "AppData", ".cache", ".npm", ".cargo",
    ".rustup", ".gradle", ".m2", "Library", ".Trash", "obj", "bin",
    ".next", ".nuxt", "coverage", ".pytest_cache", ".mypy_cache",
))


def _git(args, cwd: str, timeout: float = GIT_TIMEOUT_S) -> str:
    """跑一条 git 命令。**失败一律返回空串** —— 没有 git、不是仓库、
    权限不够、超时，都当没有。
    """
    try:
        r = subprocess.run(
            ["git"] + list(args), cwd=cwd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def git_available() -> bool:
    """机器上有没有 git。没有的话这个功能整个不存在。"""
    return bool(_git(["--version"], os.path.expanduser("~"), timeout=3.0))


def extra_roots() -> list:
    """用户在设置里加的目录。**这才是让它对别人有用的关键**。"""
    try:
        from pet_base import load_config
        raw = (load_config() or {}).get("git_roots") or ""
    except Exception:                                    # noqa: BLE001
        return []
    return [p.strip() for p in str(raw).split(os.pathsep) + str(raw).split(",")
            if p.strip()]


def _drive_roots() -> list:
    r"""盘符根下的常见开发目录（`D:\code` 这种）。"""
    out = []
    for letter in "CDEFGH":
        for name in DRIVE_NAMES:
            p = f"{letter}:\\{name}"
            if os.path.isdir(p):
                out.append(p)
    return out


def find_repos(roots=None, max_repos: int = MAX_REPOS,
               max_depth: int = MAX_DEPTH) -> list:
    """找出本地 git 仓库。

    **限深度、限数量、跳过垃圾目录** —— 不能把整块盘走一遍。

    顺序：用户自己配的目录 → 默认的常见目录 → 盘符根下的开发目录。
    """
    if roots is None:
        roots = extra_roots() + list(DEFAULT_ROOTS) + _drive_roots()
    found = []
    seen = set()
    for root in roots:
        if len(found) >= max_repos:
            return found
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, _names in os.walk(root):
            if len(found) >= max_repos:
                return found
            depth = dirpath.rstrip("\\/").count(os.sep) - base_depth
            # 隐藏目录和垃圾目录都不进
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in SKIP_DIRS]
            if os.path.isdir(os.path.join(dirpath, ".git")):
                real = os.path.realpath(dirpath)
                if real not in seen:
                    seen.add(real)
                    found.append(dirpath)
                dirnames[:] = []
                continue
            if depth >= max_depth:
                dirnames[:] = []
    return found


def _commit_hours(repo: str, n: int = MAX_COMMITS) -> list:
    """每条提交的小时（0-23）。"""
    out = _git(["log", f"-n{n}", "--format=%ad", "--date=format:%H"], repo)
    return [int(ln) for ln in out.splitlines() if ln.strip().isdigit()]


def _hot_files(repo: str, n: int = MAX_COMMITS) -> collections.Counter:
    """哪些文件被改的次数最多。"""
    out = _git(["log", f"-n{n}", "--numstat", "--format="], repo)
    c = collections.Counter()
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) == 3 and parts[2]:
            c[parts[2]] += 1
    return c


def _same_day_repeats(repo: str, n: int = MAX_COMMITS, floor: int = 5) -> list:
    """同一天里被改了很多次的文件 —— **在跟它搏斗的信号**。

    比「总共改了 31 次」更有意思：31 次分散在 6 天是正常迭代，
    12 次挤在一天里通常是在跟某个 bug 死磕。
    """
    out = _git(["log", f"-n{n}", "--format=%ad", "--date=format:%Y-%m-%d",
                "--name-only"], repo)
    day = None
    per_day = collections.defaultdict(collections.Counter)
    for ln in out.splitlines():
        if not ln.strip():
            continue
        if len(ln) == 10 and ln[4] == "-" and ln[7] == "-":
            day = ln
        elif day:
            per_day[day][ln] += 1
    hits = []
    for d, c in per_day.items():
        for f, k in c.items():
            if k >= floor:
                hits.append({"date": d, "file": f, "n": k})
    hits.sort(key=lambda h: -h["n"])
    return hits[:6]


def _message_words(repo: str, n: int = MAX_COMMITS) -> dict:
    """commit message 里各类关键词出现的次数。

    **只数词，不存整句** —— commit message 可能带客户名、issue 标题。
    """
    out = _git(["log", f"-n{n}", "--format=%s"], repo)
    subs = [ln for ln in out.splitlines() if ln.strip()]
    words = ("fix", "revert", "wip", "refactor", "test", "docs", "feat")
    c = collections.Counter()
    for s in subs:
        low = s.lower()
        for w in words:
            if w in low:
                c[w] += 1
    return {"total": len(subs), "words": dict(c)}


def _branches(repo: str) -> list:
    """本地分支，新的在前。"""
    out = _git(["for-each-ref", "--sort=-committerdate",
                "--format=%(refname:short)\t%(committerdate:relative)",
                "refs/heads"], repo)
    rows = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if parts and parts[0]:
            rows.append({"name": parts[0],
                         "when": parts[1] if len(parts) > 1 else ""})
    return rows[:8]


def repo_stats(repo: str) -> Optional[dict]:
    """一个仓库的形状。没有提交就返回 None（空仓库没意义）。"""
    if not _git(["rev-parse", "--git-dir"], repo, timeout=2.0):
        return None
    hours = _commit_hours(repo)
    if not hours:
        return None
    counts = collections.Counter(hours)
    hot = _hot_files(repo)
    words = _message_words(repo)
    return {
        "repo": repo,
        "name": os.path.basename(repo.rstrip("\\/")) or repo,
        "commits": words["total"] or len(hours),
        "hours": dict(counts),
        "hot": hot.most_common(5),
        "same_day": _same_day_repeats(repo),
        "words": words["words"],
        "branches": _branches(repo),
        "night": sum(1 for h in hours if 0 <= h < 5),
    }


def insights(st: dict) -> list:
    """从一个仓库的形状里，写出**人话**。

    每一条都必须是「你自己看不出来的那种」——
    「你提交了 62 次」没用，「同一个文件你一天改了 12 次」有用。
    """
    out = []
    name = st.get("name") or "?"

    hot = st.get("hot") or []
    if hot and hot[0][1] >= 5:
        out.append(f"{name}：`{hot[0][0]}` 前后改过 {hot[0][1]} 次")

    same = st.get("same_day") or []
    if same:
        s = same[0]
        out.append(f"{s['date']} 那天 `{s['file']}` 被改了 {s['n']} 次 —— 在跟它搏斗？")

    w = st.get("words") or {}
    total = st.get("commits") or 0
    fixes = int(w.get("fix") or 0)
    if total >= 10 and fixes >= 3:
        pct = fixes / total * 100
        out.append(f"{total} 次提交里 {fixes} 次是 fix（{pct:.0f}%）")

    night = st.get("night") or 0
    if night >= 3:
        out.append(f"凌晨 0-5 点提交了 {night} 次")

    return out


# ── 状态 ────────────────────────────────────────────────────────────────


def state_path() -> str:
    """**函数，不是常量** —— `from pet_base import *` 会复制常量，
    那之后就蹭不到测试里打的补丁了。
    """
    try:
        from pet_base import data_dir
        return os.path.join(data_dir(), STATE_NAME)
    except Exception:                                    # noqa: BLE001
        return os.path.join(os.getcwd(), STATE_NAME)


class GitWatcher:
    """扫本地仓库，攒「你的工作形状」。

    和 `pet_lint.Linter` 是同一个套路：**增量、有预算、不跳线程**。
    （lint 那版起过后台线程，把整个测试跑崩了 —— `Tcl_AsyncDelete`。）
    """

    def __init__(self, path: str = ""):
        self.path = path or ""
        self.repos: dict = {}         # path -> stats
        self.scanned: dict = {}       # path -> [head_sha, mtime]
        self.roots = None
        self._dirty = False
        self.last_scan_s = 0.0
        self._candidates = []

    def load(self) -> "GitWatcher":
        p = self.path or state_path()
        self.path = p
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return self
        if not isinstance(data, dict) or data.get("version") != 1:
            return self
        self.repos = data.get("repos") or {}
        self.scanned = data.get("scanned") or {}
        return self

    def save(self) -> None:
        if not self._dirty or not self.path:
            return
        try:
            tmp = self.path + ".part"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "repos": self.repos,
                           "scanned": self.scanned}, fh,
                          ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            pass

    def refresh(self, budget_s: float = 0.02) -> bool:
        """扫一点。返回这次有没有更新。

        **每次只花 `budget_s`** —— 挂在 `_poll` 上，不能卡住界面。
        第一次会列一遍候选仓库（走几个常见目录），之后一个个算。
        """
        t0 = time.time()
        if not self._candidates:
            self._candidates = find_repos(self.roots)
            if not self._candidates:
                return False

        changed = False
        while self._candidates:
            if time.time() - t0 > budget_s:
                break
            repo = self._candidates.pop(0)
            try:
                head = _git(["rev-parse", "HEAD"], repo, timeout=2.0).strip()
            except Exception:                            # noqa: BLE001
                continue
            rec = self.scanned.get(repo)
            if rec and rec[0] == head:
                continue
            st = repo_stats(repo)
            self.scanned[repo] = [head, time.time()]
            self._dirty = True
            changed = True
            if st:
                self.repos[repo] = st
        self.last_scan_s = time.time() - t0
        return changed

    def all_insights(self, limit: int = 6) -> list:
        """所有仓库的洞察，热的在前。"""
        rows = []
        for st in self.repos.values():
            for line in insights(st):
                rows.append((st.get("commits") or 0, line))
        rows.sort(key=lambda r: -r[0])
        return [r[1] for r in rows[:limit]]

    def totals(self) -> dict:
        commits = sum(int(s.get("commits") or 0) for s in self.repos.values())
        return {"repos": len(self.repos), "commits": commits}


def format_report(gw: GitWatcher) -> str:
    """给人看的报告。**只有文件名和次数。**"""
    if not gw.repos:
        if not git_available():
            return "这台机器上没有 git —— 这个功能需要 git 才能用。"
        return ("还没找到 git 仓库（找的是常见的代码目录）。\n"
                "或者仓库都还没提交过东西。")
    t = gw.totals()
    lines = [f"找到 {t['repos']} 个仓库，最近看过 {t['commits']} 次提交。", ""]
    for line in gw.all_insights(10):
        lines.append("· " + line)
    return "\n".join(lines)
