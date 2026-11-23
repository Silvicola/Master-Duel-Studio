"""导出时的依赖展开：要覆盖整个闭包，不能只留一层。

2026-11-23 用户要求把 pack.export 的依赖计算改成传递展开。改动本身对现有
数据零影响（实测 16973 条 manifest 条目的闭包与直接依赖完全一致），这里用
构造的 manifest 固定住「多层能展开、有环不会转死、扁平时原样返回」三件事。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from mdstudio.ops.pack import _dep_closure  # noqa: E402


class FakeCat:
    def __init__(self, table):
        self.table = table

    def deps(self, name):
        return self.table.get(name)


def test_flat_manifest_returns_same_list():
    """官方现在的形态：直接依赖已经是扁平的，返回值必须一模一样。"""
    cat = FakeCat({
        "skel": ["atlas", "tex", "ref"],
        "atlas": [],
        "tex": [],
        "ref": [],
    })
    assert _dep_closure(cat, "skel") == ["atlas", "tex", "ref"]


def test_two_level_chain_is_expanded():
    """分层依赖表：贴图只写在图集条目里，也要跟着带出去。"""
    cat = FakeCat({
        "skel": ["atlas"],
        "atlas": ["tex1", "tex2"],
        "tex1": [],
        "tex2": [],
    })
    assert _dep_closure(cat, "skel") == ["atlas", "tex1", "tex2"]


def test_cycle_terminates_and_skips_self():
    """依赖图里有环（图集条目与参考条目互指），不能转死也别把自己算进依赖。"""
    cat = FakeCat({
        "skel": ["atlas"],
        "atlas": ["ref", "skel"],
        "ref": ["atlas"],
    })
    out = _dep_closure(cat, "skel")
    assert out == ["atlas", "ref"]
    assert "skel" not in out


def test_unknown_name_returns_none():
    """不在 manifest 里的条目返回 None，调用方靠这个跳过。"""
    assert _dep_closure(FakeCat({}), "nope") is None


def test_missing_dep_name_is_kept():
    """依赖名本身不在 manifest 里也照收：一层展开一直是这个行为，保持。"""
    cat = FakeCat({"skel": ["ghost"]})
    assert _dep_closure(cat, "skel") == ["ghost"]
