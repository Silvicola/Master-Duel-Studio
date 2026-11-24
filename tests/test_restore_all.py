"""一键复原：范围筛选、合并留痕、音效回滚。

覆盖 2026-11-24 的改动：设置页把「重新同步」换成了「一键复原」。真正的撤销
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


# --------------------------------------------------------------- 范围筛选
def test_pending_counts_four_kinds_and_skips_wallpaper():
    recs = [
        _rec("cutin.video", 22804, modified=2),
        _rec("runfx.video", 22159, created=5),
        _rec("sound.custom", 22159, modified=1),      # 同卡音效，也算一处
        _rec("overframe.set", 10001, modified=1),
        _rec("wallpaper.set", 0, modified=1),          # 卡号 0：壁纸，不进
        _rec("undo", 10001, meta={"noop": True}),      # 只读留痕，不进
        _rec("pack.install", 0, meta={}),              # 汇总记录，不进
    ]
    out = TX.restore_pending(FakeCtx(recs))
    assert out["total"] == 3
    assert out["cards"] == [10001, 22159, 22804]
    assert out["by_kind"] == {"cutin": 1, "runfx": 1, "sound": 1, "art": 1}
    assert out["files"] == 2 + 5 + 1 + 1


def test_wallpaper_card_is_not_touched():
    """壁纸写的是 data.unity3d，复原要重写整个包，交给壁纸页单独还原。"""
    out = TX.restore_pending(FakeCtx([_rec("wallpaper.set", 0, modified=1)]))
    assert out == {"cards": [], "total": 0, "by_kind": {}, "files": 0}


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
        seen["trace"] = trace
        return 0

    monkeypatch.setattr(TX, "undo", fake_undo)
    return seen


def test_restore_all_passes_every_record_and_no_keep(captured):
    """一次性全撤，不留 keep：这样共享依赖不会被过户给别的记录。"""
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
