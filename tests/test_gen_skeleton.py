"""帧数参数就是「一共多少帧」，时长参数就是总时长，两者互不换算。

这是用户定的设计（2026-11-18 明确指出）：勾 60 帧 2 秒，图集就是 60 张、
动画就是 2 秒；60 帧铺在 2 秒上是 30fps 的观感，属于正常，不该被自动补帧或重采样。

曾经为了绕开「播两遍」把帧数改写成 时长×60，那是误判。真因是 gen_atlas 页间
漏了空行，导致第二页整页作废，与帧数、帧率都无关。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from mdstudio.ops.video.cutin_flip import gen_skeleton


def _anim(n, duration):
    names = [f"frame_{i}" for i in range(n)]
    j = json.loads(gen_skeleton(4800, 2700, names, duration, pop=False))
    return j, j["animations"]["animation"]["slots"]["frame"]["attachment"]


def test_frame_count_is_kept_as_is():
    for n, dur in ((60, 2.0), (72, 1.6), (36, 0.9), (48, 1.5)):
        _, att = _anim(n, dur)
        assert len(att) == n, f"{n} 帧应为 {n} 个关键帧，实际 {len(att)}"


def test_times_are_strictly_increasing():
    for n, dur in ((60, 2.0), (72, 1.6), (36, 0.9)):
        _, att = _anim(n, dur)
        times = [k["time"] for k in att]
        assert all(b > a for a, b in zip(times, times[1:])), "关键帧时间必须严格递增"


def test_last_key_equals_duration():
    for n, dur in ((60, 2.0), (72, 1.6), (36, 0.9)):
        _, att = _anim(n, dur)
        assert att[0]["time"] == 0
        assert abs(att[-1]["time"] - dur) < 1e-6, f"末帧时间应为 {dur}，实际 {att[-1]['time']}"
        assert att[-1]["name"] == f"frame_{n - 1}"


def test_attachment_order_follows_frames():
    _, att = _anim(60, 2.0)
    assert [k["name"] for k in att] == [f"frame_{i}" for i in range(60)]


def test_declared_fps_matches_official():
    j, _ = _anim(60, 2.0)
    assert j["skeleton"]["fps"] == 60, "官方 1036 张骨架全是 60，跟着写 60"
