"""动画引用资产的识别（2026-11-18，2026-11-20 改判据）。

以前按名字猜：animation / animation_kde / animation2 / P16632animation /
"P16537 animation" 都含 animation，硬编码会漏。现在按包里有没有
AnimationReferenceAsset 的字段（animationName + skeletonDataAsset）认，
因为 #19886 的引用资产叫 `kde`，一个 animation 都不含，按名字会整条跳过重定向，
游戏里停在第一帧。纯函数测试，不起引擎。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "core"))

from mdstudio.ops.video import cutin_flip as CF  # noqa: E402

BASE = "Duel/Timeline/Duel/MonsterCutIn/tcg/P14952"
REF = b"<animation-reference>"


class _Cat:
    def __init__(self, names):
        self._names = names

    def names(self):
        return self._names


class _Tx:
    def __init__(self, names, refs=()):
        self.catalog = _Cat(names)
        self._refs = set(refs)

    def read_file(self, name):
        return REF if name in self._refs else b"<other>"


def _setup(monkeypatch, names, refs):
    # 让 crc 变成恒等映射，测试里就不必算哈希
    monkeypatch.setattr(CF, "get_crc", lambda s: s)
    monkeypatch.setattr(CF, "_looks_like_anim_reference", lambda data: data == REF)
    return _Tx(names, refs)


def test_non_animation_name_is_recognized(monkeypatch):
    """#19886：引用资产叫 kde，名字里没有 animation，仍要认出来。"""
    tx = _setup(monkeypatch, [f"{BASE}/kde", f"{BASE}/P14952Timeline"], {f"{BASE}/kde"})
    assert CF._anim_asset_name(tx, 14952) == "kde"


def test_variants_are_recognized(monkeypatch):
    for tail in ("animation_kde", "animation2", "P16632animation", "P16537 animation"):
        name = f"{BASE}/{tail}"
        tx = _setup(monkeypatch, [name, f"{BASE}/P14952Timeline"], {name})
        assert CF._anim_asset_name(tx, 14952) == tail


def test_plain_animation_wins_when_both_exist(monkeypatch):
    a, b = f"{BASE}/animation_kde", f"{BASE}/animation"
    tx = _setup(monkeypatch, [a, b], {a, b})
    assert CF._anim_asset_name(tx, 14952) == "animation"


def test_none_when_no_animation_asset(monkeypatch):
    tx = _setup(monkeypatch, [f"{BASE}/P14952Timeline",
                              f"{BASE}/HighEnd_HD/P14952JS_SkeletonData"], set())
    assert CF._anim_asset_name(tx, 14952) is None


def test_subdirectory_assets_are_ignored(monkeypatch):
    """base 下一层才算，画质目录里的东西不能当成引用资产。"""
    name = f"{BASE}/HighEnd_HD/animation_dummy"
    tx = _setup(monkeypatch, [name], {name})
    assert CF._anim_asset_name(tx, 14952) is None
