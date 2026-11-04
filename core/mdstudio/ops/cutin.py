"""召唤 cut-in: 借用 / 克隆 / 卸载 / 换皮"""
import logging
import re
from pathlib import Path

import UnityPy
from PIL import Image

from mdstudio.core.bundle import replace_texture, retag, retag_str, textures
from mdstudio.core.crc import get_crc
from mdstudio.errors import AssetMissing, MDStudioError, NotOurs
from mdstudio.i18n import tr
from mdstudio.ops.tx import Tx, undo

log = logging.getLogger("mdstudio")

SD, HD = "SD", "HighEnd_HD"
QUALITIES = (SD, HD)
DEFAULT_DONOR = 11016   # Number C88 Disaster Leo, 2 骨骼的简单 rig, 最不容易出事
OPS = ("cutin.borrow", "cutin.clone", "cutin.video", "cutin.reskin", "cutin.spine", "pack.cutin")


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


def missing(cat, mrk: int) -> list:
    return sorted({d for q in QUALITIES for d in cat.missing(prefab(mrk, q))})


def _ours(ctx, mrk):
    return ctx.journal.for_card(mrk, OPS)


def _check(tx, target: int, donor: int, force: bool):
    cat, gate = tx.catalog, tx.cutin_gate
    # 目标卡官方自带动画时, 来源卡默认就是它自己: 把官方那套复制一份来改.
    # 其它情况下自己复制自己没有意义, 照旧拦掉
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
    if _ours(tx.ctx, target):
        raise MDStudioError(tr("%s already has a cut-in from this tool. Remove it first.") % target)
    if target in gate.mrks() and not force:
        raise NotOurs(tr("%s already has a game cut-in. Use force to replace it.") % target)


def borrow(ctx, target: int, donor: int = DEFAULT_DONOR, force=False):
    """复制 donor 的 SD/HD prefab 改名, 依赖照抄 donor 的 -> 骨骼贴图 Timeline 全用 donor 现成的

    注意: target 和 donor 别同一局上场, 两边 CAB 名一样 Unity 会搞混
    """
    tx = Tx(ctx, "cutin.borrow", target)
    _check(tx, target, donor, force)
    for q in QUALITIES:
        dp, tp = prefab(donor, q), prefab(target, q)
        th = get_crc(tp)
        tx.put_file(th, retag(tx.game.find_logical(dp).read_bytes(), donor, target, th, deep=False))
        tx.set_catalog(tp, tx.catalog.deps(dp))
    tx.add_cutin(target)
    tx.rec.meta = {"donor": donor}
    return tx.commit()


def clone(ctx, target: int, donor: int = DEFAULT_DONOR, force=False):
    """donor 专属的 bundle (~20 个) 全复制一份改成 P<target>, 之后随便改不影响 donor"""
    tx = Tx(ctx, "cutin.clone", target)
    _check(tx, target, donor, force)
    cat = tx.catalog
    for lp in own_paths(cat, donor):
        src = tx.game.find_logical(lp)
        tlp = retag_str(lp, donor, target)
        th = get_crc(tlp)
        tx.put_file(th, retag(src.read_bytes(), donor, target, th, deep=True))
    for q in QUALITIES:
        dp = prefab(donor, q)
        tx.set_catalog(prefab(target, q), [retag_str(d, donor, target) for d in cat.deps(dp)])
    tx.add_cutin(target)
    tx.rec.meta = {"donor": donor}
    return tx.commit()


def remove(ctx, target: int):
    recs = _ours(ctx, target)
    if not recs:
        raise NotOurs(tr("%s: no cut-in from this tool to remove") % target)
    others = [r for r in ctx.journal.load() if r.id not in {x.id for x in recs}]
    undo(ctx, recs, keep_records=others)


def is_cloned(ctx, mrk: int) -> bool:
    return any(r.op in ("cutin.clone", "cutin.spine", "pack.cutin") for r in _ours(ctx, mrk))


def list_cards(game, journal, cat=None, gate=None):
    """[(mrk, status)] status: game / yours / wiped (更新把 gate 重置了, 跑 resync)"""
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
