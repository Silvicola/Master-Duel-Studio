"""预设素材库：只复制原件、名字不许撞、内置不许删、时长只读一次。

跑法：PYTHONPATH=core python -m pytest tests/test_sound_presets.py -q
不碰游戏目录，也不真的转码；audio_seconds 被替换成固定值。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from mdstudio.ops import sound as S  # noqa: E402


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    """把 data 根与随包预设目录都指到临时目录，隔离真实文件"""
    monkeypatch.setenv("MDSTUDIO_HOME", str(tmp_path / "data"))
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    for n in ("Ciallo", "内置二"):
        (builtin / f"{n}.mp3").write_bytes(b"fake-mp3-" + n.encode())
    src = tmp_path / "我的音频.mp3"
    src.write_bytes(b"source-bytes")

    monkeypatch.setattr(S, "builtin_presets_dir", lambda: builtin)
    probe = {"n": 0}

    def fake_seconds(p):
        probe["n"] += 1
        return 1.5

    monkeypatch.setattr(S, "audio_seconds", fake_seconds)
    return src, probe


def test_builtin_and_mine_are_separate(lib):
    """内置标 builtin，用户存的没有，界面据此决定给不给删"""
    src, _ = lib
    rows = S.list_presets()
    assert {r["name"] for r in rows} == {"Ciallo", "内置二"}
    assert all(r["builtin"] for r in rows)

    S.save_preset("我的音效", src)
    rows = S.list_presets()
    mine = [r for r in rows if not r["builtin"]]
    assert [r["name"] for r in mine] == ["我的音效"]
    assert rows[0]["name"] == "我的音效"   # 用户存的排最前


def test_save_copies_the_file_without_transcoding(lib):
    """只复制原件，字节不变；预设只是素材，应用时才走 create_slot 转码"""
    src, _ = lib
    out = S.save_preset("我的音效", src)
    assert Path(out["path"]).read_bytes() == b"source-bytes"
    assert src.read_bytes() == b"source-bytes"   # 源文件不动
    assert out["seconds"] == 1.5


def test_duplicate_name_rejected_including_builtin(lib):
    """撞内置或撞自己都拒绝，界面提示换名而不是自动加后缀"""
    from mdstudio.errors import MDStudioError

    src, _ = lib
    S.save_preset("我的音效", src)
    for bad in ("我的音效", "Ciallo", "ciallo", "内置二"):
        with pytest.raises(MDStudioError):
            S.save_preset(bad, src)


def test_builtin_cannot_be_deleted(lib):
    from mdstudio.errors import NotOurs

    src, _ = lib
    S.save_preset("我的音效", src)
    with pytest.raises(NotOurs):
        S.delete_preset("Ciallo")
    assert S.delete_preset("我的音效") is True
    assert [r["name"] for r in S.list_presets()] == ["Ciallo", "内置二"]


def test_duration_is_probed_once_then_cached(lib):
    """时长探测要跑 ffmpeg，列表不许每次开弹窗都重跑一遍"""
    src, probe = lib
    S.save_preset("我的音效", src)
    probe["n"] = 0
    S.list_presets()
    first = probe["n"]
    S.list_presets()
    assert probe["n"] == first, "第二次列举不应再探测任何文件"
    assert first <= 3, "首次只该探测还没进索引的文件"


def test_invalid_names_rejected(lib):
    from mdstudio.errors import MDStudioError

    src, _ = lib
    for bad in ("", "  ", "a/b", "a:b", "a|b"):
        with pytest.raises(MDStudioError):
            S.save_preset(bad, src)
