"""bundle 内部改名 / 换贴图 / 换 TextAsset"""
import re

import UnityPy
from UnityPy.enums import TextureFormat

from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr


def retag_str(s, old, new):
    if not isinstance(s, str):
        return s
    # m_Container 里是小写路径 (p11016), 名字里是大写 P11016, 两个都换
    # 坑: 后面必须不是数字, 不然 P1101 会把 P11016 也换掉
    return re.sub(rf"([Pp]){old}(?!\d)", rf"\g<1>{new}", s)


_rep = retag_str


def _walk(v, old, new):
    if isinstance(v, str):
        return _rep(v, old, new)
    if isinstance(v, list):
        return [_walk(x, old, new) for x in v]
    if isinstance(v, dict):
        return {k: _walk(x, old, new) for k, x in v.items()}
    return v


def bundle_name(h: str) -> str:
    # 注意: AssetBundle.m_Name 必须 == "<2hex>/<8hex>" 跟自己文件名一致
    return f"{h[:2]}/{h}"


def retag(data: bytes, old: int, new: int, h: str, deep: bool) -> bytes:
    """P<old> -> P<new>

    deep=False: 只改 AssetBundle / GameObject / Mesh 名 (借用模式, 依赖还指向 donor, 别的不能动)
    deep=True : 连 Texture2D / Material / atlas 文本 / MonoBehaviour typetree 一起改 (克隆模式)
    PathID 不变, 内部 PPtr 全都还连得上
    """
    env = UnityPy.load(data)
    for o in env.objects:
        tn = o.type.name
        if tn == "AssetBundle":
            tt = o.read_typetree()
            tt["m_Container"] = [[_rep(k, old, new), v] for k, v in tt.get("m_Container", [])]
            tt["m_Name"] = bundle_name(h)
            o.save_typetree(tt)
        elif tn in ("GameObject", "Mesh") or (deep and tn in ("Texture2D", "Material")):
            try:
                d = o.read()
            except Exception:
                continue
            nn = _rep(getattr(d, "m_Name", ""), old, new)
            if nn != getattr(d, "m_Name", ""):
                d.m_Name = nn
                d.save()
        elif deep and tn == "TextAsset":
            # atlas 文本里第一行是页名 "P11016.png"
            try:
                d = o.read()
            except Exception:
                continue
            d.m_Name = _rep(d.m_Name, old, new)
            s = d.m_Script
            s = s if isinstance(s, str) else bytes(s).decode("utf-8", "surrogateescape")
            d.m_Script = _rep(s, old, new)
            d.save()
        elif deep and tn == "MonoBehaviour":
            try:
                tt = o.read_typetree()
            except Exception:
                continue
            o.save_typetree(_walk(tt, old, new))
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


def textures(data: bytes):
    env = UnityPy.load(data)
    return [o.read() for o in env.objects if o.type.name == "Texture2D"]
