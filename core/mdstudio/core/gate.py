"""两张"白名单" ScriptableObject. 卡 id 不在表里, 游戏连试都不试"""
import UnityPy

from mdstudio.core.crc import get_crc
from mdstudio.errors import AssetMissing
from mdstudio.i18n import tr

CUTIN_GATE = "Duel/ScriptableObject/CardIndividualData"
RUNFX_GATE = "Duel/ScriptableObject/CardRunEffectSetting"
# 融合召唤的通用演出表。**跟发动特效是两套系统**：
# 超融合、召唤魔术这类卡播的是融合召唤那一刻的前置演出，不是「发动特效」。
# 表里有 5 条，fusionType=0 那条是全融合共用的（mrk=0），其余是按卡指定的变体。
FUSION_FX_GATE = "Duel/ScriptableObject/FusionEffectSetting"

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

    def remove_entries(self, entries) -> list:
        """按签名删掉指定条目，返回被删掉的那些（要写进记录的 runfx_removed）。

        跟 remove_matching 的区别：这个要拿回被删的内容，复原时靠它把条目放回去。
        """
        sigs = {runfx_sig(e) for e in entries}
        gone = [e for e in self.entries if runfx_sig(e) in sigs]
        self.tt[self.FIELD] = [e for e in self.entries if runfx_sig(e) not in sigs]
        return gone

    def remove_card(self, mrk: int) -> list:
        gone = self.for_card(mrk)
        self.tt[self.FIELD] = [e for e in self.entries if e.get("mrk") != mrk]
        return gone

    def remove_card_entries(self, entries) -> int:
        """按「卡号 + player + 资源路径 + 时机」删条目，不看 seLabel。

        给 undo 兜底用：seLabel 在 runfx_sig 里，音效名改过而记录没同步时按签名
        一条都匹配不上。这几个字段能唯一定位条目，而且不会误删对手那条
        （同一张卡自己/对手两条的 player 不同，路径可能一模一样，比如 #05166）。
        """
        from mdstudio.core.crc import resolve_illust

        def key_of(e):
            return (e.get("mrk"), e.get("player"),
                    resolve_illust(e.get("effectPath") or ""),
                    resolve_illust(e.get("centerEffectPath") or ""),
                    e.get("runTiming"))

        want = {key_of(e) for e in entries}
        before = len(self.entries)
        self.tt[self.FIELD] = [e for e in self.entries if key_of(e) not in want]
        return before - len(self.tt[self.FIELD])


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


def fusion_fx_cards(game) -> dict:
    """{卡号: 游戏内备注} —— 只有融合召唤演出、没有别的可发动效果的卡。

    这些卡播的是**融合召唤那一刻的通用演出**，不是「发动特效」，所以它们改不了
    发动特效（改了也不会播）。目前是超融合两张（本体 + 超框）。

    注意两件事：

    1. 读的是游戏自己的表（FusionEffectSetting），没有硬编码「哪些卡有融合演出」：
       以后官方再加一张专用融合演出卡，这里自动跟上。

    2. **在表里不等于不能改**。召唤魔术（12852 / 3432）也在这张表里，但它还有第二段
       正常发动的效果，那一段是可以加特效的，所以不能锁。这种「有没有第二段效果」
       的信息不随包下发（本地没有 CARD_Text），只能人工记在下面这个集合里。
       漏锁的代价只是多一个无效操作，误锁会让用户改不了本该能改的卡，所以宁可漏。
    """
    other_effects = {12852, 3432}        # 召唤魔术（含异画）：另有可发动效果，不锁

    path = game.find_logical(FUSION_FX_GATE)
    if not path:
        return {}
    try:
        env = UnityPy.load(path.read_bytes())
    except Exception:
        return {}
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        try:
            tt = o.read_typetree()
        except Exception:
            continue
        infos = tt.get("fusionEffectInfos")
        if not isinstance(infos, list):
            continue
        out = {}
        for e in infos:
            mrk = e.get("mrk") or 0
            if mrk and int(mrk) not in other_effects:
                out[int(mrk)] = (e.get("comment") or "").strip()
        return out
    return {}
