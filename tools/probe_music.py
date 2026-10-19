"""手动验证：这台机器上的音乐软件，桌宠能不能读到。

**为什么是手动工具而不是自动化测试**：真 SMTC 读的是"此刻这台机器上在放什么"。
把它写进测试套件就等于让测试依赖机器状态 —— 换台机器、换首歌就挂
（AGENTS.md 规则 6 踩过：健康提醒的测试就是这么 flaky 的）。

自动化测试里用的是注入的假源；**真 SMTC 只能手动验**。

用法：

    python tools/probe_music.py

没在放音乐也能跑 —— 它会自己发布一个测试会话，验证"读得到 + 控得住"，
然后把它关掉。这样在任何机器上都能确认这条链路是通的。
"""

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {name} {extra}")
    else:
        fail += 1
        print(f"  FAIL {name} {extra}")


def show_live():
    """先看看现在真在放什么 —— 这是用户最关心的那个答案。"""
    import pet_music as M

    print("── 现在系统里的媒体会话 ──")
    s = M.MusicSource()
    s.start()
    for _ in range(12):
        time.sleep(0.5)
        if s.available:
            break
    if not s.available:
        print(f"  SMTC 不可用：{s.last_error}")
        print("  装依赖：pip install winrt-runtime winrt-Windows.Foundation \\")
        print("          winrt-Windows.Foundation.Collections \\")
        print("          winrt-Windows.Media.Control winrt-Windows.Media.Core \\")
        print("          winrt-Windows.Media.Playback winrt-Windows.Storage")
        s.stop()
        return

    time.sleep(1.5)
    sessions = s.sessions()
    cur = s.poll()
    print(f"  会话数: {len(sessions)}  {sessions}")
    if cur is None:
        print("  （现在没在放音乐 —— 打开 QQ音乐/网易云/Spotify 放一首再跑一次）")
    else:
        print(f"  来源:   {cur.app}")
        print(f"  曲目:   {cur.label()!r}")
        print(f"  专辑:   {cur.album!r}")
        print(f"  状态:   {'播放中' if cur.playing else '暂停/停止'}")
        print(f"  进度:   {cur.position:.1f}s / {cur.duration:.1f}s"
              f"  ({cur.progress() * 100:.0f}%)")
        print(f"  封面:   {'有' if cur.has_cover else '无'}")
        print(f"  刚切歌: {cur.just_changed()}")
    s.stop()


def self_test():
    """自己造一个会话，验证读+控制这条链路。"""
    print("\n── 自测（自己发布一个会话）──")
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )
        from winrt.windows.media.core import MediaSource
        from winrt.windows.media.playback import MediaPlayer
        from winrt.windows.storage import StorageFile
    except Exception as exc:                             # noqa: BLE001
        print(f"  跳过：没有 winrt 依赖（{type(exc).__name__}）")
        return

    import math
    import struct
    import wave

    tmp = os.path.join(tempfile.gettempdir(), "pet_music_probe.wav")
    rate = 22050
    with wave.open(tmp, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(250 * math.sin(2 * math.pi * 220 * i / rate)))
            for i in range(rate * 40)))

    async def run():
        f = await StorageFile.get_file_from_path_async(tmp)
        p = MediaPlayer()
        p.source = MediaSource.create_from_storage_file(f)
        p.play()
        await asyncio.sleep(3.0)
        mgr = await Manager.request_async()
        cur = mgr.get_current_session()
        chk("SMTC 里有会话", len(mgr.get_sessions()) > 0)
        chk("拿到当前会话", cur is not None)
        if cur is not None:
            info = await cur.try_get_media_properties_async()
            pb = cur.get_playback_info()
            tl = cur.get_timeline_properties()
            chk("读元数据", info is not None)
            chk("读时长", tl.end_time.total_seconds() > 0,
                f"{tl.end_time.total_seconds():.0f}s")
            before = pb.playback_status
            paused = await cur.try_pause_async()
            await asyncio.sleep(1.0)
            chk("能控制播放", paused
                and cur.get_playback_info().playback_status != before)
        p.pause()
        p.close()
        await asyncio.sleep(0.3)

    asyncio.run(run())
    try:
        os.remove(tmp)
    except OSError:
        pass


if __name__ == "__main__":
    show_live()
    self_test()
    print(f"\n  通过 {ok} / 失败 {fail}")
    sys.exit(1 if fail else 0)
