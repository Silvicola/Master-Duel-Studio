"""两张"白名单" ScriptableObject. 卡 id 不在表里, 游戏连试都不试"""
import UnityPy

from mdstudio.core.crc import get_crc
from mdstudio.errors import AssetMissing
from mdstudio.i18n import tr

CUTIN_GATE = "Duel/ScriptableObject/CardIndividualData"
RUNFX_GATE = "Duel/ScriptableObject/CardRunEffectSetting"

# 卸载时靠这几个字段认出 "是我们加的那条"
RUNFX_SIG = ("mrk", "effectNo", "player", "runTiming", "effectPath", "seLabel")


def runfx_sig(e: dict) -> tuple:
    return tuple(e.get(k) for k in RUNFX_SIG)


class _ScriptableFile:
    LOGICAL = ""
    FIELD = ""

    def __init__(self, game):
        self.game = game
        self.path = game.find_logical(self.LOGICAL)
        if not self.path:
            raise AssetMissing(tr("%s not found") % self.LOGICAL)
        # 注意: 用 bytes 读. 传路径的话 UnityPy 一直占着文件, Windows 上 os.replace 直接拒绝
        self.env = UnityPy.load(self.path.read_bytes())
        for o in self.env.objects:
            if o.type.name != "MonoBehaviour":
                continue
            tt = o.read_typetree()
            if self.FIELD in tt:
                self._obj, self.tt = o, tt
                break
        else:
            raise AssetMissing(tr("%s not found in %s") % (self.FIELD, self.LOGICAL))

    @property
    def rel(self):
        return self.game.rel(self.path)

    def save(self, ctx):
        self._obj.save_typetree(self.tt)
        ctx.write(self.path, self.env.file.save(packer="lz4"))


class CutinGate(_ScriptableFile):
    LOGICAL = CUTIN_GATE
    FIELD = "monsterCutinTable"

    def mrks(self) -> set:
        return {e["mrk"] for e in self.tt[self.FIELD]}

    def add(self, mrk: int) -> bool:
        if mrk in self.mrks():
            return False
        self.tt[self.FIELD].append({"mrk": int(mrk)})
        return True

    def remove(self, mrk: int) -> bool:
        before = len(self.tt[self.FIELD])
        self.tt[self.FIELD] = [e for e in self.tt[self.FIELD] if e["mrk"] != mrk]
        return len(self.tt[self.FIELD]) != before


class RunFxGate(_ScriptableFile):
    LOGICAL = RUNFX_GATE
    FIELD = "infoList"

    @property
    def entries(self) -> list:
        return self.tt[self.FIELD]

    def for_card(self, mrk: int) -> list:
        return [e for e in self.entries if e.get("mrk") == mrk]

    def template(self) -> dict:
        # 只拿字段结构用, 值必须全部重填 (见 ops.runfx.clean_entry)
        if not self.entries:
            raise AssetMissing(tr("the game's effect list is empty, can't build an entry (game format changed?)"))
        return self.entries[0]

    def append(self, entries):
        # 坑: journal / 包里的 entry 是旧版本存的. 游戏加了字段 -> save_typetree KeyError,
        # 而且是在 bundle/manifest 都写完之后才炸. 按当前模板对齐: 缺的补"零值", 多的扔掉
        tmpl = self.entries[0] if self.entries else None
        self.entries.extend(_conform(e, tmpl) for e in entries)

    def remove_matching(self, entries) -> int:
        sigs = {runfx_sig(e) for e in entries}
        before = len(self.entries)
        self.tt[self.FIELD] = [e for e in self.entries if runfx_sig(e) not in sigs]
        return before - len(self.tt[self.FIELD])

    def remove_card(self, mrk: int) -> list:
        gone = self.for_card(mrk)
        self.tt[self.FIELD] = [e for e in self.entries if e.get("mrk") != mrk]
        return gone


def _zero(v):
    # 不能拿模板的值当默认: 模板 infoList[0] 就是那条会卡死对局的 Sky Striker
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return 0
    if isinstance(v, float):
        return 0.0
    if isinstance(v, str):
        return ""
    if isinstance(v, list):
        return []
    if isinstance(v, dict):
        return {k: _zero(x) for k, x in v.items()}
    return v


def _conform(e: dict, tmpl) -> dict:
    if not tmpl:
        return e
    return {k: (e[k] if k in e else _zero(v)) for k, v in tmpl.items()}
