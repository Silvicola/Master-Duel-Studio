"""视频 -> 召唤 cut-in (逐帧 Spine)

做法: 先 clone 一个能跑的 cut-in (带 PlayableDirector + Timeline), 再把它的 skeleton json / atlas / 贴图
换成自己生成的. 骨架: 1 bone + 1 slot + N 个 region attachment, 一条换图 timeline.
动画名必须叫 "animation", prefab 播的就是这个名.
"""
import hashlib
import json
import logging

from mdstudio.core.baseline import family_for
from mdstudio.core.bundle import (bundle_cab, fix_bundle_dependencies, keep_atlas_materials, new_cab,
                                  replace_textasset, replace_texture, retarget_skeleton, set_image)
from mdstudio.core.crc import get_crc
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr
from mdstudio.ops import cutin as C
from mdstudio.ops.tx import Tx
from mdstudio.ops.video import frames as F
from mdstudio.progress import step

log = logging.getLogger("mdstudio")

# 诊断开关：整帧按四段染色（红→黄→绿→蓝）、顶部画进度条、左上角留两位帧号。
# 目的是判断游戏里到底播了几遍：只要第二遍再看到一次红色，就是整段重播。
# 不用读数字，录 30fps 也看得出来。平时必须为 False。
_STAMP_FRAMES = False

# 默认弹出曲线（用户 Silvicola 自己设计的，旧版 POP_CURVE_SILVICOLA 精简而来）：
# 25% 淡入起步 → 冲到 108% 过冲 → 砸到 76% 收回 → 缓回 90% 收尾。
# 帧号只表达比例，gen_skeleton 按「曲线自己的最后一帧」归一化，所以 48/60/72 帧
# 落点一致（都在 0.545 秒冲顶）。透明度烘进帧图，不写骨架轨道。
DEFAULT_POP_CURVE = "0,25,20<6,55,50<15,92,85<23,108,100<31,76,100<68,90,100"

_SEG4 = ((255, 45, 45), (255, 205, 0), (0, 205, 85), (45, 135, 255))
_TINT = 0.45          # 染色强度：既明显偏色，又看得出角色动作

_SEG = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
        "5": "afgcd", "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcdfg"}


def _seg_digit(d, x, y, w, h, t, segs, color):
    """画一个七段数字。a 上 b 右上 c 右下 d 下 e 左下 f 左上 g 中."""
    box = {
        "a": (x, y, x + w, y + t),
        "b": (x + w - t, y, x + w, y + h // 2),
        "c": (x + w - t, y + h // 2, x + w, y + h),
        "d": (x, y + h - t, x + w, y + h),
        "e": (x, y + h // 2, x + t, y + h),
        "f": (x, y, x + t, y + h // 2),
        "g": (x, y + h // 2 - t // 2, x + w, y + h // 2 + t // 2),
    }
    for s in segs:
        d.rectangle(box[s], fill=color)


def _stamp_index(frames):
    """诊断用：整帧染色 + 进度条 + 两位帧号。

    看颜色就够了：红→黄→绿→蓝 是正常一遍。第二遍如果从头来，会再看到一次红色；
    如果从中间接上，会从中间某个颜色继续；如果颜色完全消失，说明播的是另一套资源。
    """
    from PIL import Image, ImageDraw
    import numpy as np
    n = len(frames)
    out = []
    for i, fr in enumerate(frames):
        seg = min(len(_SEG4) - 1, (i * len(_SEG4)) // max(1, n))
        col = np.array(_SEG4[seg], dtype=np.float32)
        a = fr.astype(np.float32).copy()
        a[:, :, :3] = a[:, :, :3] * (1.0 - _TINT) + col * _TINT
        im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGBA")
        d = ImageDraw.Draw(im)
        w, h = im.size
        # 顶部进度条：长度就是播放进度，段色跟随当前分段
        bar = max(8, h // 16)
        d.rectangle([0, 0, w, bar], fill=(0, 0, 0, 230))
        if i:
            d.rectangle([0, 0, int(w * i / max(1, n - 1)), bar],
                        fill=tuple(int(c) for c in _SEG4[seg]) + (255,))
        # 帧号：放大到画面的 1/4 高，压缩后也认得出来
        dh = max(28, h // 4)
        dw = max(14, dh // 2)
        t = max(4, dh // 7)
        gap = max(5, t)
        mx = max(8, w // 40)
        d.rectangle([0, bar, dw * 2 + gap + mx * 3, bar + dh + mx * 2], fill=(0, 0, 0, 235))
        x = mx
        for ch in "%02d" % (i % 100):
            _seg_digit(d, x, bar + mx, dw, dh, t, _SEG.get(ch, ""), (255, 0, 255, 255))
            x += dw + gap
        out.append(np.array(im))
    return out

# 以前以为 Timeline 卡死 2 秒, 错了: cut-in 的 Timeline 是自己的 P<card>Timeline bundle, 改得动.
# Phainon (21054) 就是把它从 1.6 拉到 4.9 秒, 游戏里整段放完. 真正的上限是图集 16384px
MAX_DURATION = 15.0
FULL_SIZE = 4800     # skeleton 宽; 16:9 视频 4800x2700 = 铺满屏幕 (Phainon 实测)


def set_timeline_duration(data: bytes, seconds: float) -> bytes:
    """P<card>Timeline: 把 "animation" 片段的长度对齐到 seconds, 双向生效.

    **窗口必须严格等于动画长度, 不能多给.** 官方卡的共同点是 clip 长度与骨架里
    动画的最后一个关键帧时间相等（14952 是 1.6 对 1.6, 3881 也是 1.6 对 1.6），
    customDuration 有的写 0 有的写 1, 两种都能正常播一遍。

    我们以前给窗口多加了 0.1 秒。窗口比动画长的那一截会被运行时拿去重播动画
    开头，游戏里就看得到「同一个动作做了两遍」(2026-11-18 用户实测：抬手逗
    手上的小动物做了两次，关掉弹出效果也一样)。现在严格相等，不留余量。
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
        # 不动 m_FixedDuration。TimelineAsset 的 m_DurationMode=0 表示「长度按 clip 算」，
        # 这个模式下 Unity 根本不读 m_FixedDuration（官方 3881/14952 都写 0）。以前
        # 怀疑游戏运行时读它，改来改去既没修好问题，也和官方不一致，保持原值最稳。
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

    **页与页之间必须留一个空行。** Spine 的 atlas 解析器靠空行切页：没有空行时，
    第二个页名会被当成上一页的一个 region，于是第二页的所有 region 全部作废。
    后果很隐蔽：图集看着是两页、贴图也都在，但骨架只认得到第一页的帧，
    运行时算出的动画时长只有第一页那部分（120 张分 64+56，实际只播 64 帧 = 1.07 秒），
    窗口又是完整的 2 秒，于是在游戏里完整播两遍（2026-11-18 帧号实测，官方 atlas
    第 37 行的 P3881_2.png 前面就有一个空行，我们的漏了）。
    """
    out = []
    for page, size, regions in pages:
        if out:
            out.append("")      # 切页标记, 少了第二页整页无效
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
    """按帧取透明度, 交给 pack 烘进帧图.

    帧号按「曲线自己的最后一帧」映射到视频帧，跟 gen_skeleton 里缩放轨道同一套基准：
    改了总帧数透明度曲线也不会错位。曲线帧号等于帧数时就是逐帧取，与旧行为一致。
    """
    last = max(max(fr for fr, _, _ in curve), 1.0)
    span = max(n - 1, 1)
    return [curve_at(curve, i * last / span) for i in range(n)]


def page_textures(cat, card: int, quality: str, scale: str) -> list:
    """底盘现有的页贴图路径，按页序排好。

    页名不一定带下划线：#4007 的三页叫 P4007 / P40072 / P40073。按 P<卡>_<n> 拼路径
    会找不到第二页，报的错还是「底盘只有一页」（2026-11-19 发现）。
    这里只认「卡号后面什么都没有 / 下划线加数字 / 直接跟数字」三种。
    """
    head = f"{C.base(card)}/{quality}/{scale}/P{card}"
    out = []
    for n in cat.names():
        if not n.startswith(head):
            continue
        tail = n[len(head):]
        if not tail:
            out.append((0, n))
        elif tail.startswith("_") and tail[1:].isdigit():
            out.append((int(tail[1:]), n))
        elif tail.isdigit():
            out.append((int(tail), n))
    out.sort()
    return [n for _, n in out]


def timeline_asset(game, card: int, cat=None, deep: bool = False) -> str:
    """这张卡的 Timeline 资产逻辑路径。

    文件名不一定是 P<卡>Timeline。官方有 18 张卡的文件名是从别处复制来没改的：
    #21443 的文件叫 P21433Timeline，但它里面的 Spine clip 引用的正是 21443 自己的
    动画资产（pathID 对得上），所以那是它自家的演出，只是名字没改。

    四种写法，按顺序找：
      1. 标准名 P<卡>Timeline
      2. 顶层、以 Timeline 结尾（#21443 → P21433Timeline）
      3. 任意层级、文件名含 Timeline（#13924 → SD/P13924Timeline，
         #13668 → P13668Timeline_HD）
      4. 直接嵌在 prefab 包里（#16740），只有 deep=True 才会读包确认

    第 4 种要读一个 1.2MB 的包，所以默认不做：donor_anim_name 这类高频调用不能每次都扫。

    找不到就用标准名兜底：克隆出来的卡由 clone 建的就是 P<卡>Timeline，
    这时清单还没落盘，扫不到属于正常。
    """
    from mdstudio.core.catalog import Catalog

    base = C.base(card)
    fixed = f"{base}/P{card}Timeline"
    try:
        c = cat if cat is not None else Catalog(game)
        if fixed in c:
            return fixed
        # 第二种：顶层、名字以 Timeline 结尾
        hits = sorted(n for n in c.names()
                      if n.startswith(base + "/") and n[len(base) + 1:].count("/") == 0
                      and n.lower().endswith("timeline"))
        if hits:
            return hits[0]
        # 第三种：任意层级、文件名里含 Timeline。有多个候选时优先 HD 那档，
        # 避免挑到低清那一份。
        hits = [n for n in c.names()
                if n.startswith(base + "/") and "timeline" in n.rsplit("/", 1)[-1].lower()]
        if hits:
            hits.sort(key=lambda n: (0 if f"/{C.HD}/" in n else
                                     1 if n.endswith("_HD") else 2, n))
            return hits[0]
        # 第四种：嵌在 prefab 包里，没有独立条目可找，只能读包看有没有 TimelineAsset
        if deep:
            import UnityPy

            for q in (C.HD, C.SD):
                lp = f"{base}/{q}/P{card}"
                if lp not in c:
                    continue
                p = game.find_logical(lp)
                if not p:
                    continue
                try:
                    env = UnityPy.load(p.read_bytes())
                except Exception:
                    continue
                for o in env.objects:
                    if o.type.name != "MonoBehaviour":
                        continue
                    try:
                        tt = o.read_typetree()
                    except Exception:
                        continue
                    if "m_Tracks" in tt:
                        return lp
        return fixed
    except Exception:
        return fixed


def donor_anim_name(game, donor: int) -> str:
    """donor 的 Timeline 播放时按哪个名字找动画。

    Timeline 的 Spine 轨道上挂一个 SpineAnimationStateClip，它的
    `template.animationReference` 指向这张卡自己的 AnimationReferenceAsset，
    那个资产里的 `animationName` 才是游戏去骨架里取动画用的名字。

    名字不止一种（全库 496 张实测：animation 390、animation_kde 49、animation2 8、
    animation3 5、animation_kde2 4，另有 20 张各是 kde / Kde / kde2 / kde4 /
    animation_KDE / animation_USP / P7550 之类的独有名字）。写错名字游戏就取不到
    动画，角色停在初始姿势，看起来是一张半透明静态图（2026-11-10 用户实测 #22804）。

    取法（2026-11-19 全库核对，旧写法有 16 张会写错）：
      1. 读 Timeline 里 SpineAnimationStateClip 的 animationReference pathID；
      2. 在 donor 自己 P<donor>/ 下的顶层资产里按这个 pathID 找，读 animationName。
         按 pathID 而不是按文件名：文件名叫 P7253animation 的，里面写的是 animation；
         有的卡同时有两个资产（animation 和 animation2），只有 pathID 分得清是哪个。
      3. 没有自己的 Timeline（借别卡的）或对不上 pathID 时，退一步看「唯一一个带
         animationName 的资产」。这类卡照样有自己的动画资产，只是 Timeline 借的。
      4. 都没有才退 animation。
    """
    from mdstudio.core.crc import get_crc

    try:
        import UnityPy

        base = C.base(donor)
        pid = None
        tl = game.find(get_crc(timeline_asset(game, donor)))
        if tl:
            for o in UnityPy.load(tl.read_bytes()).objects:
                if o.type.name != "MonoBehaviour":
                    continue
                try:
                    tt = o.read_typetree()
                except Exception:
                    continue
                if tt.get("m_Name") != "SpineAnimationStateClip":
                    continue
                ref = (tt.get("template") or {}).get("animationReference") or {}
                pid = ref.get("m_PathID")
                if pid:
                    break

        from mdstudio.core.catalog import Catalog

        cat = Catalog(game)

        def collect(top_only: bool):
            """扫这张卡的包，收集所有带 animationName 的对象（pathID, 名字）。

            top_only 先只扫顶层：绝大多数卡就够，快。找不到再放开到子目录，
            因为 #13668 这类把动画资产放在 HighEnd_HD/animation_kde 下。
            """
            out = []
            for lp in cat.names():
                if not lp.startswith(base + "/"):
                    continue
                if top_only and lp[len(base) + 1:].count("/") != 0:
                    continue
                tail = lp.rsplit("/", 1)[-1]
                if "timeline" in tail.lower() or tail.endswith(".atlas"):
                    continue
                p = game.find_logical(lp)
                if not p:
                    continue
                env = UnityPy.load(p.read_bytes())
                for o in env.objects:
                    if o.type.name != "MonoBehaviour":
                        continue
                    try:
                        tt = o.read_typetree()
                    except Exception:
                        continue
                    nm = tt.get("animationName")
                    if isinstance(nm, str) and nm.strip():
                        out.append((o.path_id, nm.strip()))
            return out

        found = collect(True)
        if pid is not None and not any(x[0] == pid for x in found):
            # 顶层没命中，动画资产可能在子目录里
            found = collect(False)
        if pid is not None:
            for path_id, nm in found:
                if path_id == pid:
                    return nm
        if found:
            distinct = {nm for _, nm in found}
            if len(distinct) == 1:
                return found[0][1]
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
    # 帧数就是「一共多少帧」，时长就是总时长，二者各按各的参数走：动画时长由最后一个
    # 关键帧的时间决定，跟帧数没有换算关系（60 帧铺在 2 秒上就是 30fps 的观感，正常）。
    # 声明 fps 写 60 与官方一致，运行时只把它当编辑器提示。
    # 2026-11-18 那轮「播两遍」跟帧数、帧率都无关，真因是 gen_atlas 页间漏了空行，
    # 第二页整页作废，timeline 只剩第一页的关键帧（详见 gen_atlas 的说明）。
    fps = 60
    anim = {"slots": {"frame": {"attachment": [
        {"time": round(i * duration / max(n - 1, 1), 4), "name": nm}
        for i, nm in enumerate(names)]}}}
    skel = {"hash": hashlib.md5(f"flip{n}".encode()).hexdigest()[:11], "spine": "4.2.20",
            "x": -w / 2, "y": -h / 2, "width": w, "height": h, "fps": fps, "images": "./images/"}
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
        # 曲线帧号只是「比例」，按它自己的最后一帧归一化到时长上：这样改了总帧数，
        # 节奏不变（48 帧写的曲线配 60 帧视频，冲顶仍在 0.54 秒），曲线也不会提前
        # 结束留下静止的一截。没传曲线就用默认那条（用户自己设计的那套）。
        curve = pop_curve or parse_pop_curve(DEFAULT_POP_CURVE)
        last = max(max(fr for fr, _, _ in curve), 1.0)
        root = []
        for fr, sc, _ in curve:
            t = round(min(fr, last) / last * d, 4)
            if root and root[-1]["time"] == t:
                root[-1].update({"x": round(sc * k, 4), "y": round(sc * k, 4)})
            else:
                root.append({"time": t, "x": round(sc * k, 4), "y": round(sc * k, 4)})
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


def _anim_asset_name(tx, card: int):
    """这张卡 base 下一层的动画引用资产名。

    实测有 animation / animation_kde / animation2 / P16632animation /
    "P16537 animation" 几种写法（全项目只有约八成叫标准的 animation）。
    这里按「名字里含 animation」认，和 set_timeline_duration 匹配 clip 的口径一致。
    以前 _retarget_animation 按 `{base}/animation` 硬读，其余写法读不到就静默跳过，
    Timeline 的引用仍指向旧骨架，游戏播的是来源卡那套动画（2026-11-18 用户实测）。
    """
    base = C.base(card)
    pref = base + "/"
    cands = []
    for name in tx.catalog.names():
        if not name.startswith(pref):
            continue
        tail = name[len(pref):]
        # 只认 base 下一层的资产：HighEnd_HD/... 那些由各自目录自己管
        if not tail or "/" in tail:
            continue
        if "animation" in tail.lower():
            cands.append(tail)
    if not cands:
        return None
    return "animation" if "animation" in cands else sorted(cands)[0]


def _add_texture_page(tx, card: int, quality: str, scale: str, img) -> None:
    """给只有 1 页贴图的底盘补第 2 页，prefab / Timeline / 相机全部不动。

    材质槽是烘在 AtlasAsset 的 materials 数组里的，正常手段加不出来，但可以克隆：
      1. 第 1 页贴图包 -> 第 2 页贴图包（改名 + 换图 + 新 CAB）
      2. 第 1 页材质包 -> 第 2 页材质包（改名 + _MainTex 指到新页 + 新 CAB）
      3. AtlasAsset 的 materials / externals 各追加一项
    2026-11-18 对 #21359 做过离线验证：三步都能存回并读对，pathID 保持不变，
    PPtr 全都连得上。换模板会连 prefab 和 Timeline 一起换掉，这条路不会。
    """
    import copy

    import UnityPy

    base = C.base(card)
    page1_lp = f"{base}/{quality}/{scale}/P{card}"
    page2_lp = f"{base}/{quality}/{scale}/P{card}_2"
    mat_lp = f"{base}/{quality}/{scale}/P{card}_Material"
    mat2_lp = f"{base}/{quality}/{scale}/P{card}_Material2"
    atlas_lp = f"{base}/{quality}/{scale}/P{card}_Atlas"

    d1 = tx.read_file(get_crc(page1_lp))
    dmat = tx.read_file(get_crc(mat_lp))
    datl = tx.read_file(get_crc(atlas_lp))
    if d1 is None or dmat is None or datl is None:
        raise MDStudioError(tr("P%s: can't find its texture, material or atlas to add a second page")
                            % card)

    # 1) 第 2 页贴图包
    page1_h = get_crc(page1_lp)
    page2_h = get_crc(page2_lp)
    page2_cab = new_cab(page2_h)
    old_cab = bundle_cab(d1)
    env = UnityPy.load(d1)
    for o in env.objects:
        if o.type.name == "Texture2D":
            t = o.read()
            t.m_Name = f"P{card}_2"
            set_image(t, img, True)
            t.save()
        elif o.type.name == "AssetBundle":
            tt = o.read_typetree()
            n = f"{page2_h[:2]}/{page2_h}"
            tt["m_Name"] = n
            tt["m_AssetBundleName"] = n
            tt["m_Container"] = [(p.replace(f"/p{card}.", f"/p{card}_2."), v)
                                 for p, v in tt.get("m_Container") or []]
            o.save_typetree(tt)
    env.file.files = {(page2_cab + k[len(old_cab):] if k.startswith(old_cab) else k): v
                      for k, v in env.file.files.items()}
    tx.put_file(page2_h, env.file.save(packer="lz4"))
    tx.set_catalog(page2_lp, list(tx.catalog.deps(page1_lp) or []))

    # 2) 第 2 页材质包
    mat2_h = get_crc(mat2_lp)
    mat2_cab = new_cab(mat2_h)
    old2 = bundle_cab(dmat)
    env2 = UnityPy.load(dmat)
    sf2 = [x for x in env2.file.files.values() if hasattr(x, "externals")][0]
    for e in sf2.externals:
        e.path = f"archive:/{page2_cab}/{page2_cab}"
    for o in env2.objects:
        if o.type.name == "Material":
            tt = o.read_typetree()
            tt["m_Name"] = f"P{card}_Material2"
            o.save_typetree(tt)
        elif o.type.name == "AssetBundle":
            tt = o.read_typetree()
            n = f"{mat2_h[:2]}/{mat2_h}"
            tt["m_Name"] = n
            tt["m_AssetBundleName"] = n
            tt["m_Container"] = [(p.replace(f"/p{card}_material", f"/p{card}_material2"), v)
                                 for p, v in tt.get("m_Container") or []]
            o.save_typetree(tt)
    env2.file.files = {(mat2_cab + k[len(old2):] if k.startswith(old2) else k): v
                       for k, v in env2.file.files.items()}
    # 材质包的依赖还指着第 1 页贴图（它是从第 1 页材质克隆来的），换成第 2 页。
    # 不换的话第 2 页贴图包不会被自动加载，材质 2 的 _MainTex 是 null。
    tx.put_file(mat2_h, fix_bundle_dependencies(env2.file.save(packer="lz4"),
                                                swap={page1_h: page2_h}))
    # manifest_tcg 才是游戏真正读的依赖表（bundle 自带的 m_Dependencies 它不看）。
    # 材质 2 是从材质 1 克隆的，依赖还指着第 1 页贴图，要换成第 2 页。
    tx.set_catalog(mat2_lp, [page2_lp if d == page1_lp else d
                             for d in (tx.catalog.deps(mat_lp) or [])])

    # 3) AtlasAsset 追加第二个材质槽（fileID = externals 索引 + 1）
    env3 = UnityPy.load(datl)
    sf3 = [x for x in env3.file.files.values() if hasattr(x, "externals")][0]
    for o in env3.objects:
        if o.type.name != "MonoBehaviour":
            continue
        tt = o.read_typetree()
        mats = tt.get("materials")
        if not isinstance(mats, list) or not mats or len(mats) >= 2:
            continue
        mats.append({"m_FileID": len(sf3.externals) + 1, "m_PathID": mats[0]["m_PathID"]})
        tt["materials"] = mats
        o.save_typetree(tt)
    if len(sf3.externals) < 3:
        ne = copy.copy(sf3.externals[-1])
        ne.path = f"archive:/{mat2_cab}/{mat2_cab}"
        sf3.externals.append(ne)
    # AtlasAsset 的包里也要登记材质 2 的依赖，否则它不会被一起加载（官方 2 页卡
    # 的 _Atlas 包依赖就是「atlas 文本 + 两个材质」三条，2026-11-18 对比确认）。
    tx.put_file(get_crc(atlas_lp), fix_bundle_dependencies(env3.file.save(packer="lz4"),
                                                           add=[mat2_h]))
    # manifest 里要把材质 2 和第 2 页贴图登记到「加载链」上。官方 2 页卡（P3881）
    # 的写法是扁平列出，三个入口都带齐：prefab、SkeletonData、AtlasAsset 的
    # dependencies 里同时有「第1页贴图/材质」和「第2页贴图/材质」。
    # 少了这两条，游戏不会加载它们，AtlasAsset.materials[1] 解析成 null，
    # 运行时拿到空材质，每个 attachment 退回整张图集的 UV（2026-11-18 实测）。
    for entry in (atlas_lp, f"{base}/{quality}/P{card}JS_SkeletonData", f"{base}/{quality}/P{card}"):
        deps = list(tx.catalog.deps(entry) or [])
        if not deps:
            continue
        for extra in (mat2_lp, page2_lp):
            if extra not in deps:
                deps.append(extra)
        tx.set_catalog(entry, deps)
    log.info(tr("P%s: %s has a single texture page, added a second one without swapping the chassis"),
             card, quality)


def _strip_custom_materials(data: bytes) -> bytes:
    """清掉 prefab 上 SkeletonRendererCustomMaterials 的覆盖项。

    来源卡的 prefab 可能带这个组件，按 slot 名（`EF_RodLightA01` 这类特效槽）替换
    材质，而且 `overrideDisabled` 是 0（启用）。我们的逐帧骨架只有一个 `frame` slot，
    这些名字一个都不存在，组件初始化时取不到 slot，整个 Spine 渲染起不来。游戏里的
    表现是角色完全不出现、召唤演出卡住不结束（2026-11-19 底盘从 3881 换成 3880
    黑魔导后出现，3880 有这个组件、3881 没有）。

    只清空两个数组，保留组件本身：删组件要改 GameObject 的 m_Component 列表，
    风险比清空大得多。我们的骨架也用不到来源卡的特效材质覆盖。
    """
    import UnityPy

    try:
        env = UnityPy.load(data)
        changed = False
        for o in env.objects:
            if o.type.name != "MonoBehaviour":
                continue
            try:
                tt = o.read_typetree()
            except Exception:
                continue
            if "customSlotMaterials" not in tt:
                continue
            if tt.get("customSlotMaterials"):
                tt["customSlotMaterials"] = []
                changed = True
            if tt.get("customMaterialOverrides"):
                tt["customMaterialOverrides"] = []
                changed = True
            if changed:
                o.save_typetree(tt)
        if not changed:
            return data
        return env.file.save(packer="lz4")
    except Exception:
        log.warning(tr("could not clear the custom material overrides on the prefab, keeping it as is"))
        return data


def _fill_embedded(data: bytes, skel: str, atlas: str, pages: int, seconds: float):
    """内嵌式 prefab 包：骨架、atlas 文本、AtlasAsset、Timeline 全在这一个包里。

    全库有 5 张卡是这种结构（#3438 / #14409 / #16091 / #16740 / #23852）：它们的
    SkeletonData.skeletonJSON、AtlasAsset.atlasFile 的 m_FileID 都是 0，指向同包内的
    TextAsset。外部那套 `P<卡>JS` 和 `P<卡>.atlas` 它根本不读，写外部等于没写。

    必须在一次 env 里全部改完再 save：分开 save 会互相覆盖（后一次基于前一次的结果
    重写整包，前一次的改动就被冲掉了）。

    返回改过的 bytes；不是内嵌式（没有同包 SkeletonData）就返回 None，交回旧路径。
    """
    import UnityPy

    env = UnityPy.load(data)
    skel_mb = atlas_mb = None
    skel_tt = atlas_tt = None
    clip_mbs = []
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        try:
            tt = o.read_typetree()
        except Exception:
            continue
        ref = tt.get("skeletonJSON")
        if ref is not None and ref.get("m_FileID") == 0 and ref.get("m_PathID"):
            skel_mb, skel_tt = o, tt
        ref = tt.get("atlasFile")
        if ref is not None and ref.get("m_FileID") == 0 and "materials" in tt:
            atlas_mb, atlas_tt = o, tt
        # clip 挂在轨道对象上，跟 TimelineAsset（有 m_Tracks）是两个对象，都要收
        if "m_Clips" in tt:
            clip_mbs.append((o, tt))
    if skel_mb is None:
        return None

    skel_pid = (skel_tt.get("skeletonJSON") or {}).get("m_PathID")
    atlas_pid = (atlas_tt.get("atlasFile") or {}).get("m_PathID") if atlas_tt else None
    for o in env.objects:
        if o.type.name != "TextAsset":
            continue
        if o.path_id == skel_pid:
            d = o.read()
            d.m_Script = skel
            d.save()
        elif o.path_id == atlas_pid:
            d = o.read()
            d.m_Script = atlas
            d.save()

    # 材质槽必须等于页数：官方数据里两者恒等，差一个就可能整份图集取不到
    # 有些副本包只有骨架没有 AtlasAsset（#16091 的 animation 1 就是），那时跳过这步
    if atlas_mb is not None and atlas_tt is not None:
        mats = atlas_tt.get("materials") or []
        if len(mats) != pages:
            atlas_tt["materials"] = mats[:pages]
            atlas_mb.save_typetree(atlas_tt)

    for mb, tt in clip_mbs:
        hit = False
        for c in tt.get("m_Clips") or []:
            if "animation" in str(c.get("m_DisplayName") or "").lower():
                c["m_Duration"] = round(seconds, 3)
                hit = True
        if hit:
            mb.save_typetree(tt)

    return env.file.save(packer="lz4")


def _fill_sibling_embedded(tx, card: int, quality: str, skip_h: str, skel: str,
                           atlas: str, pages: int, seconds: float) -> int:
    """同一档里还有别的内嵌包时，也把它们换成新骨架。

    #16091 除了 prefab 包，顶层还有一个 `animation 1` 包，里面另存了一份骨架副本，
    而顶层 Timeline 的 clip 恰恰引用那一份。只改 prefab 的话，Timeline 走的那条链
    还是旧骨架，游戏里就是「角色出来了但不动」。

    SD 那套保持官方原样：它没换骨架，改了反而破坏。返回改了几个包。
    """
    n = 0
    base = C.base(card) + "/"
    for lp in tx.catalog.names():
        if not lp.startswith(base) or f"/{C.SD}/" in lp:
            continue
        if quality == C.SD and f"/{C.HD}/" in lp:
            continue
        h = get_crc(lp)
        if h == skip_h:
            continue
        data = tx.read_file(h)
        if data is None:
            continue
        new = _fill_embedded(data, skel, atlas, pages, seconds)
        if new is None:
            continue
        tx.put_file(h, new)
        n += 1
    return n


def _fix_extra_timelines(tx, card: int, seconds: float, skip=()) -> int:
    """把这张卡其余 Timeline 包里的 animation clip 也对到同一时长。

    内嵌式卡（#16091）除了 prefab 包里那份，顶层还可能有独立的 `P16091Timeline 1`，
    两个包各有一套 clip。这些卡没有 PlayableDirector 可以参照，静态判断不了游戏到底
    读哪一份，所以全部对齐到同一个时长最保险。

    没有 animation clip 的包跳过，返回改了几个。
    """
    n = 0
    base = C.base(card) + "/"
    for lp in tx.catalog.names():
        if not lp.startswith(base):
            continue
        if "timeline" not in lp.rsplit("/", 1)[-1].lower():
            continue
        h = get_crc(lp)
        if h in skip:
            continue
        data = tx.read_file(h)
        if data is None:
            continue
        try:
            tx.put_file(h, set_timeline_duration(data, seconds))
        except MDStudioError:
            continue
        n += 1
    return n


def _anim_old_cab(data: bytes):
    """动画引用资产当前指向的骨架包 CAB（从它自己的 skeletonDataAsset 引用反查）。

    不能按 `SD/P<卡>JS_SkeletonData` 猜路径：#22519 的 SD 骨架在 `SD/0.39/` 下，
    猜出来的 CAB 跟引用里记的对不上，替换就整个空转了（日志还照打「pointed」，
    实测主体静止）。同一包内引用（m_FileID 0）不算外部骨架，返回 None。
    """
    import UnityPy
    env = UnityPy.load(data)
    for sf in env.file.files.values():
        exts = list(getattr(sf, "externals", []) or [])
        for o in env.objects:
            if o.type.name != "MonoBehaviour":
                continue
            try:
                tt = o.read_typetree()
            except Exception:
                continue
            ref = tt.get("skeletonDataAsset")
            if not isinstance(ref, dict):
                continue
            fid = int(ref.get("m_FileID") or 0)
            if 1 <= fid <= len(exts):
                return getattr(exts[fid - 1], "name", "") or None
    return None


def _retarget_animation(tx, card: int):
    """只写 HD 时，把动画引用从 SD 骨架改指 HD 骨架。

    Timeline 那条 Spine 轨道播的是一个独立的 clip 资产，它的 skeletonDataAsset
    指向 **SD 骨架**（来源卡也这么挂，因为官方 SD/HD 是同一套骨骼）。不写 SD 的话
    那个引用仍指向来源卡的骨架，动画取到了也套不上我们的帧动画，主体静止不动
    （2026-11-17 实测）。这里把引用换成 HD 那一份。

    旧 CAB 从资产自己的 external 引用取，不按 SD 目录猜：#22519 的 SD 骨架在
    `SD/0.39/`，猜 `SD/P<卡>JS_SkeletonData` 会读到另一个包，替换命中 0 处。
    """
    base = C.base(card)
    name = _anim_asset_name(tx, card)
    if name is None:
        log.info(tr("P%s: no animation reference asset, skipped retargeting"), card)
        return
    h = get_crc(f"{base}/{name}")
    anim = tx.read_file(h)
    if anim is None:
        log.info(tr("P%s: animation reference %s not found, skipped retargeting"), card, name)
        return
    hd_logical = f"{base}/{C.HD}/P{card}JS_SkeletonData"
    hd_data = tx.read_file(get_crc(hd_logical))
    if not hd_data:
        raise MDStudioError(tr("P%s: can't find its skeleton data to point the animation at") % card)
    hd_cab, hd_pid = _skeleton_head(hd_data)
    if not hd_cab or not hd_pid:
        raise MDStudioError(tr("P%s: can't find its skeleton data to point the animation at") % card)
    old_cab = _anim_old_cab(anim)
    if not old_cab:
        log.info(tr("P%s: animation reference %s has no external skeleton, left as is"), card, name)
        return
    if old_cab == hd_cab:
        log.info(tr("P%s: animation reference %s already points at the HD skeleton"), card, name)
        return
    tx.put_file(h, retarget_skeleton(anim, old_cab, hd_cab, hd_pid))
    # 依赖表要跟着换：只改包内 externals 而 manifest 里没有 HD 那个包，游戏加载这个
    # 资产时 HD 骨架不在内存里，引用解析不出来，表现仍然是静止。
    # 按路径名认 SD 那一份（externals 里记的是 CAB 名，跟 get_crc 的前缀目录不同源，
    # 没法拿它反查 manifest 条目）。
    key = f"{base}/{name}"
    deps = tx.catalog.deps(key)
    if deps and hd_logical in tx.catalog:
        tail = f"P{card}JS_SkeletonData"
        new_deps = []
        for d in deps:
            if d.endswith(tail) and f"/{C.HD}/" not in d:
                d = hd_logical
            if d not in new_deps:
                new_deps.append(d)
        if new_deps != deps:
            tx.set_catalog(key, new_deps)
            log.info(tr("P%s: the dependency list of %s now loads the HD skeleton"), card, name)
    log.info(tr("P%s: pointed the animation reference %s at the HD skeleton"), card, name)


def _restore_sd(tx, card: int) -> int:
    """关掉低清兼容时，把低清那一档写回生成之前的样子。

    以前这里是「不写 SD，底盘原样留着」。可底盘上的 SD 往往就是上一次开着低清生成时
    写进去的那套帧：关掉开关重新生成，HD 换了新画面，低画质下还是上一次的旧动画
    （2026-11-19 用户提出）。所以关掉时要把有基线的 SD 文件写回去。

    只做三件事：写回有基线的、跳过基线记「原本不存在」的（克隆卡那套本来就没有官方
    原版，写不回去）、不删文件也不动依赖表。删文件会让低画质下的 prefab 找不到骨架，
    那是 #22519 静止那次的同一个坑。
    """
    paths = C.asset_paths(tx.catalog, card, C.SD)
    if not paths:
        return 0
    fam = family_for(tx.rec.op)
    n = 0
    for lp in (paths.get("prefab"), paths.get("skeleton"), paths.get("atlas"),
               paths.get("atlas_asset"), *paths.get("pages", [])):
        if not lp:
            continue
        h = get_crc(lp)
        rel = tx.game.rel(tx.game.path_for(h))
        src = tx.ctx.journal.baselines.path_of(card, fam, rel)
        if src is None:
            continue
        data = src.read_bytes()
        if tx.read_file(h) == data:
            continue
        tx.put_file(h, data)
        n += 1
    return n


def apply(ctx, card: int, source, n=120, cell=1080, duration=4.8, size=None, mode="full",
          sharp=False, pop=None, donor=None, sd_scale=0.5, preview_dir=None,
          pop_curve=None, offset_x=0.0, offset_y=0.0, zoom=1.0, rotate=0.0,
          sharp_model="realesr-animevideov3", force=False, sharp_scale=4, compat_sd=None):
    """size = skeleton 宽. None: full = 4800 (铺满屏), 其他 = 2930 (1440p 上峰值约 110% 屏高)
    pop None: full 不弹 (Phainon 那样整屏不动), 其他照官方弹出放大
    compat_sd None: 读设置里的「低清纹理兼容」, 界面与命令行都不用每次传"""
    # 来源卡（无动画卡的克隆底盘）不允许改：它的页数与材质槽会被后来克隆的卡继承，
    # 改过之后那些卡会跟着漂到不匹配的结构。复原不受这条限制。
    if C.is_donor_card(card):
        raise MDStudioError(tr("%s is a template donor card. Its cut-in cannot be changed.") % card)
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
    # 没给曲线就用默认那条（用户自己设计的那套）。界面「效果」下拉里的「默认」就是它，
    # 所以这里兜底之后，弹出一律按曲线走，不再有「官方硬编码曲线」这个隐藏分支。
    if pop and not pop_curve:
        pop_curve = parse_pop_curve(DEFAULT_POP_CURVE)
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
    # 卡自己就有官方召唤动画时，底盘就是它自己的那一套，原地改，不克隆别人的。
    # 克隆会把 prefab、Timeline、相机一起换掉，官方演出结构就没了；原地改只换
    # 骨架、图集、贴图，材料槽不够时由 _add_texture_page 补。
    from mdstudio.core.catalog import Catalog
    from mdstudio.core.gate import CutinGate
    pre = Catalog(ctx.game)
    own_chassis = int(card) in CutinGate(ctx.game).mrks()
    if own_chassis and not cloned and not C.asset_paths(pre, card, C.HD):
        # 有官方动画、但高清那一档没有可替换的整套资源（#7302 的高清目录里只有贴图，
        # 没有 prefab；#13090 连条目都没有）。原地改无从下手，按「没有自己的底盘」处理，
        # 给它克隆一套来源卡的。不这么做的话后面会只改动画引用和窗口，骨架与贴图一笔
        # 没写，用户进游戏看到的还是官方动画（2026-11-19 实测 7302）。
        own_chassis = False
        log.info(tr("P%s: its own cut-in has no HD resources to swap, cloning one"), card)
    chassis = int(card) if (cloned or own_chassis) else donor
    # 来源卡的 Spine 物体缩放不一样（实测 0.13~0.22），按 0.20 基准折算骨架尺寸，
    # 这样同一个「角色尺寸」在任何来源卡上都占同样大：屏幕像素 = 尺寸 × 屏宽/5000
    # （七张来源卡的 prefab 相机都是 camera2DSize 5.0，只有物体缩放不同）。
    size_units = float(size) * (C.DONOR_BASE_SCALE / C.donor_scale(ctx.game, chassis))

    # 先把帧和图集做出来 (最容易失败: 没装 rembg / 视频坏 / 图集超限), 成功了才去 clone,
    # 不然失败后留下一个用户没要的 donor cut-in
    # 两道前置校验，都在读帧之前：素材帧数够不够、参数算出来几页。等解码完再报，
    # 用户要白等几十秒才知道参数不对（2026-11-18 实测 60 帧 @3072 等了一分多钟）。
    have = F.source_frames(source)
    if have is not None and have < n:
        raise MDStudioError(tr("the source has only %s frame(s) but %s were requested. Lower the "
                               "frame count or use a longer clip.") % (have, n))
    # 页数按「每帧占 cell × cell」估，不看裁剪后的实际宽高：用户设的两个数就能算出
    # 结论，可预期。裁剪后帧可能更窄，实际容量更大，所以后面还有一次按真实宽高的校验，
    # 那一次只会更宽松，不会推翻这里的拒绝。
    per_page = (F.MAX_TEX // cell) ** 2
    need_pages = -(-n // per_page)
    if compat_sd:
        if need_pages > 1:
            raise MDStudioError(tr("%s frames at %spx don't fit one texture page (low-quality compat "
                                   "writes a single page). Lower the size per frame or reduce the count.")
                                % (n, cell))
    elif need_pages > 2:
        raise MDStudioError(tr("%s frames at %spx need more than two texture pages. Lower the size per "
                               "frame or reduce the frame count.") % (n, cell))
    # 解帧阶段每个帧都是一整张 RGBA，帧数 × 边长² × 4 字节就是峰值内存。界面上限放到
    # 4096 之后，200 帧 @4096 会先把这个吃干净再轮到「两页装不下」的报错，所以这里先挡。
    need_gb = n * cell * cell * 4 / 1e9
    if need_gb > 6:
        raise MDStudioError(tr("%s frames at %spx need about %s GB of memory. Reduce the frame "
                               "count or the size per frame.") % (n, cell, round(need_gb, 1)))
    frames = make_frames(source, n, cell, mode, sharp=sharp, sharp_model=sharp_model,
                         sharp_scale=sharp_scale)
    alphas = frame_alphas(pop_curve, len(frames)) if pop else None
    if _STAMP_FRAMES:
        frames = _stamp_index(frames)
    fh, fw = frames[0].shape[:2]
    # 页数由参数算，不写死：一页装得下就是 1 页，装不下看两页够不够，两页还不够
    # 就直接报错让用户调小「像素/帧」或减少帧数。底盘最多两个材质槽，第三页没地方挂。
    # 低清兼容固定单页（SD 只有一个贴图槽），装不下同样报错。
    one = F._fit_grid(len(frames), fw, fh)
    if compat_sd:
        if one is None:
            raise MDStudioError(tr("%s frames at %spx don't fit one texture page (low-quality compat "
                                   "writes a single page). Lower the size per frame or reduce the count.")
                                % (len(frames), max(fw, fh)))
        want_pages = 1
    elif one is not None:
        want_pages = 1
    elif F._fit_grid((len(frames) + 1) // 2, fw, fh) is not None:
        want_pages = 2
    else:
        raise MDStudioError(tr("%s frames at %spx need more than two texture pages. "
                               "Lower the size per frame or reduce the frame count.")
                            % (len(frames), max(fw, fh)))
    sheets = F.pack_pages(frames, premultiply=True, alphas=alphas, pages=want_pages)
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
    short = [q for q in want_q if C.chassis_pages(pre, chassis, q) < pages]
    expand_qs = set()
    if short and chassis == card:
        # 自己的底盘页数不够：给它补页，不换模板。prefab / Timeline / 相机全部保留，
        # 官方那套演出结构不动，只把缺失的贴图页和材质槽补上（见 _add_texture_page）。
        if pages > 2:
            raise MDStudioError(tr("P%s only has %s texture page(s); %s frames need %s pages. "
                                   "Use fewer frames or a smaller size.")
                                % (card, C.chassis_pages(pre, chassis, short[0]), len(names), pages))
        expand_qs = set(short)
        log.info(tr("P%s: keeps its own chassis, adding the missing texture page"), card)
    elif short:
        alt = C.donor_with_pages(ctx, pre, card, pages, want_q)
        if not alt:
            raise MDStudioError(tr("no donor has %s texture pages (%s needed). Use fewer frames "
                                   "or a smaller frame size.") % (pages, "/".join(short)))
        log.info(tr("%s: its chassis has %s texture page(s) in %s, cloning from %s instead"),
                 card, C.chassis_pages(pre, chassis, short[0]), "/".join(short), alt)
        donor = chassis = alt
        size_units = float(size) * (C.DONOR_BASE_SCALE / C.donor_scale(ctx.game, chassis))
    w = size_units
    anim_name = donor_anim_name(ctx.game, chassis)
    skel = gen_skeleton(w, w * fh / fw, names, duration, pop, pop_curve,
                        ox=offset_to_units(offset_x, w, fw), oy=offset_to_units(offset_y, w, fw),
                        zoom=float(zoom),
                        rot=float(rotate), anim_name=anim_name)

    # 克隆与替换并进同一条记录：分开提交会让历史里出现「克隆召唤动画」和
    # 「视频召唤动画」两行，看起来像做了两次（2026-11-12 用户实测）。
    tx = Tx(ctx, "cutin.video", card)
    if not cloned and not own_chassis:
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
        # 有些卡整套资源用别的卡号命名（#3898 存的是 P3893），按名字找缩放目录会落空，
        # 这时从 prefab 的依赖里反查真实路径（见 C.asset_paths）。
        dirs = {q: a["scale"] for q in (C.HD, C.SD)
                if (a := C.asset_paths(tx.catalog, card, q))}
    step(tr("packing the cut-in textures for P%s (texture compression, can take a minute)"), card)
    import gc
    gc.collect()      # 帧已经拼进 sheet 了, 先把能放的放掉, 16384 的图压 BC7 峰值要好几 G
    # 两档都写时，Timeline 的动画引用挂在 SD 骨架上就够了（SD 写的是我们自己那套）。
    # 只写 HD 时那个引用还指着来源卡的 SD 骨架，动画套不上、主体停在第一帧，
    # 所以先把引用改到 HD（见 _retarget_animation 的说明）。
    if not compat_sd:
        _retarget_animation(tx, card)
    # Timeline 先定位一次：绝大多数卡是独立包，但 #16740 这类直接嵌在 prefab 包里，
    # 那时改时长和清材质覆盖必须合并成一次写入，否则后写的会盖掉前一次。
    tl_h = get_crc(timeline_asset(ctx.game, card, pre, deep=True))
    tl_done = False
    if not compat_sd:
        # 低清这份要回到生成前的状态，不能停在「上一次开着低清写进去的那套」。
        n_sd = _restore_sd(tx, card)
        if n_sd:
            log.info(tr("P%s: put %s low-quality file(s) back the way they were before"), card, n_sd)
    for q, scale in sorted(dirs.items(), key=lambda kv: kv[0] != C.SD):
        if q in expand_qs:
            # 这一档只有 1 页材质槽，先把第 2 页的贴图包、材质包和 AtlasAsset 的槽补上，
            # 下面的循环才能照常往里写第 2 页。
            _add_texture_page(tx, card, q, scale, sheets[-1][0])
        if q == C.SD and not compat_sd:
            # 不写 SD：低清那份已经由 _restore_sd 写回生成前的样子。
            # SD 底盘多数只有一个贴图槽，两页动画写进去会直接失败。
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
        # 页贴图按底盘现有的路径走，不按 P<卡>_<n> 拼：#4007 的三页是 P4007 / P40072 / P40073。
        # 页数超过底盘时（原地扩页那条）才用拼出来的名字，那时包是 _add_texture_page 新建的。
        # 名字对不上卡号的卡（#3898 存的是 P3893）靠 own 从 prefab 依赖里反查。
        own = C.asset_paths(tx.catalog, card, q) or {}
        pages_lp = own.get("pages") or page_textures(tx.catalog, card, q, scale)
        atlas = gen_atlas([((pages_lp[i] if i < len(pages_lp)
                             else f"{b}/{scale}/P{card}_{i + 1}").rsplit("/", 1)[-1] + ".png",
                            s.size, r)
                           for i, (s, r) in enumerate(scaled)])
        # prefab 的写入：来源卡可能带按 slot 名覆盖材质的组件（3880 黑魔导就有），
        # 那些 slot 在我们的骨架里不存在，不清掉会让整个 Spine 渲染起不来。
        # 另外 #16740 这类把骨架、atlas、Timeline 全塞在 prefab 包里，必须在这里一次改完。
        ph = get_crc(C.prefab(card, q))
        pdata = tx.read_file(ph)
        if pdata is not None:
            fixed = pdata
            embedded = _fill_embedded(fixed, skel, atlas, len(scaled), round(duration, 3))
            if embedded is not None:
                fixed = embedded
                tl_done = True
                log.info(tr("P%s: this card keeps its skeleton inside the %s prefab, "
                            "wrote the skeleton, atlas and window there"), card, q)
                # 同一档可能还有别的内嵌副本包，一并写，否则 Timeline 走的那条链还是旧骨架
                extra = _fill_sibling_embedded(tx, card, q, ph, skel, atlas,
                                               len(scaled), round(duration, 3))
                if extra:
                    log.info(tr("P%s: filled %s more embedded bundle(s) with the new skeleton"),
                             card, extra)
            elif ph == tl_h:
                # Timeline 单独嵌在这个 prefab 包里
                fixed = set_timeline_duration(fixed, round(duration, 3))
                tl_done = True
                log.info(tr("P%s: cut-in window %ss (the Timeline lives inside the %s prefab)"),
                         card, round(duration, 3), q)
            fixed = _strip_custom_materials(fixed)
            if fixed is not pdata:
                tx.put_file(ph, fixed)
        writes = [(own.get("skeleton") or f"{b}/P{card}JS",
                   lambda d, s=skel: replace_textasset(d, s)),
                  (own.get("atlas") or f"{b}/{scale}/P{card}.atlas",
                   lambda d, a=atlas: replace_textasset(d, a)),
                  # 材质槽裁到实际页数：官方数据里两者恒等，差一个就可能整份图集取不到
                  (own.get("atlas_asset") or f"{b}/{scale}/P{card}_Atlas",
                   lambda d, n=len(scaled): keep_atlas_materials(d, n))]
        for i, (s, _) in enumerate(scaled):
            lp = pages_lp[i] if i < len(pages_lp) else f"{b}/{scale}/P{card}_{i + 1}"
            # 不按名字挑 Texture2D：页包命名不统一（P40072 / P4007_2 都有），
            # 但一个页包里只有一个 Texture2D，直接换就对了。
            writes.append((lp, lambda d, ss=s: replace_texture(d, ss)))
        for lp, fn in writes:
            h = get_crc(lp)
            # 克隆出来的文件还在本事务的队列里，磁盘上没有；read_file 两边都看
            data = tx.read_file(h)
            if data is None:
                raise MDStudioError(tr("this cut-in chassis has %s texture page(s) in %s, but the "
                                       "sheet needs %s. Use fewer frames or a smaller frame size.")
                                    % (len(pages_lp), q, len(scaled)))
            tx.put_file(h, fn(data))
    # 窗口严格等于动画时长。官方卡的共同点是 clip 长度等于骨架动画末帧时间
    # （14952 是 1.6 对 1.6，3881 也是），窗口多给的那一截会被 loop 填上，
    # 看得出来的是「动作重复」。这里只改 clip 长度，不动 m_FixedDuration。
    if not tl_done:
        tl_data = tx.read_file(tl_h)
        if tl_data is None:
            raise MDStudioError(tr("%s: no Timeline asset found (not as its own bundle, and not "
                                  "inside the prefab either).") % card)
        tx.put_file(tl_h, set_timeline_duration(tl_data, round(duration, 3)))
        log.info(tr("P%s: cut-in window %ss, animation %s"), card, round(duration, 3), anim_name)
    else:
        # 内嵌式卡可能还有别的 Timeline 包（#16091 顶层就有一个 P16091Timeline 1），
        # 一并改成同一时长。prefab 包已经改过，再改一次是幂等的（读的是队列里的最新版）。
        extra = _fix_extra_timelines(tx, card, round(duration, 3))
        if extra:
            log.info(tr("P%s: aligned %s more Timeline bundle(s) to %ss"),
                     card, extra, round(duration, 3))
    tx.rec.meta = {"source": str(source), "frames": len(names), "cell": cell, "duration": duration,
                   "sheet": list(sheets[0][0].size), "pages": len(sheets), "size": size,
                   "donor": (C.chassis_donor(ctx, card) or donor) if cloned else donor,
                   "pop": pop, "compat_sd": bool(compat_sd)}
    return tx.commit()
