"""融合演出特殊卡：「融合」三个版本要锁，召唤魔术两版靠读表也会被锁。

游戏表里只有一条 mrk=0 的通用「融合」演出，跟卡号连不起来，所以那三张是人工资
料；表里绑了卡号的超融合由读表得到。召唤魔术虽然也绑了演出，但游戏判定它们有真正
的发动特效，可以改。
"""
from mdstudio.core.gate import fusion_fx_cards


class _NoGate:
    """读不到表时的最简情况：只剩人工那三张。"""

    def find_logical(self, _path):
        return None


def test_typical_fusion_locked_even_without_table():
    out = fusion_fx_cards(_NoGate())
    assert out.get(4837) == "融合"
    assert out.get(20386) == "融合"
    assert out.get(23490) == "融合"


def test_table_cards_absent_without_table():
    """召唤魔术两版靠读表拿到，表读不到时不会凭空出现（人工名单里没有它们）。"""
    out = fusion_fx_cards(_NoGate())
    assert 12852 not in out
    assert 3432 not in out
