"""哪张卡有 cut-in / 发动效果, 是游戏自带的还是我们加的

状态: game (官方) / yours (本工具加的) / wiped (本工具加过, 被更新清掉了, 跑 resync) / None
"""
from dataclasses import dataclass, field

from mdstudio.core.gate import CutinGate, RunFxGate
from mdstudio.ops import cutin as C
from mdstudio.ops import runfx as R


@dataclass
class CardInfo:
    id: int
    name: str = ""
    cutin: str = None
    runfx: str = None
    effects: list = field(default_factory=list)       # [(timing, seLabel, effectPath)]
    sibling_cutin: list = field(default_factory=list)  # 同名其他 id 里有 cut-in 的
    sibling_runfx: list = field(default_factory=list)

    @property
    def animated(self):
        return bool(self.cutin or self.runfx)


class Inventory:
    """一次性读两个 gate + journal, 之后查询都走内存"""

    def __init__(self, game, journal, db=None):
        self.db = db
        self.cutin = dict(C.list_cards(game, journal, gate=CutinGate(game)))
        fx_gate = RunFxGate(game)
        self.runfx = dict(R.list_cards(game, journal, gate=fx_gate))
        self.effects = {}
        for e in fx_gate.entries:
            self.effects.setdefault(e["mrk"], []).append(
                (R.TIMING.get(e.get("runTiming"), e.get("runTiming")), e.get("seLabel") or "", e.get("effectPath") or ""))

    def info(self, cid: int) -> CardInfo:
        cid = int(cid)
        ci = CardInfo(cid, self.db.name(cid) if self.db else "", self.cutin.get(cid), self.runfx.get(cid),
                      self.effects.get(cid, []))
        if self.db:
            # 坑: 一张卡有好几个 id (异画/不同印刷), gate 只认其中一个. 只看一个 id 会误判"没有"
            sib = [s for s in self.db.same_name(cid) if s != cid]
            ci.sibling_cutin = [s for s in sib if s in self.cutin]
            ci.sibling_runfx = [s for s in sib if s in self.runfx]
        return ci

    def cards_with(self, what: str, source=None) -> list:
        """what = cutin / runfx. source = game / yours / wiped / None (全部)"""
        table = self.cutin if what == "cutin" else self.runfx
        return [self.info(c) for c, st in sorted(table.items()) if source is None or st == source]

    def search(self, query: str, has=None, limit=50) -> list:
        """has: cutin / runfx / any / none"""
        if not self.db:
            return []
        out = []
        for cid, _ in self.db.search(query, limit=10_000):
            ci = self.info(cid)
            if has == "cutin" and not ci.cutin or has == "runfx" and not ci.runfx:
                continue
            if has == "any" and not ci.animated or has == "none" and ci.animated:
                continue
            out.append(ci)
            if len(out) >= limit:
                break
        return out
