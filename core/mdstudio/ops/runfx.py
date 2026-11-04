"""发动效果 (run-effect): CardRunEffectSetting.infoList

字段速查:
  player     0 自己 / 1 对手 / 2 都行
  runTiming  0 连锁处理 / 1 被破坏 / 2 卡移动 / 3 特殊 / 4 被无效
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


def list_effects(game, gate=None):
    """[(mrk, timing, seLabel, effectPath, on_disk)]"""
    from mdstudio.core.gate import RunFxGate
    gate = gate or RunFxGate(game)
    out = []
    for e in gate.entries:
        ep = e.get("effectPath") or ""
        out.append((e["mrk"], TIMING.get(e.get("runTiming"), e.get("runTiming")),
                    e.get("seLabel") or "", ep, bool(effect_file(game, ep)) if ep else None))
    return out


def clean_entry(template: dict, card: int, effect_path: str, se_label: str, duration: float) -> dict:
    """从模板只拿字段结构, 值全部重填

    坑: infoList[0] 是天击龙那条, 带 reserveCardMoveEffect=1 + cardMoveEffectLabel
    + 23 张无关卡 id + blockInput=1 + finishSecond=-1. 照抄 = 等一个永远不会来的卡移动效果,
    锁输入, 不超时 -> 卡死. 已经踩过两次.
    """
    e = copy.deepcopy(template)
    want = {
        "enable": 1, "mrk": int(card), "effectNo": -1, "player": 2,
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


def borrow(ctx, target: int, donor: int, replace=False):
    """只改 gate: donor 的 entry 复制一份 mrk 换成 target. 资源本来就在盘上 + 在 manifest 里"""
    tx = Tx(ctx, "runfx.borrow", target)
    gate = tx.runfx_gate
    src = gate.for_card(donor)
    if not src:
        raise AssetMissing(tr("%s has no activation effect. Run `runfx list` for donors.") % donor)
    for e in src:
        ep = e.get("effectPath") or ""
        if ep and not effect_file(tx.game, ep):
            raise AssetMissing(tr("donor effect %s not downloaded yet (see it once in game)") % ep)
    if _ours(ctx, target):
        raise MDStudioError(tr("%s already has an effect from this tool. Remove it first.") % target)
    if gate.for_card(target):
        if not replace:
            raise NotOurs(tr("%s already has a game effect. Use replace to swap it.") % target)
        tx.drop_runfx_card(target)
    # 借用模式可以整条照抄: 抄的是 donor 自己的 entry, 跟 donor 的资源是配套的
    tx.add_runfx([_no_card_move(dict(copy.deepcopy(e), mrk=int(target))) for e in src])
    tx.rec.meta = {"donor": donor}
    return tx.commit()


def remove(ctx, target: int):
    recs = _ours(ctx, target)
    if not recs:
        raise NotOurs(tr("%s: no activation effect from this tool to remove") % target)
    # 视频自带声音换进去的那条也是这张卡的, 一起还原
    recs = recs + ctx.journal.for_card(target, ("sound.inject",))
    others = [r for r in ctx.journal.load() if r.id not in {x.id for x in recs}]
    undo(ctx, recs, keep_records=others)


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
        if e.get("mrk") == card and runfx_sig(e) in old:
            e["seLabel"] = label
            hit += 1
    for r in recs:
        for e in r.runfx_added:
            e["seLabel"] = label
    ctx.guard()
    if not ctx.dry_run:
        ctx.journal.update(recs)          # 跟 Tx 一样: journal 在 gate 前
    if hit:
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
            rows.append((r.card, "yours" if ok else "wiped"))
    rows += [(m, "game") for m in sorted(live - mine)]
    return sorted(set(rows))
