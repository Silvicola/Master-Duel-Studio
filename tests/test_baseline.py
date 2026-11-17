"""基线层的单元测试：不进游戏目录，只验证「同一张卡同一族只留一份」这个语义。

跑法（项目内任意能 import mdstudio 的 Python）：
    PYTHONPATH=core python -m pytest tests/test_baseline.py -q
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from mdstudio.core.baseline import BASELINE, Baselines, family_for  # noqa: E402
from mdstudio.core.journal import Journal, Record  # noqa: E402


@pytest.fixture()
def acct(tmp_path):
    """一个空账号目录 + 一个假的游戏文件"""
    j = Journal(tmp_path)
    game = tmp_path / "game"
    game.mkdir()
    f = game / "P123.bundle"
    f.write_bytes(b"ORIGINAL")
    return j, f


def test_family_mapping():
    """整合包装的卡图与自己做的超框必须归同一族，否则复原只回一半"""
    assert family_for("overframe.set") == "overframe"
    assert family_for("pack.art") == "overframe"
    assert family_for("cutin.video") == "cutin"
    assert family_for("pack.cutin") == "cutin"
    assert family_for("runfx.video") == "runfx"
    assert family_for("pack.runfx") == "runfx"


def test_first_write_saves_baseline(acct):
    j, f = acct
    rec = Record("cutin.video", 123)
    j.backup(rec, f, "P123.bundle")

    assert rec.modified["P123.bundle"] == BASELINE
    assert j.baselines.has(123, "cutin", "P123.bundle")
    assert j.baselines.path_of(123, "cutin", "P123.bundle").read_bytes() == b"ORIGINAL"


def test_second_write_does_not_copy(acct):
    """第二次生成不该再复制一份：这是本次改造的核心目的"""
    j, f = acct
    r1 = Record("cutin.video", 123)
    j.backup(r1, f, "P123.bundle")
    first = j.baselines.path_of(123, "cutin", "P123.bundle")

    # 模拟第一次生成已落盘
    f.write_bytes(b"FIRST-GENERATION")

    r2 = Record("cutin.video", 123)
    j.backup(r2, f, "P123.bundle")

    # 基线内容没变（还是原版），而且没有为第二条记录多建目录
    assert j.baselines.path_of(123, "cutin", "P123.bundle") == first
    assert first.read_bytes() == b"ORIGINAL"
    assert not (j.backups / r2.id).exists()


def test_restore_goes_to_original_not_previous(acct):
    """复原指向的永远是原版，与堆了几层无关"""
    j, f = acct
    j.backup(Record("cutin.video", 123), f, "P123.bundle")
    f.write_bytes(b"FIRST")
    r2 = Record("cutin.video", 123)
    j.backup(r2, f, "P123.bundle")
    f.write_bytes(b"SECOND")

    src = j.restore_source(r2, "P123.bundle")
    assert src.read_bytes() == b"ORIGINAL"


def test_absent_file_is_recorded(acct):
    """本来不存在的文件，复原时该删掉，不能当成「有内容」写回"""
    j, f = acct
    missing = f.parent / "P999.bundle"
    rec = Record("cutin.video", 999)
    j.backup(rec, missing, "P999.bundle")

    assert j.baselines.absent(999, "cutin", "P999.bundle")
    assert j.restore_source(rec, "P999.bundle") is None


def test_families_are_independent(acct):
    """动画与超框各自一份基线，撤动画不能带走超框那份"""
    j, f = acct
    j.backup(Record("cutin.video", 123), f, "P123.bundle")
    act = f.parent / "P123_illust.bundle"
    act.write_bytes(b"ART")
    j.backup(Record("overframe.set", 123), act, "P123_illust.bundle")

    j.baselines.drop(123, "cutin")
    assert not j.baselines.has(123, "cutin", "P123.bundle")
    assert j.baselines.has(123, "overframe", "P123_illust.bundle")


def test_old_record_still_works(tmp_path, acct):
    """老记录（modified 里存的是文件名）继续能还原，不能因为改了机制就失效"""
    j, f = acct
    rec = Record("cutin.clone", 123)
    name = "P123.bundle"
    rec.modified[name] = "P123.bundle"
    d = j.backups / rec.id
    d.mkdir(parents=True)
    (d / name).write_bytes(b"LEGACY")

    assert j.restore_source(rec, name).read_bytes() == b"LEGACY"


def test_forget_drops_dead_entries(acct):
    """旧 Ef 回收后，manifest 里不该留对不上任何记录的死条目"""
    j, f = acct
    r1 = Record("runfx.video", 123)
    j.backup(r1, f, "Ef1.bundle")
    missing = f.parent / "Ef2.bundle"
    r2 = Record("runfx.video", 123)
    j.backup(r2, missing, "Ef2.bundle")
    assert len(j.baselines.load(123, "runfx")) == 2

    j.baselines.forget(123, "runfx", ["Ef2.bundle"])
    m = j.baselines.load(123, "runfx")
    assert "Ef2.bundle" not in m
    assert "Ef1.bundle" in m


def test_expected_path_is_stable(tmp_path):
    j = Journal(tmp_path / "acct")
    p = j.baselines.expected_path(1, "cutin", "aa/bb")
    assert p == j.baselines.expected_path(1, "cutin", "aa/bb")
    assert not p.is_file()


def test_dry_run_writes_nothing(tmp_path):
    j = Journal(tmp_path / "acct")
    game = tmp_path / "game"
    game.mkdir()
    f = game / "P1.bundle"
    f.write_bytes(b"X")
    rec = Record("cutin.video", 1)
    j.backup(rec, f, "P1.bundle", dry_run=True)
    assert not j.baselines.has(1, "cutin", "P1.bundle")
    assert rec.modified["P1.bundle"] == BASELINE


def test_draft_keeps_only_two_slots(tmp_path):
    """超框素材草稿：固定两份（当前 + 上一版），不随改多少次增长"""
    j = Journal(tmp_path / "acct")
    art = tmp_path / "art.png"
    art.write_bytes(b"A")
    b = j.baselines

    for i in range(1, 5):
        art.write_bytes(f"V{i}".encode())
        assert b.save_draft(123, "overframe", {"art": str(art)}, {"scale": i})

    cur = b.draft(123, "overframe")
    assert json.loads((cur / "state.json").read_text(encoding="utf-8"))["scale"] == 4
    assert (cur / "art.png").read_bytes() == b"V4"
    prev = b._dir(123, "overframe") / "draft_prev"
    assert json.loads((prev / "state.json").read_text(encoding="utf-8"))["scale"] == 3
    assert (prev / "art.png").read_bytes() == b"V3"
    # 目录里只有「当前」和「上一版」两个槽，没有 V1 / V2 的残留
    slots = sorted(p.name for p in b._dir(123, "overframe").iterdir() if p.is_dir())
    assert slots == ["draft", "draft_prev"]


def test_draft_dry_run(tmp_path):
    j = Journal(tmp_path / "acct")
    art = tmp_path / "art.png"
    art.write_bytes(b"A")
    assert j.baselines.save_draft(1, "overframe", {"art": str(art)}, {}, dry_run=True)
    assert j.baselines.draft(1, "overframe") is None