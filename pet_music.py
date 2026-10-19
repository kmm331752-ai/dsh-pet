"""音乐源：知道你在听什么，也能替你按暂停。

## 为什么用 SMTC 而不是一家家对接

Windows 的 SMTC（System Media Transport Controls）是**系统级的媒体会话**。
播放器只要把自己发布上去，任何程序都能读到曲名/歌手/专辑/封面/进度，
还能反过来控制播放。

实测支持：QQ音乐、网易云音乐、酷狗、酷我、Spotify、Apple Music、浏览器。

**不用 OAuth，不用一家一家写适配。** 比对接各家 API 省事一个数量级。

## 为什么单开一个线程

SMTC 是 WinRT 的**异步 API**，而桌宠的 `_poll()` 跑在 Tk 主循环里 ——
在那儿 await 会把界面冻住。

所以这里是一个后台线程跑自己的 asyncio 循环，把结果放进一个槽位；
`poll()` 只读槽位，**立刻返回，永不阻塞**。

## 依赖

用**模块化**的 `winrt-*` 包，不是单体的 `winsdk`：

    winsdk（单体）      41.7 MB   ← _winrt.pyd 一个就 37.6 MB
    winrt-*（模块化）    4.8 MB   ← 功能一模一样

装：

    pip install winrt-runtime winrt-Windows.Foundation \
                winrt-Windows.Media.Control winrt-Windows.Storage.Streams

没装也能跑 —— `available` 会是 False，音乐相关的功能安静地不出现。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# 轮询间隔。SMTC 的事件订阅在跨线程时不好处理，1 秒一次的轮询足够了 ——
# 而且读的是本地系统服务，不花网络。
POLL_INTERVAL = 1.0

# 曲目切换后，这个时间内算"刚切歌"（她会有反应）
JUST_CHANGED_S = 6.0

# 播放状态：SMTC 的 MediaPlaybackStatus 枚举
CLOSED, OPENED, CHANGING, STOPPED, PLAYING, PAUSED = 0, 1, 2, 3, 4, 5


@dataclass
class NowPlaying:
    """当前在放什么。"""

    app: str = ""                   # 来源应用（进程名）
    title: str = ""
    artist: str = ""
    album: str = ""
    playing: bool = False
    position: float = 0.0           # 秒
    duration: float = 0.0           # 秒
    has_cover: bool = False
    changed_at: float = 0.0         # 这首什么时候开始的（本地时钟）

    @property
    def is_music(self) -> bool:
        return bool(self.title)

    def label(self) -> str:
        """一行显示：歌手 - 歌名，没有歌手就只显示歌名。"""
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or self.artist or ""

    def progress(self) -> float:
        """0~1 的播放进度。没有时长就返回 0。"""
        if self.duration <= 0:
            return 0.0
        return max(0.0, min(1.0, self.position / self.duration))

    def just_changed(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.changed_at) < JUST_CHANGED_S if self.changed_at else False


class MusicSource:
    """后台读 SMTC，主线程读缓存。

    `poll()` 返回缓存，**不阻塞**。第一次调用时线程还没读到东西，
    返回 None 是正常的。
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._current: Optional[NowPlaying] = None
        self._available = False          # SMTC 本身能不能用（装了依赖 + 系统支持）
        self._probed = False             # 探测过没有
        self._last_error = ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pending: list = []         # 主线程要执行的控制命令
        self._sessions: list = []        # 所有会话的应用名（给"换一个播放器"用）

    # ── 主线程接口 ──────────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_error(self) -> str:
        return self._last_error

    def poll(self) -> Optional[NowPlaying]:
        """立刻返回当前曲目（缓存）。没有就 None。"""
        with self._lock:
            return self._current

    def sessions(self) -> list:
        """所有在发布媒体会话的应用名。"""
        with self._lock:
            return list(self._sessions)

    def toggle_play(self) -> bool:
        """播放/暂停。返回是否发出去了（不代表播放器一定响应）。"""
        return self._command("toggle")

    def next_track(self) -> bool:
        return self._command("next")

    def prev_track(self) -> bool:
        return self._command("prev")

    def _command(self, name: str) -> bool:
        if not self._available:
            return False
        with self._lock:
            self._pending.append(name)
        return True

    # ── 生命周期 ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="pet-music",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── 后台线程 ────────────────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            import asyncio
            asyncio.run(self._loop())
        except Exception as exc:                         # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._available = False

    async def _loop(self) -> None:
        import asyncio

        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
            )
        except Exception as exc:                         # noqa: BLE001
            # 没装 winrt-* 包 —— 安静地不可用，不是错误
            self._last_error = f"没有 winrt 依赖：{type(exc).__name__}"
            self._probed = True
            return

        try:
            mgr = await Manager.request_async()
        except Exception as exc:                         # noqa: BLE001
            self._last_error = f"拿不到媒体会话管理器：{type(exc).__name__}: {exc}"
            self._probed = True
            return

        self._available = True
        self._probed = True
        last_key = None

        while not self._stop.is_set():
            try:
                await self._tick(mgr)
                # 曲目变了就记下时间（"刚切歌"用）
                cur = self.poll()
                key = (cur.title, cur.artist) if cur else None
                if key and key != last_key:
                    last_key = key
            except Exception as exc:                     # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(POLL_INTERVAL)

    async def _tick(self, mgr) -> None:
        sessions = mgr.get_sessions()
        names = []
        for s in sessions:
            try:
                names.append(s.source_app_user_model_id or "?")
            except Exception:                            # noqa: BLE001
                pass
        with self._lock:
            self._sessions = names

        cur = mgr.get_current_session()
        np = None
        if cur is not None:
            try:
                info = await cur.try_get_media_properties_async()
                pb = cur.get_playback_info()
                tl = cur.get_timeline_properties()
                np = NowPlaying(
                    app=cur.source_app_user_model_id or "",
                    title=(info.title or "").strip(),
                    artist=(info.artist or "").strip(),
                    album=(info.album_title or "").strip(),
                    playing=(pb.playback_status == PLAYING),
                    position=tl.position.total_seconds(),
                    duration=tl.end_time.total_seconds(),
                    has_cover=info.thumbnail is not None,
                )
            except Exception:                            # noqa: BLE001
                np = None

        # 处理控制命令
        with self._lock:
            cmds, self._pending = self._pending, []
            prev = self._current

        for c in cmds:
            try:
                if c == "toggle":
                    await cur.try_toggle_play_pause_async()
                elif c == "next":
                    await cur.try_skip_next_async()
                elif c == "prev":
                    await cur.try_skip_previous_async()
            except Exception as exc:                     # noqa: BLE001
                self._last_error = f"控制失败：{type(exc).__name__}: {exc}"

        # 曲目变了就盖时间戳
        if np is not None:
            if prev is None or (prev.title, prev.artist) != (np.title, np.artist):
                np.changed_at = time.time()
            elif prev.changed_at:
                np.changed_at = prev.changed_at

        with self._lock:
            self._current = np
