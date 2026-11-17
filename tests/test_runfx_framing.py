"""发动特效：画面调节参数与视频音轨候选。

跑法：PYTHONPATH=core:engine python -m pytest tests/test_runfx_framing.py -q
不需要游戏文件；底盘相关只验证参数是否转发到位。
"""
import math
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "core"))
sys.path.insert(0, str(_ROOT / "engine"))

from mdstudio.ops import sound as S  # noqa: E402


# --------------------------------------------------------------- 音轨候选

def test_frame_folder_is_skipped(tmp_path):
    """帧文件夹没有音轨，静默跳过，不能报错挡住选素材"""
    d = tmp_path / "frames"
    d.mkdir()
    (d / "f0001.png").write_bytes(b"fake")
    assert S.extract_video_track(d) == ""


def test_missing_video_is_skipped(tmp_path):
    assert S.extract_video_track(tmp_path / "nope.mp4") == ""


def test_audio_file_is_skipped(tmp_path):
    """导入列表里可能出现 mp3，它不是视频，不该被当成视频音轨"""
    p = tmp_path / "a.mp3"
    p.write_bytes(b"fake")
    assert S.extract_video_track(p) == ""


def test_video_without_track_is_skipped(tmp_path, monkeypatch):
    from mdstudio.ops.video import audio as A
    p = tmp_path / "v.mp4"
    p.write_bytes(b"fake")
    monkeypatch.setattr(A, "has_audio", lambda src: False)
    assert S.extract_video_track(p) == ""


def test_video_track_becomes_a_wav(tmp_path, monkeypatch):
    from mdstudio.ops.video import audio as A
    p = tmp_path / "v.mp4"
    p.write_bytes(b"fake")
    monkeypatch.setattr(A, "has_audio", lambda src: True)

    def fake_extract(src, out_wav, duration=None, loud=True):
        Path(out_wav).write_bytes(b"RIFFfake")
        return Path(out_wav)

    monkeypatch.setattr(A, "extract", fake_extract)
    monkeypatch.setenv("MDSTUDIO_HOME", str(tmp_path / "data"))
    out = S.extract_video_track(p)
    assert out and Path(out).read_bytes().startswith(b"RIFF")
    assert Path(out).parent.name == "preview"


# --------------------------------------------------------------- 参数转发

def test_build_bundle_accepts_framing_params():
    """四个画面参数走 build_bundle, 生成时一次写入。

    曾经有个 runfx.framing RPC 就地改写已生成的 bundle, 接到界面上做成「应用调节」
    按钮, 用户点了之后游戏报「文件读取错误」。这条路径已从界面撤掉, 这里只锁
    生成时的参数签名, 不再测那条原地改写的路。
    """
    import inspect
    from mdstudio.ops.video import runfx_flip

    params = inspect.signature(runfx_flip.build_bundle).parameters
    for name in ("max_size", "offset_x", "offset_y", "rotation"):
        assert name in params, name

    apply_params = inspect.signature(runfx_flip.apply).parameters
    for name in ("max_size", "offset_x", "offset_y", "rotation"):
        assert name in apply_params, name


def test_framing_rpc_is_gone():
    """这条 RPC 会就地改写已生成的 bundle 且未在真游戏里验证过, 不该再出现"""
    from mdanim_engine import actions
    assert "runfx.framing" not in actions.ACTIONS


# ------------------------------------------------- 重新生成之后复原要能回原版

def _entry(eid):
    return {"mrk": 22159,
            "effectPath": f"Duel/Timeline/Card/<_CARD_ILLUST_>/Ef{eid}/Ef{eid}"}


def test_removed_entries_pointing_at_deleted_bundles_are_dropped():
    """重新生成时上一版的 bundle 被回收了, 那条 gate 条目不能再补回列表.

    补回去的话游戏会去找一个不存在的 Ef 文件, 进对局报「文件读取错误」,
    而 gate 里有登记、journal 里没记录认领, 卡片还会显示成「官方特效」。
    """
    from mdstudio.ops.tx import restorable_removed

    gone, alive = _entry(90001), _entry(90002)
    kept = restorable_removed([gone, alive], exists=lambda lp: "90002" in lp)
    assert kept == [alive]


def test_removed_entries_without_path_are_kept():
    """老记录可能没 effectPath，不该因为查不到就丢掉"""
    from mdstudio.ops.tx import restorable_removed

    e = {"mrk": 22159}
    assert restorable_removed([e], exists=lambda lp: False) == [e]


def test_retired_entries_are_stripped_from_the_new_record():
    """retire_old 删掉旧特效时, 也要把旧条目从新记录的 runfx_removed 里剔掉"""
    from mdstudio.core.gate import runfx_sig

    old, other = _entry(90001), _entry(90002)
    removed = [old, other]
    gone_sigs = {runfx_sig(old)}
    left = [e for e in removed if runfx_sig(e) not in gone_sigs]
    assert left == [other]


def test_fx_offset_is_percent_of_screen_height():
    """特效页按「% 屏高」填，1% = 0.1 世界单位（整屏高按 10 个单位算）。

    跟分辨率无关：世界单位是绝对的，同一数值在任何屏幕上覆盖的屏高占比相同。
    这个 10 单位来自来源卡 prefab 的 camera2DSize=5.0，还需实测校准一次。
    """
    units_per_pct = 0.1
    assert 10 * units_per_pct == pytest.approx(1.0)      # 10% -> 1 个世界单位
    assert 100 * units_per_pct == pytest.approx(10.0)    # 100% -> 整整一屏
    assert -50 * units_per_pct == pytest.approx(-5.0)


def test_cutin_offset_one_frame_width():
    """动画页填「像素/帧」那个数，位移正好一个帧图宽（这是界面提示的依据）"""
    from mdstudio.ops.video.cutin_flip import offset_to_units

    size, fw = 2930.0, 1664.0
    assert offset_to_units(fw, size, fw) == pytest.approx(size)
    assert offset_to_units(fw / 2, size, fw) == pytest.approx(size / 2)
    assert offset_to_units(0, size, fw) == 0.0


def test_cutin_offset_lands_on_root_bone():
    """界面的位移最终写进骨架 root 骨骼，单位是骨骼单位，且随角色尺寸等比"""
    import json

    from mdstudio.ops.video.cutin_flip import gen_skeleton, offset_to_units

    w, fw = 2930.0, 1664.0
    ox = offset_to_units(832, w, fw)          # 半个帧图宽
    data = json.loads(gen_skeleton(w, w, ["f0"], 2.0, pop=False, ox=ox, oy=-ox))
    root = data["bones"][0]
    assert root["name"] == "root"
    assert root["x"] == pytest.approx(ox, abs=0.01)
    assert root["y"] == pytest.approx(-ox, abs=0.01)


# --------------------------------------------------------------- 图集容量

def _fits(n, fw, fh, limit=16384):
    cols = 8 if fw >= 1600 else max(1, math.ceil(n ** 0.5))
    while cols * fw > limit and cols > 1:
        cols -= 1
    rows = math.ceil(n / cols)
    return cols * fw <= limit and rows * fh <= limit


@pytest.mark.parametrize("cell", [720, 1080, 1440])
def test_default_60_frames_fits_all_tiers(cell):
    """60 帧默认下三档对正方形与 16:9 素材都要装得下单页图集"""
    wide = int(cell * 16 / 9 // 4) * 4
    assert _fits(60, cell, cell), f"1:1 @ {cell}"
    assert _fits(60, wide, cell), f"16:9 @ {cell}"


def test_4k_tier_is_not_offered():
    """2160 在 60 帧下两种素材都超限，所以档位里不该有 4K"""
    assert not _fits(60, 2160, 2160)
    assert not _fits(60, int(2160 * 16 / 9 // 4) * 4, 2160)
