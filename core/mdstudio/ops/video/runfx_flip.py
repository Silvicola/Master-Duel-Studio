"""视频 -> 发动效果 (全屏视频带声音也行)

游戏判断效果结束 = PlayableDirector.stopped (Player.log 里 TimelineObject:OnPlayableDirectorStop).
所以底盘必须有 PD, 且 Timeline 在同一个 bundle 里 (m_PlayableAsset.m_FileID == 0),
外部 Timeline 拉不长, 永远卡在原长度. 全游戏只有 5 个符合, 最长的是 Ef04887 (神圣防护罩).
"""
import copy
import logging

import UnityPy

from mdstudio.core.bundle import bundle_name, set_image
from mdstudio.core.crc import get_crc
from mdstudio.errors import AssetMissing, MDStudioError
from mdstudio.i18n import tr
from mdstudio.ops import runfx as R
from mdstudio.ops import sound as S
from mdstudio.ops.tx import Tx
from mdstudio.ops.video import frames as F
from mdstudio.progress import step

log = logging.getLogger("mdstudio")

CHASSIS = "Duel/Timeline/Card/tcg/Ef04887/Ef04887"
FIRST_ID = 90001


def effect_path(eid: int, token=False) -> str:
    return f"Duel/Timeline/Card/{'<_CARD_ILLUST_>' if token else 'tcg'}/Ef{eid}/Ef{eid}"


LAST_ID = 99999


def free_id(cat, game=None, journal=None) -> int:
    """视频效果的 Ef 编号. 坑: 以前固定从 90001 起, 大家的第一个视频都叫 Ef90001, 装别人的包直接撞;
    更新把 manifest 重置后还会复用自己旧的号, 把旧视频盖掉. 现在随机起点, 并且避开
    manifest 里有的 / 盘上已有文件的 / journal 里用过的"""
    import random
    used = set()
    if journal is not None:
        used = {r.meta.get("effect_id") for r in journal.load()}
    start = random.randint(FIRST_ID, LAST_ID)
    span = LAST_ID - FIRST_ID + 1
    for k in range(span):
        eid = FIRST_ID + (start - FIRST_ID + k) % span
        lp = effect_path(eid)
        if eid in used or lp in cat or (game is not None and game.find(get_crc(lp))):
            continue
        return eid
    raise MDStudioError(tr("no free effect id left"))


def _const(mmc, v):
    mmc = dict(mmc)
    mmc.update(minMaxState=0, scalar=float(v), minScalar=float(v))
    return mmc


def _texenvs(mat):
    out = {}
    for t in mat.get("m_SavedProperties", {}).get("m_TexEnvs", []):
        out[t[0] if isinstance(t, (list, tuple)) else t.get("first")] = t
    return out


def _tex_pid(entry):
    return (entry[1] if isinstance(entry, (list, tuple)) else entry["second"])["m_Texture"]["m_PathID"]


def frame_curve(n, cols, rows):
    # 线性 0 -> (n-0.5)/总格数, 正好停在最后一帧, 不会跑到空格子
    total = cols * rows
    end = (n - 0.5) / total if total > 1 else 1.0
    key = lambda t, v: {"time": t, "value": v, "inSlope": end, "outSlope": end,
                        "weightedMode": 0, "inWeight": 0.333, "outWeight": 0.333}
    c = {"m_Curve": [key(0.0, 0.0), key(1.0, float(end))], "m_PreInfinity": 2, "m_PostInfinity": 2,
         "m_RotationOrder": 4}
    return {"minMaxState": 1, "scalar": 1.0, "minScalar": 1.0, "maxCurve": copy.deepcopy(c),
            "minCurve": copy.deepcopy(c)}


def build_bundle(chassis: bytes, sheet, cols, rows, n, duration, eid, max_size, aspect=1.0, size=400.0) -> bytes:
    lp = effect_path(eid)
    h = get_crc(lp)
    env = UnityPy.load(chassis)
    objs = {o.path_id: o for o in env.objects}
    if not any(o.type.name == "PlayableDirector" for o in env.objects):
        raise MDStudioError(tr("chassis has no PlayableDirector, it would freeze the duel"))

    root = None
    for o in env.objects:
        if o.type.name == "AssetBundle":
            ab = o.read_typetree()
            ab["m_Name"] = ab["m_AssetBundleName"] = bundle_name(h)
            ab["m_Container"] = [["assets/resourcesassetbundle/" + lp.lower() + ".prefab", c[1]] for c in ab["m_Container"]]
            root = ab["m_Container"][0][1]["asset"]["m_PathID"]
            o.save_typetree(ab)
    if root in objs and objs[root].type.name == "GameObject":
        gt = objs[root].read_typetree()
        gt["m_Name"] = f"Ef{eid}"
        objs[root].save_typetree(gt)

    renderers = {}
    for o in env.objects:
        if o.type.name == "ParticleSystemRenderer":
            renderers[o.read_typetree()["m_GameObject"]["m_PathID"]] = o

    # 主粒子 = 材质有 _BaseMap 且贴图在本 bundle 里 (不然换不了)
    hero = None
    for o in env.objects:
        if o.type.name != "ParticleSystem":
            continue
        tt = o.read_typetree()
        r = renderers.get(tt["m_GameObject"]["m_PathID"])
        mats = r.read_typetree().get("m_Materials") if r else None
        if not mats or mats[0]["m_PathID"] not in objs:
            continue
        mo = objs[mats[0]["m_PathID"]]
        mt = mo.read_typetree()
        te = _texenvs(mt).get("_BaseMap")
        if te is None or _tex_pid(te) not in objs:
            continue
        hero = (o, tt, r, mo, mt, _tex_pid(te))
        if tt.get("UVModule", {}).get("enabled"):
            break
    if not hero:
        raise MDStudioError(tr("chassis has no usable particle (_BaseMap texture inside the bundle)"))
    ps, tt, rend, mo, mt, tex_pid = hero

    set_image(objs[tex_pid].read(), sheet)

    for t in mt.get("m_SavedProperties", {}).get("m_Colors", []):
        k = t[0] if isinstance(t, (list, tuple)) else t.get("first")
        col = t[1] if isinstance(t, (list, tuple)) else t["second"]
        if k in ("_BaseColor", "_Color"):
            col.update(r=1.0, g=1.0, b=1.0, a=1.0)
        if k == "_EmissionColor":
            col.update(r=0.0, g=0.0, b=0.0)
    mo.save_typetree(mt)

    im = tt["InitialModule"]
    im["startLifetime"] = _const(im["startLifetime"], duration)
    im["startSize"] = _const(im["startSize"], size)   # 故意给很大, 让 m_MaxParticleSize 说了算
    if abs(aspect - 1.0) > 1e-3 and "size3D" in im:
        # 非正方形视频要开 size3D 单独设高, 不然被压成方的
        im["size3D"] = True
        if "startSizeY" in im:
            im["startSizeY"] = _const(im["startSizeY"], size / aspect)
        if "startSizeZ" in im:
            im["startSizeZ"] = _const(im["startSizeZ"], size)
    im["startSpeed"] = _const(im["startSpeed"], 0.0)
    if "gravityModifier" in im:
        im["gravityModifier"] = _const(im["gravityModifier"], 0.0)
    im["maxNumParticles"] = 1
    tt["looping"], tt["lengthInSec"], tt["playOnAwake"] = 0, 0.2, 1
    if isinstance(tt.get("startDelay"), dict):
        tt["startDelay"] = _const(tt["startDelay"], 0.0)
    em = tt["EmissionModule"]
    em["rateOverTime"] = _const(em["rateOverTime"], 50.0)
    em["m_Bursts"] = []
    em["enabled"] = True    # 底盘关着的, 不开一个粒子都不发
    for mod in ("ShapeModule", "SizeModule", "ColorModule", "RotationModule", "VelocityModule", "NoiseModule",
                "ForceModule", "ClampVelocityModule", "InheritVelocityModule", "SizeBySpeedModule",
                "ColorBySpeedModule", "RotationBySpeedModule"):
        if isinstance(tt.get(mod), dict) and "enabled" in tt[mod]:
            tt[mod]["enabled"] = False
    uv = tt["UVModule"]
    uv.update(enabled=True, mode=0, tilesX=cols, tilesY=rows, animationType=0, timeMode=0, cycles=1)
    if isinstance(uv.get("startFrame"), dict):
        uv["startFrame"] = _const(uv["startFrame"], 0.0)   # 有的底盘随机起始帧
    uv["frameOverTime"] = frame_curve(n, cols, rows)
    ps.save_typetree(tt)

    rt = rend.read_typetree()
    rt["m_Enabled"] = 1
    rt["m_RenderMode"] = 0          # 坑: 底盘是 5 = None, 什么都不画
    # 按屏幕高度的比例算, 不是宽度. 16:9 视频 16:9 屏幕 1.0 刚好, 1.08 盖住底下那条缝
    rt["m_MaxParticleSize"] = float(max_size)
    rt["m_MinParticleSize"] = 0.0
    rend.save_typetree(rt)

    # 主粒子整条父链: 打开 + 位置归零 + 缩放归一 (底盘里有的偏到角落去了)
    trs = {}
    for o in env.objects:
        if o.type.name == "Transform":
            trs[o.read_typetree()["m_GameObject"]["m_PathID"]] = o
    by_pid = {o.path_id: o for o in trs.values()}
    cur, seen = trs.get(tt["m_GameObject"]["m_PathID"]), set()
    while cur is not None and cur.path_id not in seen:
        seen.add(cur.path_id)
        t = cur.read_typetree()
        t["m_LocalPosition"] = {"x": 0.0, "y": 0.0, "z": 0.0}
        t["m_LocalScale"] = {"x": 1.0, "y": 1.0, "z": 1.0}
        cur.save_typetree(t)
        go = objs.get(t["m_GameObject"]["m_PathID"])
        if go is not None:
            gt = go.read_typetree()
            if not gt.get("m_IsActive"):
                gt["m_IsActive"] = True
                go.save_typetree(gt)
        cur = by_pid.get(t["m_Father"]["m_PathID"])

    # Timeline 所有 clip 拉到 0..时长+0.5. 效果持续多久看 Timeline, 不看粒子寿命
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        try:
            t2 = o.read_typetree()
        except Exception:
            continue
        clips = t2.get("m_Clips")
        if clips and any(isinstance(c, dict) and "m_Start" in c for c in clips):
            for c in clips:
                if isinstance(c, dict) and "m_Start" in c and "m_Duration" in c:
                    c["m_Start"], c["m_Duration"] = 0.0, float(duration) + 0.5
            o.save_typetree(t2)

    # 其他粒子和渲染器全关, 只留主粒子
    for o in env.objects:
        if o.type.name == "ParticleSystem" and o.path_id != ps.path_id:
            t2 = o.read_typetree()
            t2["EmissionModule"]["rateOverTime"] = _const(t2["EmissionModule"]["rateOverTime"], 0.0)
            t2["EmissionModule"]["m_Bursts"] = []
            t2["InitialModule"]["maxNumParticles"] = 0
            o.save_typetree(t2)
        elif o.type.name in ("MeshRenderer", "SkinnedMeshRenderer", "SpriteRenderer"):
            t2 = o.read_typetree()
            t2["m_Enabled"] = 0
            o.save_typetree(t2)
    return env.file.save(packer="lz4")


# 视频自带声音默认借的 label (Phainon 那次用的, Droll & Lock Bird 的音)
DEFAULT_CARRIER = "SE_EV_EF09279_v1"


def apply(ctx, card: int, source, n=120, cell=1080, duration=5.0, mode="full", max_size=None,
          se_label="", sharp=False, eid=None, preview_dir=None, video_audio=False):
    """video_audio=True: 把视频自带的声音塞进 se_label (借来的盘上 label, 用它的卡也跟着变)"""
    if video_audio:
        if not se_label:
            raise MDStudioError(tr("to use the video's own sound, pick which sound label carries it "
                                  "(new labels are silent, so an existing one is borrowed)"))
        from mdstudio.ops.video import audio
        audio.ffmpeg_exe()
        if not audio.has_audio(source):           # 先查, 别等几分钟图集做完才说没声音
            raise MDStudioError(tr("%s has no audio track. Untick 'use the video's own audio'.") % source)
    rec = _apply(ctx, card, source, n, cell, duration, mode, max_size, se_label, sharp, eid, preview_dir)
    if video_audio:
        _video_sound(ctx, card, source, duration, se_label)
    return rec


def _video_sound(ctx, card, source, duration, label):
    import tempfile
    from pathlib import Path
    from mdstudio.ops.tx import undo
    from mdstudio.ops.video import audio
    if ctx.dry_run:
        log.info(tr("[dry-run] then: put the video's sound into %s"), label)
        return
    with tempfile.TemporaryDirectory() as d:
        wav = audio.extract(source, Path(d) / "sound.wav", duration=duration)
        prev = [r for r in ctx.journal.load() if r.op == "sound.inject" and r.meta.get("label") == label]
        if prev:
            # 这个 label 以前已经换过 (别的视频): 先还原再换, 不然 inject 会拒绝
            log.info(tr("%s already had a WAV from this tool, replacing it"), label)
            undo(ctx, prev)
        S.inject(ctx, label, wav, card=card)


def _apply(ctx, card, source, n, cell, duration, mode, max_size, se_label, sharp, eid, preview_dir):
    """mode=full 全屏视频 (不抠图), matte = 只要角色

    上限: 单边 16384px. 1920x1080 一帧最多 8x15 = 120 帧 (16200 高), 5.5 秒约 22fps. 要更顺只能降分辨率.
    """
    if preview_dir:
        from pathlib import Path
        Path(preview_dir).mkdir(parents=True, exist_ok=True)   # 先建好, 别等帧做完才发现路径不对
    from mdstudio.ops.video.cutin_flip import make_frames
    tx = Tx(ctx, "runfx.video", card)
    chassis = tx.game.find_logical(CHASSIS)
    if not chassis:
        raise AssetMissing(tr("chassis %s not downloaded yet (see Mirror Force once in game)") % CHASSIS)
    if se_label and not S.on_disk(tx.game, se_label):
        raise AssetMissing(S.why_missing(se_label))
    if any(r.op in R.OPS for r in ctx.journal.for_card(card)):
        raise MDStudioError(tr("%s already has an effect from this tool. Remove it first.") % card)

    frames = make_frames(source, n, cell, mode, sharp=sharp)
    fh, fw = frames[0].shape[:2]
    # 1080p 一帧 1920 宽, 默认列数 round(sqrt(120))=11 -> 21120px 超限, 压到 8 列
    sheet, cols, rows, regions = F.pack(frames, premultiply=False, cols=8 if fw >= 1600 else None)
    if preview_dir:
        sheet.save(f"{preview_dir}/sheet.png")
        F.preview_dark(sheet, False).save(f"{preview_dir}/preview_dark.png")
    n = len(regions)
    eid = eid or free_id(tx.catalog, tx.game, ctx.journal)
    if max_size is None:
        max_size = 1.08 if mode == "full" else 0.5

    step(tr("packing the effect bundle Ef%s (texture compression, can take a minute)"), eid)
    data = build_bundle(chassis.read_bytes(), sheet, cols, rows, n, duration, eid,
                        max_size, aspect=fw / fh)
    lp = effect_path(eid)
    tx.put_file(get_crc(lp), data)
    tx.set_catalog(lp, tx.catalog.deps(CHASSIS) or [])
    if tx.runfx_gate.for_card(card):
        tx.drop_runfx_card(card)
    tx.add_runfx([R.clean_entry(tx.runfx_gate.template(), card, effect_path(eid, token=True), se_label, duration)])
    tx.rec.meta = {"source": str(source), "effect_id": eid, "frames": n, "sheet": list(sheet.size),
                   "grid": [cols, rows], "max_size": max_size}
    return tx.commit()


def set_framing(ctx, card: int, max_size: float):
    """只改 m_MaxParticleSize, 不用重建 (重建 142MB 图集要好几分钟)"""
    recs = [r for r in ctx.journal.for_card(card) if r.op == "runfx.video"]
    if not recs:
        raise MDStudioError(tr("%s: no video effect from this tool") % card)
    rec = recs[-1]
    p = ctx.game.find_logical(effect_path(rec.meta["effect_id"]))
    if not p:
        raise AssetMissing(tr("effect bundle is gone, run resync"))
    cols, rows = rec.meta["grid"]
    env = UnityPy.load(p.read_bytes())
    # 坑: bundle 里还有别的粒子开着 UVModule, 按自己图集的格子数认主粒子, 别改错
    hero_go = None
    for o in env.objects:
        if o.type.name == "ParticleSystem":
            uv = o.read_typetree().get("UVModule", {})
            if uv.get("enabled") and uv.get("tilesX") == cols and uv.get("tilesY") == rows:
                hero_go = o.read_typetree()["m_GameObject"]["m_PathID"]
                break
    if hero_go is None:
        raise MDStudioError(tr("can't find the video particle in the bundle"))
    for o in env.objects:
        if o.type.name == "ParticleSystemRenderer":
            rt = o.read_typetree()
            if rt["m_GameObject"]["m_PathID"] == hero_go:
                rt["m_MaxParticleSize"] = float(max_size)
                o.save_typetree(rt)
    ctx.guard()
    data = env.file.save(packer="lz4")
    ctx.write(p, data)
    if not ctx.dry_run:
        # 记录里的 sha 也要跟着变, 不然以后撤销会以为这个文件被游戏换了
        from mdstudio.ops.tx import sha_of
        rec.meta.setdefault("sha", {})[ctx.game.rel(p)] = sha_of(data)
        rec.meta["max_size"] = float(max_size)
        ctx.journal.update([rec])
    log.info(tr("framing %s -> %s"), card, max_size)
