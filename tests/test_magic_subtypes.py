"""魔法/陷阱的子类型：通常、场地、装备、永续、速攻、仪式、反击。

字段是卡属性 bits 50-52（代码里叫 stype），表在 tools/cards/subtype_semantics.json。
用 ygocdb 核对过：闪刀起动－交闪是 [魔法] 通常、增殖是速攻、神之宣告是反击陷阱。
"""
from mdstudio.core.cardindex import magic_subtype


def test_spell_subtypes():
    assert magic_subtype(13, 0) == "通常魔法"
    assert magic_subtype(13, 2) == "场地魔法"
    assert magic_subtype(13, 3) == "装备魔法"
    assert magic_subtype(13, 4) == "永续魔法"
    assert magic_subtype(13, 5) == "速攻魔法"
    assert magic_subtype(13, 6) == "仪式魔法"


def test_trap_subtypes():
    assert magic_subtype(14, 0) == "通常陷阱"
    assert magic_subtype(14, 1) == "反击陷阱"
    assert magic_subtype(14, 4) == "永续陷阱"


def test_monsters_are_not_subdivided():
    """怪兽不走这张表，返回空串让调用方用原来的全名。"""
    assert magic_subtype(0, 5) == ""
    assert magic_subtype(17, 4) == ""


def test_unknown_value_falls_back():
    """表外的值不编名字，宁可显示「魔法卡」。"""
    assert magic_subtype(13, 1) == ""
    assert magic_subtype(13, None) == ""
