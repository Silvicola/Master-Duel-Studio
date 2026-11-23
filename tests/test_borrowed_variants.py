"""动画「官方动画★」的判定：谁在借别人的动画。

只有两条来源，都是官方给定的映射，不做推断：
覆盖名单 `tools/cards/cutin_borrow.json` 与超框表 `of_card_asset`。
同名表 `CARD_Same` 不参与，它只说明两张是同一张卡。
"""
from mdanim_engine.backend import _borrow_filter, _cutin_borrow_list


# --------------------------------------------------------------- 随包名单
def test_borrow_list_loads_shipped_cards():
    """浮世绘那三张异画借原版动画，名单里必须有。"""
    m = _cutin_borrow_list()
    assert m.get(23493) == 21371
    assert m.get(23494) == 18732
    assert m.get(23496) == 21054


def test_borrow_list_ignores_note_field():
    """文件里有一行 _note 说明文字，不能被当成卡号读进来。"""
    assert all(isinstance(k, int) and k > 0 for k in _cutin_borrow_list())


def test_borrow_list_excludes_plain_variant():
    """#22187 是原版、游戏里没有动画，不能被同名关系拉进来。"""
    assert 22187 not in _cutin_borrow_list()


# --------------------------------------------------------------- 三条门槛
def test_keeps_valid_borrow():
    out = _borrow_filter({23493: 21371}, lambda c: c == 21371, lambda c: True)
    assert out == {23493: 21371}


def test_card_with_own_animation_does_not_borrow():
    """自己表里就有，不算借用，也就不会带★。"""
    out = _borrow_filter({23487: 4041}, lambda c: True, lambda c: True)
    assert out == {}


def test_lender_must_be_in_gate():
    """出借方自己不在登记表里，借不过来（#23495 与 #21380 都无动画）。"""
    out = _borrow_filter({23495: 21380}, lambda c: False, lambda c: True)
    assert out == {}


def test_lender_must_have_resources():
    """出借方在表里但没资源，借了也播不出来。"""
    out = _borrow_filter({23179: 14409}, lambda c: True, lambda c: False)
    assert out == {}


def test_self_reference_is_dropped():
    out = _borrow_filter({3401: 3401}, lambda c: False, lambda c: True)
    assert out == {}


def test_empty_lender_is_dropped():
    """超框表里魔法陷阱那批底图号可能是 0。"""
    out = _borrow_filter({22770: 0}, lambda c: False, lambda c: True)
    assert out == {}


# --------------------------------------------------------------- 特效借用
def test_fx_borrow_when_own_resource_missing():
    """自己一个 Ef 文件都没有，同名的另一张有整套 -> 游戏播那一套。"""
    from mdanim_engine.backend import _fx_borrow_auto

    out = _fx_borrow_auto({8933, 21224}, {21224},
                           lambda c: [21224, 8933] if c == 8933 else [8933])
    assert out == {8933: 21224}


def test_fx_with_own_resource_does_not_borrow():
    """#21224 自己带了一整套 Ef21224，播自己的，不借。"""
    from mdanim_engine.backend import _fx_borrow_auto

    assert _fx_borrow_auto({21224}, {21224}, lambda c: [8933]) == {}


def test_fx_borrow_needs_entry_on_source():
    """对方有资源但压根没特效条目，借过去没东西可播。"""
    from mdanim_engine.backend import _fx_borrow_auto

    assert _fx_borrow_auto({12950}, {3891}, lambda c: [3891]) == {}


def test_fx_borrow_needs_resource_on_source():
    from mdanim_engine.backend import _fx_borrow_auto

    assert _fx_borrow_auto({12950}, set(), lambda c: [3891]) == {}


def test_fx_empty_path_but_own_resource_is_not_borrow():
    """#21235 闪电风暴 effectPath 是空的，但目录里有 Ef21235_near；
    空路径只表示「按自己卡号取资源」，不能当成借用。"""
    from mdanim_engine.backend import _fx_borrow_auto

    assert _fx_borrow_auto({21235}, {21235}, lambda c: [14876]) == {}
