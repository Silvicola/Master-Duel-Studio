"""安装整合包时，超框变体卡要把「借底图动画」改成自映射。

生成侧一直有这一步（cutin.clone / cutin_flip.apply / spine.import_ 都调了
`C.own_anim`），安装侧以前漏了。症状很隐蔽：本机直接生成能播，导出整合包
再装回同一台机器就变回官方原版（2026-11-24 用户实测 #22804）。
#22559 这类本来就有自己动画的卡不受影响，所以一直没人发现。

这组测试固定住「装 cutin 要走 own_anim」，用假的 Tx 记录调用，不碰真盘。
"""
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from mdstudio.ops import pack as P  # noqa: E402


class _FakeRec:
    def __init__(self):
        self.meta = None


class FakeTx:
    made = []

    def __init__(self, ctx, op, card):
        self.op, self.card = op, card
        self.rec = _FakeRec()
        self.files, self.names, self.cutins = [], [], []
        self.committed = False
        FakeTx.made.append(self)

    def put_file(self, h, data):
        self.files.append(h)

    def set_catalog(self, name, deps):
        self.names.append(name)

    def add_cutin(self, card):
        self.cutins.append(card)

    def commit(self):
        self.committed = True


class FakeGame:
    def find(self, h):
        return None

    def find_logical(self, lp):
        return None


class FakeJournal:
    def for_card(self, card, ops):
        return []


@pytest.fixture()
def env(monkeypatch):
    """把安装流程里所有会碰盘的环节换成假的，只留顺序与调用关系是真的。"""
    calls = []
    monkeypatch.setattr(P, "Tx", FakeTx)
    monkeypatch.setattr(P.C, "own_anim", lambda tx, card: calls.append((tx.op, card)) or True)
    monkeypatch.setattr(P, "_fusion_cards", lambda ctx: {})
    monkeypatch.setattr(P, "_allowed", lambda *a: True)
    monkeypatch.setattr(P, "_write_plan", lambda catalog, need, exists: sorted(catalog))
    FakeTx.made.clear()
    ctx = types.SimpleNamespace(game=FakeGame(), journal=FakeJournal(), dry_run=True)
    return ctx, calls


def _meta(card, kind="cutin"):
    sub = {"catalog": {f"Duel/Timeline/Duel/MonsterCutIn/tcg/P{card}/HighEnd_HD/P{card}": []}}
    if kind == "runfx":
        sub = {"catalog": {f"Card/Effect/Ef{card}/Ef{card}": []}, "runfx": []}
    return {"cards": [{"mrk": card, kind: sub}]}


def _read(_entry):
    return b"data"


def test_cutin_install_registers_self_mapping(env):
    """装动画必须调 own_anim，否则变体卡进游戏还是播底图那套。"""
    ctx, calls = env
    done, overwritten = P._install(ctx, _meta(22804), _read, "batch-1")

    assert done == [("cutin", 22804)]
    assert calls == [("pack.cutin", 22804)]
    assert FakeTx.made[0].committed


def test_own_anim_runs_after_gate_entry(env):
    """先 add_cutin 再改映射：反过来会先写到旧表上。"""
    ctx, calls = env
    order = []
    orig = P.C.own_anim

    def spy(tx, card):
        order.append(("own_anim", list(tx.cutins)))
        return orig(tx, card)

    P.C.own_anim = spy
    try:
        P._install(ctx, _meta(22804), _read, "batch-2")
    finally:
        P.C.own_anim = orig
    assert order == [("own_anim", [22804])]


def test_runfx_install_does_not_touch_anim_gate(env):
    """装特效不该改召唤动画的映射表，那是另一族的事。"""
    ctx, calls = env
    P._install(ctx, _meta(8933, "runfx"), _read, "batch-3")
    assert calls == []


def test_selected_filter_skips_other_kind(env):
    """只勾了特效时，同一张卡的动画不该被装，也就不会碰映射表。"""
    ctx, calls = env
    meta = {"cards": [{
        "mrk": 22804,
        "cutin": {"catalog": {"Duel/Timeline/Duel/MonsterCutIn/tcg/P22804/HighEnd_HD/P22804": []}},
        "runfx": {"catalog": {"Card/Effect/Ef22804/Ef22804": []}, "runfx": []},
    }]}
    done, _ = P._install(ctx, meta, _read, "batch-4",
                         selected=[{"card": 22804, "kind": "runfx"}])
    assert done == [("runfx", 22804)]
    assert calls == []
