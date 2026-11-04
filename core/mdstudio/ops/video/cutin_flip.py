"""视频 -> 召唤 cut-in (逐帧 Spine)

做法: 先 clone 一个能跑的 cut-in (带 PlayableDirector + Timeline), 再把它的 skeleton json / atlas / 贴图
换成自己生成的. 骨架: 1 bone + 1 slot + N 个 region attachment, 一条换图 timeline.
动画名必须叫 "animation", prefab 播的就是这个名.
"""
import hashlib
import json
import logging

from mdstudio.core.bundle import replace_textasset, replace_texture
from mdstudio.core.crc import get_crc
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr
from mdstudio.ops import cutin as C
from mdstudio.ops.tx import Tx
from mdstudio.ops.video import frames as F
from mdstudio.progress import step

log = logging.getLogger("mdstudio")

# 以前以为 Timeline 卡死 2 秒, 错了: cut-in 的 Timeline 是自己的 P<card>Timeline bundle, 改得动.
# Phainon (21054) 就是把它从 1.6 拉到 4.9 秒, 游戏里整段放完. 真正的上限是图集 16384px
MAX_DURATION = 15.0
FULL_SIZE = 4800     # skeleton 宽; 16:9 视频 4800x2700 = 铺满屏幕 (Phainon 实测)


def set_timeline_duration(data: bytes, seconds: float) -> bytes:
    """P<card>Timeline: 把 TimelineAsset.m_FixedDuration 和 "animation" 片段的结束时间
    严格设成 seconds, 双向生效 (长了缩, 短了拉).

    以前是"只拉长不缩短". 结果先按 4.8 秒生成一次, 再改成 1.5 秒重新生成时,
    时间轴留在 4.9 秒而动画只有 1.5 秒, 播完干等 3.4 秒, 游戏里看起来像卡住
    (2026-10-22 用户实测 1.9 秒尾巴). 现在每次生成都按当前时长对齐.
    """
    import UnityPy
    env = UnityPy.load(data)
    hit = False
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        try:
            tt = o.read_typetree()
        except Exception:
            continue
        changed = False
        if "m_FixedDuration" in tt and abs(float(tt["m_FixedDuration"]) - seconds) > 1e-6:
            tt["m_FixedDuration"] = float(seconds)
            changed = True
        for c in tt.get("m_Clips") or []:
            if c.get("m_DisplayName") != "animation":
                continue
            hit = True
            start = float(c.get("m_Start", 0))
            want = max(0.05, float(seconds) - start)   # 片段长度不能是负数
            if abs(float(c.get("m_Duration", 0)) - want) > 1e-6:
                c["m_Duration"] = want
                changed = True
        if changed:
            o.save_typetree(tt)
    if not hit:
        raise MDStudioError(tr("the cut-in Timeline has no 'animation' clip to stretch"))
    return env.file.save(packer="lz4")


def gen_atlas(page: str, size, regions) -> str:
    lines = [page, f"size:{size[0]},{size[1]}", "filter:Linear,Linear", "pma:true", "scale:1"]
    for name, x, y, w, h in regions:
        lines += [name, f"bounds:{x},{y},{w},{h}"]
    return "\n".join(lines) + "\n"


# 中文输入法容易打出全角标点, 先统一转成半角再解析
_CURVE_PUNCT = str.maketrans({"（": "(", "）": ")", "，": ",", "：": ":", "；": ";", "＜": "<"})


def parse_pop_curve(spec: str) -> list:
    """'(0,10,10)(8,70,50)' -> [(0.0, 0.10, 0.10), (8.0, 0.70, 0.50)]

    每段写成 (帧,缩放,透明度) (百分比), 段与段连着写就行. 括号、冒号、分号、
    尖括号 < 、空格都当段分隔, 所以 '0,10,10:8,70,50' 和老的 '0,10,10<8,70,50'
    也照样能读; 全角括号/逗号/冒号会先转成半角, 中文输入法打出来也不报错.
    帧号是**绝对帧号**: 动画第 f 帧就用曲线第 f 帧的值, 曲线没写到的后面保持末值.
    缩放走 root 骨骼的 scale 轨道; 透明度烘进帧图 (见 frames.pack).
    """
    spec = spec.translate(_CURVE_PUNCT)
    normalized = spec
    for ch in "()[]{}<>;:":
        normalized = normalized.replace(ch, " ")
    out = []
    for part in normalized.split():
        bits = part.replace(",", "-").split("-")
        try:
            if len(bits) != 3 or not all(b.strip() for b in bits):
                raise ValueError
            f, s, o = (float(x) for x in bits)
        except ValueError:
            raise MDStudioError(tr("Keyframe should look like 0,10,10 (frame,scale,opacity): %s")
                               % part) from None
        out.append((f, s / 100.0, max(0.0, min(1.0, o / 100.0))))
    if len(out) < 2:
        raise MDStudioError(tr("The scale curve needs at least 2 keyframes"))
    if any(b[0] <= a[0] for a, b in zip(out, out[1:])):
        raise MDStudioError(tr("Scale curve frames must increase from left to right"))
    return out


def curve_at(curve, frame: float) -> float:
    """取曲线在第 frame 帧的透明度 (线性插值). 超出末帧就保持末帧的值"""
    if frame <= curve[0][0]:
        return curve[0][2]
    prev = curve[0]
    for k in curve[1:]:
        if k[0] >= frame:
            span = k[0] - prev[0]
            r = 0.0 if span <= 0 else (frame - prev[0]) / span
            return prev[2] + (k[2] - prev[2]) * r
        prev = k
    return curve[-1][2]


def frame_alphas(curve, n: int) -> list:
    """按帧取透明度, 交给 pack 烘进帧图. 帧号是绝对帧号, 曲线后面没有的帧保持末值"""
    return [curve_at(curve, float(i)) for i in range(n)]


def gen_skeleton(w: float, h: float, names, duration: float, pop=True, pop_curve=None,
                 ox: float = 0.0, oy: float = 0.0, zoom: float = 1.0, rot: float = 0.0) -> str:
    """pop = 官方 cut-in 那个"弹出放大", 从 Disaster Leo 的 skeleton 量出来的 (60fps):
    root scale 0.7 @0 -(ease-out)-> 0.95 @22.9% -(线性)-> 1.1 @结尾, Body 往上飘一点
    不加的话逐帧动画看着就像贴了个 gif

    pop_curve = 自定义关键帧 [(帧, 缩放, 透明度)]. 这里只用缩放 (root 骨骼 scale 轨道);
    透明度不写进骨架, 由 frames.pack 烘进帧图. 不要在这里加 slots.*.color 轨道:
    那个轨道在这游戏的运行时里会让整份骨架加载失败.

    ox / oy = 整体位移, 骨骼单位 (apply 里已经从帧图像素换算过来, y 正方向朝上)
    zoom = 整体倍率, 直接乘在缩放关键帧上; pop 关掉时写进 setup 姿势
    rot = 整体旋转, 单位角度, 正数逆时针 (和 Spine 的约定一致, 直接写入)
    """
    n = len(names)
    skel = {"hash": hashlib.md5(f"flip{n}".encode()).hexdigest()[:11], "spine": "4.2.20",
            "x": -w / 2, "y": -h / 2, "width": w, "height": h, "fps": 60, "images": "./images/"}
    anim = {"slots": {"frame": {"attachment": [
        {"time": round(i * duration / max(n - 1, 1), 4), "name": nm} for i, nm in enumerate(names)]}}}
    root_bone = {"name": "root"}
    if ox or oy:
        root_bone["x"], root_bone["y"] = round(ox, 2), round(oy, 2)
    if rot:
        root_bone["rotation"] = round(float(rot), 2)      # 正数逆时针, 与 Spine 一致
    if not pop and abs(zoom - 1.0) > 1e-9:
        # 没有缩放动画时, 整体倍率只能写进 setup 姿势
        root_bone["scaleX"] = root_bone["scaleY"] = round(zoom, 4)
    if pop:
        d = duration
        k = zoom
        if pop_curve:
            # 曲线帧号 = 成片帧号 (绝对). 第 i 帧落在 i/(帧数-1)*时长; 超出片尾的帧截到片尾,
            # 曲线没写到的后面自然保持末值 (Spine 轨道会在最后一个关键帧之后保持)。
            span = max(n - 1, 1)
            root = []
            for fr, sc, _ in pop_curve:
                t = round(min(fr, span) / span * d, 4)
                if root and root[-1]["time"] == t:
                    root[-1].update({"x": round(sc * k, 4), "y": round(sc * k, 4)})
                else:
                    root.append({"time": t, "x": round(sc * k, 4), "y": round(sc * k, 4)})
        else:
            c = [round(0.05 * d, 4), 0.816, round(0.11 * d, 4), 0.931]
            root = [{"time": 0, "x": round(0.7 * k, 4), "y": round(0.7 * k, 4), "curve": c + c},
                    {"time": round(0.229375 * d, 4), "x": round(0.95 * k, 4), "y": round(0.95 * k, 4)},
                    {"time": round(d, 4), "x": round(1.1 * k, 4), "y": round(1.1 * k, 4)}]
        f = h / 1948.0
        anim["bones"] = {
            "root": {"scale": root},
            "Body": {"translate": [{"time": 0, "x": 0, "y": round(-46.41 * f, 2)},
                                   {"time": round(d, 4), "x": 0, "y": round(33.75 * f, 2)}]},
        }
    data = {
        "skeleton": skel,
        "bones": [root_bone, {"name": "Body", "parent": "root"}],
        "slots": [{"name": "frame", "bone": "Body", "attachment": names[0]}],
        "skins": [{"name": "default", "attachments": {"frame": {nm: {"width": w, "height": h} for nm in names}}}],
        "animations": {"animation": anim},
    }
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def make_frames(source, n=48, cell=768, mode="auto", crop_pad=1.04, sharp=False,
                sharp_model="realesr-animevideov3", sharp_scale=4):
    """auto = 自己判断要不要去背景; 其余是手动指定 (CLI 用)

    auto 的判断顺序:
      1. 素材自带透明通道 -> 不抠像, 只按包围盒收紧;
      2. 边框检出幕布色 (绿/蓝/品红/红都行) -> 去背景;
      3. 两者都没有 -> 整段原样用 (全屏)。
    """
    raw = F.sample(source, n)
    if not raw:
        raise MDStudioError(tr("no frames read from %s") % source)
    whole = mode == "full"
    if mode == "auto":
        if F.has_alpha(raw):
            step(tr("the frames already have a transparent background, AI cut-out is skipped"))
            fr = F.crop_alpha(raw, crop_pad)
        elif F.pick_key_color(raw[0]) is not None:
            step(tr("backdrop detected, removing it"))
            fr = F.matte(raw, crop_pad, key="auto")
        else:
            step(tr("no transparency and no backdrop found, using the whole frame"))
            fr = F.opaque(raw, cell, full=True)
            whole = True
    elif F.has_alpha(raw):
        # 帧文件夹常常本来就是抠好的 PNG 序列: 透明通道直接用, 再跑一遍 AI 抠像
        # 等于让它重新猜一次, 半透明的纱/光效会被抹平
        if mode.startswith("matte"):
            step(tr("the frames already have a transparent background, AI cut-out is skipped"))
        fr = F.opaque(raw, cell, full=(mode == "full"))
    elif mode.startswith("matte"):
        # 幕布色自动认; 老方案里可能存着 matte_blue, 走手动指定那条路
        key = "blue" if mode.endswith("blue") else "auto"
        fr = F.matte(raw, crop_pad, key=key)
    else:
        fr = F.opaque(raw, cell, full=(mode == "full"))
    if whole:
        # 坑: fit 按长边缩到 cell, full 的 cell 是"高" -> 1920x1080 被缩成 1080x608, 糊
        fr = F.fit_height(fr, cell)
    else:
        fr = F.fit(fr, cell)
    # 超分放在缩放到最终尺寸之后做: 输入小一圈, 跑得快, 也少一次重采样
    return F.sharpen(fr, cell, model=sharp_model, scale=sharp_scale) if sharp else fr


def apply(ctx, card: int, source, n=120, cell=1080, duration=4.8, size=None, mode="full",
          sharp=False, pop=None, donor=C.DEFAULT_DONOR, sd_scale=0.5, preview_dir=None,
          pop_curve=None, offset_x=0.0, offset_y=0.0, zoom=1.0, rotate=0.0,
          sharp_model="realesr-animevideov3", force=False, sharp_scale=4):
    """size = skeleton 宽. None: full = 4800 (铺满屏), 其他 = 2750 (官方大概 2456, 角色大小)
    pop None: full 不弹 (Phainon 那样整屏不动), 其他照官方弹出放大"""
    if preview_dir:
        from pathlib import Path
        Path(preview_dir).mkdir(parents=True, exist_ok=True)   # 先建好, 别等帧做完才发现路径不对
    if duration > MAX_DURATION:
        raise MDStudioError(tr("duration max is %ss") % MAX_DURATION)
    # 关键字曲线经 RPC 传进来时是 "0,10,10<8,70,50" 这种字符串 (CLI/GUI 自己会先解析),
    # 不在这里转一下的话, 后面会拿字符串当关键帧表用, 报 float 和 str 不能比较
    if isinstance(pop_curve, str):
        pop_curve = parse_pop_curve(pop_curve) if pop_curve.strip() else None
    size = size or (FULL_SIZE if mode == "full" else 2750)
    pop = (mode != "full") if pop is None else pop
    cloned = C.is_cloned(ctx, card)
    if not cloned and C._ours(ctx, card):
        raise MDStudioError(tr("%s has a borrowed cut-in. Remove it, the video needs its own clone.") % card)

    # 先把帧和图集做出来 (最容易失败: 没装 rembg / 视频坏 / 图集超限), 成功了才去 clone,
    # 不然失败后留下一个用户没要的 donor cut-in
    frames = make_frames(source, n, cell, mode, sharp=sharp, sharp_model=sharp_model,
                         sharp_scale=sharp_scale)
    fh, fw = frames[0].shape[:2]
    alphas = frame_alphas(pop_curve, len(frames)) if (pop and pop_curve) else None
    sheet, cols, rows, regions = F.pack(frames, premultiply=True, alphas=alphas)
    if preview_dir:
        sheet.save(f"{preview_dir}/sheet.png")
        F.preview_dark(sheet, True).save(f"{preview_dir}/preview_dark.png")
    names = [r[0] for r in regions]
    w = float(size)
    unit = float(size) / float(fw)          # 帧图 1 像素 -> 骨骼单位
    skel = gen_skeleton(w, w * fh / fw, names, duration, pop, pop_curve,
                        ox=float(offset_x) * unit, oy=float(offset_y) * unit, zoom=float(zoom),
                        rot=float(rotate))
    page = f"P{card}.png"

    if not cloned:
        log.info(tr("cloning %s from %s first"), card, donor)
        C.clone(ctx, card, donor, force=force)
        if ctx.dry_run:
            # dry-run 下 clone 没真写, 后面找不到 P<card> 的文件, 到这就够看了
            log.info(tr("[dry-run] then: replace skeleton + atlas + texture of P%s (%s frames)"),
                     card, len(names))
            return None
    tx = Tx(ctx, "cutin.video", card)
    dirs = C.scale_dirs(tx.catalog, card)
    if not dirs:
        raise MDStudioError(tr("P%s: can't find its scale folders in the manifest") % card)
    step(tr("packing the cut-in textures for P%s (texture compression, can take a minute)"), card)
    import gc
    gc.collect()      # 帧已经拼进 sheet 了, 先把能放的放掉, 16384 的图压 BC7 峰值要好几 G
    # SD (半尺寸) 先做, HD 那张最大的放最后, 峰值不叠在一起
    for q, scale in sorted(dirs.items(), key=lambda kv: kv[0] != C.SD):
        b = f"{C.base(card)}/{q}"
        sh, rg = sheet, regions
        if q == C.SD and sd_scale != 1:
            # SD 画质用半尺寸, 省显存
            sw, shh = max(4, int(sheet.width * sd_scale) // 4 * 4), max(4, int(sheet.height * sd_scale) // 4 * 4)
            sh = sheet.resize((sw, shh))
            rg = [(nm, int(x * sd_scale), int(y * sd_scale), int(ww * sd_scale), int(hh * sd_scale))
                  for nm, x, y, ww, hh in regions]
        for lp, fn in ((f"{b}/P{card}JS", lambda d: replace_textasset(d, skel)),
                       (f"{b}/{scale}/P{card}.atlas", lambda d, s=sh, r=rg: replace_textasset(d, gen_atlas(page, s.size, r))),
                       (f"{b}/{scale}/P{card}", lambda d, s=sh: replace_texture(d, s, name=f"P{card}"))):
            p = tx.game.find(get_crc(lp))
            if not p:
                raise MDStudioError(tr("missing %s") % lp)
            tx.put_existing(p, fn(p.read_bytes()))
    # 动画比 donor 的 Timeline 长就把 Timeline 拉长, 不然放到一半被切
    tl = tx.game.find(get_crc(f"{C.base(card)}/P{card}Timeline"))
    if not tl:
        raise MDStudioError(tr("missing P%sTimeline") % card)
    tx.put_existing(tl, set_timeline_duration(tl.read_bytes(), round(duration + 0.1, 3)))
    tx.rec.meta = {"source": str(source), "frames": len(names), "cell": cell, "duration": duration,
                   "sheet": list(sheet.size), "size": size, "pop": pop}
    return tx.commit()
