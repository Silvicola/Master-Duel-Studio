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


def test_cutin_offset_is_character_height_multiple():
    """界面填的是「角色高度倍数」：1.0 = 挪一个角色高度（2880 单位 = 一屏）

    2026-11-21 改的口径。旧版按帧图像素算，换素材含义就变，所以不再依赖分辨率。
    """
    from mdstudio.ops.video.cutin_flip import offset_to_units

    assert offset_to_units(1.0, 2880.0) == pytest.approx(2880.0)
    assert offset_to_units(0.5, 2880.0) == pytest.approx(1440.0)
    assert offset_to_units(0, 2880.0) == 0.0


def test_cutin_offset_is_percent_of_screen():
    """界面填 % 屏宽 / % 屏高，换算成骨骼单位。

    一屏高 = 骨架宽 / 角色高度（骨架宽已经按来源卡的 Spine 物体缩放折算过），
    所以 50% 屏宽 = 0.5 × 一屏高 × 16/9。
    """
    from mdstudio.ops.video.cutin_flip import screen_offsets

    assert screen_offsets(50, 0, 1.0, 2880) == pytest.approx((2560, 0))
    assert screen_offsets(0, 25, 1.0, 2880) == pytest.approx((0, 720))
    # 角色高度 0.5：骨架只占半屏，一屏高 = 5760 单位
    assert screen_offsets(50, 0, 0.5, 2880) == pytest.approx((5120, 0))
    assert screen_offsets(0, 0, 1.0, 2880) == (0.0, 0.0)


def test_cutin_offset_does_not_depend_on_skeleton_scale():
    """底图的 Spine 物体缩放不同（实测 0.09~0.28），同一个百分比要落到同一处。

    2026-11-22 的错：界面按固定 2880 单位换算，缩放 0.09 的卡上 50% 只挪了 22% 屏宽。
    """
    from mdstudio.ops.video.cutin_flip import screen_offsets

    a = screen_offsets(50, 10, 1.0, 2880)      # 缩放 0.20 的底图
    b = screen_offsets(50, 10, 1.0, 6400)      # 缩放 0.09 的底图
    assert a[0] / 2880 == pytest.approx(b[0] / 6400)
    assert a[1] / 2880 == pytest.approx(b[1] / 6400)


def test_cutin_offset_lands_on_root_bone():
    """界面的位移最终写进骨架 root 骨骼，单位是骨骼单位，且随角色尺寸等比"""
    import json

    from mdstudio.ops.video.cutin_flip import gen_skeleton, offset_to_units

    w = 2880.0
    ox = offset_to_units(-0.5, w)             # 半个角色高度
    data = json.loads(gen_skeleton(w, w, ["f0"], 2.0, pop=False, ox=ox, oy=-ox))
    root = data["bones"][0]
    assert root["name"] == "root"
    assert root["x"] == pytest.approx(ox, abs=0.01)
    assert root["y"] == pytest.approx(-ox, abs=0.01)


# --------------------------------------------------------------- 画面高度
# 界面填的「画面高度」= 可见内容占屏高，不是帧占屏高。
# 2026-11-22 用户实测：填 0.5，预览和游戏里都只有三成多高。原因是 matte 模式把
# 素材裁成正方形，1080² 的帧里只有 790 是画面，而换算时用了帧高。

def _shown_px(size_h, frame_w, content_h, frame_h=None, screen=(1920, 1080)):
    """按引擎口径还原：内容在屏幕上占多少像素高"""
    from mdstudio.ops.video.runfx_flip import size_h_to_max_size
    frame_h = content_h if frame_h is None else frame_h
    quad = size_h_to_max_size(size_h, frame_w, frame_h, content_h) * screen[0]
    return quad * content_h / frame_w


def test_size_h_counts_visible_content_not_frame():
    """正方形帧 (1080²) 里只有 790 高是画面：填 0.5 要画到屏高的一半"""
    from mdstudio.ops.video.runfx_flip import VISUAL_PAD

    # 有透明留白时补 3% 柔边（实测肉眼只看得到 47.6%，差的正是这圈淡边）
    assert _shown_px(0.5, 1080, 790, 1080) == pytest.approx(540 * VISUAL_PAD, rel=1e-3)
    assert _shown_px(1.0, 1080, 790, 1080) == pytest.approx(1080 * VISUAL_PAD, rel=1e-3)


def test_size_h_unchanged_for_full_frame():
    """整帧不透明（16:9 全屏视频）时口径不变，也不补柔边：1.0 铺满，1.08 多一点"""
    assert _shown_px(1.0, 1920, 1080) == pytest.approx(1080, rel=1e-3)
    assert _shown_px(1.08, 1920, 1080) == pytest.approx(1166, rel=1e-3)


def test_content_box_ignores_transparent_padding():
    """包围盒只认 alpha，不能把上下留白算成画面"""
    import numpy as np

    from mdstudio.ops.video.frames import content_box

    f = np.zeros((1080, 1080, 4), np.uint8)
    f[145:935, 10:1070] = 255                     # 可见画面 1060 x 790
    assert content_box([f]) == (1060, 790)
    assert content_box([f, None, f]) == (1060, 790)


def test_content_box_falls_back_when_nothing_is_opaque():
    """全透明帧不炸，交给调用方退回帧尺寸"""
    import numpy as np

    from mdstudio.ops.video.frames import content_box

    assert content_box([np.zeros((64, 64, 4), np.uint8)]) == (0, 0)


# --------------------------------------------------------------- 生成时的条目
# 2026-11-22 之前生成特效是「删光自己那侧 + 加一条 runTiming=0」，多时机的卡
# （连锁发动 + 被破坏）生成后只剩第一条。现在照抄每条条目，只换 effectPath。

_GATE_TMPL = {
    "mrk": 11016, "runTiming": 0, "player": 2, "effectPath": "old", "centerEffectPath": "old",
    "seLabel": "", "finishSecond": 0.0, "waitFinish": 0, "blockInput": 1,
    "reserveCardMoveEffect": 1, "cardMoveEffectLabel": "x",
    "cardMoveEffectTargetCardID": [1], "cardMoveEffectMoveType": [1],
    "effectNo": 5, "playType": 0, "delay": 0.0,
}

_NEW_PATH = "Duel/Timeline/Card/<_CARD_ILLUST_>/Ef90001/Ef90001"


def test_retarget_entries_keeps_every_timing():
    """两个时机的卡生成后还是两条，路径都换到新特效，音效各留各的"""
    from mdstudio.ops.video.runfx_flip import retarget_entries

    mine = [
        {"mrk": 22159, "runTiming": 0, "player": 0, "seLabel": "SE_A"},
        {"mrk": 22159, "runTiming": 1, "player": 2, "seLabel": "SE_B", "delay": 0.25},
    ]
    out = retarget_entries(mine, _GATE_TMPL, 22159, _NEW_PATH, "", 2.0)

    assert [e["runTiming"] for e in out] == [0, 1]
    assert [e["player"] for e in out] == [0, 2]
    assert [e["seLabel"] for e in out] == ["SE_A", "SE_B"]
    assert [e["delay"] for e in out] == [0.0, 0.25]
    assert all(e["effectPath"] == e["centerEffectPath"] == _NEW_PATH for e in out)
    assert all(e["mrk"] == 22159 for e in out)


def test_retarget_entries_keeps_the_safety_values():
    """照抄时机，但卡移动那套、硬超时、不锁输入仍按 clean_entry 定死"""
    from mdstudio.ops.video.runfx_flip import retarget_entries

    out = retarget_entries([{"mrk": 22159, "runTiming": 1, "player": 2}],
                           _GATE_TMPL, 22159, _NEW_PATH, "", 2.0)
    e = out[0]
    assert e["reserveCardMoveEffect"] == 0
    assert e["cardMoveEffectLabel"] == ""
    assert e["waitFinish"] == 1
    assert e["blockInput"] == 0
    assert e["finishSecond"] == pytest.approx(2.8)


def test_retarget_entries_gives_one_entry_when_nothing_existed():
    """无特效的卡没有可抄的条目，照旧按模板造一条连锁发动"""
    from mdstudio.ops.video.runfx_flip import retarget_entries

    out = retarget_entries([], _GATE_TMPL, 22159, _NEW_PATH, "", 2.0)
    assert len(out) == 1
    assert out[0]["runTiming"] == 0
    assert out[0]["player"] == 2
    assert out[0]["effectPath"] == _NEW_PATH


def test_retarget_entries_explicit_sound_overrides_all():
    """命令行给了 --se 就统一盖掉，不然每个时机还是自己的官方音效"""
    from mdstudio.ops.video.runfx_flip import retarget_entries

    mine = [{"mrk": 22159, "runTiming": 0, "seLabel": "SE_A"},
            {"mrk": 22159, "runTiming": 1, "seLabel": "SE_B"}]
    out = retarget_entries(mine, _GATE_TMPL, 22159, _NEW_PATH, "SE_NEW", 2.0)
    assert [e["seLabel"] for e in out] == ["SE_NEW", "SE_NEW"]


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
