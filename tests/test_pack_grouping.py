"""整合包结构：按卡分组、三小项、音效归入特效。

包结构在 2026-11-17 从扁平的 (mrk, kind) 条目表改成按卡号分组。
这组测试固定住新结构，防止导出/安装两边改回不一致。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from mdstudio.ops import pack as P  # noqa: E402


def test_sound_is_not_packaged():
    """整合包不涉及音效：三种音效 op 都不归到任何一类。"""
    for op in ("sound.custom", "sound.set", "sound.inject"):
        assert P._kind(op) is None
    assert P._kind("runfx.video") == "runfx"
    assert P._kind("cutin.video") == "cutin"
    assert P._kind("overframe.set") == "art"


def test_group_cards_puts_three_slots_under_one_card():
    entries = [
        {"mrk": 100, "kind": "cutin", "catalog": {"a": []}, "runfx": []},
        {"mrk": 100, "kind": "runfx", "catalog": {"b": []}, "runfx": [{"x": 1}]},
        {"mrk": 100, "kind": "art", "layers": {"scale": 1.0}},
        {"mrk": 200, "kind": "cutin", "catalog": {"c": []}, "runfx": []},
    ]
    cards = P._group_cards(entries)
    assert len(cards) == 2
    by_id = {c["mrk"]: c for c in cards}
    assert set(k for k in ("art", "cutin", "runfx") if k in by_id[100]) == {"art", "cutin", "runfx"}
    assert "runfx" not in by_id[200]


def test_ungroup_roundtrip():
    """展开后再分组，内容一致。安装和预览都靠这个展开函数读新结构。"""
    entries = [
        {"mrk": 100, "kind": "cutin", "catalog": {"a": []}, "runfx": []},
        {"mrk": 100, "kind": "runfx", "catalog": {"b": []}, "runfx": [{"x": 1}]},
        {"mrk": 100, "kind": "art", "layers": {"scale": 1.0}},
    ]
    cards = P._group_cards(entries)
    flat = P._ungroup_cards({"cards": cards})
    assert len(flat) == 3
    assert P._group_cards(flat) == cards


def test_ungroup_skips_bad_entries():
    meta = {"cards": [{"mrk": 100, "cutin": {"catalog": {"a": []}}}, {}, {"no": 1}]}
    out = P._ungroup_cards(meta)
    assert len(out) == 1
    assert out[0]["kind"] == "cutin"


def test_inspect_table_reads_grouped_cards():
    """CLI 的 pack inspect 要能读分组结构。

    2026-11-17 改分组时漏了 cli.py 这一处，直接用 m["cards"] 读 kind
    会 KeyError。这条测试盯住「读到的扁平行数与包里的条目数一致」。
    """
    from mdstudio import cli as C
    meta = {"name": "t", "tool": "mdstudio", "cards": [
        {"mrk": 100, "cutin": {"catalog": {"a": [], "b": []}}, "art": {"layers": {}}},
        {"mrk": 200, "runfx": {"catalog": {"c": []}, "runfx": []}},
    ]}
    rows = C.pack_inspect_rows(meta)
    assert sorted(rows) == [("art", 100, 0), ("cutin", 100, 2), ("runfx", 200, 1)]


def test_write_plan_covers_keys_even_if_on_disk():
    """安装写入计划：清单 key 盘上已有也要覆盖，纯依赖盘上已有则不动。

    2026-11-17 实测：#14952 官方自带召唤动画，复原后 7 个改写过的文件
    回到官方原版留在盘上，旧规则「盘上已有不覆盖」把它们跳过，装出来
    主体消失。这条测试固定住「key 必写、纯依赖让位游戏」的边界。
    """
    catalog = {"P100/a": [], "P100/b": ["P100/a", "shared/x"]}
    need = set(catalog) | {d for deps in catalog.values() for d in deps}
    plan = P._write_plan(catalog, need, exists=lambda lp: lp == "shared/x")
    assert plan == ["P100/a", "P100/b"]


def test_write_plan_writes_missing_dependencies():
    """纯依赖盘上没有时照写（新装的机器上共享文件可能缺）。"""
    catalog = {"P100/a": ["shared/x"]}
    plan = P._write_plan(catalog, {"P100/a", "shared/x"}, exists=lambda lp: False)
    assert plan == ["P100/a", "shared/x"]
