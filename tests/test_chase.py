"""追鼠标的测试。

## 这个功能最怕的三件事

1. **永远追** —— oneko 就是因为这个出名地有点烦。她必须只在
   「光标在动 + 离得远 + 还有力气」的时候才过来。
2. **追不到** —— 目标没夹到屏幕里，她会一路走穿。
3. **卡住** —— 追到之后不切状态，或者追太久不放弃。

测法都走 `_poll()`（规则 1），不直接调 `_try_chase`。
"""

import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import pet_base as B  # noqa: E402

_TMP = None


def setUpModule():
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="dsh-pet-chase-test-")
    B.data_dir = lambda: _TMP
    import pet_i18n as I
    I.set_language("zh")


import pet_i18n as I  # noqa: E402
import pet_sources as S  # noqa: E402
import pet_types as PT  # noqa: E402


class ChaseBase(unittest.TestCase):
    """造一个干净的 CompanionSource，直接调 poll()。"""

    def setUp(self):
        # **钉死系统空闲时间** —— 不然 `idle > IDLE_SLEEP` 会先把她哄睡，
        # 根本轮不到追鼠标（规则 6：测试不许依赖机器状态）。
        self._orig_idle = S.idle_seconds
        S.idle_seconds = lambda: 1.0
        self.addCleanup(lambda: setattr(S, "idle_seconds", self._orig_idle))
        self.c = S.CompanionSource(os.path.join(_TMP, f"mood-{id(self)}.json"))
        self.c.state = "idle"
        self.c.state_until = 0.0
        self.c.pet_x = 100
        self.c.mood.energy = 0.8
        # 关掉低频噪声：别让她突然打盹/饿/打招呼，搅乱状态
        self.c.next_roam = time.time() + 9999
        self.c.next_peek = time.time() + 9999
        self.c.next_hungry = time.time() + 9999
        self.c.next_lonely = time.time() + 9999

    def _poll(self, dt=1.0, mouse_at=None, mouse_x=None):
        now = time.time() + dt
        if mouse_at is not None:
            self.c.mouse_at = mouse_at
        if mouse_x is not None:
            self.c.mouse_x = mouse_x
        # poll 内部取 now；为了让"鼠标刚动过"成立，把 mouse_at 设成现在
        return self.c.poll()


class TestChaseStarts(ChaseBase):
    def test_far_mouse_after_gaze_starts_chase(self):
        """**先看后追。** 第一次 poll 是 gazing，看完才 chasing。"""
        now = time.time()
        self.c.mouse_at = now
        self.c.mouse_x = 1200                          # 远
        snap = self.c.poll()
        self.assertEqual(snap.state, "gazing", "第一步应该是先看你")

        # 让 gazing 的保持时间过期
        self.c.state_until = 0.0
        self.c.mouse_at = time.time()
        snap = self.c.poll()
        self.assertEqual(snap.state, "chasing", "看完了应该在远处追过来")

    def test_near_mouse_does_not_chase(self):
        """**够近就不追** —— 不会扑到光标上。"""
        self.c.pet_x = 100
        # 中心 = 100 + 124 = 224。放 250，离中心 26px，在死区里
        self.c.mouse_at = time.time()
        self.c.mouse_x = 250
        self.c.state = "gazing"
        self.c.state_until = 0.0
        snap = self.c.poll()
        self.assertNotEqual(snap.state, "chasing")

    def test_stale_mouse_does_not_chase(self):
        """**你不动她就不来。** 光标停了很久，她回去过日子。"""
        self.c.mouse_at = time.time() - 60
        self.c.mouse_x = 1500
        self.c.state = "gazing"
        self.c.state_until = 0.0
        snap = self.c.poll()
        self.assertNotEqual(snap.state, "chasing")

    def test_tired_does_not_chase(self):
        self.c.mood.energy = 0.05
        self.c.mouse_at = time.time()
        self.c.mouse_x = 1500
        self.c.state = "gazing"
        self.c.state_until = 0.0
        snap = self.c.poll()
        self.assertNotEqual(snap.state, "chasing", "没力气还追")


class TestChaseEnds(ChaseBase):
    def _start_chase(self, mouse_x=1200):
        self.c.mouse_at = time.time()
        self.c.mouse_x = mouse_x
        self.c.state = "gazing"
        self.c.state_until = 0.0
        snap = self.c.poll()
        self.assertEqual(snap.state, "chasing", "没进 chasing")
        return snap

    def test_arrives_and_looks_at_you(self):
        """追到了切 `gazing` —— **16 方向素材用在这儿**。"""
        self._start_chase()
        self.c.state_until = 0.0
        # 模拟"走到了"：把窗口挪到光标附近
        self.c.pet_x = self.c.mouse_x - B.WIN_W / 2
        self.c.mouse_at = time.time()
        snap = self.c.poll()
        self.assertEqual(snap.state, "gazing", "到了应该停下来看你")

    def test_gives_up_after_too_long(self):
        """**不跟着跑到天涯海角。**"""
        self._start_chase()
        self.c.chase_since = time.time() - (B.CHASE_MAX_S + 1)
        self.c.state_until = 0.0
        self.c.mouse_at = time.time()
        snap = self.c.poll()
        self.assertEqual(snap.state, "idle", "追太久了应该放弃")

    def test_gives_up_when_you_stop_moving(self):
        self._start_chase()
        self.c.state_until = 0.0
        self.c.mouse_at = time.time() - 10             # 早就没动了
        snap = self.c.poll()
        self.assertEqual(snap.state, "idle", "你不该一直跟着一个不动的光标")

    def test_target_is_clamped_to_the_screen(self):
        """**目标要夹进屏幕** —— 不然她会一路走穿。"""
        self.c.mouse_x = 999999
        self.c.mouse_at = time.time()
        self.c.state = "gazing"
        self.c.state_until = 0.0
        snap = self.c.poll()
        self.assertEqual(snap.state, "chasing")
        vx, _vy, vw, _vh = B.virtual_screen()
        self.assertLessEqual(snap.wander, vx + vw - B.WIN_W + 1)
        self.assertGreaterEqual(snap.wander, vx - 1)


class TestChasePresentsItself(ChaseBase):
    def test_uses_a_faster_step_than_wander(self):
        """追要比溜达快 —— 不然"赶过来"看起来像"散步过来"。"""
        self.c.mouse_x = 1200
        self.c.mouse_at = time.time()
        self.c.state = "gazing"
        self.c.state_until = 0.0
        snap = self.c.poll()
        self.assertEqual(snap.wander_step, B.CHASE_STEP)
        self.assertGreater(B.CHASE_STEP, B.WANDER_STEP)

    def test_wander_step_is_zero_when_not_chasing(self):
        """不在追的时候必须是 0 —— 让 PetApp 用默认步子。"""
        self.c.mouse_at = 0
        snap = self.c.poll()
        self.assertEqual(snap.wander_step, 0)

    def test_mirror_faces_the_cursor(self):
        self.c.mouse_x = 1200                          # 右边
        self.c.mouse_at = time.time()
        self.c.state = "gazing"
        self.c.state_until = 0.0
        snap = self.c.poll()
        self.assertEqual(snap.state, "chasing")
        self.assertFalse(snap.mirror, "往右追不该镜像")

    def test_state_has_a_colour(self):
        """新状态要在 PALETTE 里 —— 不然信息卡没颜色。"""
        self.assertIn("chasing", B.PALETTE)
        self.assertIn("label", B.PALETTE["chasing"])


class TestSnapshotContract(unittest.TestCase):
    def test_wander_step_defaults_to_zero(self):
        self.assertEqual(PT.Snapshot().wander_step, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
