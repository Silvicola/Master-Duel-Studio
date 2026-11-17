"""视频 -> 召唤 cut-in (逐帧 Spine)

做法: 先 clone 一个能跑的 cut-in (带 PlayableDirector + Timeline), 再把它的 skeleton json / atlas / 贴图
换成自己生成的. 骨架: 1 bone + 1 slot + N 个 region attachment, 一条换图 timeline.
动画名必须叫 "animation", prefab 播的就是这个名.
"""
import hashlib
import json
import logging

from mdstudio.core.bundle import (fix_clip_duration_mode, keep_atlas_materials, replace_textasset,
                                  replace_texture, retarget_skeleton)
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
        # 有些来源卡把 clip 设成「跟随动画时长」（customDuration=0，如 18182）。
        # 骨架被换掉之后，运行时算不出动画时长，clip 变 0 长，角色停在初始姿势。
        # 统一改成固定时长，跟 3881 这类能正常播的底盘一致（2026-11-17 实测）。
        tpl = tt.get("template")
        if isinstance(tpl, dict) and "customDuration" in tpl and not tpl["customDuration"]:
            tpl["customDuration"] = 1
            changed = True
        for c in tt.get("m_Clips") or []:
            # 轨道名不统一: 实测有 animation / animation_kde（超魔导龙骑士这类）/
            # animation2 / P16632animation / "P16537 animation" 六种写法，
            # 全项目 479 条里只有 399 条叫标准的 animation。按「名字里含 animation」
            # 认，否则遇到 animation_kde 的卡直接判成「没有可拉长的片段」。
            if "animation" not in str(c.get("m_DisplayName") or "").lower():
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


def gen_atlas(pages) -> str:
    """pages: [(页文件名, (宽,高), regions)] -> Spine atlas 文本（可以多页）

    页名必须是 P<卡>.png / P<卡>_2.png：游戏的材质槽和骨架就认这两个名字。
    """
    out = []
    for page, size, regions in pages:
        out += [page, f"size:{size[0]},{size[1]}", "filter:Linear,Linear", "pma:true", "scale:1"]
        for name, x, y, w, h in regions:
            out += [name, f"bounds:{x},{y},{w},{h}"]
    return "\n".join(out) + "\n"


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


def donor_anim_name(game, donor: int) -> str:
    """donor 的 Timeline 播放时按哪个名字找动画。

    Timeline 引一个 Spine.Unity 的 AnimationReferenceAsset，里面记着 animationName，
    游戏拿这个名字去骨架里取动画。它不一定是标准的 animation：实测 #14952 是
    animation_kde，#11016 是 animation。骨架里写错名字，游戏找不到就停在初始姿势，
    看起来像一张半透明的静态图（2026-11-10 用户实测 #22804）。

    定位方式：donor 的资产文件名通常等于 Timeline 里那条 clip 的 m_DisplayName，
    直接拼 {P<donor>/<名字>} 找。任何一步读不出来就退回 animation，不影响主流程。
    """
    from mdstudio.core.crc import get_crc

    try:
        tl = game.find(get_crc(f"{C.base(donor)}/P{donor}Timeline"))
        if not tl:
            return "animation"
        import UnityPy

        disp = ""
        for o in UnityPy.load(tl.read_bytes()).objects:
            if o.type.name != "MonoBehaviour":
                continue
            try:
                tt = o.read_typetree()
            except Exception:
                continue
            for c in tt.get("m_Clips") or []:
                d = str(c.get("m_DisplayName") or "")
                if "animation" in d.lower():
                    disp = d
                    break
            if disp:
                break
        if not disp:
            return "animation"

        asset = game.find(get_crc(f"{C.base(donor)}/{disp}"))
        if not asset:
            return "animation"
        for o in UnityPy.load(asset.read_bytes()).objects:
            if o.type.name != "MonoBehaviour":
                continue
            try:
                tt = o.read_typetree()
            except Exception:
                continue
            name = tt.get("animationName")
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass
    return "animation"


def offset_to_units(offset, size_units: float, frame_width: float) -> float:
    """界面上的「左右移 / 上下移」（帧图像素）-> 骨骼单位。

    帧图宽度会被铺满到 size_units 个骨骼单位（角色尺寸），所以
    1 帧图像素 = size_units / frame_width 个骨骼单位。
    推论：填的数值等于 frame_width（也就是界面上的「像素/帧」）时，
    位移正好一个帧图宽。这条是界面提示的依据，别改成别的口径。

    换来源卡时 size_units 会被 donor_scale 归一化，所以同一个数值在
    不同来源卡上移动的屏幕距离一致。
    """
    return float(offset) * float(size_units) / float(frame_width)


def gen_skeleton(w: float, h: float, names, duration: float, pop=True, pop_curve=None,
                 ox: float = 0.0, oy: float = 0.0, zoom: float = 1.0, rot: float = 0.0,
                 anim_name: str = "animation") -> str:
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
        "animations": {anim_name: anim},
    }
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def needs_matte(source) -> bool:
    """这次导入会不会用到 AI 抠像，判断口径和 make_frames 的 auto 分支一致。

    界面在点「生成」之前问一句，缺模型就先让用户去装，别等跑了半分钟才报错。
    读不出来就当作不需要：真错误留给正式流程去报，这里不能替它下结论。
    """
    raw = F.sample(source, 1)
    return bool(raw) and not F.has_alpha(raw) and F.pick_key_color(raw[0]) is not None


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


def _skeleton_head(data: bytes):
    """-> (包内 CAB 名, SkeletonDataAsset 的 pathID)。找不到回 ("", 0)。"""
    import UnityPy
    env = UnityPy.load(data)
    cab = next(iter(env.file.files), "")
    if cab.endswith(".resS"):
        cab = cab[:-5]
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        try:
            tt = o.read_typetree()
        except Exception:
            continue
        if "skeletonJSON" in tt:            # SkeletonDataAsset 的独有字段
            return cab, o.path_id
    return cab, 0


def _retarget_animation(tx, card: int):
    """只写 HD 时，把动画引用从 SD 骨架改指 HD 骨架。

    Timeline 那条 Spine 轨道播的是一个独立的 clip 资产，它的 skeletonDataAsset
    指向 **SD 骨架**（来源卡也这么挂，因为官方 SD/HD 是同一套骨骼）。不写 SD 的话
    那个引用仍指向来源卡的骨架，动画取到了也套不上我们的帧动画，主体静止不动
    （2026-11-17 实测）。这里读新卡自己的三个包，把引用换成 HD 那一份。
    """
    base = C.base(card)
    anim = tx.read_file(get_crc(f"{base}/animation"))
    if anim is None:
        return                              # 这套底盘没有动画引用资产，没得改
    hd_data = tx.read_file(get_crc(f"{base}/{C.HD}/P{card}JS_SkeletonData"))
    sd_data = tx.read_file(get_crc(f"{base}/{C.SD}/P{card}JS_SkeletonData"))
    if not hd_data or not sd_data:
        raise MDStudioError(tr("P%s: can't find its skeleton data to point the animation at") % card)
    hd_cab, hd_pid = _skeleton_head(hd_data)
    sd_cab, _ = _skeleton_head(sd_data)
    if not hd_cab or not sd_cab or not hd_pid:
        raise MDStudioError(tr("P%s: can't find its skeleton data to point the animation at") % card)
    tx.put_file(get_crc(f"{base}/animation"),
                retarget_skeleton(anim, sd_cab, hd_cab, hd_pid))
    log.info(tr("pointing the animation reference at the HD skeleton"))


def apply(ctx, card: int, source, n=120, cell=1080, duration=4.8, size=None, mode="full",
          sharp=False, pop=None, donor=None, sd_scale=0.5, preview_dir=None,
          pop_curve=None, offset_x=0.0, offset_y=0.0, zoom=1.0, rotate=0.0,
          sharp_model="realesr-animevideov3", force=False, sharp_scale=4, compat_sd=None):
    """size = skeleton 宽. None: full = 4800 (铺满屏), 其他 = 2930 (1440p 上峰值约 110% 屏高)
    pop None: full 不弹 (Phainon 那样整屏不动), 其他照官方弹出放大
    compat_sd None: 读设置里的「低清纹理兼容」, 界面与命令行都不用每次传"""
    # 已经有本工具做的动画时一律拦下，先复原再生成（跟特效页同一口径）。
    # 放在最前面：不建 Tx、不读帧、不查依赖，什么都没动就退回去。
    if C.generated(ctx, card):
        raise MDStudioError(tr("%s already has a cut-in from this tool. Restore it first.") % card)
    if compat_sd is None:
        from mdstudio import settings as _settings
        compat_sd = bool(_settings.get("compat_sd", False))
    if preview_dir:
        from pathlib import Path
        Path(preview_dir).mkdir(parents=True, exist_ok=True)   # 先建好, 别等帧做完才发现路径不对
    if duration > MAX_DURATION:
        raise MDStudioError(tr("duration max is %ss") % MAX_DURATION)
    # BC7 按 4x4 块压, 帧边长必须是 4 的倍数, 否则贴图尺寸对不上
    cell = max(4, int(cell) // 4 * 4)
    # 关键字曲线经 RPC 传进来时是 "0,10,10<8,70,50" 这种字符串 (CLI/GUI 自己会先解析),
    # 不在这里转一下的话, 后面会拿字符串当关键帧表用, 报 float 和 str 不能比较
    if isinstance(pop_curve, str):
        pop_curve = parse_pop_curve(pop_curve) if pop_curve.strip() else None
    size = size or (FULL_SIZE if mode == "full" else 2930)
    pop = (mode != "full") if pop is None else pop
    # 来源卡必须由界面按卡片类型给出（见 cutin.donor_for）。漏传就报错，不再静默回落 11016。
    if not donor:
        raise MDStudioError(tr("pick a donor card for this animation first"))
    donor = int(donor)
    cloned = C.is_cloned(ctx, card)
    # 借来的动画不能直接改（2026-11-13 起这张卡只要自己生成过就算「自己的」）：
    # 借来的那套资源属于别的卡，往里面写贴图会把人家的动画一起改掉。
    # 这里只剩「确实借了、且从没自己做过」才拦 —— is_cloned 已经认 cutin.video，
    # 所以重复生成不会再掉进这个分支。
    if not cloned and C.borrowed(ctx, card):
        raise MDStudioError(tr("%s has a borrowed cut-in. Remove it, the video needs its own clone.") % card)
    # 已经克隆过的卡，底盘就是它自己那一套（克隆时骨架、图集、Timeline 都复制了一份）。
    # 动画名和缩放必须按自己的底盘读：否则用户在来源卡框里改一张，名字会对不上，
    # 缩放系数变了还会让人物在屏幕上突然大一圈。想换底盘得先复原。
    chassis = int(card) if cloned else donor
    # 来源卡的 Spine 物体缩放不一样（实测 0.13~0.22），按 0.20 基准折算骨架尺寸，
    # 这样同一个「角色尺寸」在任何来源卡上都占同样大：屏幕像素 = 尺寸 × 屏宽/5000
    # （七张来源卡的 prefab 相机都是 camera2DSize 5.0，只有物体缩放不同）。
    size_units = float(size) * (C.DONOR_BASE_SCALE / C.donor_scale(ctx.game, chassis))

    # 先把帧和图集做出来 (最容易失败: 没装 rembg / 视频坏 / 图集超限), 成功了才去 clone,
    # 不然失败后留下一个用户没要的 donor cut-in
    frames = make_frames(source, n, cell, mode, sharp=sharp, sharp_model=sharp_model,
                         sharp_scale=sharp_scale)
    fh, fw = frames[0].shape[:2]
    alphas = frame_alphas(pop_curve, len(frames)) if (pop and pop_curve) else None
    # 低清兼容只写一页：SD 底盘多数只有一个贴图槽，第二页没地方放。与其等写到一半
    # 才报「只有一页贴图」，不如现在把上限算出来直接说清楚。判断用实际帧宽高，
    # full 模式下帧不是正方形，拿「像素/帧」比会不准。
    if compat_sd and F._fit_grid(len(frames), fw, fh) is None:
        limit = F.one_page_limit(len(frames))
        raise MDStudioError(tr("low-quality compat writes a single texture page: %s frames allow "
                               "at most about %spx per frame (now %s)")
                            % (len(frames), limit, cell))
    # 三档统一（官方 / 官方★ / 无动画）：低清兼容写 1 页，其余固定写 2 页。
    # 页数不再由参数碰巧决定，选双页来源卡时也不会因为像素小就退化成单页。
    sheets = F.pack_pages(frames, premultiply=True, alphas=alphas,
                          pages=1 if compat_sd else 2)
    if preview_dir:
        for i, (sh, _) in enumerate(sheets):
            sh.save(f"{preview_dir}/sheet{'' if i == 0 else f'_{i + 1}'}.png")
            F.preview_dark(sh, True).save(f"{preview_dir}/preview_dark{'' if i == 0 else f'_{i + 1}'}.png")
    names = [r[0] for _, rs in sheets for r in rs]
    pages = len(sheets)
    # 底盘页数不够就换一张双页来源卡。#21382 这类自带官方动画的卡，官方那套只有一页
    # 贴图（P<卡>_Atlas 里只有一个材质槽），两页序列帧写不进去。材质槽烘在资源里加不
    # 出来，只能改用双页来源卡克隆一份。只写 HD 时只查 HD 的页数。
    want_q = [C.HD] + ([C.SD] if compat_sd else [])
    from mdstudio.core.catalog import Catalog
    pre = Catalog(ctx.game)
    short = [q for q in want_q if C.chassis_pages(pre, chassis, q) < pages]
    if short:
        alt = C.donor_with_pages(ctx, pre, card, pages, want_q)
        if not alt:
            raise MDStudioError(tr("no donor has %s texture pages (%s needed). Use fewer frames "
                                   "or a smaller frame size.") % (pages, "/".join(short)))
        log.info(tr("%s: its chassis has %s texture page(s) in %s, cloning from %s instead"),
                 card, C.chassis_pages(pre, chassis, short[0]), "/".join(short), alt)
        donor = chassis = alt
        size_units = float(size) * (C.DONOR_BASE_SCALE / C.donor_scale(ctx.game, chassis))
    w = size_units
    skel = gen_skeleton(w, w * fh / fw, names, duration, pop, pop_curve,
                        ox=offset_to_units(offset_x, w, fw), oy=offset_to_units(offset_y, w, fw),
                        zoom=float(zoom),
                        rot=float(rotate), anim_name=donor_anim_name(ctx.game, chassis))

    # 克隆与替换并进同一条记录：分开提交会让历史里出现「克隆召唤动画」和
    # 「视频召唤动画」两行，看起来像做了两次（2026-11-12 用户实测）。
    tx = Tx(ctx, "cutin.video", card)
    if not cloned:
        log.info(tr("cloning %s from %s first"), card, donor)
        C.clone(ctx, card, donor, force=force, tx=tx)
        if ctx.dry_run:
            # dry-run 下 clone 没真写, 后面找不到 P<card> 的文件, 到这就够看了
            log.info(tr("[dry-run] then: replace skeleton + atlas + texture of P%s (%s frames)"),
                     card, len(names))
            return None
    C.own_anim(tx, card)
    dirs = C.scale_dirs(tx.catalog, card)
    if not dirs:
        raise MDStudioError(tr("P%s: can't find its scale folders in the manifest") % card)
    step(tr("packing the cut-in textures for P%s (texture compression, can take a minute)"), card)
    import gc
    gc.collect()      # 帧已经拼进 sheet 了, 先把能放的放掉, 16384 的图压 BC7 峰值要好几 G
    # 两档都写时，Timeline 的动画引用挂在 SD 骨架上就够了（SD 写的是我们自己那套）。
    # 只写 HD 时那个引用还指着来源卡的 SD 骨架，动画套不上、主体停在第一帧，
    # 所以先把引用改到 HD（见 _retarget_animation 的说明）。
    if not compat_sd:
        _retarget_animation(tx, card)
    # SD (半尺寸) 先做, HD 那张最大的放最后, 峰值不叠在一起
    for q, scale in sorted(dirs.items(), key=lambda kv: kv[0] != C.SD):
        if q == C.SD and not compat_sd:
            # 不写 SD：底盘那套低清贴图原样留着，引用已改到 HD 骨架。
            # SD 底盘多数只有一个贴图槽，两页动画写进去会直接失败。
            log.info(tr("keeping the SD textures as the chassis has them (low-quality compat is off)"))
            continue
        b = f"{C.base(card)}/{q}"
        scaled = []
        for sheet, regions in sheets:
            sh, rg = sheet, regions
            if q == C.SD and sd_scale != 1:
                # SD 画质用半尺寸, 省显存
                sw, shh = max(4, int(sheet.width * sd_scale) // 4 * 4), max(4, int(sheet.height * sd_scale) // 4 * 4)
                sh = sheet.resize((sw, shh))
                rg = [(nm, int(x * sd_scale), int(y * sd_scale), int(ww * sd_scale), int(hh * sd_scale))
                      for nm, x, y, ww, hh in regions]
            scaled.append((sh, rg))
        atlas = gen_atlas([(f"P{card}{'' if i == 0 else f'_{i + 1}'}.png", s.size, r)
                           for i, (s, r) in enumerate(scaled)])
        writes = [(f"{b}/P{card}JS", lambda d, s=skel: replace_textasset(d, s)),
                  (f"{b}/{scale}/P{card}.atlas", lambda d, a=atlas: replace_textasset(d, a)),
                  # 材质槽裁到实际页数：官方数据里两者恒等，差一个就可能整份图集取不到
                  (f"{b}/{scale}/P{card}_Atlas",
                   lambda d, n=len(scaled): keep_atlas_materials(d, n))]
        for i, (s, _) in enumerate(scaled):
            suffix = "" if i == 0 else f"_{i + 1}"
            writes.append((f"{b}/{scale}/P{card}{suffix}",
                           lambda d, ss=s, suf=suffix: replace_texture(d, ss, name=f"P{card}{suf}")))
        for lp, fn in writes:
            h = get_crc(lp)
            # 克隆出来的文件还在本事务的队列里，磁盘上没有；read_file 两边都看
            data = tx.read_file(h)
            if data is None:
                # 只有第二页会走到这: 克隆来的底盘只带一个贴图槽
                if lp.endswith(f"P{card}_2"):
                    raise MDStudioError(tr("this cut-in chassis has only one texture page, but the sheet needs two. "
                                          "Clone from a two-page card (see the donor list) first."))
                raise MDStudioError(tr("missing %s") % lp)
            tx.put_file(h, fn(data))
    # 动画比 donor 的 Timeline 长就把 Timeline 拉长, 不然放到一半被切
    # Timeline 可能是本次克隆出来的，同样要走事务队列而不是磁盘
    tl_h = get_crc(f"{C.base(card)}/P{card}Timeline")
    tl_data = tx.read_file(tl_h)
    if tl_data is None:
        raise MDStudioError(tr("missing P%sTimeline") % card)
    tx.put_file(tl_h, set_timeline_duration(tl_data, round(duration + 0.1, 3)))
    tx.rec.meta = {"source": str(source), "frames": len(names), "cell": cell, "duration": duration,
                   "sheet": list(sheets[0][0].size), "pages": len(sheets), "size": size,
                   "donor": (C.chassis_donor(ctx, card) or donor) if cloned else donor,
                   "pop": pop, "compat_sd": bool(compat_sd)}
    return tx.commit()
