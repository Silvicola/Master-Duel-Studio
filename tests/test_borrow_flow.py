"""借用与复原的边界：借用只搬自己那条、借用的标签、复原白名单的两条路径。

用假的 gate / journal 对象（list_cards 支持注入 gate），不碰真实安装目录。
"""
import sys
import pytest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from mdstudio.ops.runfx import borrow_entries, list_cards  # noqa: E402
from mdstudio.ops.tx import restorable_removed  # noqa: E402
from mdstudio.errors import MDStudioError  # noqa: E402

EF4887 = "Duel/Timeline/Card/tcg/Ef4887/Ef4887"


def entry(mrk, player, path=EF4887, se=""):
    return {"mrk": mrk, "player": player, "effectPath": path,
            "centerEffectPath": path, "runTiming": 0, "seLabel": se}


# ---------------------------------------------------------------- 借用搬哪几条

def test_borrow_only_takes_the_self_side():
    """双条目 donor（自己/对手各一条）只搬自己那条，对手条目不上目标卡"""
    got = borrow_entries([entry(4887, 0), entry(4887, 1)], 15299)
    assert [e["player"] for e in got] == [0]
    assert got[0]["mrk"] == 15299


def test_borrow_keeps_shared_entry_of_single_entry_donor():
    """单条目 donor（player=2 双方共用）原样搬"""
    got = borrow_entries([entry(4887, 2)], 15299)
    assert [e["player"] for e in got] == [2]


def test_borrow_drops_card_move_baggage():
    """天击龙那条模板带的卡移动字段必须清掉，否则目标卡会等一个永远不来的效果"""
    src = [dict(entry(4887, 0), reserveCardMoveEffect=1, cardMoveEffectLabel="x",
                cardMoveEffectTargetCardID=[13671])]
    got = borrow_entries(src, 15299)[0]
    assert got["reserveCardMoveEffect"] == 0
    assert got["cardMoveEffectLabel"] == ""
    assert got["cardMoveEffectTargetCardID"] == []


# ---------------------------------------------------------------- 借用的标签

def test_borrow_record_reads_as_borrowed():
    """借用之后界面该是「官方特效★」，不是「自改特效」"""
    e = entry(15299, 0)
    gate = SimpleNamespace(entries=[e])
    journal = SimpleNamespace(load=lambda: [
        SimpleNamespace(op="runfx.borrow", card=15299, runfx_added=[e], runfx_removed=[])])
    assert dict(list_cards(None, journal, gate))[15299] == "borrowed"


def test_video_record_still_reads_as_self():
    e = entry(15299, 0)
    gate = SimpleNamespace(entries=[e])
    journal = SimpleNamespace(load=lambda: [
        SimpleNamespace(op="runfx.video", card=15299, runfx_added=[e], runfx_removed=[])])
    assert dict(list_cards(None, journal, gate))[15299] == "yours"


def test_missing_entry_reads_as_wiped():
    """记录在、gate 条目没了（游戏更新）：仍是本工具的改动，复原要能点"""
    e = entry(15299, 0)
    gate = SimpleNamespace(entries=[])
    journal = SimpleNamespace(load=lambda: [
        SimpleNamespace(op="runfx.video", card=15299, runfx_added=[e], runfx_removed=[])])
    assert dict(list_cards(None, journal, gate))[15299] == "wiped"


# ---------------------------------------------------------------- 复原白名单

def test_restorable_keeps_entry_whose_center_path_exists():
    """effectPath 空、靠 centerEffectPath 隐式指向自己的那 9 条，不能被误丢"""
    e = entry(13631, 2, path="")
    e["centerEffectPath"] = "Duel/Timeline/Card/tcg/Ef13631/Ef13631"
    assert restorable_removed([e], lambda lp: "Ef13631" in lp) == [e]


def test_restorable_drops_entry_when_center_path_is_gone_too():
    """只看 effectPath 的旧实现会把这条补回去，游戏进对局就报文件读取错误"""
    e = entry(13631, 2, path="")
    e["centerEffectPath"] = "Duel/Timeline/Card/tcg/Ef13631/Ef13631"
    assert restorable_removed([e], lambda lp: False) == []


def test_restorable_drops_entry_when_both_paths_are_gone():
    e = entry(13631, 2)
    assert restorable_removed([e], lambda lp: False) == []


# ---------------------------------------------------------------- 先复原再生成

def test_regenerate_without_restore_is_refused():
    """引擎层也得拦：界面的拦截挡不住命令行，规则要两边一致。

    断言在解帧、查底盘、建 Tx 之前就抛，什么都没碰。
    """
    from mdstudio.ops.video import runfx_flip as F

    ctx = SimpleNamespace(journal=SimpleNamespace(for_card=lambda card, ops=None: [object()]),
                          dry_run=False)
    with pytest.raises(MDStudioError) as err:
        F._apply(ctx, 15299, "whatever", 60, 1080, 2.0, "full", 0.5, "", False, 90001, None)
    msg = str(err.value)
    assert "复原" in msg or "Restore it first" in msg
