"""动画引用资产名的模糊匹配（2026-11-18）。

实测有 animation / animation_kde / animation2 / P16632animation /
"P16537 animation" 几种写法，硬编码 animation 会漏掉大多数卡，导致
Timeline 的引用指向旧骨架。纯函数测试，不起引擎。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "core"))

from mdstudio.ops.video import cutin_flip as CF  # noqa: E402

BASE = "Duel/Timeline/Duel/MonsterCutIn/tcg/P14952"


class _Cat:
    def __init__(self, names):
        self._names = names

    def names(self):
        return self._names


class _Tx:
    def __init__(self, names):
        self.catalog = _Cat(names)


def test_variants_are_recognized():
    for tail in ("animation_kde", "animation2", "P16632animation", "P16537 animation"):
        tx = _Tx([f"{BASE}/{tail}", f"{BASE}/P14952Timeline",
                  f"{BASE}/HighEnd_HD/P14952JS_SkeletonData"])
        assert CF._anim_asset_name(tx, 14952) == tail


def test_plain_animation_wins_when_both_exist():
    tx = _Tx([f"{BASE}/animation_kde", f"{BASE}/animation"])
    assert CF._anim_asset_name(tx, 14952) == "animation"


def test_none_when_no_animation_asset():
    tx = _Tx([f"{BASE}/P14952Timeline",
              f"{BASE}/HighEnd_HD/P14952JS_SkeletonData"])
    assert CF._anim_asset_name(tx, 14952) is None


def test_subdirectory_assets_are_ignored():
    """base 下一层才算，画质目录里的东西不能当成引用资产。"""
    tx = _Tx([f"{BASE}/HighEnd_HD/animation_dummy"])
    assert CF._anim_asset_name(tx, 14952) is None
