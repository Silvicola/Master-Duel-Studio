"""卡图页超框档位：界面标签统一、筛选合并（2026-11-17 用户定稿）。

纯函数测试，不起引擎、不碰游戏目录。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "core"))

from mdanim_engine.backend import _art_filter_tags, Backend  # noqa: E402


def test_self_filter_collects_both_selfflavours():
    """筛选「自改超框」要同时收原本官方有的和原本没有的。"""
    assert _art_filter_tags("self") == ("self", "selfOfficial")
    # 其余档位一对一，别顺手放宽
    assert _art_filter_tags("official") == ("official",)
    assert _art_filter_tags("none") == ("none",)


def test_art_of_keeps_four_internal_states():
    """界面合并了标签，内部判据仍是四态：分享页与本机状态靠它区分。"""
    art_of = Backend._art_of
    assert art_of(1, {1}, set()) == "official"
    assert art_of(1, {1}, {1}) == "selfOfficial"
    assert art_of(1, set(), {1}) == "self"
    assert art_of(1, set(), set()) == "none"
