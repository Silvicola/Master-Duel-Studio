"""端到端：生成 → 再生成 → 复原，验证磁盘与 journal 的真实行为。

用假的 Game / Ctx，不碰真实安装目录。Tx 的提交顺序、undo 的还原与删除
都是真代码（来自 mdstudio.ops.tx）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from mdstudio.core.context import Ctx  # noqa: E402
from mdstudio.core.journal import Journal  # noqa: E402
from mdstudio.ops.tx import Tx, undo  # noqa: E402


class FakeGame:
    """只有 rel / abs / path_for / find，够 Tx 用"""

    def __init__(self, root: Path):
        self.root = root
        self.dir = root.parent

    def rel(self, p) -> str:
        return Path(p).relative_to(self.root).as_posix()

    def abs(self, rel: str) -> Path:
        return self.root / rel

    def path_for(self, h: str) -> Path:
        return self.root / h[:2] / h

    def find(self, h: str):
        p = self.path_for(h)
        return p if p.is_file() else None

    def find_logical(self, lp: str):
        return None

    def accounts(self):
        return ["acct"]


class FakeCatalog:
    def __init__(self, path: Path):
        self.items = {}
        self.path = path
        self.path.write_bytes(b"catalog")

    def set(self, name, deps):
        prev = self.items.get(name)
        self.items[name] = list(deps)
        return prev

    def remove(self, name):
        self.items.pop(name, None)

    def deps(self, name):
        return self.items.get(name)

    def save(self, ctx):
        pass

    def missing(self, name):
        return []


class FakeGate:
    def __init__(self, path: Path):
        self.mrks = set()
        self.entries = []
        self.path = path
        self.path.write_bytes(b"gate")

    def add(self, m):
        if m in self.mrks:
            return False
        self.mrks.add(m)
        return True

    def remove(self, m):
        self.mrks.discard(m)

    def append(self, items):
        self.entries.extend(items)

    def remove_matching(self, items):
        pass

    def for_card(self, card):
        return []

    def remove_card(self, card):
        return []

    def save(self, ctx):
        pass


@pytest.fixture()
def env(tmp_path):
    from mdstudio.core.catalog import encode, get_crc, MANIFEST

    game = FakeGame(tmp_path / "game")
    game.root.mkdir(parents=True)
    j = Journal(tmp_path / "acct")
    ctx = Ctx(game, dry_run=False, journal=j, check_running=False)
    # undo() 内部会自己建一个真 Catalog，需要盘上有合法的 manifest_tcg
    mh = get_crc(MANIFEST)
    mp = game.path_for(mh)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_bytes(encode({"informations": []}))
    # 造一个「官方已有」的文件，模拟官方动画卡
    h = "a" * 8
    p = game.path_for(h)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"OFFICIAL")
    return ctx, game, j, h, p


def _tx(ctx, card=11111):
    t = Tx(ctx, "cutin.video", card)
    base = ctx.journal.folder / "fakes"
    base.mkdir(parents=True, exist_ok=True)
    t._catalog = FakeCatalog(base / "catalog.json")
    t._cutin = FakeGate(base / "cutin_gate.bin")
    t._runfx = FakeGate(base / "runfx_gate.bin")
    t.game.find_logical = lambda lp: None
    return t


def _generate(ctx, h, payload):
    """只写文件、不登记 gate：集成测试聚焦备份与还原，gate 由单测单独覆盖"""
    t = _tx(ctx)
    t.put_file(h, payload)
    return t.commit()


def test_repeat_generation_keeps_one_baseline(env):
    ctx, game, j, h, p = env
    _generate(ctx, h, b"V1")
    _generate(ctx, h, b"V2")
    _generate(ctx, h, b"V3")

    assert p.read_bytes() == b"V3"
    assert len(j.load()) == 3
    # 三次生成只有一份基线，文件数不随次数增长
    files = list((j.baselines.folder / "11111" / "cutin" / "files").glob("*"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"OFFICIAL"


def test_restore_returns_to_official(env):
    ctx, game, j, h, p = env
    _generate(ctx, h, b"V1")
    _generate(ctx, h, b"V2")
    _generate(ctx, h, b"V3")

    recs = j.load()
    undo(ctx, recs, keep_records=[])

    assert p.read_bytes() == b"OFFICIAL"      # 复原始终回官方
    # 三条生成记录成对消失，只留下一条只读复原留痕给历史页（2026-11-17 定稿：
    # 复原也是一条）。noop=true 不可再撤，文件数为三条记录的 modified+created 之和。
    left = j.load()
    assert len(left) == 1 and left[0].op == "undo" and left[0].card == 11111
    assert left[0].meta["noop"] is True and left[0].meta["kind"] == "cutin"
    assert left[0].meta["files"] == 3
    assert not (j.baselines.folder / "11111" / "cutin").exists()   # 复原即删


def test_created_file_is_deleted_on_restore(env):
    """无动画卡：整套 P<卡> 是新建的，复原要删掉，不能写回空内容"""
    ctx, game, j, h, p = env
    p.unlink()                                # 模拟盘上原本没有
    t = _tx(ctx)
    t.put_file(h, b"NEW")
    t.commit()
    assert p.read_bytes() == b"NEW"

    undo(ctx, j.load(), keep_records=[])
    assert not p.exists()                     # 回到「无动画」


def test_other_family_baseline_survives(env):
    """只复原动画，同卡的超框基线不能跟着消失"""
    ctx, game, j, h, p = env
    _generate(ctx, h, b"V1")

    h2 = "b" * 8
    p2 = game.path_for(h2)
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_bytes(b"OFFICIAL-ART")
    t = Tx(ctx, "overframe.set", 11111)
    base = ctx.journal.folder / "fakes"
    t._catalog = FakeCatalog(base / "catalog.json")
    t._cutin = FakeGate(base / "cutin_gate.bin")
    t._runfx = FakeGate(base / "runfx_gate.bin")
    t.put_file(h2, b"ART-V1")
    t.commit()

    anim = [r for r in j.load() if r.op == "cutin.video"]
    others = [r for r in j.load() if r not in anim]
    undo(ctx, anim, keep_records=others)

    art_rec = next(r for r in j.load() if r.op == "overframe.set")
    art_rel = next(iter(art_rec.modified))
    assert j.baselines.has(11111, "overframe", art_rel)
    assert not (j.baselines.folder / "11111" / "cutin").exists()
