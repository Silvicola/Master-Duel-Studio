"""复原安全性：音效名同步、死指针不许补回、残留条目不许删文件。

2026-11-14 实测事故：给特效换自定义音效后点「复原」，gate 里的条目因为 seLabel
变了、按签名一条都匹配不上，结果 gate 没清掉而 bundle 被删，游戏进对局报
「文件读取错误 EfNNNNN」，卡片还显示成官方特效。这几条用例把该行为钉住。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "core"))
sys.path.insert(0, str(_ROOT / "engine"))


class _FakeJournal:
    def __init__(self, recs):
        self.recs = list(recs)
        self.updates = 0

    def for_card(self, card, ops=None):
        return [r for r in self.recs
                if r.card == card and (ops is None or r.op in ops)]

    def update(self, changed):
        self.updates += 1
        changed = list(changed)
        by_id = {r.id: r for r in changed}
        self.recs = [by_id.get(r.id, r) for r in self.recs]


def _rec(card=22159, op="runfx.video", rid="r1", added=()):
    return SimpleNamespace(id=rid, card=card, op=op, runfx_added=list(added),
                           runfx_removed=[], created=[], modified={}, meta={}, catalog={})


def _entry(eid=92230, label="", card=22159):
    return {"mrk": card, "effectNo": eid, "player": 0, "runTiming": 0,
            "effectPath": f"Duel/Timeline/Card/<_CARD_ILLUST_>/Ef{eid}/Ef{eid}",
            "seLabel": label}


def test_switching_sound_copies_the_label_into_the_record():
    """换音效时必须把 seLabel 抄回记录，否则复原时按签名找不到自己的条目"""
    from mdstudio.ops.sound import sync_se_label

    rec = _rec(added=[_entry(label="")])
    journal = _FakeJournal([rec])
    ctx = SimpleNamespace(journal=journal, dry_run=False)

    sync_se_label(ctx, 22159, [_entry(label="SE_EV_CUSTOM22159")])

    assert rec.runfx_added[0]["seLabel"] == "SE_EV_CUSTOM22159"
    assert journal.updates == 1


def test_restore_sound_maps_each_entry_its_own_label():
    """还原原音是逐条不同值，配对按「除 seLabel 外的字段」，不能一把刷成同一个"""
    from mdstudio.ops.sound import sync_se_label

    a, b = _entry(92230, ""), _entry(93331, "")
    rec = _rec(added=[dict(a), dict(b)])
    ctx = SimpleNamespace(journal=_FakeJournal([rec]), dry_run=False)

    sync_se_label(ctx, 22159, [_entry(92230, "SE_EV_01"), _entry(93331, "SE_EV_02")])

    assert rec.runfx_added[0]["seLabel"] == "SE_EV_01"
    assert rec.runfx_added[1]["seLabel"] == "SE_EV_02"


def test_nothing_written_when_labels_already_match():
    """已经一致就别去写 journal，省得每次开卡都产生一次写盘"""
    from mdstudio.ops.sound import sync_se_label

    rec = _rec(added=[_entry(label="SE_EV_01")])
    journal = _FakeJournal([rec])
    ctx = SimpleNamespace(journal=journal, dry_run=False)

    sync_se_label(ctx, 22159, [_entry(label="SE_EV_01")])

    assert journal.updates == 0


def test_sync_is_a_noop_in_dry_run():
    from mdstudio.ops.sound import sync_se_label

    rec = _rec(added=[_entry(label="")])
    journal = _FakeJournal([rec])
    ctx = SimpleNamespace(journal=journal, dry_run=True)

    sync_se_label(ctx, 22159, [_entry(label="SE_EV_CUSTOM22159")])

    assert rec.runfx_added[0]["seLabel"] == ""
    assert journal.updates == 0


class _FakeGate:
    """只带 gate 的条目表，不走真 bundle"""

    FIELD = "list"

    def __init__(self, entries):
        self.tt = {self.FIELD: entries}

    @property
    def entries(self):
        return self.tt[self.FIELD]


def _bind_gate_methods():
    from mdstudio.core.gate import RunFxGate

    _FakeGate.remove_card_entries = RunFxGate.remove_card_entries
    _FakeGate.remove_entries = RunFxGate.remove_entries
    return _FakeGate


def test_gate_can_drop_entries_by_identity_when_signatures_differ():
    """签名对不上时的兜底：按「卡号 + player + 路径 + 时机」删。

    同一张卡自己/对手两条可能指向同一个 Ef（#05166），所以键里必须带 player，
    否则会把对手那条一起删掉。
    """
    gate = _bind_gate_methods()([
        _entry(90001, "SE_EV_KEEP"),
        _entry(92230, "SE_EV_CUSTOM22159"),        # 被换过音效的自己那条
        dict(_entry(92230, ""), player=1),         # 对手那条，路径相同
        _entry(92230, "", card=12345),
    ])

    # 记录里记的音效是空的，gate 上那条被改成过 SE_EV_CUSTOM22159：
    # 签名对不上，但身份（player + 路径 + 时机）一致，兜底要认得出来
    victim = dict(_entry(92230, ""), player=0)
    assert gate.remove_card_entries([victim]) == 1
    assert [e.get("seLabel") for e in gate.entries] == ["SE_EV_KEEP", "", ""]
    assert gate.entries[1].get("player") == 1      # 对手那条还在
    assert gate.entries[2].get("mrk") == 12345     # 别的卡不受影响


def test_gate_remove_entries_returns_what_it_dropped():
    """要被删的条目得原样拿回来，复原时靠它放回去"""
    gate = _bind_gate_methods()([_entry(90001, ""), _entry(90002, "")])
    gone = gate.remove_entries([dict(_entry(90001, ""))])
    assert [e["effectNo"] for e in gone] == [90001]
    assert [e["effectNo"] for e in gate.entries] == [90002]



# --------------------------------------------------- 自己/对手与「原音」的身份

def test_self_entries_skips_the_opponent():
    """player=1 是对手（资源名带 Op 那批），换特效换音效都不能碰它"""
    from mdstudio.ops.sound import self_entries

    rows = [dict(_entry(90001, "SELF"), player=0),
            dict(_entry(90002, "OPP"), player=1),
            dict(_entry(90003, "BOTH"), player=2)]
    assert [e["seLabel"] for e in self_entries(rows)] == ["SELF", "BOTH"]


def test_origins_are_read_by_identity_not_position(monkeypatch):
    """同一张卡两条路径可能一样（#05166），靠 player 区分，不能按顺序对"""
    from mdstudio.ops import sound as S

    rows = [dict(_entry(92230, "SE_SELF"), player=0),
            dict(_entry(92230, "SE_OPP"), player=1)]
    k0, k1 = S.entry_key(rows[0]), S.entry_key(rows[1])
    stored = {"22159": [{"p": 0, "ep": k0[1], "cp": "", "t": 0, "se": "SE_SELF_OLD"},
                        {"p": 1, "ep": k1[1], "cp": "", "t": 0, "se": "SE_OPP_OLD"}]}
    monkeypatch.setattr(S, "_load_origins", lambda: stored)

    m = S.origins_map(22159, rows)
    assert m[k0] == "SE_SELF_OLD"
    assert m[k1] == "SE_OPP_OLD"


def test_origins_legacy_list_still_reads(monkeypatch):
    """老版本存的是纯字符串列表，不能因为升级就读不出来"""
    from mdstudio.ops import sound as S

    rows = [dict(_entry(90001, "A"), player=0), dict(_entry(90002, "B"), player=0)]
    monkeypatch.setattr(S, "_load_origins", lambda: {"22159": ["A_OLD", "B_OLD"]})
    assert list(S.origins_map(22159, rows).values()) == ["A_OLD", "B_OLD"]
