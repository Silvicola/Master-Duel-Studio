"""来源卡表的回归测试。

这六张是用户逐档确认的（2026-11-19），改动它们会直接影响所有无动画卡的克隆底盘，
所以用测试把编号钉死。同时守住「来源卡不允许改动画」这条规则。
"""

from mdstudio.ops import cutin as C

# 用户确认的卡号。改这里之前先确认对应卡仍然满足：官方召唤动画、HD 2 页 2 槽、
# 不在强力召唤表、Timeline 可定位、prefab 里读得到 Spine 物体缩放、camera2DSize = 5.0。
EXPECT = {
    "normal": 3880,
    "effect": 3880,
    "fusion": 3435,
    "ritual": 15640,
    "synchro": 8121,
    "xyz": 12328,
    "link": 13088,
}


def test_donors_table_is_exactly_the_confirmed_cards():
    assert C.DONORS == EXPECT


def test_no_dead_keys_left():
    """上游继承的三个死键（灵摆、魔法、陷阱）已经清掉，不该再回来。"""
    for key in ("pendulum", "spell", "trap"):
        assert key not in C.DONORS


def test_donor_cards_is_deduped():
    assert sorted(C.donor_cards()) == sorted({3435, 3880, 8121, 12328, 13088, 15640})


def test_donor_for_maps_each_type():
    cases = {
        "融合怪兽": 3435,
        "仪式怪兽": 15640,
        "同调怪兽": 8121,
        "超量怪兽": 12328,
        "连接怪兽": 13088,
        "通常怪兽": 3880,
        "效果怪兽": 3880,
        "特殊召唤／效果": 3880,
        # 灵摆卡带第二个属性，去掉灵摆之后要落到对应档
        "超量／灵摆／效果": 12328,
        "融合／灵摆／效果": 3435,
    }
    for subtype, want in cases.items():
        assert C.donor_for(subtype) == want, f"{subtype} 应落到 {want}"


def test_is_donor_card_covers_all_six():
    for card in C.donor_cards():
        assert C.is_donor_card(card), f"P{card} 应被识别为来源卡"


def test_is_donor_card_rejects_normal_cards():
    for card in (21359, 21443, 22116, 4007, 22804, 14952):
        assert not C.is_donor_card(card), f"P{card} 不是来源卡"
