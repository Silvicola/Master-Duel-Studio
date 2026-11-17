"""发动效果 (run-effect): CardRunEffectSetting.infoList

字段速查:
  player     0 自己 / 1 对手 / 2 都行
  runTiming  0 连锁发动 / 1 被破坏 / 2 卡移动 / 3 特殊 / 4 被无效
  playType   0 3D Timeline / 1 HUD / 2 简单
  effectPath 空 = 内置效果, 只有声音
"""
import copy
import logging

from mdstudio.core.crc import get_crc, resolve_illust
from mdstudio.errors import AssetMissing, MDStudioError, NotOurs
from mdstudio.i18n import tr
from mdstudio.ops.tx import Tx, undo

log = logging.getLogger("mdstudio")

OPS = ("runfx.borrow", "runfx.video", "pack.runfx")
# 时机列: 值只用于显示 (数据是 runTiming 整数), 在这儿译, 各界面直接用
TIMING = {0: tr("chain"), 1: tr("destroyed"), 2: tr("moved"), 3: tr("special"), 4: tr("negated")}


def effect_file(game, effect_path: str):
    return game.find(get_crc(resolve_illust(effect_path))) if effect_path else None


def effect_ready(game, entry: dict) -> bool:
    """这条 entry 要播的特效在不在盘上。三条规则都是为了不把能用的来源卡挡在门外：

      - 两条路径都空 = 内置效果、只有声音，本来就没有 bundle，算可用；
      - 路径里还剩没被替换掉的占位符（比如 <_RESOURCE_TYPE_>）= 运行期才决定，无从核对，算可用；
      - 其余情况只要有一条路径的文件在盘上就算可用。游戏按 useCenterEffect 决定播哪条，
        #15296 三战之才就是 effectPath 写了个全游戏都不存在的 Ef15926，实际播的是
        center 的 Ef15296 —— 只看 effectPath 会误判成「资源没下载」。
    """
    paths = [p.strip() for p in (entry.get("effectPath") or "",
                                 entry.get("centerEffectPath") or "") if (p or "").strip()]
    if not paths:
        return True
    if any("<_" in p for p in paths):
        return True
    return any(bool(effect_file(game, p)) for p in paths)


def list_effects(game, gate=None):
    """[(mrk, timing, seLabel, effectPath, on_disk)]"""
    from mdstudio.core.gate import RunFxGate
    gate = gate or RunFxGate(game)
    out = []
    for e in gate.entries:
        ep = e.get("effectPath") or ""
        out.append((e["mrk"], TIMING.get(e.get("runTiming"), e.get("runTiming")),
                    e.get("seLabel") or "", ep, effect_ready(game, e)))
    return out


def clean_entry(template: dict, card: int, effect_path: str, se_label: str, duration: float,
                player: int = 2) -> dict:
    """从模板只拿字段结构, 值全部重填

    坑: infoList[0] 是天击龙那条, 带 reserveCardMoveEffect=1 + cardMoveEffectLabel
    + 23 张无关卡 id + blockInput=1 + finishSecond=-1. 照抄 = 等一个永远不会来的卡移动效果,
    锁输入, 不超时 -> 卡死. 已经踩过两次.
    """
    e = copy.deepcopy(template)
    want = {
        # player: 0=自己, 1=对手, 2=双方共用。跟着被替换的那条走，别一律写 2，
        # 否则双条目卡（自己/对手各一条）会变成双方都播我们做的视频。
        "enable": 1, "mrk": int(card), "effectNo": -1, "player": int(player),
        "runTiming": 0, "useTargetEffect": 0, "playType": 0, "effectType": 0,
        "effectPath": effect_path, "positionalEffect": [], "rotationType": 0,
        "delay": 0.0, "extraSettings": 0,
        # 能跑的原版 entry (4842 / 4678) 都走 center, centerEffectPath == effectPath
        "useCenterEffect": 1, "centerPlayType": 0, "centerEffectType": 0,
        "centerEffectPath": effect_path, "centerDelay": 0.0, "centerExtraSettings": 0,
        "reserveCardMoveEffect": 0, "cardMoveEffectLabel": "",
        "cardMoveEffectTargetCardID": [], "cardMoveEffectMoveType": [],
        "seLabel": se_label or "", "is3Dse": 0, "isNormalSe": 1,
        # waitFinish=0 会被立刻回收啥都看不到; =1 + 正数 finishSecond = 有硬超时, 不会卡死
        "waitFinish": 1, "finishSecond": float(duration) + 0.8, "blockInput": 0,
        "cameraShakeType": "", "expandPath": "", "essential": 0,
    }
    for k, v in want.items():
        if k in e:   # 只写已有字段, typetree 结构不能变
            e[k] = v
    if isinstance(e.get("pivot"), dict):
        e["pivot"] = {"x": 0.0, "y": 0.0, "z": 0.0}
    return e


def _no_card_move(e: dict) -> dict:
    """卡移动效果绑的是 donor 自己那几张卡 (天击龙 13671 / 3434), 换了 mrk 就永远等不到 -> 卡死"""
    for k, v in (("reserveCardMoveEffect", 0), ("cardMoveEffectLabel", ""),
                 ("cardMoveEffectTargetCardID", []), ("cardMoveEffectMoveType", [])):
        if k in e:
            e[k] = v
    return e


def _ours(ctx, card):
    return ctx.journal.for_card(card, OPS)


def _effect_paths(e: dict) -> list:
    return [p.strip() for p in (e.get("centerEffectPath") or "", e.get("effectPath") or "")
            if (p or "").strip()]


def _clone_effect_bundle(tx, old_lp: str, eid: int) -> None:
    """把一份官方特效 bundle 复制成新编号，写进同一个 Tx，撤销时一起删掉。"""
    import UnityPy

    from mdstudio.core.bundle import bundle_name
    from mdstudio.ops.video.runfx_flip import effect_path

    new_lp = effect_path(eid)
    h = get_crc(new_lp)
    src = tx.game.find(get_crc(old_lp))
    if src is None:
        raise AssetMissing(tr("donor effect %s not downloaded yet (see it once in game)") % old_lp)
    env = UnityPy.load(src.read_bytes())
    root = None
    for o in env.objects:
        if o.type.name != "AssetBundle":
            continue
        ab = o.read_typetree()
        ab["m_Name"] = ab["m_AssetBundleName"] = bundle_name(h)
        ab["m_Container"] = [
            ["assets/resourcesassetbundle/" + new_lp.lower() + ".prefab", c[1]]
            for c in ab["m_Container"]]
        if ab["m_Container"]:
            root = ab["m_Container"][0][1]["asset"]["m_PathID"]
        o.save_typetree(ab)
    objs = {o.path_id: o for o in env.objects}
    if root in objs and objs[root].type.name == "GameObject":
        gt = objs[root].read_typetree()
        gt["m_Name"] = f"Ef{eid}"
        objs[root].save_typetree(gt)
    tx.put_file(h, env.file.save(packer="lz4"))
    tx.set_catalog(new_lp, tx.catalog.deps(old_lp) or [])


def _own_effect(tx, entries, target, donor):
    """超框变体借自己底图那套特效时，给它一份独有的资源。

    #22770 那行和 #22692 那行写死同一个 Ef22692。只把行号换成 22770，
    以后动这份资源还是会连底图一起动，所以这里把新行指到克隆出来的新编号。
    别的借用情形（换一张别的卡的特效）照旧共用，不额外占空间。
    """
    from mdstudio.ops import overframe as OF

    try:
        base = {int(t): int(b or 0) for t, b in OF.bundled_gate_records(tx.ctx)}.get(int(target))
    except Exception:
        return entries, []
    if not base or base != int(donor):
        return entries, []

    from mdstudio.ops.video.runfx_flip import effect_path, free_id

    paths = {resolve_illust(p) for e in entries for p in _effect_paths(e)}
    made = {}
    for old in sorted(paths):
        eid = free_id(tx.catalog, tx.game, tx.ctx.journal)
        _clone_effect_bundle(tx, old, eid)
        made[old] = eid
    out = []
    for e in entries:
        e = dict(e)
        for k in ("centerEffectPath", "effectPath"):
            p = (e.get(k) or "").strip()
            if p and resolve_illust(p) in made:
                e[k] = effect_path(made[resolve_illust(p)], token=True)
        out.append(e)
    return out, list(made.values())


def borrow_entries(src, target: int) -> list:
    """donor 的条目里能搬到 target 上的那些。

    只取「自己 / 双方共用」那几条：donor 若是双条目卡（自己/对手各一条，player=0/1），
    它的对手条目搬到目标卡上会让对手发动时也播这套，不是用户要的。
    """
    from mdstudio.ops import sound as S

    return S.self_entries(
        [_no_card_move(dict(copy.deepcopy(e), mrk=int(target))) for e in src])


def borrow(ctx, target: int, donor: int, replace=False):
    """只改 gate: donor 的 entry 复制一份 mrk 换成 target. 资源本来就在盘上 + 在 manifest 里"""
    from mdstudio.ops import sound as S

    tx = Tx(ctx, "runfx.borrow", target)
    gate = tx.runfx_gate
    src = gate.for_card(donor)
    if not src:
        raise AssetMissing(tr("%s has no activation effect. Run `runfx list` for donors.") % donor)
    for e in src:
        if not effect_ready(tx.game, e):
            ep = e.get("centerEffectPath") or e.get("effectPath") or ""
            raise AssetMissing(tr("donor effect %s not downloaded yet (see it once in game)") % ep)
    if _ours(ctx, target):
        raise MDStudioError(tr("%s already has an effect from this tool. Remove it first.") % target)
    if gate.for_card(target):
        if not replace:
            raise NotOurs(tr("%s already has a game effect. Use replace to swap it.") % target)
        # 只摘自己那条。双条目卡（自己/对手各一条）里 player=1 是对手的，
        # 整张卡一起 drop 会让对手发动时也播你借来的这套。视频路径一直这么分，
        # 借用以前是 drop_runfx_card，连对手那条一起顶掉（2026-11-15 修）。
        mine = S.self_entries(gate.for_card(target))
        if mine:
            tx.drop_runfx_entries(mine)
    # 借用模式可以整条照抄: 抄的是 donor 自己的 entry, 跟 donor 的资源是配套的。
    # 只抄「自己/共用」那几条: donor 若是双条目卡, 它的对手条目不该搬到目标卡上。
    entries = borrow_entries(src, target)
    if not entries:
        raise MDStudioError(tr("donor %s only has an opponent-side entry, nothing to borrow") % donor)
    entries, eids = _own_effect(tx, entries, target, donor)
    tx.add_runfx(entries)
    tx.rec.meta = {"donor": donor, "effect_ids": eids}
    # free_id 按这个键避让旧编号（游戏更新把 manifest 重置后也别复用）
    if eids:
        tx.rec.meta["effect_id"] = eids[0]
    return tx.commit()


def remove(ctx, target: int):
    recs = _ours(ctx, target)
    if not recs:
        raise NotOurs(tr("%s: no activation effect from this tool to remove") % target)
    # 视频自带声音换进去的那条也是这张卡的, 一起还原
    sound_recs = ctx.journal.for_card(
        target, ("sound.inject", "sound.custom", "sound.set", "sound.restore_card"))
    # 自建音效槽是本工具加进去的, 特效都撤了它就是孤儿: 文件、白名单、记录三份残留,
    # 而且 game 里那条登记还指着它。先撤白名单, 再交给 undo 删文件。
    from mdstudio.ops import sound as S

    for r in sound_recs:
        lb = (r.meta or {}).get("label") or ""
        if not lb.startswith(S.CUSTOM_PREFIX):
            continue
        try:
            S.drop_slot(ctx, lb)
        except Exception as e:
            log.warning(tr("could not drop the custom sound slot %s: %s"), lb, e)
    recs = recs + sound_recs
    others = [r for r in ctx.journal.load() if r.id not in {x.id for x in recs}]
    kept = undo(ctx, recs, keep_records=others)
    # undo 补回来的条目带的是「生成那一刻」的音效。如果你在那之前把官方音效改过，
    # 那个值还不是最原始的，这里按记录再回滚一次，然后扔掉记录。
    S.apply_origins(ctx, target)
    return {"card": int(target), "kept": int(kept or 0)}


def set_sound(ctx, card: int, label: str):
    """给自己加的特效换音效 label (不重建视频/bundle). 空 = 静音
    坑: seLabel 在 runfx_sig 里, gate 和 journal 必须一起改, 不然撤销/resync 认不出这条"""
    from mdstudio.core.gate import RunFxGate, runfx_sig
    from mdstudio.ops import sound as S
    label = (label or "").strip()
    recs = [r for r in _ours(ctx, card) if r.runfx_added]
    if not recs:
        raise NotOurs(tr("%s: no activation effect from this tool (only effects you added can get "
                         "a new sound)") % card)
    if label and not S.on_disk(ctx.game, label):
        raise AssetMissing(S.why_missing(label))
    gate = RunFxGate(ctx.game)
    old = {runfx_sig(e) for r in recs for e in r.runfx_added}
    hit = 0
    for e in gate.entries:
        # 只改自己那条：player=1 是对手的，别动
        if e.get("mrk") == card and e.get("player") != 1 and runfx_sig(e) in old:
            e["seLabel"] = label
            hit += 1
    for r in recs:
        for e in r.runfx_added:
            if e.get("player") != 1:
                e["seLabel"] = label
    ctx.guard()
    if not ctx.dry_run:
        ctx.journal.update(recs)          # 跟 Tx 一样: journal 在 gate 前
    if hit:
        S.sync_se_label(ctx, card, gate.for_card(card))
        gate.save(ctx)
    else:
        log.warning(tr("%s: effect isn't in the game's list right now (game update?), run resync"), card)
    log.info(tr("effect sound of %s -> %s"), card, label or tr("(silent)"))


def list_cards(game, journal, gate=None):
    from mdstudio.core.gate import RunFxGate, runfx_sig
    gate = gate or RunFxGate(game)
    live = {e["mrk"] for e in gate.entries}
    sigs = {runfx_sig(e) for e in gate.entries}
    rows, mine = [], set()
    for r in journal.load():
        if r.op in OPS:
            mine.add(r.card)
            ok = all(runfx_sig(e) in sigs for e in r.runfx_added)
            # 借用单独的档：播的是别卡的官方那套，界面标「官方特效★」而不是「自改」。
            # 但它仍旧是本工具写进去的，复原要能撤，所以两个档都算「本工具的改动」。
            rows.append((r.card, ("borrowed" if r.op == "runfx.borrow" else "yours") if ok else "wiped"))
    rows += [(m, "game") for m in sorted(live - mine)]
    return sorted(set(rows))
