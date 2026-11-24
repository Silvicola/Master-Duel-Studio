"""快捷复原：范围筛选、合并留痕、音效回滚。

覆盖 2026-11-24 的改动：设置页把「重新同步」换成了「快捷复原」。真正的撤销
仍走 `tx.undo`（已有 test_undo_safety / test_overwrite_flow 覆盖），这里只钉住
新增的那层：撤哪些记录、怎么汇总、留几条痕。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from mdstudio.core.journal import Record  # noqa: E402
from mdstudio.errors import MDStudioError  # noqa: E402
from mdstudio.ops import tx as TX  # noqa: E402


class FakeJournal:
    def __init__(self, recs):
        self.recs = recs
        self.committed = []

        class _Base:
            def drop(self, card, fam):
                return None

        self.baselines = _Base()

    def load(self):
        return list(self.recs)

    def commit(self, rec):
        self.committed.append(rec)

    def update(self, changed):
        return None

    def drop(self, ids):
        return None

    def bump_activity(self):
        return None

    def restore_source(self, rec, rel):
        return None


class FakeCtx:
    def __init__(self, recs, dry_run=False):
        self.journal = FakeJournal(recs)
        self.dry_run = dry_run
        self.guarded = 0

        class _Game:
            def abs(self, rel):
                return Path("C:/__mdstudio_never_exists__") / "x"

        self.game = _Game()

    def guard(self):
        self.guarded += 1

    def delete(self, path):
        return None

    def write(self, path, data):
        return None


def _rec(op, card, modified=0, created=0, meta=None):
    r = Record(op, card)
    r.modified = {f"a{i}/h{i}": "bak" for i in range(modified)}
    r.created = [f"b{i}/h{i}" for i in range(created)]
    r.meta = dict(meta or {})
    return r


# --------------------------------------------------------------- 勾选列表
def test_pending_lists_one_row_per_card_and_kind():
    """界面上每行是「一张卡的一种改动」；同卡同类的多条记录合成一行。"""
    recs = [
        _rec("cutin.video", 22804, modified=2),
        _rec("cutin.reskin", 22804, modified=3),      # 同卡同类的后续改动
        _rec("runfx.video", 22159, created=5),
        _rec("sound.custom", 22159, modified=1),      # 音效并进特效那一行
        _rec("overframe.set", 10001, modified=1),
        _rec("wallpaper.set", 0, modified=1),          # 卡号 0：壁纸，不进
        _rec("undo", 10001, meta={"noop": True}),      # 只读留痕，不进
        _rec("pack.install", 0, meta={}),              # 汇总记录，不进
    ]
    out = TX.restore_pending(FakeCtx(recs))
    rows = {(r["card"], r["kind"]): r["files"] for r in out["items"]}
    assert rows == {(10001, "art"): 1, (22159, "runfx"): 6, (22804, "cutin"): 5}
    assert out["total"] == 3 and out["files"] == 12
    assert out["by_kind"] == {"art": 1, "cutin": 1, "runfx": 1}


def test_wallpaper_card_is_not_listed():
    """壁纸写的是 data.unity3d，复原要重写整个包，交给壁纸页单独还原。"""
    out = TX.restore_pending(FakeCtx([_rec("wallpaper.set", 0, modified=1)]))
    assert out["items"] == [] and out["total"] == 0 and out["files"] == 0


def test_nothing_to_restore_raises():
    with pytest.raises(MDStudioError):
        TX.restore_all(FakeCtx([]))


# --------------------------------------------------------------- 执行
@pytest.fixture()
def captured(monkeypatch):
    """把 undo 换成记录参数的桩，只看 restore_all 传了什么进去。"""
    seen = {}

    def fake_undo(ctx, records, keep_records=(), trace=None):
        seen["records"] = records
        seen["keep"] = list(keep_records)
        return 0

    monkeypatch.setattr(TX, "undo", fake_undo)
    return seen


def test_restore_all_passes_every_record_and_no_keep(captured):
    """全部行都勾上，等于一次全撤，keep 为空。"""
    recs = [_rec("cutin.video", 100, modified=1), _rec("overframe.set", 200, modified=1)]
    TX.restore_all(FakeCtx(recs))
    assert len(captured["records"]) == 2
    assert captured["keep"] == []


def test_restore_all_rolls_back_sound_labels(captured, monkeypatch):
    """先把官方音效改成 X 再生成特效的卡，复原后要回到最原始那个。"""
    from mdstudio.ops import sound as S

    hit = []
    monkeypatch.setattr(S, "apply_origins", lambda ctx, card: hit.append(card))
    TX.restore_all(FakeCtx([
        _rec("runfx.video", 22159, created=1),
        _rec("sound.set", 22159, modified=1),
        _rec("overframe.set", 10001, modified=1),
    ]))
    assert hit == [22159]          # 只有带音效的卡会回滚，卡图的卡跳过


def test_dry_run_does_not_roll_back_sound(captured, monkeypatch):
    """预演不该删音效的 origins 状态（apply_origins 在 dry_run 下会顺手清掉它）。"""
    from mdstudio.ops import sound as S

    hit = []
    monkeypatch.setattr(S, "apply_origins", lambda ctx, card: hit.append(card))
    TX.restore_all(FakeCtx([_rec("runfx.video", 22159, created=1)], dry_run=True))
    assert hit == []


# --------------------------------------------------------------- 只撤勾选的行
def test_picks_only_selects_that_row(captured):
    """只勾超框那一行：只有超框记录进 undo，动画与特效原样留着。"""
    art = _rec("overframe.set", 100, modified=1)
    cutin = _rec("cutin.video", 200, created=2)
    fx = _rec("runfx.video", 300, created=3)
    TX.restore_all(FakeCtx([art, cutin, fx]), picks=[{"card": 100, "kind": "art"}])
    assert captured["records"] == [art]


def test_picks_keep_the_rest(captured):
    """没勾的必须当 keep 传进去，否则共享依赖会被当无主文件删掉。"""
    art = _rec("overframe.set", 100, modified=1)
    cutin = _rec("cutin.video", 200, created=2)
    TX.restore_all(FakeCtx([art, cutin]), picks=[{"card": 100, "kind": "art"}])
    assert captured["keep"] == [cutin]


def test_picks_take_sound_with_the_effects_row(captured, monkeypatch):
    """勾了特效那一行，同一张卡换过的音效一起撤。"""
    from mdstudio.ops import sound as S

    monkeypatch.setattr(S, "apply_origins", lambda ctx, card: 0)
    fx = _rec("runfx.video", 300, created=1)
    snd = _rec("sound.set", 300, modified=1)
    other = _rec("runfx.video", 400, created=1)
    TX.restore_all(FakeCtx([fx, snd, other]), picks=[{"card": 300, "kind": "runfx"}])
    assert [r.op for r in captured["records"]] == ["runfx.video", "sound.set"]


def test_picks_for_unknown_row_raises(captured):
    """勾了一行但这台机器上没有对应改动：报「没有可撤的」，不要空跑。"""
    with pytest.raises(MDStudioError):
        TX.restore_all(FakeCtx([_rec("cutin.video", 200, created=1)]),
                       picks=[{"card": 999, "kind": "art"}])


def test_garbage_picks_restore_nothing(captured):
    """勾选数据一条都认不出来时不能退化成「全撤」。"""
    cutin = _rec("cutin.video", 200, created=1)
    with pytest.raises(MDStudioError):
        TX.restore_all(FakeCtx([cutin]), picks=[{"card": "x"}, None, {"kind": "art"}])


# ------------------------------------------------- 共用表：单卡复原也不能回填
class _Gate:
    entries: list = []

    def remove(self, m):
        return False

    def remove_matching(self, items):
        return []

    def remove_card_entries(self, items):
        return []

    def append(self, items):
        return None


class _Cat:
    def set(self, name, deps):
        return None

    def remove(self, name):
        return None


class _TxStub:
    """只够 undo 空跑：撤的是单张卡、记录里没有文件改动。"""

    def __init__(self, ctx, op, card, record=True, reverse=False):
        self.cutin_gate = _Gate()
        self.runfx_gate = _Gate()
        self.catalog = _Cat()
        self._writes = []

        class _Rec:
            meta = {}

        self.rec = _Rec()

    def commit(self):
        return None


def _undo_ctx(monkeypatch):
    monkeypatch.setattr(TX, "Tx", _TxStub)
    monkeypatch.setattr(TX, "drop_preview_cache", lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(TX, "_restore_overframe_gate", lambda ctx, cards: seen.append(list(cards)))
    return seen


def test_single_restore_does_not_revert_gate_when_art_remains(monkeypatch):
    """动画页单独复原一张卡，但它还挂着超框：of_card_asset 不能回填。
    两个族共用这张表，回填会把超框的登记改回「借底图」。"""
    seen = _undo_ctx(monkeypatch)
    anim = _rec("cutin.video", 22804, modified=1)
    art = _rec("overframe.set", 22804, modified=1)
    TX.undo(FakeCtx([anim, art]), [anim], keep_records=[art])
    assert seen == []


def test_single_restore_reverts_gate_when_card_is_clean(monkeypatch):
    """这张卡撤完就没别的改动了，按内置表回填才对。"""
    seen = _undo_ctx(monkeypatch)
    anim = _rec("cutin.video", 22804, modified=1)
    TX.undo(FakeCtx([anim]), [anim], keep_records=[])
    assert seen == [[22804]]


def test_restore_art_keeps_gate_for_card_with_animation(monkeypatch):
    """超框页单独复原，同一张卡的动画还在：同样不回填。"""
    from mdstudio.ops import overframe as OF

    seen = _undo_ctx(monkeypatch)
    monkeypatch.setattr(OF, "clear_edit", lambda *a, **k: None)
    anim = _rec("cutin.video", 22804, modified=1)
    art = _rec("overframe.set", 22804, modified=1)
    TX.undo(FakeCtx([anim, art]), [art], keep_records=[anim])
    assert seen == []
