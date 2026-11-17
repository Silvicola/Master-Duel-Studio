"""bundle 内部改名 / 换贴图 / 换 TextAsset"""
import hashlib
import re

import UnityPy
from UnityPy.enums import TextureFormat

from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr


def bundle_cab(data: bytes) -> str:
    """这个包内部 SerializedFile 的 CAB 名。

    跨包引用（材质→贴图、AtlasAsset→材质、SkeletonData→骨架）记的就是它，
    形如 archive:/CAB-xxx/CAB-xxx。克隆时必须一起改，否则克隆出来的包
    还指着来源卡那套（2026-11-16 实测 3900 的 externals 与 3881 完全相同）。
    """
    env = UnityPy.load(data)
    return next(iter(env.file.files.keys()), "")


def new_cab(h: str) -> str:
    return "CAB-" + hashlib.md5(h.encode()).hexdigest()


def rebase_stream(path: str, cab_map: dict) -> str:
    """把贴图流路径里的 CAB 换成新的。

    Texture2D 的 m_StreamData.path 是字符串 "archive:/CAB-旧/CAB-旧.resS"，
    改包名时如果不动它，包名是新的、贴图却仍去来源卡的 CAB 里读像素数据，
    游戏直接报 unable to read texture data，主体就画不出来（2026-11-17 日志实锤）。
    """
    if not isinstance(path, str) or not path.startswith("archive:/"):
        return path
    name = path[len("archive:/"):].split("/")[0]
    to = cab_map.get(name)
    return path.replace(name, to) if to else path


def retarget_skeleton(data: bytes, old_cab: str, new_cab_name: str, new_path_id: int) -> bytes:
    """把动画资产里指向某个骨架的引用改成另一个骨架。

    Timeline 那条 Spine 轨道播的不是绑定骨架上的动画，而是一个独立的 clip 资产，
    它的 skeletonDataAsset 指向 **SD 骨架**（来源卡也这么挂，因为官方 SD/HD 是
    同一套骨骼）。只写 HD 时 SD 仍是来源卡那一套，动画取到了也套不上，主体会
    停在第一帧不动（2026-11-17 实测）。这里把 externals 与 PPtr 一起改到 HD。

    old_cab / new_cab_name 都已经过克隆改名，指的是新卡自己的包。
    返回新数据；没找到可改的引用时原样返回。
    """
    env = UnityPy.load(data)
    hit = 0
    for sf in env.file.files.values():
        slot = 0
        for i, e in enumerate(list(getattr(sf, "externals", []) or [])):
            if getattr(e, "name", "") == old_cab:
                e.path = f"archive:/{new_cab_name}/{new_cab_name}"
                slot = i + 1
        if not slot:
            continue
        for o in env.objects:
            if o.type.name != "MonoBehaviour":
                continue
            try:
                tt = o.read_typetree()
            except Exception:
                continue
            if "skeletonDataAsset" not in tt:
                continue
            tt["skeletonDataAsset"] = {"m_FileID": slot, "m_PathID": new_path_id}
            o.save_typetree(tt)
            hit += 1
    return env.file.save(packer="lz4") if hit else data


def retag_str(s, old, new):
    if not isinstance(s, str):
        return s
    # m_Container 里是小写路径 (p11016), 名字里是大写 P11016, 两个都换
    # 坑: 后面必须不是数字, 不然 P1101 会把 P11016 也换掉
    return re.sub(rf"([Pp]){old}(?!\d)", rf"\g<1>{new}", s)


def rename_pages(s, page_map):
    """把页名换成统一写法，再留给 retag_str 处理卡号。

    官方页名不统一：P3881_2 带下划线，P156402 不带。后者被 retag_str 的 (?!\\d)
    挡住改不到，克隆出来还叫旧名，写第二页时按 P<新卡>_2 找不到。
    page_map: {"156402": "9999_2"} —— 键是纯数字，前缀 P/p 大小写都认。
    """
    for old, new in page_map.items():
        s = re.sub(rf"([Pp]){old}(?!\d)", rf"\g<1>{new}", s)
    return s


def _walk(v, fn):
    if isinstance(v, str):
        return fn(v)
    if isinstance(v, list):
        return [_walk(x, fn) for x in v]
    if isinstance(v, dict):
        return {k: _walk(x, fn) for k, x in v.items()}
    return v


def bundle_name(h: str) -> str:
    # 注意: AssetBundle.m_Name 必须 == "<2hex>/<8hex>" 跟自己文件名一致
    return f"{h[:2]}/{h}"


def retag(data: bytes, old: int, new: int, h: str, deep: bool, crc_map: dict = None,
          page_map: dict = None, cab_map: dict = None, cab: str = None) -> bytes:
    """P<old> -> P<new>

    deep=False: 只改 AssetBundle / GameObject / Mesh 名 (借用模式, 依赖还指向 donor, 别的不能动)
    deep=True : 连 Texture2D / Material / atlas 文本 / MonoBehaviour typetree 一起改 (克隆模式)
    PathID 不变, 内部 PPtr 全都还连得上

    crc_map: {旧哈希8位: 新哈希8位}。AssetBundle.m_Dependencies 存的是哈希路径,
    改卡号的字符串替换够不到它, 必须显式重定向 (2026-11-16: 克隆后 SkeletonData
    仍指向 donor 的骨架/atlas, 游戏读到旧资源, 主体不可见)。只有 clone 传这个表。
    page_map: {旧页名: 新页名}，把官方不统一的页名先统一成 P<新卡>_<n>。只有 clone 传。
    cab_map: {旧 CAB 名: 新 CAB 名}，跨包引用记的是 CAB 名，不改就一直指着来源卡。
    cab: 这个包自己的新 CAB 名。
    """
    def rep(s):
        if not isinstance(s, str):
            return s
        if page_map:
            s = rename_pages(s, page_map)
        return retag_str(s, old, new)

    env = UnityPy.load(data)
    for o in env.objects:
        tn = o.type.name
        if tn == "AssetBundle":
            tt = o.read_typetree()
            tt["m_Container"] = [[rep(k), v] for k, v in tt.get("m_Container", [])]
            tt["m_Name"] = bundle_name(h)
            # m_AssetBundleName 是运行时认的包名，必须跟 m_Name 一起改。
            # 只改 m_Name 的话，克隆出来的包在游戏里仍自称来源卡的那个包名，
            # 和来源卡撞名 —— 特效页那条能正常显示的路径一直是两个一起设的
            # （runfx.py / runfx_flip.py），克隆这边漏了。
            if "m_AssetBundleName" in tt:
                tt["m_AssetBundleName"] = bundle_name(h)
            if crc_map:
                # 依赖是 "ab/abcdef12" 形式，目录前缀必须是新哈希自己的前两位:
                # 沿用旧依赖的前缀会指向一个不存在的路径 (2026-11-16 实测
                # 15640 克隆后变成 d0/0e3f1cdc, 而文件在 0e/0e3f1cdc)
                fixed = []
                for d in (tt.get("m_Dependencies") or []):
                    new_h = crc_map.get(d.split("/")[-1])
                    fixed.append(f"{new_h[:2]}/{new_h}" if new_h else d)
                tt["m_Dependencies"] = fixed
            o.save_typetree(tt)
        elif tn in ("GameObject", "Mesh") or (deep and tn in ("Texture2D", "Material")):
            try:
                d = o.read()
            except Exception:
                continue
            changed = False
            nn = rep(getattr(d, "m_Name", ""))
            if nn != getattr(d, "m_Name", ""):
                d.m_Name = nn
                changed = True
            if tn == "Texture2D" and cab_map:
                sd = getattr(d, "m_StreamData", None)
                p = getattr(sd, "path", None)
                np = rebase_stream(p, cab_map)
                if np != p:
                    sd.path = np
                    changed = True
            if changed:
                d.save()
        elif deep and tn == "TextAsset":
            # atlas 文本里第一行是页名 "P11016.png"
            try:
                d = o.read()
            except Exception:
                continue
            d.m_Name = rep(d.m_Name)
            s = d.m_Script
            s = s if isinstance(s, str) else bytes(s).decode("utf-8", "surrogateescape")
            d.m_Script = rep(s)
            d.save()
        elif deep and tn == "MonoBehaviour":
            try:
                tt = o.read_typetree()
            except Exception:
                continue
            o.save_typetree(_walk(tt, rep))
    files = env.file.files
    if files:
        if cab_map:
            for sf in files.values():
                for e in getattr(sf, "externals", []) or []:
                    to = cab_map.get(e.name)
                    if to:
                        e.path = f"archive:/{to}/{to}"
        if cab:
            # 一个包里有主文件和 .resS 资源流两条记录，名字必须一起换：
            # 只换主文件会让两者对不上，重新加载时对象全丢（实测过）。
            base = next(iter(files))
            base = base[:-5] if base.endswith(".resS") else base
            if base and base != cab:
                env.file.files = {(cab + k[len(base):] if k.startswith(base) else k): v
                                  for k, v in files.items()}
    return env.file.save(packer="lz4")


def apply_texture_settings(d):
    # set_image 不会动 filter, 默认可能是 Point -> 放大全是锯齿
    try:
        ts = d.m_TextureSettings
        for attr, val in (("m_FilterMode", 1), ("m_Aniso", 2), ("m_MipBias", 0.0),
                          ("m_WrapMode", 1), ("m_WrapU", 1), ("m_WrapV", 1), ("m_WrapW", 1)):
            if hasattr(ts, attr):
                setattr(ts, attr, val)
    except AttributeError:
        pass


def set_image(d, img, bc7=True):
    """PC 上原版就是 BC7 (fmt 25), 编不了再退 RGBA32"""
    try:
        d.set_image(img, target_format=TextureFormat.BC7 if bc7 else TextureFormat.RGBA32, mipmap_count=1)
    except MemoryError:
        # 坑: 以前这里也退 RGBA32, 那张图是 BC7 的 4 倍大, 内存不够只会再炸一次, 报错还看不懂
        raise MDStudioError(tr("not enough memory to pack a %sx%s texture. Close other programs, "
                              "or use fewer frames / a smaller frame height.")
                           % (img.width, img.height)) from None
    except Exception:
        d.set_image(img, target_format=TextureFormat.RGBA32, mipmap_count=1)
    apply_texture_settings(d)
    d.save()


def replace_texture(data: bytes, img, name=None, keep_size=False, bc7=True) -> bytes:
    """keep_size=True: 换皮用, 尺寸跟原图走 (atlas 坐标不能变); False: flipbook 整张换新尺寸"""
    env = UnityPy.load(data)
    n = 0
    for o in env.objects:
        if o.type.name != "Texture2D":
            continue
        d = o.read()
        if name and d.m_Name != name:
            continue
        src = img
        if keep_size and img.size != (d.m_Width, d.m_Height):
            src = img.resize((d.m_Width, d.m_Height))
        set_image(d, src, bc7)
        n += 1
    if not n:
        raise ValueError(tr("no Texture2D %s in bundle") % (name or ""))
    return env.file.save(packer="lz4")


def replace_textasset(data: bytes, text: str) -> bytes:
    env = UnityPy.load(data)
    for o in env.objects:
        if o.type.name == "TextAsset":
            d = o.read()
            d.m_Script = text
            d.save()
    return env.file.save(packer="lz4")


def fix_clip_duration_mode(data: bytes) -> bytes:
    """把 Timeline 里 Spine clip 的 customDuration 设成 1（用写死的片段时长）。

    来源卡有一半是 customDuration=0（运行时向骨架要动画时长，如 14952/18182），
    一半是 1（如 3881）。纯克隆不改骨架，怎么都播；一旦把骨架换成逐帧动画，
    runtime 解析不到那个时长，clip 变 0 长，角色停在初始姿势（2026-11-17 实测）。
    纯克隆和视频生成都走这个函数，时长数值本身由 set_timeline_duration 另设。
    """
    env = UnityPy.load(data)
    hit = 0
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        try:
            tt = o.read_typetree()
        except Exception:
            continue
        tpl = tt.get("template")
        if isinstance(tpl, dict) and "customDuration" in tpl and not tpl["customDuration"]:
            tpl["customDuration"] = 1
            o.save_typetree(tt)
            hit += 1
    return env.file.save(packer="lz4") if hit else data


def keep_atlas_materials(data: bytes, n: int) -> bytes:
    """把 AtlasAsset 的材质槽裁到 n 个。

    官方数据里材质槽数恒等于图集页数（实测 1278 张贴图、15 张代表卡，从无例外）。
    我们生成 1 页时底盘还是 2 个槽，两边就对不上了。多出来的槽目前看不出别的用途。
    """
    env = UnityPy.load(data)
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        try:
            tt = o.read_typetree()
        except Exception:
            continue
        mats = tt.get("materials")
        if not isinstance(mats, list) or len(mats) <= n:
            continue
        tt["materials"] = mats[:n]
        o.save_typetree(tt)
    return env.file.save(packer="lz4")


def textures(data: bytes):
    env = UnityPy.load(data)
    return [o.read() for o in env.objects if o.type.name == "Texture2D"]
