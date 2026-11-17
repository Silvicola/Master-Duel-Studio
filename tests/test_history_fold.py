"""历史页折叠：同卡同族合成一行。纯函数测试，不起引擎、不碰游戏目录。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "core"))

from mdanim_engine.backend import fold_history  # noqa: E402


def row(rid, op, card, t, batch="", **kw):
    d = {"id": rid, "op": op, "card": card, "time": f"2026-11-13 10:{t:02d}",
         "_time": float(t), "batch": batch}
    d.update(kw)
    return d


def test_same_card_same_family_folds_into_one():
    rows = [
        row("c", "cutin.video", 11111, 30),
        row("b", "cutin.video", 11111, 20),
        row("a", "cutin.video", 11111, 10),
    ]
    out = fold_history(rows)
    assert len(out) == 1
    assert out[0]["id"] == "c"          # 最新那条作代表
    assert out[0]["count"] == 3
    assert len(out[0]["revisions"]) == 3
    assert "_time" not in out[0]


def test_different_families_are_separate():
    rows = [
        row("d", "runfx.video", 11111, 40),
        row("c", "overframe.set", 11111, 30),
        row("b", "cutin.video", 11111, 20),
        row("a", "cutin.video", 11111, 10),
    ]
    out = fold_history(rows)
    assert {r["op"] for r in out} == {"runfx.video", "overframe.set", "cutin.video"}
    assert next(r for r in out if r["op"] == "cutin.video")["count"] == 2


def test_different_cards_are_separate():
    out = fold_history([row("a", "cutin.video", 1, 10), row("b", "cutin.video", 2, 20)])
    assert len(out) == 2


def test_sound_merges_into_effect_row():
    rows = [
        row("s", "sound.inject", 11111, 21),
        row("f", "runfx.video", 11111, 20),
        row("s2", "sound.inject", 11111, 11),
        row("f2", "runfx.video", 11111, 10),
    ]
    out = fold_history(rows)
    assert len(out) == 1
    assert out[0]["id"] == "f"
    assert out[0]["count"] == 2          # 音效不额外算一次生成
    ops = [r["op"] for r in out[0]["revisions"]]
    assert ops.count("sound.inject") == 2


def test_sound_stands_alone_without_effect():
    out = fold_history([row("s", "sound.inject", 11111, 10)])
    assert len(out) == 1 and out[0]["op"] == "sound.inject"


def test_pack_rows_are_not_folded():
    """整合包摘要和明细各留各的，C# 靠 batch 归组，折掉就挂不上子项了"""
    rows = [
        row("d", "pack.cutin", 11111, 40, batch="B1"),
        row("c", "pack.cutin", 22222, 30, batch="B1"),
        row("i", "pack.install", 0, 20, batch="B1"),
    ]
    out = fold_history(rows)
    assert len(out) == 3
    assert [r["id"] for r in out] == ["d", "c", "i"]
    assert all(r.get("count", 1) == 1 for r in out)


def test_pack_detail_marked_covered_by_later_own_work():
    """包明细留着，用户自己那条并进子项，不再另起独立行"""
    rows = [
        row("own", "cutin.video", 11111, 40),
        row("p", "pack.cutin", 11111, 30, batch="B1"),
    ]
    out = fold_history(rows)
    assert len(out) == 1
    detail = out[0]
    assert detail["id"] == "p"
    assert detail["covered"] is True
    assert detail["own_count"] == 1


def test_pack_detail_counts_only_later_own_changes():
    """次数只数用户自己后来改的，包装的那次不重复算"""
    rows = [
        row("o3", "cutin.video", 11111, 60),
        row("o2", "cutin.video", 11111, 50),
        row("p", "pack.cutin", 11111, 30, batch="B1"),
        row("o1", "cutin.video", 11111, 20),   # 装包之前改的，不算
    ]
    out = fold_history(rows)
    # 装包之前那条不是「被包覆盖」，仍单独留一行
    assert len(out) == 2
    p = next(r for r in out if r["id"] == "p")
    assert p["own_count"] == 2
    assert p["covered"] is True


def test_pack_detail_not_covered_by_earlier_own_work():
    """用户先自己改、后装包覆盖，包是生效版本，不该标被覆盖"""
    rows = [
        row("p", "pack.cutin", 11111, 40, batch="B1"),
        row("own", "cutin.video", 11111, 30),
    ]
    out = fold_history(rows)
    assert next(r for r in out if r["id"] == "p")["covered"] is False
    assert any(r["id"] == "own" for r in out)


def test_export_and_install_never_fold():
    rows = [row("a", "pack.export", 0, 10), row("b", "pack.export", 0, 20)]
    assert len(fold_history(rows)) == 2


def test_combined_rules_in_one_history():
    """同一页里两套规则并存：有包明细的卡并入子项，没有的折成一行"""
    rows = [
        row("o2", "cutin.video", 11111, 60),
        row("o1", "cutin.video", 11111, 50),
        row("p", "pack.cutin", 11111, 30, batch="B1"),
        row("i", "pack.install", 0, 29, batch="B1"),
        row("x", "cutin.video", 22222, 20),
        row("x0", "cutin.video", 22222, 10),
    ]
    out = fold_history(rows)
    p = next(r for r in out if r["id"] == "p")
    x = next(r for r in out if r["id"] == "x")
    assert p["own_count"] == 2 and p["covered"] is True
    assert x["count"] == 2
    assert any(r["id"] == "i" for r in out)
    # 用户自己的两条不另起独立行
    assert not any(r["id"] in ("o1", "o2") for r in out)
    assert not any(r["id"] == "x0" for r in out)
    assert all("_time" not in r for r in out)
