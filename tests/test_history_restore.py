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


def test_old_undo_skips_sound_when_replaying_ops():
    """旧留痕里混着音效 op 时，回放要挑非音效的那条：音效不算一条历史。"""
    r = _undo(ops=("runfx.video", "sound.custom"))
    out = _backend([r]).history_list()["records"]
    arch = [x for x in out if x.get("archived")]
    assert len(arch) == 1 and arch[0]["op"] == "runfx.video"


def test_archived_list_replays_every_op():
    """新格式：archived 是一张卡的列表，每种 op 都要展开成一行。"""
    archived = [
        {"op": "cutin.video", "time": 800.0, "meta": {"source": "hero.mp4", "files": 18}},
        {"op": "overframe.set", "time": 600.0, "meta": {"files": 1}},
    ]
    r = _undo(card=10001, archived=archived, ops=("cutin.video", "overframe.set"))
    r.meta["kind"] = "cutin"
    out = _backend([r]).history_list()["records"]
    arch = [x for x in out if x.get("archived")]
    assert [x["op"] for x in arch] == ["overframe.set", "cutin.video"]   # 新的在前
    assert len({x["id"] for x in out}) == len(out)                        # id 不撞
    undo = next(x for x in out if x["op"] == "undo")
    assert out.index(undo) < out.index(arch[0])


def test_old_single_archive_backfills_missing_ops():
    """老留痕只存了音效那条摘要：ops 里剩下的特效要补出来，音效不补。"""
    r = _undo(card=22159, ops=("runfx.video", "sound.custom"),
              archived={"op": "sound.custom", "time": 901.0,
                        "meta": {"label": "SE_EV_CUSTOM22159", "files": 1}})
    r.meta["kind"] = "runfx"
    out = _backend([r]).history_list()["records"]
    arch = [x for x in out if x.get("archived")]
    assert [x["op"] for x in arch] == ["runfx.video"]


def _rec(op, card=22159, t=900.0, files=1, meta=None):
    r = Record(op, card)
    r.time = t
    r.created = [f"{i:032x}" for i in range(files)]
    r.meta = meta or {}
    return r


def test_summaries_keep_each_op_but_drop_sound():
    """「视频特效 + 音效」一起复原：只留特效那条，文件数仍按两条算。"""
    from mdstudio.ops.tx import undo_summaries

    out = undo_summaries([
        _rec("runfx.video", t=100.0, files=1, meta={"donor": 3880}),
        _rec("sound.custom", t=200.0, files=1, meta={"label": "SE_EV_CUSTOM22159"}),
    ])
    c = out[22159]
    assert [a["op"] for a in c["archived"]] == ["runfx.video"]
    assert c["files"] == 2 and c["kind"] == "runfx"
    assert c["ops"] == ["runfx.video", "sound.custom"]


def test_summaries_keep_two_distinct_ops():
    """同卡两种改动（动画 + 超框）各留一条，按时间排。"""
    from mdstudio.ops.tx import undo_summaries

    out = undo_summaries([
        _rec("overframe.set", t=300.0),
        _rec("cutin.video", t=200.0, meta={"sha": {"a": 1, "b": 2}}),
    ])
    c = out[22159]
    assert [a["op"] for a in c["archived"]] == ["cutin.video", "overframe.set"]
    assert c["archived"][0]["meta"]["files"] == 2   # 有 sha 时按 sha 计数
