"""视频 -> 发动效果 (全屏视频带声音也行)

游戏判断效果结束 = PlayableDirector.stopped (Player.log 里 TimelineObject:OnPlayableDirectorStop).
所以底盘必须有 PD, 且 Timeline 在同一个 bundle 里 (m_PlayableAsset.m_FileID == 0),
外部 Timeline 拉不长, 永远卡在原长度.

2026-11-22 全库扫过 (工具: tools 外的临时脚本, 结果已删): `Ef<号>/Ef<号>` 这种主包 37 个,
都有 PD; Timeline 同包的只有 3 个 —— Ef04887 / Ef23491 / Ef4842。其中主粒子能用标准
URP Unlit + `_BaseMap` 换图的只有 Ef04887 (神圣防护罩), 另外两个是自定义粒子 shader
(`fxs_ParticleColorTexture_01_v14_k`) + `_Texture`, 混合模式没验过。所以底盘就它一个。
"""
import copy
import logging
import math

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

# 柔边补偿：内容框按 alpha>30 量，外面那圈淡到近乎透明的辉光不算在内，
# 于是「填 0.5」在肉眼上看只有 0.47（2026-11-22 用 2560×1440 录像量的 47.6%）。
# 放大 3% 让可见边界落在 0.5 附近。改这个数要同时看「硬边是否溢出」。
VISUAL_PAD = 1.03


def effect_path(eid: int, token=False) -> str:
    return f"Duel/Timeline/Card/{'<_CARD_ILLUST_>' if token else 'tcg'}/Ef{eid}/Ef{eid}"


LAST_ID = 99999


def screen_ratio_to_max_size(size_h: float, frame_w: int, content_h: int) -> float:
    """界面「画面高度占屏高」-> m_MaxParticleSize（占屏宽）。

    屏幕按 16:9。quad 高 = 宽 × 帧高/帧宽，而画面只占帧高的 content_h/帧高：
        高 = 宽 × 帧高/帧宽 × content_h/帧高 = 宽 × content_h/帧宽
    → 宽 = 高 × 帧宽/content_h × 屏高/屏宽。
    整帧不透明时 content_h == 帧高，结果与旧口径完全一致（16:9 素材不受影响）。
    """
    if not content_h:
        return float(size_h) * (9.0 / 16.0)
    return float(size_h) * (9.0 / 16.0) * (frame_w / content_h)


def size_h_to_max_size(size_h: float, frame_w: int, frame_h: int, content_h: int) -> float:
    """界面「画面高度」-> m_MaxParticleSize。有透明留白时补一点柔边（VISUAL_PAD）。"""
    m = screen_ratio_to_max_size(size_h, frame_w, content_h)
    return m * VISUAL_PAD if 0 < content_h < frame_h else m


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


def build_bundle(chassis: bytes, sheet, cols, rows, n, duration, eid, max_size, aspect=1.0, size=400.0,
                 offset_x=0.0, offset_y=0.0, rotation=0.0) -> bytes:
    """offset_x/y 是世界单位（界面按屏高百分比换算后传入），rotation 是角度。
    三者在生成时一次写进 bundle，跟 max_size 一样只在这一次生效；改了要重新生成。
    """
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
    # 平面内旋转。单位弧度，正数逆时针，跟召唤动画 rot 的约定一致。
    # RotationModule 已关掉，startRotation 仍生效（它是初值，不是随时间旋转）。
    # 只在真的转了的时候才动这个字段：默认 0 就别碰，产出的字节跟改动前一模一样。
    if rotation and "startRotation" in im:
        im["startRotation"] = _const(im["startRotation"], math.radians(float(rotation)))
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

    # 画面偏移走 Shape 模块的发射位置，不走 Transform。
    # 2026-11-15 实测：写在 Transform.m_LocalPosition 上完全不动（0 和 10 两个值，
    # 截图逐像素比对位移 0），写成 (0,10,0) 时画面仍在正中。而 Shape 的发射位置
    # 是粒子自己的数据，不受运行时对 Transform 的摆布。
    # 关掉形状的随机半径，只留一个确定的偏移点，不然粒子每次会撒在锥体里。
    sh = tt.get("ShapeModule")
    if isinstance(sh, dict):
        sh["enabled"] = True
        sh["type"] = 0                                   # Sphere
        sh["angle"] = 0.0
        sh["length"] = 0.0
        sh["randomDirectionAmount"] = 0.0
        sh["alignToDirection"] = False
        if isinstance(sh.get("radius"), dict):
            sh["radius"]["value"] = 0.0
        sh["m_Position"] = {"x": float(offset_x), "y": float(offset_y), "z": 0.0}
        sh["m_Scale"] = {"x": 1.0, "y": 1.0, "z": 1.0}
        sh["m_Rotation"] = {"x": 0.0, "y": 0.0, "z": 0.0}
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
        # 位置一律归零：偏移改由 Shape 模块的发射位置承担（见上），
        # 这里只负责把底盘里那些偏到角落去的初始位置拉回原点。
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
          se_label="", sharp=False, eid=None, preview_dir=None, video_audio=False,
          offset_x=0.0, offset_y=0.0, rotation=0.0,
          offx_pct=0.0, offy_pct=0.0, size_h=None,
          sharp_model="realesr-animevideov3", sharp_scale=4):
    """video_audio=True: 把视频自带的声音塞进 se_label (借来的盘上 label, 用它的卡也跟着变)"""
    if video_audio:
        if not se_label:
            raise MDStudioError(tr("to use the video's own sound, pick which sound label carries it "
                                  "(new labels are silent, so an existing one is borrowed)"))
        from mdstudio.ops.video import audio
        audio.ffmpeg_exe()
        if not audio.has_audio(source):           # 先查, 别等几分钟图集做完才说没声音
            raise MDStudioError(tr("%s has no audio track. Untick 'use the video's own audio'.") % source)
    rec = _apply(ctx, card, source, n, cell, duration, mode, max_size, se_label, sharp, eid, preview_dir,
                 offset_x, offset_y, rotation, sharp_model, sharp_scale, offx_pct, offy_pct, size_h)
    if video_audio:
        _video_sound(ctx, card, source, duration, se_label)
    return rec


def _cache_preview(ctx, card: int, frames, duration: float, size: float,
                   offx_pct: float, offy_pct: float, rotation: float, se_label: str) -> None:
    """把这次生成的每一帧存一份，给软件内的特效预览读。

    和动画那条不同：特效帧是整块贴图，屏幕上多大由「画面大小」决定（占屏高的比例），
    位置还要记界面上的百分比偏移 —— 生成时传下去的是世界单位，离开补偿曲线就读不懂了。
    帧数按界面参数原样存，只把长边压小一档，免得播放器一次解出几百 MB。
    """
    if ctx.dry_run:
        return
    try:
        import json
        import time
        from PIL import Image
        from mdstudio.ops import preview as PV

        root = PV.folder_of(ctx, card, PV.KIND_RUNFX)
        root.mkdir(parents=True, exist_ok=True)
        for old in root.glob("*.webp"):
            old.unlink()

        n = len(frames)
        limit = 900 if n <= 120 else 720 if n <= 200 else 600
        w = h = 0
        for i, fr in enumerate(frames):
            im = Image.fromarray(fr, "RGBA")
            if max(im.size) > limit:
                k = limit / max(im.size)
                im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))),
                               Image.LANCZOS)
            im.save(root / f"{i:03d}.webp", "WEBP", quality=90, method=4)
            w, h = im.size

        meta = {
            "kind": PV.KIND_RUNFX, "card": int(card), "frames": n,
            "width": w, "height": h,
            "duration": round(float(duration), 3),
            # 画面大小就是「占屏高的比例」，预览按它还原尺寸
            "size": round(float(size), 4),
            "offx_pct": round(float(offx_pct or 0), 3),
            "offy_pct": round(float(offy_pct or 0), 3),
            "rotation": round(float(rotation or 0), 3),
            "se": se_label or "",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
        log.info(tr("P%s: wrote %s effect preview frame(s) for the in-app player"), card, n)
    except Exception as e:
        # 预览缓存写不出来不该让整次生成失败
        log.warning(tr("P%s: could not write the effect preview frames (%s)"), card, e)


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


def retarget_entries(mine, template, card: int, path: str, se_label: str, duration: float) -> list:
    """这张卡「自己那侧」的每条条目都改成指向新特效，时机原样保留。

    2026-11-22 之前是删光再加一条 runTiming=0：多时机的卡（连锁发动 + 被破坏）
    生成后只剩第一条，第二个时机静默失效。这里照抄每条条目的时机、播放方、
    延迟与音效，只换路径。其余字段仍走 clean_entry 的安全值（卡移动那套清掉、
    给硬超时、不锁输入），那几条是踩过坑的。

    se_label 非空（命令行 --se）时统一盖掉所有条目的音效；空则沿用各自原来的，
    这样「连锁发动」和「被破坏」可以各带各的官方音效。
    """
    if not mine:
        return [R.clean_entry(template, card, path, se_label, duration, player=2)]
    out = []
    for e in mine:
        # player=0 是「自己」，别写成 or 2 把 0 吃掉
        p = e.get("player")
        p = p if isinstance(p, int) else 2
        n = R.clean_entry(template, card, path, se_label, duration, player=p)
        if isinstance(e.get("runTiming"), int):
            n["runTiming"] = e["runTiming"]
        if isinstance(e.get("delay"), (int, float)) and "delay" in n:
            n["delay"] = float(e["delay"])
        if not se_label and "seLabel" in n:
            n["seLabel"] = e.get("seLabel") or ""
        out.append(n)
    return out


def _apply(ctx, card, source, n, cell, duration, mode, max_size, se_label, sharp, eid, preview_dir,
           offset_x=0.0, offset_y=0.0, rotation=0.0,
           sharp_model="realesr-animevideov3", sharp_scale=4,
           offx_pct=0.0, offy_pct=0.0, size_h=None):
    """mode=full 全屏视频 (不抠图), matte = 只要角色

    上限: 单边 16384px. 1920x1080 一帧最多 8x15 = 120 帧 (16200 高), 5.5 秒约 22fps. 要更顺只能降分辨率.
    """
    # 已经有本工具做过的改动（生成过视频，或借用过别卡的特效）时一律拦下，先复原再生成。
    # 放在最前面：不查依赖、不建 Tx、不解帧，什么都没动就退回去。
    #
    # 2026-11-13 曾反过来（允许直接再生成一版，新 Ef 落地后 retire_old 回收旧的），
    # 2026-11-15 用户改回「只能先复原再生成」：同一张卡上两条记录并存时，复原要按时间
    # 倒序撤两族记录，顺序错一次就会留下读不到的登记或者孤儿 bundle。调参时最常做的
    # 动作，不该是最容易出事的动作。界面一直有这道拦截，这里是让引擎同口径（CLI/TUI 也拦）。
    if ctx.journal.for_card(card, R.OPS):
        raise MDStudioError(tr("%s already has an effect from this tool. Restore it first.") % card)
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
    old = []

    frames = make_frames(source, n, cell, mode, sharp=sharp,
                         sharp_model=sharp_model, sharp_scale=sharp_scale)
    fh, fw = frames[0].shape[:2]
    ch = fh
    if size_h is not None:
        # 界面填的是「画面高度占屏高的比例」；帧四周可能有透明留白（matte 模式把素材
        # 裁成正方形），要按可见内容的包围盒换算。2026-11-22 前用的是帧高，
        # 1080² 帧里只有 790 是画面，于是填 0.5 实际只画到屏高的 37%。
        cw, ch = F.content_box(frames)
        if not ch:
            ch = fh
        max_size = size_h_to_max_size(size_h, fw, fh, ch)
    elif max_size is None:
        max_size = 1.08 if mode == "full" else 0.5
    # 放在 pack 之前：pack 边拼边把 frames[i] 置 None 释放内存
    _cache_preview(ctx, card, frames, duration, max_size, offx_pct, offy_pct, rotation, se_label)
    # 1080p 一帧 1920 宽, 默认列数 round(sqrt(120))=11 -> 21120px 超限, 压到 8 列
    sheet, cols, rows, regions = F.pack(frames, premultiply=False, cols=8 if fw >= 1600 else None)
    if preview_dir:
        sheet.save(f"{preview_dir}/sheet.png")
        F.preview_dark(sheet, False).save(f"{preview_dir}/preview_dark.png")
    n = len(regions)
    eid = eid or free_id(tx.catalog, tx.game, ctx.journal)

    step(tr("packing the effect bundle Ef%s (texture compression, can take a minute)"), eid)
    # 界面传来的画面参数原样打一行：位移不生效时要能分清是「没传到」还是「传了没用」
    log.info("runfx.video %s: size_h=%s -> max_size=%s (frame %sx%s, content h=%s) offset=(%s, %s) rotation=%s",
             card, size_h, max_size, fw, fh, ch, offset_x, offset_y, rotation)
    data = build_bundle(chassis.read_bytes(), sheet, cols, rows, n, duration, eid,
                        max_size, aspect=fw / fh,
                        offset_x=offset_x, offset_y=offset_y, rotation=rotation)
    lp = effect_path(eid)
    tx.put_file(get_crc(lp), data)
    tx.set_catalog(lp, tx.catalog.deps(CHASSIS) or [])
    # 只换「自己」那条。双条目卡（自己/对手各一条）里 player=1 是对手，
    # 资源名带 Op（#4887、#14627 都是）。整张卡一起 drop 会把对手那条也顶掉，
    # 结果你做的视频在对手发动时也播。
    path = effect_path(eid, token=True)
    mine = S.self_entries(tx.runfx_gate.for_card(card))
    if mine:
        tx.drop_runfx_entries(mine)
    tx.add_runfx(retarget_entries(mine, tx.runfx_gate.template(), card, path, se_label, duration))
    tx.rec.meta = {"source": str(source), "effect_id": eid, "frames": n, "sheet": list(sheet.size),
                   "grid": [cols, rows], "max_size": max_size,
                   "offset_x": offset_x, "offset_y": offset_y, "rotation": rotation}
    rec = tx.commit()
    # 新的一版已经落盘并且接管了 gate，现在才轮到清理旧的。
    # 顺序反过来会在生成失败时把用户原来的特效删掉。
    _retire_old(ctx, card, rec, old)
    return rec


def _retire_old(ctx, card: int, keep, old) -> None:
    """回收这张卡上被新一版顶掉的旧特效（2026-11-13 用户定稿：生成成功即回收）。

    为什么必须回收：free_id() 每次都避开用过的号，所以重新生成写的是另一个
    Ef 号，旧文件不会自动被覆盖。一个视频特效 bundle 常有几十到上百 MB，
    留着就是永久孤儿——manifest 里还有登记、盘上还占着，但没有任何卡在用它。

    删之前必须确认没有别的卡借用它：`runfx.borrow` 可以让一张卡直接播别人做的
    效果号（gate 里指向同一条 Ef），删掉那张卡的动画就进游戏卡死了。
    needed_files() 里已经有这套判断（借用记录指向的 bundle 也算它的依赖）。
    """
    from mdstudio.core.catalog import Catalog
    from mdstudio.ops.tx import needed_files

    if ctx.dry_run or not old:
        return
    gone = {r.id for r in old}
    others = [r for r in ctx.journal.load() if r.id not in gone]
    # 还有谁在用这些 Ef：别的记录登记过的依赖 + 它们借用的效果号
    in_use = set()
    for r in others:
        in_use |= needed_files(r)

    removable = []
    for r in old:
        eid = r.meta.get("effect_id")
        if not eid:
            continue
        h = get_crc(effect_path(int(eid)))
        if f"{h[:2]}/{h}" in in_use:
            log.info(tr("Ef%s is still used by another card, kept"), eid)
            continue
        removable.append((r, int(eid)))
    if not removable:
        return

    # 不走 undo()：它会顺手把 gate 条目删掉、把更早的 runfx_removed 补回来。
    # 新一版已经接管 gate，这里只需要清理文件、manifest 和 journal 记录。
    try:
        cat = Catalog(ctx.game)
        for r, eid in removable:
            lp = effect_path(eid)
            cat.remove(lp)
            for rel in r.created:
                ctx.delete(ctx.game.abs(rel))
        cat.save(ctx)
        ctx.journal.drop([r.id for r, _ in removable])
        # 关键: 被回收的旧条目必须从新记录的 runfx_removed 里剔掉。
        #
        # 重新生成时 drop_runfx_card 会把上一版的 gate 条目塞进新记录的
        # runfx_removed，可上一版的 bundle 刚被上面删掉。留着的话，之后点「复原」，
        # undo 会把这条补回 gate —— 指向一个已经不存在的 Ef 文件，游戏进对局直接
        # 报「文件读取错误 Duel/Timeline/Card/.../EfNNNNN」，而且 journal 里没有
        # 记录认领这条 gate 条目，卡片会显示成「官方特效」、复原按钮也变灰。
        # 剔掉之后，复原的结果就是这张卡没有特效，正好等于回游戏原版。
        from mdstudio.core.gate import runfx_sig
        gone_sigs = {runfx_sig(e) for r, _ in removable for e in r.runfx_added}
        left = [e for e in keep.runfx_removed if runfx_sig(e) not in gone_sigs]
        # 交接：旧记录里存着「动手之前游戏本来是什么样」。不交接的话，再生成一次
        # 就把官方条目弄丢了，复原只能回到无特效，回不去官方特效。
        seen = {runfx_sig(e) for e in left}
        for r, _ in removable:
            for e in r.runfx_removed:
                sig = runfx_sig(e)
                if sig not in seen:
                    left.append(e)
                    seen.add(sig)
        if len(left) != len(keep.runfx_removed):
            keep.runfx_removed = left
            ctx.journal.update([keep])
        # 旧 Ef 是新建文件，基线里记的是「原本不存在」；记录都删了，
        # manifest 里对应的条目也一起清掉，免得留下永远对不上的死条目
        b = ctx.journal.baselines
        b.forget(card, "runfx", {rel for r, _ in removable for rel in [*r.created, *r.modified]})
        # 这一族的记录全撤完才删基线，别连别的族的一起删
        from mdstudio.core.baseline import family_for
        if not any(x.card == card and family_for(x.op) == "runfx"
                   for x in ctx.journal.load()):
            b.drop(card, "runfx")
        log.info(tr("retired %s old effect(s) for %s"), len(removable), card)
    except Exception as e:
        # 回收失败不影响本次生成（新的已经在跑），但要说出来，别静默留垃圾
        log.warning(tr("old effect cleanup failed for %s: %s"), card, e)


# 注意: 这个函数是就地改写一张**已经生成好**的 bundle, 跟 build_bundle 造新文件不同。
# 2026-11-14: 曾把它接进 WinUI 做成「应用调节」按钮, 用户点了之后游戏报
# 「文件读取错误 Ef<id>」, 卡片状态也从自改特效掉成官方特效。已从界面撤掉。
# 现在只剩 CLI / 旧 PySide 界面 / TUI 在用, 且只改 max_size。要用在界面上之前,
# 必须先在真游戏里验证过改写后的 bundle 能被读进来。
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
