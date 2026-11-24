"""召唤 cut-in: 借用 / 克隆 / 卸载 / 换皮"""
import logging
import re
from pathlib import Path

import UnityPy
from PIL import Image

from mdstudio.core.bundle import (bundle_cab, fix_clip_duration_mode, new_cab, rename_pages,
                                  replace_texture, retag, retag_str, textures)
from mdstudio.core.crc import get_crc
from mdstudio.errors import AssetMissing, MDStudioError, NotOurs
from mdstudio.i18n import tr
from mdstudio.ops.tx import Tx, undo

log = logging.getLogger("mdstudio")

SD, HD = "SD", "HighEnd_HD"
QUALITIES = (SD, HD)
DEFAULT_DONOR = 11016   # 超量档的来源卡；其它调用方应该走 donor_for() 按类型取
DONOR_BASE_SCALE = 0.20  # 来源卡 Spine 物体的标准缩放，换算见 donor_scale()

# 七档来源卡（2026-11-12 全量扫描 525 张官方召唤动画后按「骨骼少、部件少」挑的）。
# 全部要求 SD 与 HD 都至少两页：两页是底盘上限（材质 P<卡>_P<卡>_2 只有两个贴图槽）。
# 来源卡（模板卡）：2026-11-16 重选，全部为真实 2 页 HD、骨骼最少的卡。
# 只有 6 个实际档位，normal/effect 共用一张（界面显示「通常／效果」）。
# 筛选口径：atlas 声明 2 页 + 骨骼最少 + 克隆预演通过。
DONORS = {
    # 六个卡型各一张，2026-11-19 用户逐档确认。挑选口径：官方召唤动画、HD 正好 2 页 2 槽、
    # 不在强力召唤表、Timeline 能定位、prefab 里读得到 Spine 物体缩放、camera2DSize = 5.0。
    # 这六张在界面上被锁住，不允许改动画：它们是无动画卡的克隆底盘，结构被改过之后，
    # 之后从它们克隆出来的卡会继承漂移的页数／材质槽。
    "normal": 3880,     # 通常／效果（含衍生物、灵摆通常/效果）共用：黑魔导
    "effect": 3880,
    "fusion": 3435,     # 融合：龙女仆 总管龙女
    "ritual": 15640,    # 仪式：仪龙机巧 降星：天龙
    "synchro": 8121,    # 同调：霞之谷雷神鬼
    "xyz": 12328,       # 超量：电子光虫 总线独角仙
    "link": 13088,      # 连接：星杯剑士 阿乌拉姆
}


def donor_cards() -> set:
    """当前配置里所有来源卡的卡号（normal/effect 共用一张，去重后是 6 张）。"""
    return set(DONORS.values())


def is_donor_card(card: int) -> bool:
    """这张卡是不是来源卡。

    来源卡是无动画卡的克隆底盘，它的页数与材质槽数会被后来的卡继承。所以它的动画
    不允许改：改过之后，从它克隆出来的卡会跟着漂到不匹配的结构（2026-11-19 用户定稿，
    不做开关，没有例外。复原不受限制，复原只会把卡还原）。
    """
    return int(card) in donor_cards()


# 判定顺序就是子类型里的机制链，灵摆卡靠它落到对应档
_SECOND = (("融合", "fusion"), ("仪式", "ritual"), ("同调", "synchro"),
           ("超量", "xyz"), ("连接", "link"))


def donor_for(subtype_cn: str = "", coarse: str = "") -> int:
    """按卡片类型取来源卡。

    判定顺序：融合 → 仪式 → 同调 → 超量 → 连接，然后 通常（含衍生物），
    其余（效果、协调、翻转、灵魂、特殊召唤）归效果档。
    灵摆卡不用单独判：数据里每张灵摆卡都带第二个属性（超量／灵摆／效果 之类），
    去掉灵摆之后自然落进上面某一档。coarse 参数只为兼容调用方保留。
    """
    s = subtype_cn or ""
    for cn, code in _SECOND:
        if cn in s:
            return DONORS[code]
    if "通常" in s or "衍生物" in s:
        return DONORS["normal"]
    return DONORS["effect"]


_DONOR_SCALE_CACHE = {}


def chassis_donor(ctx, card: int) -> int:
    """这张卡现在这套底盘是从哪张卡克隆来的（0 = 没有记录）。

    历史里显示来源卡用。复原之后记录没了，返回值也就是 0。
    """
    for r in _ours(ctx, int(card)):
        try:
            d = int(r.meta.get("donor") or 0)
        except (TypeError, ValueError):
            d = 0
        # 自己的卡号不算底盘（官方卡是"改本身"）
        if d and d != int(card):
            return d
    return 0


def donor_scale(game, donor: int) -> float:
    """来源卡 prefab 里 Spine 物体（SpineP<卡> / Spine P<卡> / SpinP<卡>）的 localScale。

    同一个「角色尺寸」在不同来源卡上占屏大小不一样，就差在这个值上：
    每尺寸单位多少屏幕像素 = 屏宽/5000（七张来源卡的 prefab 相机都是 camera2DSize 5.0,
    只有物体缩放不同，0.13 到 0.22）。读不出来按 0.20 算（多数卡是这个值）。
    """
    d = int(donor)
    if d in _DONOR_SCALE_CACHE:
        return _DONOR_SCALE_CACHE[d]
    val = DONOR_BASE_SCALE
    try:
        p = game.find(get_crc(prefab(d, SD)))
        if p:
            env = UnityPy.load(p.read_bytes())
            names = {}
            for o in env.objects:
                if o.type.name == "GameObject":
                    try:
                        names[o.path_id] = o.read().m_Name
                    except Exception:
                        pass
            for o in env.objects:
                if o.type.name != "Transform":
                    continue
                try:
                    tt = o.read_typetree()
                except Exception:
                    continue
                nm = names.get(tt.get("m_GameObject", {}).get("m_PathID"), "")
                # 结尾不能只认纯数字：12328/7698 这类卡的物体叫「Spine P12328JS」，
                # 多一个 JS 后缀。漏掉它们就退回 0.20 兜底，而真实缩放是 0.25，
                # 克隆出来的角色会大约偏大 25%（2026-11-19 统计来源卡时发现）。
                if re.match(r"^spin\w*\s*p\d+\w*$", nm.strip(), re.I):
                    x = (tt.get("m_LocalScale") or {}).get("x")
                    if x:
                        val = float(x)
                    break
    except Exception:
        pass
    _DONOR_SCALE_CACHE[d] = val
    return val

# "cutin.borrow" 是已下线的旧功能（2026-11-12 清理），保留在 OPS 里只为让老记录还能识别与撤销
OPS = ("cutin.borrow", "cutin.clone", "cutin.video", "cutin.reskin", "cutin.spine", "pack.cutin")

# 「生成类」记录：这几条一旦存在，这张卡上就不许再生成，必须先复原（2026-11-15 用户定稿）。
# 不含 cutin.reskin：换贴图本来就是生成之后才做的下一步，不能跟着一起拦。
GEN_OPS = ("cutin.borrow", "cutin.clone", "cutin.video", "cutin.spine")


def generated(ctx, card: int) -> bool:
    """这张卡上有没有本工具做的、还没复原的生成类改动。"""
    return bool(ctx.journal.for_card(int(card), GEN_OPS))


def base(mrk: int) -> str:
    return f"Duel/Timeline/Duel/MonsterCutIn/tcg/P{mrk}"


def prefab(mrk: int, q: str) -> str:
    # 游戏按 GameObject 名找 prefab, 名字必须正好是 P<mrk>
    return f"{base(mrk)}/{q}/P{mrk}"


def closure(cat, mrk: int) -> set:
    """SD+HD prefab + 全部依赖. 分享时必须整包带上"""
    out = set()
    for q in QUALITIES:
        p = prefab(mrk, q)
        if p in cat:
            out.add(p)
            out |= set(cat.deps(p))
    return out


def own_paths(cat, mrk: int) -> list:
    """只属于这张卡的 (路径里有 P<mrk>), 共享的舞台 P13571 之类不算"""
    pat = re.compile(rf"/P{mrk}(?!\d)")
    return sorted(p for p in closure(cat, mrk) if pat.search(p))


def scale_dirs(cat, mrk: int) -> dict:
    """{quality: scale 目录}, 每张卡不一样 (0.5/1, 0.45/0.9 ...), 别写死"""
    pre = base(mrk) + "/"
    out = {}
    # 坑: 贴图子 bundle 不一定有自己的 manifest 条目 (克隆出来的就没有), 只出现在 prefab 的依赖里
    names = list(closure(cat, mrk)) or list(cat.names())
    for n in names:
        if n.startswith(pre) and n.endswith(f"/P{mrk}"):
            seg = n[len(pre):].split("/")
            if len(seg) == 3:
                out[seg[0]] = seg[1]
    return out


def page_tails(cat, mrk: int, quality: str = HD) -> list:
    """这张卡某档画质下自己的页贴图名（第 1 页在前）。

    只认落在这张卡自己目录里、且名字正好是 P<卡> / P<卡>_n / P<卡>n 的条目。
    必须按目录归属判：P19172 既是卡 19172 的第一页、又长得像卡 1917 的第二页，
    光比文件名分不出来（全库有 169 个这种 P<基号>2，其中 52 个是真卡号）。
    """
    d = scale_dirs(cat, mrk).get(quality)
    if not d:
        # 名字对不上卡号的卡（#3898 存的是 P3893）连缩放目录都读不到，先从依赖里反查
        a = asset_paths(cat, mrk, quality)
        if a:
            pre2 = f"{base(mrk)}/{quality}/"
            return [n[len(pre2):] for n in a["pages"]]
        return []
    pre = f"{base(mrk)}/{quality}/{d}/"
    pat = re.compile(rf"P{mrk}(?:_(\d+)|(\d+))?")
    hit = []
    # 只在这张卡自己的依赖闭包里找（约 20 条），别扫全库 17k 条：
    # donor_with_pages 会连着查十几张卡，全库扫描单次要 1.4ms，叠起来 300ms+。
    for name in (closure(cat, mrk) or cat.names()):
        if not name.startswith(pre):
            continue
        tail = name[len(pre):]
        if "/" in tail:
            continue
        m = pat.fullmatch(tail)
        if not m:
            continue
        hit.append((int(m.group(1) or m.group(2) or 1), tail))
    if not hit:
        # 名字对不上卡号的卡（#3898 存的是 P3893）：从 prefab 依赖里反查到手
        a = asset_paths(cat, mrk, quality)
        if a:
            pre2 = f"{base(mrk)}/{quality}/"
            return [n[len(pre2):] for n in a["pages"]]
    return [t for _, t in sorted(hit)]


def asset_paths(cat, mrk: int, quality: str = HD):
    """这张卡某一档的整套资源路径，从 prefab 的依赖里反查。

    不能按 P<卡> 拼：#3898 在自己目录下存了整套名字叫 P3893 的资源（抄 3893 那套
    来的时候没改名），prefab 的依赖指向它们。按卡号拼会一个都找不到，页数算成 0，
    生成时这一档被整个跳过（静默无效）。全库有 27 张是这种写法。

    返回 {prefab, atlas, atlas_asset, skeleton, pages, scale}，没有可替换的资源时回 None。
    """
    b = base(mrk)
    mark = f"/{quality}/"
    # 官方有 12 张的 prefab 名字写歪了（尾部带空格、多一层缩放目录），按字面拼查不到
    # 依赖，这一档就被静默跳过，写出来「有动画没画面」。另外同一张卡可能既有主 prefab
    # 又有挂图集的 prefab，所以按候选顺序试，第一个能查到本档图集的才算数。
    prefab_lp = deps = atlas_asset = None
    for cand in cat.prefab_candidates(b, quality, mrk):
        cd = cat.deps(cand) or []
        ca = next((d for d in cd if d.startswith(b + "/") and mark in d
                   and d.endswith("_Atlas")), None)
        if ca:
            prefab_lp, deps, atlas_asset = cand, cd, ca
            break
    if not deps:
        return None
    stem = atlas_asset[: -len("_Atlas")]
    rest = stem[len(f"{b}/{quality}/"):]
    scale = rest.rsplit("/", 1)[0] if "/" in rest else ""
    # 页贴图三种写法都从 stem 往后接：P<卡> / P<卡>_2 / P<卡>2
    pages = []
    for n in deps:
        if not n.startswith(stem):
            continue
        tail = n[len(stem):]
        if not tail:
            pages.append((0, n))
        elif tail.startswith("_") and tail[1:].isdigit():
            pages.append((int(tail[1:]), n))
        elif tail.isdigit():
            pages.append((int(tail), n))
    # 骨架优先取本档那一份：deps 里 SD 与 HD 各有一个 P<卡>JS，取第一个会拿到 SD 的，
    # 写进去的骨架和 HD 的 atlas/贴图就对不上（#19148 实测）
    skel = (next((d for d in deps if d.startswith(b + "/") and f"/{quality}/" in d
                  and d.endswith("JS") and not d.endswith("JS_SkeletonData")), None)
            or next((d for d in deps if d.startswith(b + "/") and d.endswith("JS")
                     and not d.endswith("JS_SkeletonData")), None))
    return {"prefab": prefab_lp, "atlas": stem + ".atlas", "atlas_asset": atlas_asset,
            "skeleton": skel, "pages": [n for _, n in sorted(pages)], "scale": scale}


def chassis_pages(cat, mrk: int, quality: str = HD) -> int:
    """这张卡某档画质的底盘有几页贴图。

    页数由 P<卡>_Atlas 里的材质槽数量决定，槽烘在资源里加不出来，
    所以页数不够时只能换底盘克隆（见 cutin_flip.apply）。

    官方页名不统一：P3881_2 带下划线，P156402 不带，两种都要认（实测 15 张代表卡
    与 atlas 声明的页数完全一致）。以前只认 P<卡>_2，仪式 15640、同调 7698、
    连接 13508 被当成单页卡，要 2 页时静默换成了 3881 的底盘。
    """
    return len(page_tails(cat, mrk, quality))


def page_rename_map(cat, donor: int, target: int) -> dict:
    """{旧页名: 新页名}，只收 retag_str 改不到的那种写法。

    第 1 页是 P<卡>，第 2 页起官方有两种写法：P3881_2 带下划线、
    P156402 不带。带下划线的 retag_str 本来就改得到；不带下划线的被
    (?!\\d) 挡住，克隆后会以旧名留在新卡目录里，写第二页时按
    P<target>_2 找不到文件。这里只给后者建映射。
    """
    out = {}
    for q in QUALITIES:
        for n, tail in enumerate(page_tails(cat, donor, q), start=1):
            if n == 1 or tail == f"P{donor}_{n}":
                continue
            out[f"{donor}{n}"] = f"{target}_{n}"
    return out


def default_donor(ctx, card: int) -> int:
    """按这张卡的卡种取来源卡（灵摆按第二个属性）。"""
    from mdstudio.core.cardindex import CardIndex
    try:
        st = CardIndex.ensure(ctx.game).status(int(card)) or {}
    except Exception:
        st = {}
    return donor_for(st.get("subtype_cn") or "", st.get("coarse") or "")


def donor_with_pages(ctx, cat, card: int, pages: int, qualities) -> int:
    """找一张在指定画质下页数够的来源卡。七张来源卡都是双页，正常一次就中。"""
    first = default_donor(ctx, card)
    if first and all(chassis_pages(cat, first, q) >= pages for q in qualities):
        return first
    for cand in DONORS.values():
        if all(chassis_pages(cat, cand, q) >= pages for q in qualities):
            return cand
    return 0


def missing(cat, mrk: int) -> list:
    return sorted({d for q in QUALITIES for d in cat.missing(prefab(mrk, q))})


def _ours(ctx, mrk):
    return ctx.journal.for_card(mrk, OPS)


def _check(tx, target: int, donor: int, force: bool):
    cat, gate = tx.catalog, tx.cutin_gate
    # 目标卡和来源卡不能是同一张：没有官方动画的卡「自己克隆自己」没有意义。
    # 有官方动画的卡以前允许这么传（老方案：先把官方那套复制一份再改），2026-11-22
    # 起视频生成一律克隆来源卡，这条路已经没有调用方；判断留着，不冒险改行为。
    if target == donor and target not in gate.mrks():
        raise MDStudioError(tr("target and donor are the same card"))
    if donor not in gate.mrks():
        raise AssetMissing(tr("%s has no cut-in, can't be a donor") % donor)
    for q in QUALITIES:
        if prefab(donor, q) not in cat or not tx.game.find_logical(prefab(donor, q)):
            raise AssetMissing(tr("donor %s %s prefab not downloaded yet (see it once in game)") % (donor, q))
    miss = missing(cat, donor)
    if miss:
        raise AssetMissing(tr("donor %s is missing %s file(s) on disk, e.g. %s") % (donor, len(miss), miss[0]))
    # 这张卡已经有本工具的动画时不再拦（2026-11-13 用户定稿）：允许在自改动画上
    # 直接再生成一次，写入按覆盖处理，复原永远回游戏原版。
    #
    # 换来源卡仍然被拦（见 cutin_flip.apply 的 chassis 判断），那是另一回事：
    # 底盘换了动画名和缩放系数都对不上，角色的屏幕尺寸会跳。
    if target in gate.mrks() and not force and not _ours(tx.ctx, target):
        raise NotOurs(tr("%s already has a game cut-in. Use force to replace it.") % target)


def own_anim(tx, target: int) -> bool:
    """超框版原本借底图动画时，把它登记成「用自己的」。

    游戏查召唤动画会先看 of_card_asset：`22804 -> 14952` 表示播 14952 那套。
    只写 P22804 的资源不改这张表，游戏仍然去读 14952，等于白做。这里把表改成
    自映射（`22804 -> 22804`），写进同一个 Tx 和资源一起提交，避免出现「表改了
    但资源没写」的半套状态。

    返回是否动了表。普通卡、本来就有自己动画的卡、底图没动画的卡都返回 False。
    """
    from mdstudio.ops import overframe as OF

    cid = int(target)
    try:
        builtin = {int(t): int(b) for t, b in OF.bundled_gate_records(tx.ctx)}
        live = OF.gate_records(tx.ctx)
    except Exception:
        return False
    base = builtin.get(cid, 0)
    if not base or base == cid:
        return False
    if {int(t): int(b) for t, b in live}.get(cid) == cid:
        return False
    if base not in tx.cutin_gate.mrks():
        return False
    new = OF._add_entry(live, cid, cid)
    gp = OF.gate_bundle_path(tx.ctx)
    tx.put_existing(gp, OF._gate_bytes_with(gp, new))
    log.info(tr("%s borrows its cut-in from %s, registering it as its own"), cid, base)
    return True


def clone(ctx, target: int, donor: int = DEFAULT_DONOR, force=False, tx=None):
    """donor 专属的 bundle (~20 个) 全复制一份改成 P<target>, 之后随便改不影响 donor

    tx 给了就并进那个事务：视频生成会紧接着在同一张卡上写骨架与贴图，
    分成两条记录会让历史里出现「克隆」和「视频」两行，看起来像做了两次
    （2026-11-12 用户实测）。并进去之后一条记录撤销也更干净。
    """
    own = tx is None
    if own:
        tx = Tx(ctx, "cutin.clone", target)
    _check(tx, target, donor, force)
    own_anim(tx, target)
    cat = tx.catalog
    # 先建 old_crc -> new_crc 全表：m_Dependencies 存的是哈希，字符串改名够不到，
    # 必须显式重定向，否则克隆出的 SkeletonData/Atlas 仍指向 donor 资源。
    paths = own_paths(cat, donor)
    pgs = page_rename_map(cat, donor, target)

    def renamed(lp):
        # 先统一页名（P156402 -> P<target>_2），再换卡号，两步顺序不能反：
        # 反过来后 "P<target>_2" 里已经没有旧卡号可供第二步匹配了
        return retag_str(rename_pages(lp, pgs), donor, target)

    crc_map = {get_crc(lp): get_crc(renamed(lp)) for lp in paths}
    # 跨包引用记的是 CAB 名（archive:/CAB-xxx/CAB-xxx），不是哈希也不是路径。
    # 不一起改的话，克隆出来的包还指着来源卡那套，游戏加载时解析不到 → 卡住不显示。
    # 先读一遍每个来源包自己的 CAB 名，建旧→新对照。
    cab_map = {bundle_cab(tx.game.find_logical(lp).read_bytes()): new_cab(get_crc(renamed(lp)))
               for lp in paths}
    for lp in paths:
        src = tx.game.find_logical(lp)
        tlp = renamed(lp)
        th = get_crc(tlp)
        data = retag(src.read_bytes(), donor, target, th, deep=True,
                     crc_map=crc_map, page_map=pgs,
                     cab_map=cab_map, cab=new_cab(th))
        # 来源卡约一半把 clip 设成「跟随动画时长」，换骨架后会变 0 长、角色静止。
        # 纯克隆也统一改成固定时长，这样「官方动画 / 官方动画★」两档与视频、
        # Spine 两条路一致（2026-11-17 实测 #14952 → #22804）。
        if tlp.endswith(f"P{target}Timeline"):
            data = fix_clip_duration_mode(data)
        tx.put_file(th, data)
    # 清单条目要逐个登记，不能只登 prefab。来源卡 18 个包在清单里个个有条目；
    # 只登 prefab 的话，克隆出来的骨架/图集/贴图在清单里查不到，游戏按清单取包
    # 就取不到 —— 实测纯克隆官方动画过去也是「有动画没主体」（2026-11-17）。
    prefabs = {prefab(donor, q) for q in QUALITIES}
    for lp in paths:
        if lp in cat and lp not in prefabs:
            tx.set_catalog(renamed(lp), [renamed(d) for d in (cat.deps(lp) or [])])
    for q in QUALITIES:
        dp = prefab(donor, q)
        tx.set_catalog(prefab(target, q), [renamed(d) for d in cat.deps(dp)])
    tx.add_cutin(target)
    if not own:
        tx.rec.meta["donor"] = donor   # 并进别人的事务时只留来源卡号，别覆盖它的 meta
        return None
    tx.rec.meta = {"donor": donor}
    return tx.commit()


def remove(ctx, target: int):
    recs = _ours(ctx, target)
    if not recs:
        raise NotOurs(tr("%s: no cut-in from this tool to remove") % target)
    others = [r for r in ctx.journal.load() if r.id not in {x.id for x in recs}]
    undo(ctx, recs, keep_records=others)


def borrowed(ctx, mrk: int) -> bool:
    """这张卡现在播的是别人那套资源（cutin.borrow），而不是自己的一份。

    借用记录不改任何 `P<mrk>` 文件，它只是把登记表指向来源卡的号。所以往里面写
    贴图会改掉来源卡的动画。只有确实借了、且从没自己克隆过，才需要拦一次；
    自己生成过一次之后就有独立的一套，可以随便覆盖。
    """
    return any(r.op == "cutin.borrow" for r in _ours(ctx, mrk))


def is_cloned(ctx, mrk: int) -> bool:
    """这张卡是不是已经有「自己的一套」资源，可以直接往里换骨架与贴图。

    必须包含 cutin.video（2026-11-13 修）：视频生成会把克隆和写贴图并进同一条
    cutin.video 记录（历史里不该出现「克隆」「视频」两行）。以前这里只认
    cutin.clone / cutin.spine / pack.cutin，于是自己生成过一次的卡会被判成
    「没克隆过」，而去走「借来的动画」分支报错。_ours 认 cutin.video、这里不认，
    两个判断互相矛盾，用户实测第二次生成必报 22116 是借来的动画。
    """
    return any(r.op in ("cutin.clone", "cutin.spine", "cutin.video", "pack.cutin")
               for r in _ours(ctx, mrk))


def list_cards(game, journal, cat=None, gate=None):
    """[(mrk, status)] status: game / yours / wiped (更新把 gate 重置了, 需要重做)"""
    from mdstudio.core.gate import CutinGate
    gate = gate or CutinGate(game)
    live = gate.mrks()
    mine = {}
    for r in journal.load():
        if r.op in OPS:
            mine.setdefault(r.card, r.op)
    rows = [(m, "yours" if m in mine else "game") for m in sorted(live)]
    rows += [(m, "wiped") for m in sorted(mine) if m not in live]
    return rows


def official_origin(journal) -> set:
    """我们动手之前本来就有官方动画的卡。

    判据：这张卡最早那条动画记录里没把它写进登记表。
    登记表本来就没有的卡，写入时要补一条才播得出来，`Tx.add_cutin` 会记进
    `cutin_mrks`，spine 那条路走的是 `meta["gate"]`，两处都要看。
    官方本来就有的卡，表里已经有了补不进去，于是两边都是空的。
    """
    first = {}
    for r in journal.load():
        if r.op not in OPS:
            continue
        cur = first.get(r.card)
        if cur is None or r.time < cur.time:
            first[r.card] = r

    def added(rec) -> bool:
        return bool(rec.cutin_mrks) or bool(rec.meta.get("gate"))

    return {c for c, r in first.items() if not added(r)}


# ---- 这张卡播的到底是谁的动画 ----
def owner_of(ctx, card: int, cat=None, pairs=None) -> int:
    """这张卡的召唤动画实际由哪个卡号提供。0 表示没有可播的动画。

    三种结果：
      - 返回自己 = 它有自己的一套（官方动画）
      - 返回别的号 = 借的（官方超框变体 #22804 借 #14952；#22768 借 #22690）
      - 返回 0 = 它和它的底图都没有动画资源，游戏里放不出来

    判据是资源清单里有没有 `P<卡号>`，这是唯一直接的证据。借谁看超框登记表
    （of_card_asset：触发号 → 底图号）。底图号也必须有资源，否则「两个号都没
    动画」会被错判成「借了底图」。
    """
    from mdstudio.ops import overframe as OF

    cid = int(card)
    if cat is None:
        cat = Catalog(ctx.game)
    if _has_own_anim(cat, cid):
        return cid
    if pairs is None:
        try:
            pairs = dict(OF.bundled_gate_records(ctx))
        except Exception:
            pairs = {}
    base = int(pairs.get(cid, 0) or 0)
    if base and base != cid and _has_own_anim(cat, base):
        return base
    return 0


_OWN_ANIM_CACHE: dict = {}


def _own_anim_ids(cat) -> set:
    """资源清单里出现过 `P<卡号>` 的卡号集合。整表扫一遍，之后按卡号查。"""
    key = id(cat)
    hit = _OWN_ANIM_CACHE.get(key)
    if hit is not None:
        return hit
    pat = re.compile(r"/P(\d+)(?:/|$)")
    ids = set()
    for n in cat.names():
        if "/MonsterCutIn/" not in n:
            continue
        m = pat.search(n)
        if m:
            ids.add(int(m.group(1)))
    _OWN_ANIM_CACHE[key] = ids
    return ids


def _has_own_anim(cat, cid: int) -> bool:
    return int(cid) in _own_anim_ids(cat)


# ---- 换皮 ----
def reskin_export(ctx, target: int, out_dir) -> list:
    from mdstudio.core.catalog import Catalog
    cat = Catalog(ctx.game)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    saved = []
    for lp in own_paths(cat, target):
        p = ctx.game.find_logical(lp)
        if not p:
            continue
        q = HD if f"/{HD}/" in lp else SD
        for d in textures(p.read_bytes()):
            f = out / f"{q}_{d.m_Name}.png"
            d.image.save(f)
            saved.append(f)
    # atlas 一起导出, 画的时候照着 bounds 画
    for lp in own_paths(cat, target):
        if lp.endswith(".atlas") and ctx.game.find_logical(lp):
            q = HD if f"/{HD}/" in lp else SD
            env = UnityPy.load(ctx.game.find_logical(lp).read_bytes())
            for o in env.objects:
                if o.type.name == "TextAsset":
                    s = o.read().m_Script
                    s = s if isinstance(s, str) else bytes(s).decode("utf-8", "replace")
                    (out / f"{q}_P{target}.atlas.txt").write_text(s, encoding="utf-8")
    return saved


def reskin_import(ctx, target: int, png_files) -> int:
    """文件名 = reskin_export 导出的名字 (SD_P21365.png ...), 尺寸不变"""
    if is_donor_card(target):
        raise MDStudioError(tr("%s is a template donor card. Its cut-in cannot be changed.") % target)
    if not is_cloned(ctx, target):
        raise NotOurs(tr("%s is not a clone. Reskinning a borrowed cut-in would repaint the donor "
                         "too. Clone it first.") % target)
    tx = Tx(ctx, "cutin.reskin", target)
    wanted = {}
    for f in map(Path, png_files):
        # 坑: HD 目录名 "HighEnd_HD" 自己就带下划线, 不能按第一个 _ 拆
        q = next((x for x in (HD, SD) if f.stem.startswith(x + "_")), None)
        if q is None:
            continue
        wanted[(q, f.stem[len(q) + 1:])] = f
    if not wanted:
        raise MDStudioError(tr("no file named like the exported ones (SD_xxx.png / HighEnd_HD_xxx.png)"))
    n = 0
    for lp in own_paths(tx.catalog, target):
        q = HD if f"/{HD}/" in lp else SD
        p = tx.game.find_logical(lp)
        if not p:
            continue
        data = p.read_bytes()
        hits = [(d.m_Name, wanted[(q, d.m_Name)]) for d in textures(data) if (q, d.m_Name) in wanted]
        for name, f in hits:
            # 一个 bundle 可能有两页, 在同一份 data 上连续改
            try:
                with Image.open(f) as im:
                    img = im.convert("RGBA")
            except OSError as e:
                raise MDStudioError(tr("%s: not an image (%s)") % (f.name, e)) from None
            data = replace_texture(data, img, name=name, keep_size=True)
        if hits:
            tx.put_existing(p, data)
            n += len(hits)
    if not n:
        raise MDStudioError(tr("no matching texture names. Use the names from reskin export."))
    tx.commit()
    return n
