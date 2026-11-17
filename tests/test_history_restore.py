"""复原留痕展开：复原后原改动照常显示（2026-11-17 用户实测定稿）。

纯函数式测试，用假的 ctx/journal/db，不起引擎、不碰游戏目录。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "core"))

from mdanim_engine.backend import Backend  # noqa: E402
from mdstudio.core.journal import Record  # noqa: E402


def _backend(records):
    b = Backend.__new__(Backend)
    ctx = SimpleNamespace(journal=SimpleNamespace(load=lambda: records))
    b._ctx = lambda: ctx
    b._names = lambda ctx: SimpleNamespace(name=lambda cid: "测试卡" if cid else "")
    return b


def _undo(card=14952, *, archived=None, ops=("pack.cutin",)):
    r = Record("undo", card)
    r.time = 1000.0
    r.meta = {"kind": "cutin", "files": 18, "ops": list(ops), "noop": True}
    if archived is not None:
        r.meta["archived"] = archived
    return r


def test_restore_keeps_original_change_in_history():
    """新格式：复原留痕带归档摘要，历史里应出现原改动 + 复原两条。"""
    r = _undo(archived={"op": "pack.cutin", "time": 900.0,
                        "meta": {"source": "hero.mp4", "files": 18,
                                 "pack": "某个整合包"}})
    out = _backend([r]).history_list()["records"]
    arch = [x for x in out if x.get("archived")]
    undo = [x for x in out if x["op"] == "undo"]
    assert len(arch) == 1 and len(undo) == 1
    assert arch[0]["op"] == "pack.cutin" and arch[0]["card"] == 14952
    assert arch[0]["from"] == ""          # 不把整合包名贴回去
    assert arch[0]["undoable"] is False   # 已复原，不可再撤
    assert arch[0]["detail"]["source"] == "hero.mp4"
    # 新的在前：复原行在上，原改动在下
    assert out.index(undo[0]) < out.index(arch[0])


def test_old_undo_still_replays_from_ops():
    """旧格式留痕没有归档摘要，仍能按 ops 回放一条，老数据不丢。"""
    out = _backend([_undo()]).history_list()["records"]
    arch = [x for x in out if x.get("archived")]
    assert len(arch) == 1
    assert arch[0]["op"] == "pack.cutin" and arch[0]["card"] == 14952
    assert arch[0]["from"] == "" and arch[0]["undoable"] is False
    undo = next(x for x in out if x["op"] == "undo")
    assert out.index(undo) < out.index(arch[0])
