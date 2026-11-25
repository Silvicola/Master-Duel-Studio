"""给 UI 用的数据整形.

复用 mdstudio 包, 这里只做两件事: 缓存重的查询, 把字段裁成 UI 要的形状.
"""
import json
import os
import threading
from pathlib import Path

from mdstudio import runtime, settings

# 卡片状态原值是 game / yours / wiped / shared / borrowed / None, 界面上按标签分.
# official      自己带的官方
# shared        借用同名卡的（动画页写「官方动画★」，特效页写「官方特效★」）
# borrowed      本工具替这张卡借来的官方那套（特效页），标签同样是★，但复原要能撤
# self          自改
# selfOfficial  只剩卡图（超框）在用：官方本来就有，被我们的版本覆盖了
# 动画页 2026-11-15 定稿：自改一律不带★，跟特效页对齐。来历（原本官方 / 原本★ / 原本没有）
# 走行上的 cutin_origin，标签不承担两件事。
_TAG = {"game": "official", "yours": "self", "wiped": "self", "shared": "shared",
        # 借别卡的官方特效（runfx.borrow 写的）：标签跟官方★一样，但仍旧算本工具的改动，
        # 复原要能撤到它，所以单独占一个状态值，别并进 "shared"。
        "borrowed": "shared"}


def _tag(row: dict, dim: str) -> str:
    # 筛选档只看状态本身：自改不带★（2026-11-15 跟特效页对齐），来历交给悬停提示。
    return _TAG.get(row.get(dim) or "", "none")


_ART_ORDER = ("cutin", "runfx", "art")


def _art_size(path: str) -> tuple:
    """读图片宽高，读不到给 (0, 0)。只读文件头，不整张解码。"""
    import os
    if not path or not os.path.isfile(path):
        return (0, 0)
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


def _kind_order(kinds) -> list:
    """卡的类型按 动画 → 特效 → 卡图 排, 去重."""
    want = [k for k in _ART_ORDER if k in kinds]
    return want + [k for k in sorted(set(kinds)) if k and k not in want]


CUTIN_BORROW_FILE = "tools/cards/cutin_borrow.json"
_BORROW = None


def _cutin_borrow_list() -> dict:
    """官方逐张决定的「借谁的动画」，随包发布。

    键是借的卡号，值是出借的卡号。这类关系不在任何游戏表里：同名表只说明两张是
    同一张卡，不说明共用动画（#22187 与 #23493 的登记与资源形态完全一样，结果相反）。
    游戏出新卡后这份名单不会自动跟上，得人工补。
    """
    global _BORROW
    if _BORROW is None:
        p = runtime.bundle_root() / CUTIN_BORROW_FILE
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            _BORROW = {int(k): int(v) for k, v in raw.items()
                       if str(k).isdigit() and str(v).isdigit()}
        except (OSError, ValueError):
            _BORROW = {}
    return _BORROW


def _fx_borrow_auto(with_entry, owners, same_of) -> dict:
    """异画借用特效：自己没有 Ef 资源，同名同描述的另一张有。

    判据只用两个事实：卡在不在条目表里、磁盘上有没有 `Ef<卡号>/` 那套资源。
    有资源的播自己的，没资源的只能播别人的——不需要名单，也不需要看 effectPath
    里写了什么（那字段空着的时候反而多数是「用自己那套」，见 #4343 雷击）。

    #21235 闪电风暴是个要小心的例子：effectPath 空、但目录里有
    `Ef21235/Ef21235_near` 这些文件，所以它在 owners 里，不会被判成借用。
    """
    out = {}
    for cid in with_entry:
        if cid in owners:
            continue
        for other in same_of(cid):
            if other != cid and other in with_entry and other in owners:
                out[cid] = other
                break
    return out


def _borrow_filter(pairs: dict, originally, has_anim) -> dict:
    """从「谁借谁」的原始映射里筛出真正成立的。

    三条同时满足才留下：自己原本不在登记表里、出借方原本在、出借方资源齐全。
    """
    out = {}
    for cid, base in pairs.items():
        if not base or base == cid:
            continue
        if originally(cid) or not originally(base) or not has_anim(base):
            continue
        out[int(cid)] = int(base)
    return out


def _pass(row: dict, cutin: str, runfx: str, kind: str = "") -> bool:
    """同时满足各个维度的筛选条件才留下. 空串表示这一维不限。"""
    if kind and row.get("kind") != kind:
        return False
    if cutin and _tag(row, "cutin") != cutin:
        return False
    if runfx and _tag(row, "runfx") != runfx:
        return False
    return True


def _assets_status() -> dict:
    """组件清单。纯查文件，不碰游戏目录，也不该起子进程。"""
    from mdstudio import assets

    return assets.status()


def _probe_file(path, want="image") -> dict:
    """导入前的合法性检查。只读文件头，也放主进程里答，别为一次判断开子进程。"""
    from mdstudio import probe

    return probe.check(path, want)


def _art_filter_tags(art: str) -> tuple:
    """卡图页筛选参数 → 要收的内部档位。

    self 同时收 selfOfficial：界面上两者已并成「自改超框」一档，来历看行标签
    的悬停（2026-11-17 用户定稿）。其余档位一对一。
    """
    return ("self", "selfOfficial") if art == "self" else (art,)


def _state_of(kind, row, art_tag) -> str:
    """界面上的三态：official 游戏自带、modified 被本工具改过、none 没有。"""
    v = art_tag if kind == "art" else _tag(row or {}, kind)
    if v in ("self", "selfOfficial"):
        return "modified"
    if v in ("official", "shared"):
        return "official"
    return "none"


def _pack_entry_sizes(pack_path) -> dict:
    """包里每个文件的原始大小。用来粗判效果号撞车，不读内容。"""
    import zipfile

    p = Path(str(pack_path))
    if not p.is_file():
        return {}
    try:
        with zipfile.ZipFile(p) as zf:
            return {i.filename: i.file_size for i in zf.infolist()}
    except Exception:
        return {}


def _effect_clash(ctx, mrk, catalogs, sizes) -> str:
    """同名的特效文件本机已经有、大小又对不上 = 别人和本机用了同一个内部编号。

    装上去两边会互相覆盖，所以界面上要拦。这里用大小粗判，比读内容快得多；
    万一大小正好一样，安装时还有一次按内容比的检查兜底。
    """
    from mdstudio.core.crc import get_crc

    if not sizes:
        return ""
    for kind, cat in catalogs:
        if kind != "runfx":
            continue
        for name in cat:
            h = get_crc(name)
            try:
                have = ctx.game.find(h)
            except Exception:
                continue
            if not have:
                continue
            want = sizes.get(f"files/{h[:2]}/{h}")
            if want is None:
                continue
            try:
                if have.stat().st_size != want:
                    return "与已有特效冲突"
            except OSError:
                continue
    return ""


class Backend:
    def __init__(self):
        self._db = None
        self._cat = None
        self._status = None          # (cards, 借动画的, 借特效的)
        self._fusion = None          # {卡号: 备注}, 走融合召唤演出的卡
        self._fx_borrowed = None     # {卡号: 出借特效的底图}, 超框变体
        self._borrowed = None        # {卡号: 出借动画的那张}, 超框 + 异画同族
        self._art = None             # (官方超框卡号, 本工具改过的超框卡号)
        self._lock = threading.Lock()
        # 试听/抽轨会在 data/sound/preview 下留临时 wav，启动时清一次：
        # 这时候没有播放器持有句柄，是唯一能安全全删的时刻
        try:
            from mdstudio.ops import sound as _S

            _S.purge_preview_files()
        except Exception:
            pass

    def invalidate(self) -> None:
        """动作跑完之后调: 卡的状态变了, 缓存必须作废"""
        with self._lock:
            self._status = None
            self._fx_borrowed = None
            self._borrowed = None
            self._art = None

    def reset(self) -> None:
        """换了游戏目录或账号: 连卡名表和动态索引一起作废, 否则会拿着旧账号的数据"""
        with self._lock:
            self._db = None
            self._cat = None
            self._status = None
            self._fusion = None
            self._fx_borrowed = None
            self._borrowed = None
        from mdstudio.core.cardindex import clear
        clear()

    # ---------------------------------------------------------------- 上下文
    def _ctx(self):
        from mdstudio.core.context import Ctx
        return Ctx.open(None, dry_run=False)

    def _names(self, ctx):
        if self._db is None:
            from mdstudio.core.cards import CardDB
            self._db = CardDB(ctx.game).load()
        return self._db

    def _index(self, ctx):
        from mdstudio.core.cardindex import CardIndex
        return CardIndex.ensure(ctx.game)

    @staticmethod
    def _flags(idx, cid) -> dict:
        """所有页面共用的一组卡数据状态，避免各页各自猜。"""
        from mdstudio.ops import cutin as C

        s = idx.status(cid)
        return {"unreleased": s["unreleased"],
                "unknown_subtype": s["unknown_subtype"],
                "locked": s["locked"] or not idx.ok,
                # 来源卡（克隆底盘模板）：界面标徽章并禁用生成/导入，后端另有三处硬拦
                "donor": C.is_donor_card(cid),
                "subtype": s["subtype"], "subtype_cn": s["subtype_cn"],
                "coarse_cn": s["coarse_cn"], "abilities": s["abilities"]}

    def _rows(self, ctx):
        with self._lock:
            if self._status is None:
                from mdstudio.ops import cutin as C
                from mdstudio.ops import runfx as R

                cut = dict(C.list_cards(ctx.game, ctx.journal))
                fx = dict(R.list_cards(ctx.game, ctx.journal))
                # 借同名卡动画的卡（超框变体 #22804 借 #14952、异画 #23496 借 #21054）
                # 不在登记表里，但游戏里确实会播动画，列表要收进来，否则只能靠搜索才看得到
                borrowed = self._borrowed_variants(ctx)
                for cid in borrowed:
                    if cid not in cut:
                        cut[cid] = "shared"
                # 特效同理：超框变体的特效就是底图那套（#22770 借 #22692）
                fx_borrowed = self._borrowed_fx(ctx)
                for cid in fx_borrowed:
                    # 用户自己改过的（yours/wiped）保持原状，别被改回「借用」
                    if fx.get(cid) in (None, "game"):
                        fx[cid] = "shared"
                # 走融合召唤演出、没有发动特效条目的卡（超融合那两张）不在门禁表里，
                # 但游戏确实播一段官方演出，按「官方特效」统计与展示。界面另有
                # special 标记把它们标成「特殊卡」并锁死，见 status()。
                # 名单读游戏自己的 FusionEffectSetting，官方以后加了新卡自动跟上。
                for cid in self._fusion_fx(ctx):
                    if cid not in fx:
                        fx[cid] = "game"
                # 两边合并: 只列有召唤动画的卡, 发动特效那 60 多张卡就选不到了
                #
                # 过了索引这一关才进列表：超框登记表里有 25 个预留号（22793、22798…）
                # 既没卡名也没 CARD_Prop，连卡图都没有，游戏里永远不会出现。
                # 它们靠 of_card_asset 被算成「借用底图」，于是会以「卡号 22793」这种
                # 空行混进列表。索引已经判定这些号不可见，这里跟着它走，别再各判一套。
                idx = self._index(ctx)
                ids = [c for c in sorted(set(cut) | set(fx))
                       if idx.row(c) and idx.row(c)["visible"]]
                cards = [{"id": c, "cutin": cut.get(c), "runfx": fx.get(c)} for c in ids]
                self._status = (cards, borrowed, fx_borrowed)
            return self._status

    def _art_ids(self, ctx):
        """(官方超框卡号集合, 本工具改过的超框卡号集合)。

        卡图页的筛选要用。整张列表共用一份，别逐卡去游戏目录里读纹理。
        和 overframe_gate 同源：那边给界面打标签，这边给搜索做过滤。
        """
        with self._lock:
            if self._art is None:
                from mdstudio.ops import overframe as OF

                self._art = (set(OF.official_registered(ctx)), set(OF.ours(ctx)))
            return self._art

    @staticmethod
    def _art_of(cid: int, official: set, mine: set) -> str:
        """这张卡的超框状态。四个互斥取值，是内部判据（分享页与 _state_of 都用）。

        selfOfficial = 官方本来就有，被本工具的版本覆盖了。界面上它与 self 同显示
        「自改超框」，来历由行标签悬停区分（2026-11-17 用户定稿）。
        """
        has_official = cid in official
        has_mine = cid in mine
        if has_official and has_mine:
            return "selfOfficial"
        if has_official:
            return "official"
        if has_mine:
            return "self"
        return "none"

    def _ef_owners(self, ctx) -> set:
        """{磁盘上有 `Ef<卡号>/` 那套文件的卡号}。

        只要目录里有一个文件就算：官方有的卡只有 `_near` / `_far` / `Timeline`，
        没有同名主文件（#21235 闪电风暴），按主文件名判会把它误判成「没有资源」。
        """
        try:
            names = self._catalog(ctx).names()
        except Exception:
            return set()
        pre = "Duel/Timeline/Card/tcg/Ef"
        out = set()
        for n in names:
            if not n.startswith(pre):
                continue
            seg = n[len(pre):].split("/", 1)[0]
            if len(seg) >= 5 and seg[:5].isdigit():
                out.add(int(seg[:5]))
        return out

    def _same_name_of(self, ctx):
        """给一个卡号，返回同名的其它版本（卡名与效果文本都一样）。

        同名表里还有「相关卡」（青眼白龙 ↔ 青眼亚白龙），那些不能算一伙；
        flag 数值也不可靠（flag=0 里两种都有），只能比对文本。
        """
        idx = self._index(ctx)

        def f(cid: int):
            me = idx.row(cid) or {}
            name, desc = me.get("name"), me.get("desc")
            if not name:
                return ()
            out = []
            for other, _flag in me.get("aliases") or []:
                him = idx.row(other) or {}
                if him.get("name") == name and him.get("desc") == desc:
                    out.append(int(other))
            return out

        return f

    def _borrowed_fx(self, ctx) -> dict:
        """{卡号: 出借特效的底图卡号}。超框变体里，底图有发动特效的那些。

        三条来源，全是可判定的，没有卡号名单：
          - 超框表 + 变体自己没有条目，靠底图的条目播（#22813 <- #20257）
          - 超框表 + 变体有条目，但 seLabel 与 effectPath 跟底图一模一样
            （#22770 <- #22692、#22822 <- #15409、#22823 <- #20555）
          - 异画借用：自己没有任何 `Ef<卡号>/` 资源，同名同描述的另一张有
            （#12950 灰流丽 <- #3891、#8933 效果遮蒙者 <- #21224、#13587 屋敷童子 <- #3892）

        第三条为什么不用名单：这三对里，没资源的那张（12950、8933、13587）本地
        一个文件都没有，它不可能播自己的，只可能播同名有资源那张。#21224 反过来
        带了一整套 Ef21224，是 8933 借它，不是它借 8933。
        """
        if self._fx_borrowed is not None:
            return self._fx_borrowed
        from mdstudio.core.gate import RunFxGate
        from mdstudio.ops import overframe as OF

        try:
            pairs = dict(OF.bundled_gate_records(ctx))
            by = {e["mrk"]: e for e in RunFxGate(ctx.game).entries}
        except Exception:
            self._fx_borrowed = {}
            return self._fx_borrowed
        out = {}
        for cid, base in pairs.items():
            cid, base = int(cid), int(base or 0)
            if not base or base == cid or base not in by:
                continue
            me = by.get(cid)
            ref = by[base]
            if me is None:
                out[cid] = base
                continue
            # effectPath 里写的是 <_CARD_ILLUST_> 或底图卡号，两边字面一致才算同一套资源
            same = ((me.get("seLabel") or "") == (ref.get("seLabel") or "")
                    and (me.get("effectPath") or "") == (ref.get("effectPath") or ""))
            if same:
                out[cid] = base
        # 异画借用：自己一点 Ef 资源都没有，同名的另一张有整套，游戏就播那一套。
        # 超框表已经判定的以表为准（setdefault），这里只补表里没有的。
        for cid, base in _fx_borrow_auto(
                set(by), self._ef_owners(ctx), self._same_name_of(ctx)).items():
            out.setdefault(cid, base)
        self._fx_borrowed = out
        return out

    def _fx_origin_kind(self, ctx) -> dict:
        """{卡号: "official" | "shared"} —— 动手之前那套是它自己的，还是借别人的。

        被改过的卡，journal 的 runfx_removed 里留着被替换掉的原条目；不在表里就是
        原本没有特效（克隆）。有条目时再看资源号：Ef 号等于卡号的是自有资源，
        指向别卡的是借用（#22770 那条写的是 Ef22692）。路径为空也算自有，
        有 9 条官方条目靠 useCenterEffect 隐式指向自己（#14876、#13631 等）。
        """
        import re

        from mdstudio.ops.runfx import OPS

        pat = re.compile(r"Ef(\d{5})")
        out = {}
        try:
            recs = ctx.journal.load()
        except Exception:
            return out
        for r in recs:
            if r.op not in OPS or not r.runfx_removed:
                continue
            # 名单里写明的借用，条目的 Ef 号可能等于自己（#21224 有整套 Ef21224，
            # 实际播 #8933），所以不能只看路径，先认名单。
            listed = int(r.card) in self._borrowed_fx(ctx)
            shared_only = True
            for e in r.runfx_removed:
                p = (e.get("centerEffectPath") or "") or (e.get("effectPath") or "")
                m = pat.search(p)
                if not (m and int(m.group(1)) != r.card):
                    shared_only = False
                    break
            out[int(r.card)] = "shared" if (listed or shared_only) else "official"
        return out

    def _fx_origin(self, ctx) -> set:
        """被我们改过、但原本就有官方特效的卡（走的是覆盖，不是克隆）。"""
        return set(self._fx_origin_kind(ctx))

    def _catalog(self, ctx):
        """资源清单只读一次。判「底图有没有动画」要扫整张清单，重建一次约 0.3s。"""
        if self._cat is None:
            from mdstudio.core.catalog import Catalog
            self._cat = Catalog(ctx.game)
        return self._cat

    def _kind_of(self, db, cid: int, borrowed: dict = None) -> str:
        """卡种：monster / spell / trap；判断不出来返回空串。

        直接从游戏 CARD_Prop 读（见 CardDB.kind），代码里没有任何卡种表。
        超框变体有一批号根本不在 CARD_Prop 里（22769 那 48 个），它们自己没数据，
        卡种跟着底图走：底图是怪兽，这张也就是怪兽。
        """
        k = db.kind(cid)
        if k or not borrowed:
            return k
        base = borrowed.get(int(cid))
        return db.kind(base) if base else ""

    def _fusion_fx(self, ctx) -> dict:
        """{卡号: 备注} —— 走融合召唤演出、因而没有发动特效的卡。读一次缓存。

        「修改卡牌特效」列表里要把这批标成特殊卡并锁死：它们跟发动特效是两套系统，
        改了也不会播，让用户能点等于给一个必然无效的操作。
        """
        if self._fusion is None:
            from mdstudio.core.gate import fusion_fx_cards
            try:
                self._fusion = fusion_fx_cards(ctx.game)
            except Exception:
                self._fusion = {}
        return self._fusion

    def _borrowed_variants(self, ctx) -> dict:
        """{卡号: 提供动画的卡号}。自己没有、播的是别人那套的卡。

        两条来源，都是官方给定的映射，不做推断：

          - 覆盖名单 `tools/cards/cutin_borrow.json`：官方逐张决定的借用，比如浮世绘
            那批异画（#23493 借 #21371）。这类关系在同名表里看不出来：#22187 与
            #23493 数据形态完全一样（自己无登记无资源、同名的另一张有），但前者原版
            没有动画、后者借原版，只差官方有没有给它做这段演出。
          - 超框表 `of_card_asset`：变体 -> 底图，方向写在表里（#23180 借 #17037）。

        两条都要过同一道检查：自己原本不在 `monsterCutinTable` 里，出借方原本在表里、
        且有 `P<号>` 资源。生成时会给没条目的卡补一条登记，所以「原本」要用 journal
        还原（`C.official_origin` 给的是反向集合：改过但原本就有的）。

        魔法/陷阱类超框（#22770、#22823）的底图没有动画资源，不会落进来，
        它们只有发动特效，不该被标成「借用动画」。
        """
        if self._borrowed is not None:
            return self._borrowed
        from mdstudio.core.gate import CutinGate
        from mdstudio.ops import cutin as C
        from mdstudio.ops import overframe as OF

        cat = self._catalog(ctx)
        try:
            pairs = {int(k): int(v) for k, v in dict(OF.bundled_gate_records(ctx)).items() if v}
        except Exception:
            pairs = {}
        # 名单优先：同一张卡两边都写了时，以名单为准
        pairs.update(_cutin_borrow_list())
        try:
            live = set(CutinGate(ctx.game).mrks())
        except Exception:
            live = set()

        # 我们动过的卡：补过条目的那批，现在的表里算它，原本不算
        try:
            touched = {r.card for r in ctx.journal.load() if r.op in C.OPS}
        except Exception:
            touched = set()
        try:
            orig = C.official_origin(ctx.journal)
        except Exception:
            orig = live - touched

        def originally(cid: int) -> bool:
            """动手之前它就在登记表里吗。"""
            if cid not in live:
                return False
            return cid in orig if cid in touched else True

        out = _borrow_filter(pairs, originally, lambda c: C._has_own_anim(cat, c))
        self._borrowed = out
        return out

    # ------------------------------------------------------------------ 查询
    def info(self) -> dict:
        from mdstudio import __build__, __version__

        ctx = self._ctx()
        g = ctx.game
        return {
            "version": __version__,
            "build": __build__,
            "game_dir": str(g.dir),
            "account": g.group,
            "account_label": settings.label(g.group),
            "accounts": g.accounts(),
            "journal": str(ctx.journal.folder),
            "records": len(ctx.journal.load()),
        }

    def stats(self) -> dict:
        """设置页「关于」那四个数 + 最近 30 天。纯只读，不写文件也不建目录。

        三个数量按「卡」算，不按操作次数：一张卡生成三次再复原，只算一张；
        复原之后记录没了，就归零。备份体积只量撤销用的两份目录（backups 与
        baselines），不含壁纸整合包那份 data.unity3d 原图 —— 那是几个 G，跟
        这几个数不是一个量级。
        """
        from mdstudio.ops import cutin as C
        from mdstudio.ops import pack as P
        from mdstudio.ops import runfx as R

        ctx = self._ctx()
        j = ctx.journal
        try:
            recs = j.load()
        except Exception:
            recs = []          # 记录坏了不该让整个设置页读不出数
        # 可用卡按类型分组：魔法/陷阱看 coarse，其余（通常/效果/融合/同调/超量/连接/
        # 灵摆/仪式）都算怪兽。一次遍历一万四千条，几十毫秒。
        kinds = {"monster": 0, "spell": 0, "trap": 0}
        try:
            idx = self._index(ctx)
            for cid in idx.visible:
                row = idx.cards.get(cid)
                if not row:
                    continue
                coarse = row.get("coarse")
                kinds[coarse if coarse in ("spell", "trap") else "monster"] += 1
        except Exception:
            kinds = {"monster": 0, "spell": 0, "trap": 0}

        return {
            "cutin": len({r.card for r in recs if r.op in C.OPS}),
            "runfx": len({r.card for r in recs if r.op in R.OPS}),
            "art": len({r.card for r in recs if r.op in P.ART_OPS}),
            "backup_mb": round(_dir_mb(j.backups) + _dir_mb(j.baselines.folder), 1),
            "days": j.activity_days(30),
            "kinds": kinds,
        }

    def status(self) -> dict:
        ctx = self._ctx()
        db = self._names(ctx)
        cards, shared, fx_shared = self._rows(ctx)
        origin = self._official_origin(ctx)
        fx_origin_kind = self._fx_origin_kind(ctx)
        fx_origin = set(fx_origin_kind)
        fusion = self._fusion_fx(ctx)
        idx = self._index(ctx)
        # 借用过的卡：donor 号只在 journal 的记录里有，超框变体那套 _borrowed_fx 查不到
        borrow_src = {}
        try:
            for r in ctx.journal.load():
                if r.op != "runfx.borrow":
                    continue
                d = (r.meta or {}).get("donor")
                if d:
                    borrow_src[int(r.card)] = int(d)
        except Exception:
            borrow_src = {}
        for c in cards:
            c["name"] = db.name(c["id"]) or ""
            c["kind"] = self._kind_of(db, c["id"], shared)
            # 通常怪兽 / 衍生物没有发动特效，特效模式的列表要把它们剔掉
            c["plain"] = db.is_plain(c["id"])
            c.update(self._flags(idx, c["id"]))
            # 借用同名卡动画的超框变体: 界面标签带★, 悬停提示说明借的是哪张
            if c["id"] in shared:
                c["src"] = shared[c["id"]]
                c["src_name"] = db.name(shared[c["id"]]) or ""
            # 自改动画的来历：原本是借底图的（★）/ 原本自带官方 / 原本没有。
            # 判定两张原始表都读 bundled 那份，所以生成之后仍然认得（own_anim 只改 live 表）。
            if c["cutin"] in ("yours", "wiped"):
                c["cutin_origin"] = ("shared" if c["id"] in shared
                                     else "official" if c["id"] in origin else "")
            # 特效那一维同理
            if c["id"] in fx_shared:
                c["fx_src"] = fx_shared[c["id"]]
                c["fx_src_name"] = db.name(fx_shared[c["id"]]) or ""
            # 借来的那套：来历是 donor，界面悬停要写「借自 #X」
            if c["runfx"] == "borrowed" and c["id"] in borrow_src:
                c["fx_src"] = borrow_src[c["id"]]
                c["fx_src_name"] = db.name(borrow_src[c["id"]]) or ""
            if c["runfx"] != "shared" and (c["id"] in fx_origin or c["id"] in fx_shared):
                c["fx_from_official"] = True
            # 自改那行不带标记，来历放进悬停：来自无特效 / 来自官方特效 / 来自官方特效★。
            # 空串 = 原本没有特效（克隆），official = 覆盖了它自己那套，shared = 原本是借的。
            if c["runfx"] in ("yours", "wiped"):
                c["fx_origin"] = fx_origin_kind.get(c["id"], "")
            # 走融合召唤演出、没有可改发动特效的卡：界面标「特殊卡」并锁死。
            # 前端 ParseCards 只认字符串，备注为空也得给个空串而不是 bool True，
            # 否则这张卡会变成能点的普通卡。
            if c["id"] in fusion:
                c["special"] = fusion[c["id"]] or "特殊卡"
        return {"cards": cards, "count": len(cards)}

    def _official_origin(self, ctx) -> set:
        """被我们改过、但原本就有官方动画的卡。

        判据在 journal 里：官方本来就有条目的卡，往登记表加不进新条目，
        所以 `cutin_mrks` 是空的；自己补过条目的卡才会有记录。
        """
        from mdstudio.ops import cutin as C

        try:
            return C.official_origin(ctx.journal)
        except Exception:
            return set()

    def search(self, q: str = "", limit: int = 200,
               cutin: str = "", runfx: str = "",
               has_anim: bool = True, kind: str = "",
               no_plain: bool = False, art: str = "") -> dict:
        """搜卡. 空串 = 有召唤动画或发动特效的卡全列

        有查询词时从**完整卡名表**里搜(14257 张), 不只是本工具动过的那几张,
        否则"耀圣"这种能匹配很多卡的词只出得来几张. 搜到的卡再把动画状态补上,
        没有动画的卡状态是 None, 界面上显示"无".

        cutin / runfx 是标签筛选, 取值 official / self / none, 空串表示不限.

        kind 是卡种筛选: monster / spell / trap, 空串不限. 召唤动画只存在于怪兽卡,
        魔法陷阱卡再多也只有发动特效, 所以在「修改召唤动画」那半边搜卡时要限定
        monster —— 否则会搜出一张魔法卡, 再告诉用户它「无动画」, 那是错的:
        它跟召唤动画根本不相干。

        has_anim=False 时不补动画状态、空串就列全部卡名表. 卡图页要的是所有卡,
        跟动画页那个"只收有动画或有特效的卡"的列表不是一回事。

        no_plain=True 时剔掉通常怪兽和衍生物：这类卡游戏里根本不会有发动特效，
        在「修改卡牌特效」里搜出来只会得到一个必然无效的操作。判据在
        CardDB.is_plain，跟卡种一样直接读 CARD_Prop，不列卡号。
        """
        ctx = self._ctx()
        db = self._names(ctx)
        q = (q or "").strip()

        if not has_anim:
            # art 是卡图页的筛选，四个取值互斥且完备：
            # official / self / none 是超框三档（自改合并了「原本官方有」与「原本没有」，
            # 来历看行标签的悬停），unreleased 是未上线。
            # 带筛选时候选要放大，先按 limit 截断再过滤会把符合条件的结果截掉：
            # 三个超框档都是全表里的小比例（官方那档只占个位数到十位数），
            # 按 limit 截断后的 200 条里很可能一条都不剩。
            want = limit * 10 if art else limit
            if not q:
                rows = [{"id": cid, "name": nm} for cid, nm in
                        sorted(db.visible_names().items(), key=lambda kv: kv[0])]
            else:
                rows = [{"id": cid, "name": nm} for cid, nm in db.search(q, limit=want)]
            idx = self._index(ctx)
            if art:
                official, mine = self._art_ids(ctx)
                if art == "unreleased":
                    # 未上线单独一档，不管有没有官方超框都归这里
                    rows = [r for r in rows if idx.status(r["id"])["unreleased"]]
                else:
                    # 三个超框档只收已上线的卡。未上线的 31 张里有 10 张官方本来就
                    # 有超框，不排掉会在「官方超框」和「未上线」两档里同时出现。
                    want_tags = _art_filter_tags(art)
                    rows = [r for r in rows
                            if not idx.status(r["id"])["unreleased"]
                            and self._art_of(r["id"], official, mine) in want_tags]
            total = len(rows)
            rows = rows[:limit]
            # real != 0 表示这个号是占位副本, 值是游戏真正渲染的号
            for r in rows:
                r["real"] = db.placeholder_of(r["id"])
                r.update(self._flags(idx, r["id"]))
            return {"cards": rows, "count": total}

        # 只有动画/特效这条分支才需要状态表和借用关系。卡图页走上面那条，
        # 以前两边都无条件调 _rows()，首次要构造 Catalog（解密 4MB manifest
        # 约 1.9 秒），卡图页白等这一下，还把后面页面的查询全堵在引擎队列里。
        cards, borrowed, fx_borrowed = self._rows(ctx)
        filt = bool(cutin or runfx or kind)

        # 「无动画 / 无特效」是全表补集，不能只在 _rows() 那批有资源的卡里找。
        # 那批只有几百张，数出来的其实是「有特效但没动画」的十几张。
        # 这一档改从完整卡名表出发，跟卡图页「无超框」同一套口径。
        none_dim = "cutin" if cutin == "none" else "runfx" if runfx == "none" else ""
        if none_dim:
            return self._search_none(ctx, db, none_dim, q, limit, kind, no_plain)

        if not q:
            out = [{"id": c["id"], "name": db.name(c["id"]) or "",
                    "cutin": c["cutin"], "runfx": c["runfx"],
                    "kind": self._kind_of(db, c["id"], borrowed),
                    "real": db.placeholder_of(c["id"])} for c in cards]
            self._fill_src(ctx, db, out)
            self._fill_fx_src(ctx, db, out)
            # 空查询是列表的默认路径，来历也要补，否则自改那行的悬停一律写「来自无动画」
            self._fill_origin(ctx, out)
            out = [r for r in out if _pass(r, cutin, runfx, kind)]
            if no_plain:
                out = [r for r in out if not db.is_plain(r["id"])]
            idx = self._index(ctx)
            for r in out:
                r.update(self._flags(idx, r["id"]))
            self._fill_special(ctx, out)
            return {"cards": out, "count": len(out)}

        state = {c["id"]: c for c in cards}
        seen = set()
        out = []
        # 带筛选时把候选放大: 先在 limit 处截断再过滤, 会把真正符合条件的结果漏掉
        # no_plain 也是一种筛选, 候选同样要放大, 否则搜一个常见词会只剩几条
        want = limit * 10 if (filt or no_plain) else limit
        for cid, name in db.search(q, limit=want):
            if cid in seen:
                continue
            seen.add(cid)
            c = state.get(cid)
            out.append({"id": cid, "name": name,
                        "cutin": c["cutin"] if c else None,
                        "runfx": c["runfx"] if c else None,
                        "kind": self._kind_of(db, cid, borrowed),
                        "real": db.placeholder_of(cid)})

        # 卡名表里没匹配上的, 但状态表里有且名字含查询词的, 也要能搜到
        ql = q.lower()
        for c in cards:
            if c["id"] in seen:
                continue
            name = db.name(c["id"]) or ""
            if ql in name.lower() or ql in str(c["id"]):
                seen.add(c["id"])
                out.append({"id": c["id"], "name": name,
                            "cutin": c["cutin"], "runfx": c["runfx"],
                            "kind": self._kind_of(db, c["id"], borrowed),
                            "real": db.placeholder_of(c["id"])})
        self._fill_src(ctx, db, out)
        self._fill_fx_src(ctx, db, out)
        self._fill_origin(ctx, out)
        if filt:
            out = [r for r in out if _pass(r, cutin, runfx, kind)]
        if no_plain:
            out = [r for r in out if not db.is_plain(r["id"])]
        if filt or no_plain:
            out = out[:limit]
        idx = self._index(ctx)
        for r in out:
            r.update(self._flags(idx, r["id"]))
        self._fill_special(ctx, out)
        return {"cards": out, "count": len(out)}

    def _search_none(self, ctx, db, dim, q, limit, kind, no_plain) -> dict:
        """「无动画 / 无特效」档：从完整卡名表取补集，跟卡图页「无超框」同源。

        _rows() 只收有动画或有特效的那几百张，在里面数「无」得到的是
        「有特效但没动画」这类交集，不是全游戏里没这个资源的卡。这里改成
        从 db.visible_names() 出发，逐张问状态表，再扣掉以下几类：

        - 该维度有资源的（game / yours / wiped / shared，含借用的变体）
        - 占位副本（游戏不渲染的另一个号，前端本来就跳过）
        - 未上线（卡图页「无超框」同样排除）
        - 融合召唤演出的特殊卡（它们归「官方特效」，不落进「无特效」）

        数量全部现算：卡名表、门禁表、索引、融合演出表都随游戏更新，
        这里不写死任何卡号或总数。count 取过滤后、截断前的总数，界面
        据此显示真实规模，列表仍只回前 limit 条。
        """
        idx = self._index(ctx)
        cards, borrowed, _ = self._rows(ctx)
        state = {c["id"]: c for c in cards}
        fusion = self._fusion_fx(ctx)

        # 带筛选时候选要放大，先在 limit 处截断再过滤会把结果漏掉。
        # 空查询直接走完整卡名表，不受 limit 影响，count 才是真实总数。
        cand = ([(cid, nm) for cid, nm in db.search(q, limit=limit * 10)]
                if q else sorted(db.visible_names().items(), key=lambda kv: kv[0]))

        rows = []
        for cid, name in cand:
            if cid in fusion:
                continue
            if db.placeholder_of(cid):
                continue
            if idx.status(cid)["unreleased"]:
                continue
            k = self._kind_of(db, cid, borrowed)
            if kind and k != kind:
                continue
            plain = db.is_plain(cid)
            if no_plain and plain:
                continue
            c = state.get(cid)
            rows.append({"id": cid, "name": name,
                         "cutin": c["cutin"] if c else None,
                         "runfx": c["runfx"] if c else None,
                         "kind": k, "plain": plain, "real": 0})

        # 借用底图资源的超框变体不一定在门禁表里，补完 borrowed 才知道它其实
        # 会播动画 / 特效。不补就会把这类卡误算成「无」，跟主分支口径不一致。
        self._fill_src(ctx, db, rows)
        self._fill_fx_src(ctx, db, rows)
        out = [r for r in rows if _tag(r, dim) == "none"]

        total = len(out)
        out = out[:limit]
        for r in out:
            r.update(self._flags(idx, r["id"]))
        return {"cards": out, "count": total}

    def _fill_special(self, ctx, rows) -> None:
        """给走融合召唤演出的卡打上 special，界面据此标「特殊卡」并锁死。

        值是游戏表里的备注（如「超融合」），没有备注就给 True。
        """
        fusion = self._fusion_fx(ctx)
        if not fusion:
            return
        for r in rows:
            if r.get("id") not in fusion:
                continue
            # 备注可能为空；前端只认字符串，给空串会把特殊卡变成能点的普通卡
            r["special"] = fusion.get(r.get("id")) or "特殊卡"

    def _fill_fx_src(self, ctx, db, rows) -> None:
        """给借特效的卡补上「借的是哪张」，并落成 shared 状态。

        跟动画那一维是两套数据：底图有动画不代表有特效，反之亦然。
        """
        todo = [r for r in rows if not r.get("fx_src")]
        if not todo:
            return
        shared = self._borrowed_fx(ctx)
        if not shared:
            return
        for r in todo:
            base = shared.get(r["id"])
            if not base:
                continue
            r["fx_src"] = base
            r["fx_src_name"] = db.name(base) or ""
            if not r.get("runfx"):
                r["runfx"] = "shared"

    def _fill_origin(self, ctx, rows) -> None:
        """给自改动画补上「这套是从哪来的」：原本官方自带 / 原本借同名卡（★）/ 原本没有。

        两种「官方来源」都算：官方本来就有自己一套的；原本借同族那套播放的
        （超框变体借底图、异画借同名的另一个版本）。
        后者克隆时会给它补登记条目，靠 journal 判不出来，得用借用表兜住。
        借用表里超框那一支读的是未改动的原始表，所以生成之后它仍然算「原本借的」。
        """
        official = self._official_origin(ctx)
        borrowed = self._borrowed_variants(ctx)
        fx_kind = self._fx_origin_kind(ctx)
        fx_origin = set(fx_kind) | set(self._borrowed_fx(ctx))
        for r in rows:
            if r.get("cutin") in ("yours", "wiped"):
                r["cutin_origin"] = ("shared" if r.get("id") in borrowed
                                     else "official" if r.get("id") in official else "")
            if r.get("id") in fx_origin and r.get("runfx") != "shared":
                r["fx_from_official"] = True
            if r.get("runfx") in ("yours", "wiped"):
                r["fx_origin"] = fx_kind.get(r.get("id"), "")

    def _fill_src(self, ctx, db, rows) -> None:
        """给借动画的卡补上「借的是哪张」。

        搜索结果里会冒出没进过登记表的卡：超框变体，以及借同名版本的异画
        （#23496 借 #21054）。它们的 cutin 是 None，但游戏里会播同族那套。
        不补这一项，界面只能显示「无动画」。
        """
        todo = [r for r in rows if not r.get("src")]
        if not todo:
            return
        borrowed = self._borrowed_variants(ctx)
        if not borrowed:
            return
        for r in todo:
            base = borrowed.get(r["id"])
            if not base:
                continue
            r["src"] = base
            r["src_name"] = db.name(base) or ""
            if not r.get("cutin"):
                r["cutin"] = "shared"


    # ------------------------------------------------------------------ 发动特效
    def runfx_donors(self) -> dict:
        """借用来源: 游戏里每个带发动特效的卡

        on_disk 是 effectPath 指向的 bundle 在不在盘上. 不在的话借过来游戏里放不出来,
        界面上要标出来, 别让人挑一个空壳.
        """
        from mdstudio.ops import runfx as R

        ctx = self._ctx()
        db = self._names(ctx)
        # 来源卡自己是什么来路，界面上要标出来：官方 / 官方★（借用同名卡）/ 自改。
        # 直接取列表那套已经合并过的状态与借用表，别在这里另判一套。
        cards, _, fx_borrowed = self._rows(ctx)
        state = {c["id"]: c["runfx"] for c in cards}
        fx_kind = self._fx_origin_kind(ctx)
        fx_origin = set(fx_kind)
        # 一张卡常常有多条 entry(主效果 + 它的 Op 版本), 界面上只该出现一次
        seen = {}
        for mrk, timing, se, ep, on_disk in R.list_effects(ctx.game):
            ok = True if on_disk is None else bool(on_disk)
            cur = seen.get(mrk)
            if cur is None:
                kind = _tag({"runfx": state.get(mrk),
                             "fx_from_official": mrk in fx_origin}, "runfx")
                row = {"id": mrk, "name": db.name(mrk) or "",
                       "timing": str(timing or ""), "se": se or "", "ready": ok,
                       "kind": kind, "fx_origin": fx_kind.get(mrk, "")}
                base = fx_borrowed.get(mrk)
                if base:
                    row["src"] = base
                    row["src_name"] = db.name(base) or ""
                seen[mrk] = row
            elif ok and not cur["ready"]:
                cur["ready"] = True
        out = sorted(seen.values(), key=lambda e: e["id"])
        return {"effects": out, "count": len(out)}


    # ------------------------------------------------------------------ 整合包分享
    def preview_list(self, kind=None) -> list:
        """预览缓存清单（类型、卡号、帧数、字节数、生成时间）。kind=None 列两类。"""
        from mdstudio.ops import preview as PV

        ctx = self._ctx()
        db = self._names(ctx)
        rows = PV.list_cards(ctx, kind)
        for x in rows:
            x["name"] = db.name(x["card"]) or ""
        return rows

    def preview_meta(self, card, kind="cutin") -> dict:
        from mdstudio.ops import preview as PV

        ctx = self._ctx()
        meta = PV.read_meta(ctx, int(card), kind) or {}
        if meta:
            # 播放器要按 000.webp / 001.webp 自己读，得知道落在哪个目录
            meta["dir"] = str(PV.folder_of(ctx, int(card), kind))
            meta.setdefault("kind", kind)
        return meta

    def preview_drop(self, card, kind="cutin") -> dict:
        from mdstudio.ops import preview as PV

        return {"dropped": PV.drop(self._ctx(), int(card), kind)}

    def preview_sound(self, card) -> dict:
        """这张卡的特效现在挂着哪条音效、能不能在软件里放。

        预览缓存里的 se 字段靠不住：生成特效时界面还没挑音效，音效是之后用
        「音效」那块单独挂上去的（sound.custom / sound.inject），缓存不会跟着改。
        所以每次打开预览现问一次，换音效、还原音效都能立刻反映。
        """
        from mdstudio.ops import runfx as R
        from mdstudio.ops import sound as S

        ctx = self._ctx()
        label = ""
        for rec in ctx.journal.for_card(int(card), R.OPS):
            for e in rec.runfx_added or []:
                if e.get("seLabel"):
                    label = e["seLabel"]
                    break
            if label:
                break

        if not label:
            return {"label": "", "playable": False, "reason": "none"}
        if label in S.BUILTIN:
            # 打包在 data.unity3d 里，fmod 解不开
            return {"label": label, "playable": False, "reason": "builtin"}
        if not S.on_disk(ctx.game, label):
            return {"label": label, "playable": False, "reason": "missing"}
        return {"label": label, "playable": True, "reason": ""}

    def pack_list(self) -> dict:
        """本工具加过的卡, 一眼看完, 方便挑哪些导出"""
        from mdstudio.ops import pack as P

        ctx = self._ctx()
        db = self._names(ctx)
        cards = {}
        for card, kinds in P.exportable(ctx.journal).items():
            cards[str(card)] = {"kinds": sorted(kinds), "name": db.name(card) or ""}
        return {"cards": cards, "count": len(cards)}

    def pack_inspect(self, pack_path: str) -> dict:
        """看看别人的包里有什么卡、什么类型

        一张卡的动画/特效/卡图是三条独立条目, 界面上要合成一行: 卡号、卡名、类型一起显示,
        否则同一张卡会重复出现三次。
        """
        from mdstudio.ops import pack as P

        meta = P.inspect(pack_path)
        ctx = self._ctx()
        db = self._names(ctx)
        merged: dict = {}
        cards_by_mrk = {int(c["mrk"]): c for c in meta.get("cards", [])
                        if isinstance(c, dict) and "mrk" in c}
        for c in P._ungroup_cards(meta):
            mrk = c.get("mrk")
            if not isinstance(mrk, int):
                continue
            slot = merged.setdefault(mrk, {"mrk": mrk, "kinds": [], "entries": 0,
                                           "catalogs": [], "layers": False})
            slot["kinds"].append(c.get("kind"))
            if c.get("kind") == "art" and c.get("layers"):
                slot["layers"] = True
            cat = c.get("catalog") or {}
            slot["catalogs"].append((c.get("kind"), cat))
            slot["entries"] += len(cat)

        # 本机这三类各自现在是什么状态：official=游戏自带没动过、
        # modified=本工具改过（自己做的或以前装的包）、none=没有。
        cards_now, borrowed, _ = self._rows(ctx)
        state = {c["id"]: c for c in cards_now}
        vis = db.visible_names()
        pack_sizes = _pack_entry_sizes(pack_path)
        gate = self.overframe_gate()
        official_set, mine_set = set(gate["official"]), set(gate["ours"])

        cards = []
        for mrk in sorted(merged):
            slot = merged[mrk]
            row = state.get(mrk)
            art_tag = self._art_of(mrk, official_set, mine_set)
            has, cur, blocked = {}, {}, {}
            for kind in ("cutin", "runfx", "art"):
                has[kind] = kind in slot["kinds"]
                if not has[kind]:
                    cur[kind], blocked[kind] = "none", ""
                    continue
                cur[kind] = _state_of(kind, row, art_tag)
                blocked[kind] = "" if mrk in vis else "本机没有这张卡"
                # 安装闸门：本机这一项被本工具改过就禁装，提示先去对应页复原。
                # 官方、借用、没有三种状态不算改过（_state_of 分别返回 official / none）。
                if not blocked[kind] and cur[kind] == "modified":
                    page = "卡图页" if kind == "art" else "动画页"
                    blocked[kind] = f"本机已改过，先到{page}复原"
            if not blocked.get("runfx") and has["runfx"]:
                why = _effect_clash(ctx, mrk, slot["catalogs"], pack_sizes)
                if why:
                    blocked["runfx"] = why
            cards.append({
                "mrk": mrk,
                "name": db.name(mrk) or "",
                "kind": ",".join(_kind_order(slot["kinds"])),
                "entries": slot["entries"],
                "has": has,
                "state": cur,
                "blocked": blocked,
                "known": mrk in vis,
                # 这个包里的超框带分层素材：装上之后卡图页还能接着调
                "layers": bool(slot["layers"]),
            })
        try:
            import os
            size = os.path.getsize(pack_path) if os.path.isfile(pack_path) else 0
        except OSError:
            size = 0
        return {"name": meta.get("name"), "tool": meta.get("tool"),
                "format": meta.get("format"), "size": size, "cards": cards}

    # ------------------------------------------------------------------ 装过的包
    def pack_installed(self) -> dict:
        from mdstudio.ops import pack as P

        return {"packs": P.installed_packs(self._ctx(), 20)}

    # ------------------------------------------------------------------ 历史
    def history_list(self) -> dict:
        """这个工具做过的所有改动, 新的在前"""
        import time

        ctx = self._ctx()
        db = self._names(ctx)
        recs = []

        def emit(r_id, op, card, t, meta, *, archived=False):
            """把一条记录变成历史页的行。archived 是复原前那条改动的回放，
            照常显示，但不可撤销（journal 里已经没有它了）。"""
            donor = "" if op == "cutin.spine" else (meta.get("donor") or "")
            if isinstance(donor, int) and donor == card:
                donor = ""
            src = donor or meta.get("label") or meta.get("pack") or ""
            if isinstance(src, int) and src:
                src = f"{src} {db.name(src)}".strip()
            # 回放行照常显示改动本身，不把包名又贴回去（用户定稿：整合包装入
            # 的记录只按装了什么归类，不带整合包标识）。
            if archived:
                src = ""
            detail = {k: meta[k] for k in ("source", "label", "path", "kinds",
                                           "overwritten", "kind", "files") if k in meta}
            sha = meta.get("sha")
            if isinstance(sha, dict):
                detail["files"] = len(sha)
            recs.append({
                "id": r_id,
                "op": op,
                "card": card,
                "batch": meta.get("batch") or "",
                "name": db.name(card) if card else "",
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(t)),
                "_time": t,
                "from": str(src),
                "undoable": not archived and not meta.get("noop"),
                "cards": meta.get("cards"),
                "total": meta.get("total"),
                "detail": detail,
                "archived": archived,
            })

        def _kind_of(op: str) -> str:
            """op → 界面上那三类。pack.* 的族在第二段，其余在第一段。"""
            a, _, b = str(op).partition(".")
            fam = b if a == "pack" else a
            if fam in ("overframe", "art"):
                return "art"
            if fam == "cutin":
                return "cutin"
            if fam in ("runfx", "sound"):
                return "runfx"
            return ""

        for r in ctx.journal.load():
            # 恢复原版后的安装摘要已标记 reverted：明细全撤完了，它不再算
            # "当前生效的改动"，历史页不该再显示（记录留着给分享页看）。
            if r.op == "pack.install" and r.meta.get("reverted"):
                continue
            if r.op == "undo":
                # 复原前那条改动照常显示：留痕里存着它的 op / 时间 / 展示字段。
                # 先回放再放复原行，倒序之后就是「复原在上、原改动在下」。
                # archived 现在是一张卡一份列表（每种 op 一条）；旧记录是单个 dict，
                # 两种都要能读。
                arch = r.meta.get("archived")
                if isinstance(arch, dict):
                    arch = [arch]
                if arch:
                    covered = set()
                    for i, a in enumerate(arch):
                        if not (isinstance(a, dict) and a.get("op")):
                            continue
                        if str(a["op"]).startswith("sound."):
                            continue       # 音效不算一条历史（2026-11-23 用户定稿）
                        show = dict(a.get("meta") or {})
                        show["kind"] = r.meta.get("kind") or ""
                        emit(f"archived-{r.id}-{i}", a["op"], r.card,
                             a.get("time") or r.time, show, archived=True)
                        covered.add(a["op"])
                    # 老留痕只存了一条摘要（以前一张卡只留最新那条），按 ops 把缺的
                    # 补出来：否则「视频特效 + 音效」一起撤时历史里只剩复原行。
                    # 音效不算一条历史，不补。缺的那条没有展示字段，文件数留空。
                    for op in r.meta.get("ops") or []:
                        if str(op).startswith("sound.") or op in covered:
                            continue
                        emit(f"archived-{r.id}-{op}", op, r.card, r.time - 1,
                             {"kind": r.meta.get("kind") or "", "files": 0}, archived=True)
                elif r.meta.get("ops"):
                    # 旧格式留痕（改动前生成）：只有 op 列表，没有时间与展示字段。
                    # 照样回放一条，时间取复原前一秒，老记录也能看到原改动。
                    # 音效不算一条历史，优先挑列表里最后一个非音效的 op。
                    ops = [o for o in r.meta["ops"] if not str(o).startswith("sound.")]
                    if not ops:
                        ops = list(r.meta["ops"])
                    show = {"kind": r.meta.get("kind") or "",
                            "files": r.meta.get("files") or 0}
                    emit(f"archived-{r.id}", ops[-1], r.card,
                         r.time - 1, show, archived=True)
            # 旧留痕里一张卡撤了两族（超框 + 特效）只写了一条 undo，kind 只能填一个，
            # 另一族的复原行在历史页看不到。按 ops 补出缺的那几条复原行；文件数只算在
            # 第一条上，其余留空（前端 files 为 0 时右侧不显示）。
            # 新记录本身一卡一族，拆出来只有一项，走下面的原路。
            if r.op == "undo":
                fams = []
                for op in r.meta.get("ops") or []:
                    f = _kind_of(op)
                    if f and f not in fams:
                        fams.append(f)
                if len(fams) > 1:
                    for i, f in enumerate(fams):
                        m = dict(r.meta)
                        m["kind"] = f
                        # 文件数只算一次，落在倒序后最上面那条（同一次复原是一批，
                        # 摊到每条上会重复计数）
                        if i < len(fams) - 1:
                            m.pop("files", None)
                        emit(f"{r.id}-{f}", "undo", r.card, r.time, m)
                    continue
            emit(r.id, r.op, r.card, r.time, r.meta)
        # 一次操作一行，不合并。同卡同族的多条记录只可能来自「已复原的历史」，
        # 它们各是一次独立的操作；折叠会把「已被撤销的」和「当前生效的」揉进一行
        #（2026-11-23 用户定稿：做什么显示什么）。
        recs.reverse()
        for row in recs:
            row.pop("_time", None)
        return {"records": recs, "count": len(recs)}

    # ------------------------------------------------------- 快捷复原的清单
    def history_restore_preview(self, picks=None) -> dict:
        """勾选列表要的那些行：只读 journal，不碰游戏文件。

        以前这一支挂在 history.restore_all（ACTIONS）下面，设置页每次读清单都会
        占住主任务槽：按钮被 OnMainBusy 灰一下再亮，其他页的写区跟着压暗，切页
        的时候生成按钮还会被挤成 Busy（2026-11-25 用户实测「快捷复原」闪三次）。
        清单是查，放 QUERY 里主进程直答。
        """
        from mdstudio.ops.tx import restore_pending

        ctx = self._ctx()
        data = restore_pending(ctx, picks=picks)
        # 勾选列表上要写卡名，前端没有卡库，这里补上
        try:
            db = self._names(ctx)
            for it in data.get("items") or []:
                it["name"] = db.name(int(it["card"])) or ""
        except Exception:
            pass
        return data

    # ------------------------------------------------------------------ 音效
    def sound_slots(self) -> dict:
        """游戏里所有发动特效用到的音效槽

        只有盘上有的槽能换声音: 全新 label 游戏不认, 换了也是静音.
        顺带给出每个槽被多少张卡用着, 换之前得让人看见影响面.
        """
        from mdstudio.core.gate import RunFxGate
        from mdstudio.ops import sound as S

        ctx = self._ctx()
        db = self._names(ctx)
        gate = RunFxGate(ctx.game)
        slots = {}
        for e in gate.entries:
            lb = e.get("seLabel")
            if not lb:
                continue
            s = slots.setdefault(lb, {"label": lb, "cards": set()})
            if e.get("mrk"):
                s["cards"].add(int(e["mrk"]))
        out = []
        for lb, s in sorted(slots.items()):
            cards = sorted(s["cards"])
            out.append({
                "label": lb,
                "on_disk": bool(S.on_disk(ctx.game, lb)),
                # 内置于 data.unity3d 的槽游戏里响得好好的, 只是没有独立文件可换,
                # 跟"文件没下载"是两回事, 界面上要说清楚.
                "builtin": lb in S.BUILTIN,
                "name": db.name(cards[0]) if cards else "",
                "cards": cards,
                "count": len(cards),
            })
        return {"slots": out, "count": len(out)}

    # ------------------------------------------------------------------ 方案
    def cutin_donors(self) -> dict:
        """界面上「源卡预设」下拉的内容。

        编号与 cutin.donor_for 共用同一张表（ops/cutin.py 的 DONORS）。
        以前界面自己抄了一份 2026-10-24 的旧编号，换表之后两边对不上（用户实测）。
        """
        from mdstudio.ops import cutin as C

        db = self._names(self._ctx())
        order = (("effect", "通常／效果"), ("fusion", "融合"), ("ritual", "仪式"),
                 ("synchro", "同步"), ("xyz", "超量"), ("link", "连接"))
        return {"donors": [{"mrk": C.DONORS[code], "label": label,
                            "name": db.name(C.DONORS[code]) or ""}
                           for code, label in order]}

    def cutin_donor(self, card: int) -> dict:
        """这张卡该用哪个来源卡（模板）。

        按卡片类型取，灵摆卡按第二个属性（灵摆／通常 算通常、融合／灵摆 算融合）。
        界面选卡时用它自动填来源卡框，用户还能改。
        """
        from mdstudio.ops import cutin as C

        ctx = self._ctx()
        try:
            st = self._index(ctx).status(int(card)) or {}
        except Exception:
            st = {}
        mrk = C.donor_for(st.get("subtype_cn") or "", st.get("coarse") or "")
        return {"mrk": mrk, "name": self._names(ctx).name(mrk) or ""}

    def schemes_list(self) -> dict:
        from mdstudio.gui import schemes as SCH
        return {"schemes": SCH.load()}

    # ------------------------------------------------------------------ 超框卡图
    def overframe_frames(self) -> dict:
        from mdstudio.ops import overframe as OF

        return {"frames": [{"id": n, "name": OF.frame_name(n)} for n in OF.frames()]}

    def overframe_status(self, card: int) -> dict:
        from mdstudio.ops import overframe as OF

        ctx = self._ctx()
        db = self._names(ctx)
        data = OF.status(ctx, card, db.frame_code(card))
        # real != 0 表示这个号是占位副本, 值是游戏真正渲染的号
        data["real"] = db.placeholder_of(card)
        data.update(self._flags(self._index(ctx), card))
        return data

    def overframe_preview(self, card: int, art=None, subject=None,
                          scale=1.0, dx=0, dy=0, lore_alpha=1.0,
                          effect="", holo_scope="") -> dict:
        """只出图，不碰游戏文件。界面拖动参数时高频调用，所以不进事务、不写 journal。"""
        from mdstudio.ops import overframe as OF

        ctx = self._ctx()
        idx = self._index(ctx)
        st = idx.status(int(card))
        if not idx.ok:
            from mdstudio.errors import MDStudioError
            raise MDStudioError("卡数据无法解析，已禁用写入：" + idx.error)
        if st["unknown_subtype"]:
            from mdstudio.errors import MDStudioError
            raise MDStudioError("游戏出现未知卡型 %s，卡框未确认，已拒绝合成"
                                % st["subtype"])
        # 卡框要按游戏当前数据判，不能靠静态表：新卡不在表里就会被画成效果怪兽
        code = self._names(ctx).frame_code(int(card))
        if art is None:
            art = str(OF.base_art(ctx, int(card), code))
        out = OF.compose(int(card), art=art, subject=subject,
                         scale=scale, dx=dx, dy=dy, lore_alpha=lore_alpha,
                         effect=effect or OF.EFFECT_PRISMATIC, code=code, ctx=ctx)
        return {"path": str(out)}

    def overframe_layout(self, card: int) -> dict:
        """给前端本地绘制预览用：卡框图路径 + 插图窗几何 + 底色卡图路径。

        如果这张卡之前应用过，返回上次用的素材与参数（存在 edits.json 里），
        界面据此自动加载，方便继续调整；否则用游戏当前卡图当底。
        """
        import os
        from mdstudio.ops import overframe as OF

        ctx = self._ctx()
        code = self._names(ctx).frame_code(int(card))
        nn = OF.frame_of_card(int(card), code)
        ax, ay, aw, ah = OF.art_box(nn)
        lx, ly, lw, lh = OF.lore_rect(nn)

        edit = OF.load_edit(ctx, int(card)) or {}
        art = edit.get("art") or ""
        if art and os.path.isfile(art):
            base = art
            has_edit = True
            fullbleed = False
        else:
            has_edit = False
            # 满画幅超框卡：插画本来就是整张卡面，没有卡名栏。
            # 预览直接显示原图，别塞进标准卡框的插图窗，否则看到的只是被放大的局部。
            _, imeta = OF.read_illust(ctx, int(card))
            fullbleed = bool(imeta.get("overframe")
                             and int(imeta.get("width") or 0) == OF.RASTER_W
                             and int(imeta.get("height") or 0) == OF.CARD_H
                             and int(nn) not in OF.PENDULUM)
            try:
                base = str(OF.base_art(ctx, int(card), code, raw=fullbleed))
            except Exception:
                base = ""

        # 效果：老记录可能存的是旧键名，统一规范化
        effect = OF.normalize_effect(edit.get("effect"))

        subj = edit.get("subject") or ""

        # 连接怪兽：额外给两张箭头层，界面自己叠（点亮层不透明、未点亮层跟着
        # 效果框透明度一起淡出）。写进游戏的整卡贴图是同一套逻辑合成的。
        link = {"markers": 0, "lit": "", "dim": ""}
        if nn == OF.LINK_FRAME:
            try:
                mk = OF.markers_of_card(int(card))
                if mk:
                    lp, dp = OF.link_arrow_split_png(
                        nn, runtime.data_dir() / "overframe" / "link", effect, mk)
                    link = {"markers": mk, "lit": str(lp), "dim": str(dp)}
            except Exception:
                pass

        # 两套卡框图 + 三张外沿素材都给，界面切效果时不用再跑一趟
        return {
            "frame": nn,
            "link": link,
            "frame_path": str(OF.frame_path(nn, effect)),
            "frame_path_prismatic": str(OF.frame_path(nn, OF.EFFECT_PRISMATIC)),
            "frame_path_solid": str(OF.frame_path(nn, OF.EFFECT_KEEP)),
            "border_rainbow_path": str(runtime.bundle_root() / OF.BORDER_RAINBOW_FILE),
            "border_glory_path": str(runtime.bundle_root() / OF.BORDER_GLORY_FILE),
            "border_official_path": str(runtime.bundle_root() / OF.BORDER_OFFICIAL_FILE),
            "ax": ax, "ay": ay, "aw": aw, "ah": ah,
            "lore_top": OF.lore_top(nn),
            "lore_x": lx, "lore_y": ly, "lore_w": lw, "lore_h": lh,
            "lore_bottom": ly + lh,
            "card_w": OF.CARD_W, "card_h": OF.CARD_H,
            "overflow_scale": OF.OVERFLOW_SCALE,
            "base_art": base,
            # 底图原始尺寸给前端：界面据此决定完整显示还是裁切填满
            "base_art_w": _art_size(base)[0],
            "base_art_h": _art_size(base)[1],
            # 满画幅超框卡：界面把插画铺满整张卡，不叠卡框和文字栏
            "fullbleed": fullbleed,
            "has_edit": has_edit,
            "edit_art_name": edit.get("art_name", "") if has_edit else "",
            "edit_subject": subj if (subj and os.path.isfile(subj)) else "",
            "edit_subject_name": edit.get("subject_name", "") if has_edit else "",
            "edit_scale": float(edit.get("scale", 1.0)) if has_edit else 1.0,
            "edit_dx": int(edit.get("dx", 0)) if has_edit else 0,
            "edit_dy": int(edit.get("dy", 0)) if has_edit else 0,
            "edit_lore_alpha": float(edit.get("lore_alpha", 1.0)) if has_edit else 1.0,
            "edit_effect": effect if has_edit else OF.EFFECT_PRISMATIC,
        }

    def overframe_gate(self) -> dict:
        """登记成超框的卡号：官方（游戏本来就有）+ 自改（本工具加的）。

        给左列表打标签用：一次读一张登记表就能覆盖整张列表，
        不必逐卡去游戏目录里读纹理，否则 200 张卡的搜索结果要发 200 次请求。

        官方与自改分开返回：一张官方超框卡被自改之后两个标签都要留着，
        撤销自改则只去掉「自改」。
        """
        from mdstudio.ops import overframe as OF

        ctx = self._ctx()
        official = OF.official_registered(ctx)
        mine = OF.ours(ctx)
        return {"cards": sorted(official | mine),
                "official": sorted(official),
                "ours": sorted(mine)}

    def overframe_current(self, card: int) -> dict:
        """把游戏里这张卡现在的图导出来给界面看，用来判是否已经超框"""
        from mdstudio.ops import overframe as OF

        ctx = self._ctx()
        img, meta = OF.read_illust(ctx, card)
        if img is None:
            return {"exists": False, "error": meta.get("error")}
        out = OF._default_out(card).with_name(f"{int(card)}_game.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out)
        return {"exists": True, "path": str(out),
                "width": meta["width"], "height": meta["height"],
                "overframe": meta["overframe"]}

    # ------------------------------------------------------------------ Rush Duel


    def _base_art_cached(self, ctx, card: int) -> str:
        """兜底卡图（游戏当前插图）解一次就缓存，拖动主体时不再反复读游戏文件。"""
        cache = getattr(self, "_rd_base_cache", None)
        if cache is None:
            cache = {}
            self._rd_base_cache = cache
        p = cache.get(card)
        if p and Path(p).is_file():
            return p
        from mdstudio.ops import overframe as OF

        p = str(OF.base_art(ctx, int(card), self._names(ctx).frame_code(int(card))))
        cache[card] = p
        return p




    # ------------------------------------------------------------------ 全局外观



    # ------------------------------------------------------------------ 壁纸
    def wallpaper_status(self) -> dict:
        """大厅壁纸 / 商店背景有没有被我们换过。"""
        from mdstudio.ops import wallpaper as WP

        return WP.status(self._ctx())

    def wallpaper_preview(self, which: str) -> dict:
        """导出游戏里当前那张纹理，给界面看。"""
        from mdstudio.ops import wallpaper as WP

        return WP.preview(self._ctx(), which)

    def wallpaper_previews(self, which: str) -> list:
        """导出这一类下的全部纹理（商店背景有 6 个包）。"""
        from mdstudio.ops import wallpaper as WP

        return WP.previews(self._ctx(), which)

    # ------------------------------------------------------------------ 设置
    def settings_get(self, force_detect=False) -> dict:
        """游戏目录与账号. 没存过就试着自动找一份.

        已经存过且目录还在时**不再探测**——探测要读注册表、解析 Steam 清单、
        摸各个盘符，在挂了网络盘的机器上会卡好几秒。第一次找到后直接落盘，
        之后每次进设置页都是读文件，不再重扫。
        """
        from mdstudio.core.game import detect_game_dir, list_accounts

        saved = settings.load()
        saved_dir = saved.get("game_dir") or ""
        usable = bool(saved_dir) and (Path(saved_dir) / "LocalData").is_dir()
        gdir = saved_dir if usable else ""
        detected = ""
        # 存过且还在、又不是手动重检 → 直接信它，省掉这次探测
        if gdir and not force_detect:
            detected = gdir
        else:
            try:
                detected = str(detect_game_dir(None, force=force_detect))
            except Exception:
                detected = ""
            if detected and detected != saved_dir:
                # 第一次找到（或旧目录失效）就记下来，别每次开都重扫
                settings.save(game_dir=detected)
            gdir = detected or gdir

        accounts = list_accounts(gdir) if gdir else []
        saved_acc = saved.get("account") or ""
        acc = saved_acc
        missing = False
        if saved_acc and not settings.valid_account(gdir, saved_acc):
            # 存过、但那个账号目录现在不在（Steam 更新期间很常见）。这里**不能**
            # 改选另一个账号：一改就会落盘，从此永久指向错的副本，历史也跟着换。
            # 保持原值 + 报告缺失，界面提示「暂时不可用」，目录回来自然恢复。
            missing = True
        elif not saved_acc and accounts:
            # 从来没选过：按目录名取第一个并落盘。以前只把 accounts[0] 放进返回值、
            # 不写配置，界面显示着「已选」，而 Ctx.open 每次读盘都重新猜一个。
            # 实测后果：历史页与分享页突然清空，重启又恢复（2026-11-12）。
            acc = accounts[0]
            settings.save(account=acc)

        return {
            "game_dir": gdir,
            "detected": detected,
            "account": acc,
            "account_ok": bool(acc) and settings.valid_account(gdir, acc),
            "account_missing": missing,
            "accounts": [{"id": a, "label": settings.label(a)} for a in accounts],
            # 生成召唤动画时要不要连低清贴图一起重写。存在配置里，引擎生成时自己读，
            # 界面不需要在每次生成时往下传（传了也是同一个值，还可能传旧）。
            "compat_sd": bool(saved.get("compat_sd", False)),
        }

    def app_ready(self, check_game: bool = False) -> dict:
        """写游戏之前必须先满足的几项（安装整合包用它做闸门）。

        全是廉价判断：读配置、看目录在不在、（需要时）看一眼游戏进程。
        **故意不查卡索引和超框登记表**：那两样要几秒到几十秒，在这里查会把
        主循环占住，反而制造出「点了没反应」。它们慢，但只会慢，不会写错。
        """
        from mdstudio.core.game import is_running, list_accounts

        saved = settings.load()
        gdir = saved.get("game_dir") or ""
        game_ok = bool(gdir) and (Path(gdir) / "LocalData").is_dir()
        accounts = list_accounts(gdir) if game_ok else []
        acc = saved.get("account") or ""
        acc_ok = bool(acc) and settings.valid_account(gdir, acc)

        blockers = []
        if not game_ok:
            blockers.append("game_dir")
        elif not acc_ok:
            blockers.append("account_missing" if acc else "account")
        running = bool(is_running()) if check_game else False
        if running:
            blockers.append("game_running")

        return {"game_dir": gdir, "game_dir_ok": game_ok,
                "account": acc, "account_ok": acc_ok,
                "accounts": len(accounts), "game_running": running,
                "blockers": blockers}

    def cards_refresh(self) -> dict:
        """手动重新同步：无视指纹重读游戏数据并重建索引。"""
        from mdstudio.core.cardindex import CardIndex, clear
        ctx = self._ctx()
        clear()
        idx = CardIndex.ensure(ctx.game, force=True)
        self.reset()
        return self.cards_index_info()

    def cards_index_info(self) -> dict:
        """设置页/诊断用：索引是不是新的，有没有表外卡型，警告日志在哪。"""
        from mdstudio import progress
        from mdstudio.i18n import tr

        # 先报一句再 import：下面那句 `from ... import cardindex` 本来会连带把 UnityPy
        # 拉进来。index 那边已经改成惰性导入，所以这里既快又不会冤枉花掉一秒。
        progress.step(tr("preparing the card index"))

        from mdstudio.core.journal import home_dir
        from mdstudio.core.cardindex import WARN_FILE

        idx = self._index(self._ctx())
        return {"ok": idx.ok, "error": idx.error,
                "cards": len(idx.cards), "visible": len(idx.visible),
                "built_at": idx.built_at, "lang": idx.lang,
                "unparsed_sources": list(idx.unparsed_sources),
                "unknown_subtypes": sorted(idx.unknown_subtypes),
                "warning_log": str(home_dir() / WARN_FILE)}

    def settings_set(self, game_dir=None, account=None, name=None, compat_sd=None) -> dict:
        # 只有换了目录或账号才丢掉缓存：卡名表、卡索引、列表状态全是按账号算的。
        # 单独改备注名不该触发这些重算。以前这里无条件 reset()，实测改一个别名要
        # 白读 0.3 秒索引 + 1.5 秒列表状态，界面上就是「只是加个别名，下面的卡牌
        # 数据却刷了一次」（2026-11-12 用户发现）。
        before = settings.load()
        # 有动作在跑时不许换账号或换目录。那个动作已经在子进程里按旧账号解析好了
        # 写入路径，中途换号会让记录、备份和实际写入落到两个账号上，界面也跟着跳。
        # 备注名不受限制：它不影响任何路径。
        switching = game_dir is not None or (account is not None and account != before.get("account"))
        jobs = getattr(self, "jobs", None)
        if switching and jobs is not None and jobs.busy():
            from mdstudio.errors import MDStudioError
            from mdstudio.i18n import tr
            raise MDStudioError(tr("a task is still running. Wait for it to finish, then switch account."))
        if game_dir is not None:
            # 选错目录以前是直接存下来的，之后每个功能都会报「找不到游戏文件」，
            # 用户还得自己想到再点一次「更改…」才能救回来。这里先验后存。
            from mdstudio import probe
            from mdstudio.errors import MDStudioError

            r = probe.check(game_dir, "game_dir")
            if not r["ok"]:
                raise MDStudioError(r["reason"])
            settings.save(game_dir=game_dir)
        if account is not None:
            settings.save(account=account)
        if name is not None and account:
            settings.set_account_name(account, name)
        # 三态: None = 这次没提这件事，别去覆盖存着的值
        if compat_sd is not None:
            settings.save(compat_sd=bool(compat_sd))

        after = settings.load()
        if (before.get("game_dir"), before.get("account")) != \
                (after.get("game_dir"), after.get("account")):
            self.reset()
        return self.settings_get()


def _timings(be, card: int):
    from .actions import runfx_timings
    return runfx_timings(be._ctx(), card)


def _dir_mb(p) -> float:
    """目录占多少 MB。读不到的文件跳过，不为了一个数字报错。"""
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / (1024.0 * 1024.0)


def _sound_presets_list():
    from mdstudio.ops import sound as S
    rows = S.list_presets()
    return {"presets": rows, "count": len(rows)}


def _sound_presets_save(name, audio_path):
    """预设只是素材库里的文件操作。放主进程直答, 走 ACTIONS 会 spawn 子进程,
    打包后子进程 argv[0] 是 worker 模块路径, app_root() 会指错, data 落错地方。
    项目里 settings.set 与 cards.refresh 也是同样的写法。"""
    from mdstudio.ops import sound as S
    return S.save_preset(name, audio_path)


def _sound_presets_delete(name):
    from mdstudio.ops import sound as S
    return {"deleted": bool(S.delete_preset(name))}


def _sound_track(path):
    from mdstudio.ops import sound as S
    return S.extract_video_track(path)


def _assets_needs(kind="", source=None, sharp=False):
    """组件预检。跟 assets.status 一样在主进程直答。

    放在 ACTIONS 里每次会现 spawn 一个 Python，实测光 import 就要 1.07 秒，
    视频素材还要再 import OpenCV；界面点「生成」到弹确认框之间只做这一件事，
    用户就以为没点上（2026-11-19 用户实测）。
    """
    from . import actions
    return {"missing": actions.missing_assets(kind, source, sharp)}


def _cutin_precheck(kind="cutin.video", source=None, n=0, cell=0, compat_sd=None, sharp=False,
                    max_pages=2):
    from . import actions
    return actions.cutin_precheck(kind=kind, source=source, n=n, cell=cell,
                                  compat_sd=compat_sd, sharp=sharp, max_pages=max_pages)


QUERY = {
    "ping": lambda be, **kw: {"pong": True},
    "info": lambda be, **kw: be.info(),
    # 设置页「关于」那四个数与 30 天热力图。只读查询，别排进任务队列：
    # 生成动画跑着的时候设置页也得能显示（走 ACTIONS 会被挤成 Busy）。
    "stats": lambda be, **kw: be.stats(),
    "cards.status": lambda be, **kw: be.status(),
    "cards.search": lambda be, q="", limit=200, cutin="", runfx="", has_anim=True, kind="",
                              no_plain=False, art="", **kw:
        be.search(q, limit, cutin, runfx, has_anim, kind, no_plain, art),
    "runfx.donors": lambda be, **kw: be.runfx_donors(),
    # 音效状态是纯只读查询，走 ACTIONS 会排进任务队列，还可能被别的任务挤成 Busy。
    # 放到这里直答：冷启动 1 秒（定位游戏数据），之后 3 毫秒。
    "runfx.timings": lambda be, card=0, **kw: _timings(be, int(card)),
    "cards.refresh": lambda be, **kw: be.cards_refresh(),
    "cards.index_info": lambda be, **kw: be.cards_index_info(),
    "settings.get": lambda be, **kw: be.settings_get(),
    # 安装整合包前的闸门检查。check_game 会多看一次游戏进程（tasklist 约 50ms），
    # 只在真要写盘时打开；页面级的状态判断用默认的廉价版。
    "app.ready": lambda be, check_game=False, **kw: be.app_ready(check_game),
    # 组件清单只是查几个文件在不在，不需要游戏上下文，也不该起子进程。
    # 走 ACTIONS 那条路每次要现 spawn 一个 Python（实测 265ms），设置页每次都
    # 因此先空一块再「啪」地冒出来。放这里在主进程里答，实测 1ms。
    "assets.status": lambda be, **kw: _assets_status(),
    # 组件预检与生成前检查：都是只看文件与素材、不碰游戏目录的活，主进程直答。
    "assets.needs": lambda be, kind="", source=None, sharp=False, **kw:
        _assets_needs(kind, source, sharp),
    "cutin.precheck": lambda be, kind="cutin.video", source=None, n=0, cell=0,
        compat_sd=None, sharp=False, **kw:
        _cutin_precheck(kind, source, n, cell, compat_sd, sharp),
    "probe.file": lambda be, path="", want="image", **kw: _probe_file(path, want),
    "settings.set": lambda be, **kw: be.settings_set(**kw),
    "pack.list": lambda be, **kw: be.pack_list(),
    # 预览缓存只是列目录与读一个 json，不碰游戏目录，主进程直答。
    "preview.list": lambda be, kind=None, **kw: be.preview_list(kind),
    "preview.meta": lambda be, card=0, kind="cutin", **kw: be.preview_meta(card, kind),
    "preview.drop": lambda be, card=0, kind="cutin", **kw: be.preview_drop(card, kind),
    "preview.sound": lambda be, card=0, **kw: be.preview_sound(card),
    "pack.installed": lambda be, **kw: be.pack_installed(),
    "pack.inspect": lambda be, pack_path, **kw: be.pack_inspect(pack_path),
    "history.list": lambda be, **kw: be.history_list(),
    # 快捷复原的勾选清单：只读 journal，放这里主进程直答。挂在 history.restore_all
    # （ACTIONS）下面时，读一次清单就占住主任务槽，设置页按钮会灰一下再亮。
    "history.restore_preview": lambda be, picks=None, **kw:
        be.history_restore_preview(picks),
    "sound.slots": lambda be, **kw: be.sound_slots(),
    # 预设列表跟 sound.slots 一样是纯只读查询。走 ACTIONS 会在生成特效期间
    # 被挤成 Busy, 弹窗里就一条预设都不显示。时长已经缓存, 直答约 3ms。
    "sound.presets": lambda be, **kw: _sound_presets_list(),
    "sound.presets.save": lambda be, name="", audio_path="", **kw:
        _sound_presets_save(name, audio_path),
    "sound.presets.delete": lambda be, name="", **kw: _sound_presets_delete(name),
    # 抽视频音轨只是 ffmpeg 转码，不需要游戏上下文，也不该被长任务挤成 Busy。
    "sound.track": lambda be, path="", **kw: {"path": _sound_track(path)},
    "schemes.list": lambda be, **kw: be.schemes_list(),
    "overframe.frames": lambda be, **kw: be.overframe_frames(),
    "overframe.status": lambda be, card, **kw: be.overframe_status(card),
    "overframe.current": lambda be, card, **kw: be.overframe_current(card),
    "overframe.gate": lambda be, **kw: be.overframe_gate(),
    "overframe.preview": lambda be, card, art=None, subject=None,
        scale=1.0, dx=0, dy=0, lore_alpha=1.0, effect="", holo_scope="", **kw:
        be.overframe_preview(card, art=art, subject=subject,
                             scale=scale, dx=dx, dy=dy, lore_alpha=lore_alpha,
                             effect=effect, holo_scope=holo_scope),
    "overframe.layout": lambda be, card, **kw: be.overframe_layout(card),
    "cutin.donor": lambda be, card, **kw: be.cutin_donor(card),
    "cutin.donors": lambda be, **kw: be.cutin_donors(),
    "wallpaper.status": lambda be, **kw: be.wallpaper_status(),
    "wallpaper.preview": lambda be, which="lobby", **kw: be.wallpaper_preview(which),
    "wallpaper.previews": lambda be, which="shop", **kw: be.wallpaper_previews(which),
}
