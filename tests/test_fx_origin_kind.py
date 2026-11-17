"""自改那一行的悬停要说清来历：来自无特效 / 来自官方特效 / 来自官方特效★。

判据全在 journal 的 runfx_removed 里：空 = 原本没有特效（克隆）；有条目时看资源号，
Ef 号等于卡号是自有的，指向别卡是借用（#22770 那条写的是 Ef22692）。
路径为空也算自有，有 9 条官方条目靠 useCenterEffect 隐式指向自己。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from mdanim_engine.backend import Backend  # noqa: E402


def _rec(op, card, removed):
    return SimpleNamespace(op=op, card=card, runfx_removed=removed)


def _entry(card: int, ef: int | None, center: bool = True) -> dict:
    path = "" if ef is None else f"Duel/Timeline/Card/<_CARD_ILLUST_>/Ef{ef}/Ef{ef}"
    return {"mrk": card,
            "centerEffectPath": path if center else "",
            "effectPath": "" if center else path}


def _kind(recs) -> dict:
    ctx = SimpleNamespace(journal=SimpleNamespace(load=lambda: recs))
    return Backend()._fx_origin_kind(ctx)


def test_cloned_card_has_no_origin():
    """原本没有特效：克隆时会话里没有可替换的条目，记录里 runfx_removed 为空"""
    assert _kind([_rec("runfx.video", 22159, [])]) == {}


def test_own_official_effect_is_official():
    assert _kind([_rec("runfx.video", 20257, [_entry(20257, 20257)])]) == {20257: "official"}


def test_borrowed_variant_is_shared():
    """#22770 借 #22692，被替换的那条写的是 Ef22692"""
    assert _kind([_rec("runfx.video", 22770, [_entry(22770, 22692)])]) == {22770: "shared"}


def test_entry_without_path_counts_as_own():
    """#14876 这类靠 useCenterEffect 指自己，路径是空的，不能当成借用"""
    assert _kind([_rec("runfx.video", 14876, [_entry(14876, None)])]) == {14876: "official"}


def test_two_entries_one_own_wins():
    """双条目卡两条都是自己的资源"""
    rec = _rec("runfx.video", 15299, [_entry(15299, 15299), _entry(15299, 15299)])
    assert _kind([rec]) == {15299: "official"}


def test_non_runfx_records_are_ignored():
    """音效记录不带 runfx_removed，不该把卡算进「原本有特效」"""
    assert _kind([_rec("sound.custom", 15299, [])]) == {}


def test_broken_journal_falls_back_to_empty():
    def boom():
        raise OSError("journal unreadable")

    ctx = SimpleNamespace(journal=SimpleNamespace(load=boom))
    assert Backend()._fx_origin_kind(ctx) == {}
