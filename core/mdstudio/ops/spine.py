import copy
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

import UnityPy
from PIL import Image

from mdstudio import runtime
from mdstudio.core.bundle import retag, retag_str, set_image
from mdstudio.core.crc import get_crc
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr
from mdstudio.ops import cutin as C
from mdstudio.ops.passcodes import PASSCODES
from mdstudio.ops.tx import Tx
from mdstudio.progress import tick

log = logging.getLogger("mdstudio")

OP = "cutin.spine"
DONOR = C.DEFAULT_DONOR
ANIM = "animation"
SPINE_SHADER = "Spine/Skeleton"
RUNTIME = (4, 2)
MAP_FILE = "cards.json"
SCAN_DEPTH = 4
BUNDLE_EXT = (".bundle", ".ab", ".unity3d", ".assetbundle", ".zip")
CHASSIS_BOX = (-1328.0, -936.0, 2456.0, 1948.0)
CHASSIS_SCALE = 0.2
DATA_SCALE = 0.01


def _text(d) -> str:
    s = d.m_Script
    return s if isinstance(s, str) else bytes(s).decode("utf-8", "surrogateescape")


def pages_of(atlas: str) -> list:
    out, head = [], True
    for line in atlas.splitlines():
        s = line.strip()
        if not s:
            head = True
            continue
        if head:
            out.append(s[:-4] if s.endswith(".png") else s)
        head = False
    return out


def regions_of(atlas: str) -> set:
    out, head = set(), True
    for line in atlas.splitlines():
        s = line.rstrip()
        if not s.strip():
            head = True
            continue
        if head:
            head = False
            continue
        if ":" not in s and not s.startswith((" ", "\t")):
            out.add(s)
    return out


def is_pma(atlas: str) -> bool:
    return bool(re.search(r"^\s*pma\s*:\s*true\s*$", atlas, re.M | re.I))


def _set_pma(atlas: str) -> str:
    """直通 alpha 的导出会被我们预乘, atlas 必须跟着标 pma:true, 否则游戏里颜色发暗"""
    lines = atlas.splitlines()
    out = []
    for i, line in enumerate(lines):
        out.append(line)
        if line.strip().lower().endswith(".png"):
            nxt = [x.strip().lower() for x in lines[i + 1:i + 8]]
            if not any(x.startswith("pma:") for x in nxt):
                out.append("pma:true")
    return "\n".join(out) + "\n"


def _upgrade_atlas(atlas: str) -> str:
    """Spine 3.x 的 atlas 写法换成 4.x.

    3.x 用 xy/size/orig/offset, 4.x 只认 bounds/offsets; 把 3.x 直接喂给 4.x 运行时
    会得到 0 尺寸区域 (整张图不显示). 已经是 4.x 的原样返回.
    """
    if re.search(r"^\s*bounds\s*:", atlas, re.M | re.I):
        return atlas
    page_keys = ("size", "format", "filter", "repeat", "pma", "scale")
    out, region, in_page = [], None, False

    def flush():
        nonlocal region
        if region is None:
            return
        name, kv = region
        x, y = kv.get("xy", (0, 0))
        w, h = kv.get("size", (0, 0))
        out.append(name)
        out.append(f"  bounds:{x},{y},{w},{h}")
        if "orig" in kv or "offset" in kv:
            ow, oh = kv.get("orig", (w, h))
            ox, oy = kv.get("offset", (0, 0))
            out.append(f"  offsets:{ox},{oy},{ow},{oh}")
        if str(kv.get("rotate", "false")).lower() == "true":
            out.append("  rotate:90")
        out.append(f"  index:{kv.get('index', -1)}")
        region = None

    for line in atlas.splitlines():
        s = line.strip()
        if not s:
            flush()
            out.append("")
            continue
        k, _, v = s.partition(":")
        if not line[:1].isspace():          # 页文件 / 页属性 / 区域名
            flush()
            if s.lower().endswith(".png"):
                out.append(s)
                in_page = True
                continue
            if in_page and k.strip().lower() in page_keys:
                out.append(f"{k.strip()}:{v.strip().replace(', ', ',')}")
                continue
            in_page = False
            region = (s, {})
            continue
        if region is None:
            continue
        nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", v)]
        if len(nums) >= 2:
            region[1][k.strip().lower()] = (int(nums[0]), int(nums[1]))
        elif nums:
            region[1][k.strip().lower()] = int(nums[0])
        else:
            region[1][k.strip().lower()] = v.strip()
    flush()
    return "\n".join(out).rstrip() + "\n"


def rename_pages(atlas: str, mrk: int) -> str:
    names = [f"P{mrk}", f"P{mrk}_2"]
    lines, head, k = atlas.splitlines(), True, 0
    for i, line in enumerate(lines):
        if not line.strip():
            head = True
            continue
        if head:
            if k >= len(names):
                raise MDStudioError(tr("atlas has more than %s pages, the chassis only has %s")
                                   % (len(names), len(names)))
            lines[i] = names[k] + ".png"
            k += 1
        head = False
    return "\n".join(lines) + "\n"


def rename_anim(skel: bytes, anim: str) -> bytes:
    if not anim or anim == ANIM:
        return skel
    j = json.loads(skel.decode("utf-8"))
    anims = j.get("animations") or {}
    if anim not in anims:
        raise MDStudioError(tr("skeleton has no animation %s") % anim)
    if ANIM in anims:
        anims[ANIM + "_orig"] = anims.pop(ANIM)
    j["animations"] = {(ANIM if k == anim else k): v for k, v in anims.items()}
    return json.dumps(j, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def spine_version(skel: bytes):
    v = str((json.loads(skel.decode("utf-8")).get("skeleton") or {}).get("spine") or "")
    m = re.match(r"(\d+)\.(\d+)", v)
    return (int(m.group(1)), int(m.group(2))) if m else None

CONVERTER_DIR = "tools/spineconv"
CONVERTER_EXE = "SpineSkeletonDataConverter.exe"
CONVERTER_TARGET = "4.2.11"


def _converter_path():
    p = runtime.bundle_root() / CONVERTER_DIR / CONVERTER_EXE
    return p if p.is_file() else None


def convert_skeleton(skel: bytes) -> bytes:
    """3.x / 5.x 的骨架升到 4.x, 让游戏 4.2 运行时能加载.

    游戏跑 Spine 4.2, 而很多同人素材还是 3.8 导出的. 上游用随包的
    SpineSkeletonDataConverter (原生 exe, 不依赖 Java) 做跨大版本升级 —— 它会重算
    旋转时间轴和曲线格式. 转换器对中文路径名会报 filesystem error, 所以输入输出
    一律用纯 ASCII 临时名.
    """
    exe = _converter_path()
    if exe is None:
        raise MDStudioError(tr("spine version converter not found at %s")
                           % (runtime.bundle_root() / CONVERTER_DIR))
    with tempfile.TemporaryDirectory() as td:
        tin = Path(td) / "spine_in.json"
        tout = Path(td) / "spine_out.json"
        tin.write_bytes(skel)
        r = subprocess.run([str(exe), str(tin), str(tout), "-v", CONVERTER_TARGET],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not tout.is_file():
            raise MDStudioError(tr("spine version upgrade failed (exit %s): %s")
                               % (r.returncode, (r.stderr or r.stdout).strip()[-500:]))
        return tout.read_bytes()


def _premultiply(img: Image.Image) -> Image.Image:
    import numpy as np
    a = np.asarray(img.convert("RGBA"), dtype=np.uint16)
    a[..., :3] = (a[..., :3] * a[..., 3:4] + 127) // 255
    return Image.fromarray(a.astype("uint8"), "RGBA")


class Source:
    def __init__(self, skel: bytes, atlas: str, textures: list, anim=None, loop=None, scale=None, pos=None,
                 label=""):
        self.skel, self.atlas, self.textures = skel, atlas, textures
        self.pages = pages_of(atlas)
        if len(self.pages) > 2:
            raise MDStudioError(tr("%s atlas pages, the chassis only has 2") % len(self.pages))
        if len(self.textures) != len(self.pages):
            raise MDStudioError(tr("%s atlas page(s) but %s texture(s)")
                               % (len(self.pages), len(self.textures)))
        ver = spine_version(skel)
        if ver and ver[0] != RUNTIME[0]:
            # 上游做法: 先用随包转换器把 3.x 升到 4.x, 再喂给游戏 4.2 运行时.
            skel = convert_skeleton(skel)
            self.skel = skel
            ver = spine_version(skel)
        if ver and ver[0] != RUNTIME[0]:
            raise MDStudioError(tr("skeleton is Spine %s.%s, the game runs Spine %s.%s. "
                                  "Export it again as Spine %s.x JSON.")
                               % (ver[0], ver[1], RUNTIME[0], RUNTIME[1], RUNTIME[0]))
        if ver and ver != RUNTIME:
            log.warning(tr("skeleton is Spine %s.%s, the game runs %s.%s; "
                           "it usually loads, check it in Solo"), *ver, *RUNTIME)
        names = self.anims
        if anim is None:
            anim = ANIM if ANIM in names else (names[0] if names else ANIM)
        self.anim, self.loop, self.scale, self.pos, self.label = anim, loop, scale, pos, label

    @property
    def anims(self) -> list:
        return list((json.loads(self.skel.decode("utf-8")).get("animations") or {}).keys())

    def fit(self, extra=1.0):
        sk = json.loads(self.skel.decode("utf-8")).get("skeleton") or {}
        try:
            x, y, w, h = (float(sk[k]) for k in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            return
        if w <= 0 or h <= 0:
            return
        cx, cy, cw, ch = CHASSIS_BOX
        k = min(cw / w, ch / h) * extra
        s = CHASSIS_SCALE * k
        unit = DATA_SCALE * CHASSIS_SCALE
        self.scale = {"x": s, "y": s, "z": 1.0}
        self.pos = {"x": ((cx + cw / 2) - (x + w / 2) * k) * unit, "y": ((cy + ch / 2) - (y + h / 2) * k) * unit,
                    "z": 0.0}

    @classmethod
    def from_bundle(cls, data: bytes, label=""):
        try:
            env = UnityPy.load(data)
        except Exception as e:
            raise MDStudioError(tr("%s: not a Unity bundle (%s)") % (label or "file", e)) from None
        skel = atlas = None
        texs, sa = {}, None
        for o in env.objects:
            tn = o.type.name
            if tn == "TextAsset":
                d = o.read()
                s = _text(d)
                if s.lstrip().startswith("{") and '"skeleton"' in s[:2000]:
                    skel = s.encode("utf-8", "surrogateescape")
                elif ".png" in s[:300]:
                    atlas = s
            elif tn == "Texture2D":
                d = o.read()
                texs[d.m_Name] = d
            elif tn == "MonoBehaviour":
                try:
                    tt = o.read_typetree()
                except Exception:
                    continue
                if "skeletonDataAsset" in tt and "_animationName" in tt:
                    sa = tt
        if skel is None or atlas is None:
            raise MDStudioError(tr("%s: no Spine skeleton JSON + atlas inside") % (label or "bundle"))
        textures = []
        for name in pages_of(atlas):
            d = texs.get(name)
            if d is None:
                raise MDStudioError(tr("atlas page %s has no texture") % name)
            textures.append({"raw": d.get_image_data(), "w": int(d.m_Width), "h": int(d.m_Height),
                             "fmt": int(d.m_TextureFormat)})
        scale = pos = None
        if sa:
            scale, pos = _node_transform(env, sa["m_GameObject"]["m_PathID"])
        return cls(skel, atlas, textures, anim=(sa or {}).get("_animationName") or None,
                   loop=(sa or {}).get("loop"), scale=scale, pos=pos, label=label)

    @classmethod
    def from_files(cls, files: dict, label="", anim=None, scale=1.0):
        skel_name = next((n for n in files if n.lower().endswith((".json", ".json.txt"))), None)
        atlas_name = next((n for n in files if n.lower().endswith((".atlas", ".atlas.txt"))), None)
        if skel_name is None:
            if any(n.lower().endswith((".skel", ".skel.bytes")) for n in files):
                raise MDStudioError(tr("%s: binary .skel is not supported, export the skeleton as JSON") % label)
            raise MDStudioError(tr("%s: no skeleton .json") % label)
        if atlas_name is None:
            raise MDStudioError(tr("%s: no .atlas") % label)
        skel = files[skel_name]()
        atlas = files[atlas_name]().decode("utf-8-sig")
        pma = is_pma(atlas)
        atlas = _upgrade_atlas(atlas)          # 3.x 的 xy/size 写法先升到 4.x
        if not pma:
            atlas = _set_pma(atlas)            # 下面会预乘贴图, atlas 得跟着标
        by_name = {n.replace("\\", "/").rsplit("/", 1)[-1].lower(): n for n in files}
        textures = []
        for page in pages_of(atlas):
            n = by_name.get(f"{page}.png".lower())
            if n is None:
                raise MDStudioError(tr("%s: atlas page %s.png not found next to the atlas") % (label, page))
            img = Image.open(io.BytesIO(files[n]())).convert("RGBA")
            textures.append({"img": img if pma else _premultiply(img)})
        src = cls(skel, atlas, textures, anim=anim, label=label)
        src.fit(scale)
        return src

    @classmethod
    def load(cls, path, anim=None, scale=1.0):
        p = Path(path)
        if p.is_dir():
            files = {f.name: f.read_bytes for f in p.iterdir() if f.is_file()}
            return cls.from_files(files, label=p.name, anim=anim, scale=scale)
        if zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                files = {n: (lambda n=n: z.read(n)) for n in z.namelist() if not n.endswith("/")}
                return cls.from_files(files, label=p.name, anim=anim, scale=scale)
        src = cls.from_bundle(p.read_bytes(), label=p.name)
        if anim:
            if anim not in src.anims:
                raise MDStudioError(tr("skeleton has no animation %s, it has %s") % (anim, src.anims))
            src.anim = anim
        if scale != 1.0 and src.scale:
            src.scale = {k: (v * scale if k != "z" else v) for k, v in src.scale.items()}
        return src


ATLAS_EXT = (".atlas", ".atlas.txt")


def _has_atlas(names) -> bool:
    return any(n.lower().endswith(ATLAS_EXT) for n in names)


def _is_export_dir(p: Path) -> bool:
    try:
        with os.scandir(p) as it:
            return _has_atlas(e.name for e in it if e.is_file())
    except OSError:
        return False


def _key(name: str):
    stem = name.split(".", 1)[0]
    return int(stem) if stem.isdigit() else None


def _read_map(d: Path) -> dict:
    f = d / MAP_FILE
    if not f.is_file():
        return {}
    try:
        raw = json.loads(f.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        raise MDStudioError(tr("%s: not valid JSON (%s)") % (f, e)) from None
    return {str(k): int(v) for k, v in raw.items()}


def _match(name: str, mapping: dict, is_dir: bool):
    if name in mapping:
        return mapping[name], ""
    pc = _key(name)
    if pc is None or pc not in PASSCODES:
        return None
    if not is_dir and "." in name and Path(name).suffix.lower() not in BUNDLE_EXT:
        return None
    return PASSCODES[pc]


def scan(path) -> list:
    root = Path(path)
    if not root.exists():
        raise MDStudioError(tr("not found: %s") % root)
    if root.is_file() or _is_export_dir(root):
        return []
    found = {}
    maps = {}
    base = len(root.parts)
    for d, dirs, files in os.walk(root):
        dp = Path(d)
        depth = len(dp.parts) - base
        if depth and _has_atlas(files):
            dirs[:] = []
            hit = _match(dp.name, maps.get(dp.parent, {}), True)
            if hit:
                found.setdefault(dp.parent, []).append((dp.name, hit[0], hit[1], dp))
            continue
        if depth >= SCAN_DEPTH:
            dirs[:] = []
        maps[dp] = mapping = _read_map(dp) if MAP_FILE in files else {}
        for n in files:
            if n == MAP_FILE:
                continue
            hit = _match(n, mapping, False)
            if hit:
                found.setdefault(dp, []).append((n, hit[0], hit[1], dp / n))
    out, seen = [], set()
    for d in sorted(found, key=lambda k: (-len(found[k]), str(k))):
        for row in sorted(found[d], key=lambda r: (_key(r[0]) is None, _key(r[0]) or 0, r[0])):
            if row[1] not in seen:
                seen.add(row[1])
                out.append(row)
    return sorted(out, key=lambda r: r[1])


def by_card(path, mrk: int):
    for row in scan(path):
        if row[1] == mrk:
            return row
    raise MDStudioError(tr("%s: no Spine cut-in for this card under %s") % (mrk, path))


def _components(env, go_pathid) -> set:
    go = next((o for o in env.objects if o.path_id == go_pathid), None)
    if go is None:
        return set()
    out = set()
    for c in go.read_typetree().get("m_Component", []):
        ref = c.get("component", c) if isinstance(c, dict) else c[1]
        out.add(ref.get("m_PathID") if isinstance(ref, dict) else ref)
    return out


def _node_transform(env, go_pathid):
    ids = _components(env, go_pathid)
    for o in env.objects:
        if o.type.name == "Transform" and o.path_id in ids:
            tt = o.read_typetree()
            return tt.get("m_LocalScale"), tt.get("m_LocalPosition")
    return None, None


def _put_texture(o, tex):
    if "img" in tex:
        set_image(o.read(), tex["img"])
        return
    tt = o.read_typetree()
    tt["m_Width"], tt["m_Height"], tt["m_TextureFormat"] = tex["w"], tex["h"], tex["fmt"]
    tt["m_MipCount"] = 1
    tt["m_CompleteImageSize"] = len(tex["raw"])
    tt["image data"] = tex["raw"]
    tt["m_StreamData"] = {"offset": 0, "size": 0, "path": ""}
    o.save_typetree(tt)


def swap(data: bytes, mrk: int, src: Source, spine_mat=None) -> bytes:
    env = UnityPy.load(data)
    pages = {f"P{mrk}": 0, f"P{mrk}_2": 1}
    changed = False
    sa_go = None
    for o in env.objects:
        tn = o.type.name
        if tn == "TextAsset":
            d = o.read()
            s = _text(d)
            if s.lstrip().startswith("{"):
                d.m_Script = rename_anim(src.skel, src.anim).decode("utf-8", "surrogateescape")
            elif ".png" in s[:300]:
                d.m_Script = rename_pages(src.atlas, mrk)
            else:
                continue
            d.save()
            changed = True
        elif tn == "Texture2D":
            k = pages.get(o.read().m_Name)
            if k is not None and k < len(src.textures):
                _put_texture(o, src.textures[k])
                changed = True
        elif tn == "Material" and spine_mat and len(src.pages) > 1:
            tt = o.read_typetree()
            if tt["m_Name"] == f"P{mrk}_P{mrk}_2":
                _spine_material(env, o, tt, spine_mat)
                changed = True
        elif tn == "MonoBehaviour":
            try:
                tt = o.read_typetree()
            except Exception:
                continue
            if "skeletonDataAsset" in tt and "_animationName" in tt:
                tt["_animationName"] = ANIM
                if src.loop is not None and "loop" in tt:
                    tt["loop"] = src.loop
                o.save_typetree(tt)
                sa_go = tt["m_GameObject"]["m_PathID"]
                changed = True
    if sa_go is not None and (src.scale or src.pos):
        ids = _components(env, sa_go)
        for o in env.objects:
            if o.type.name == "Transform" and o.path_id in ids:
                tt = o.read_typetree()
                if src.scale:
                    tt["m_LocalScale"] = src.scale
                if src.pos:
                    tt["m_LocalPosition"] = src.pos
                o.save_typetree(tt)
    return env.file.save(packer="lz4") if changed else data


def spine_material(data: bytes):
    env = UnityPy.load(data)
    mat = sh = None
    for o in env.objects:
        if o.type.name == "Material":
            mat = o.read_typetree()
        elif o.type.name == "Shader":
            tt = o.read_typetree()
            if tt.get("m_ParsedForm", {}).get("m_Name") == SPINE_SHADER:
                sh = tt
    if mat is None or sh is None:
        raise MDStudioError(tr("chassis page-1 material has no Spine/Skeleton shader"))
    return mat, sh


def _spine_material(env, mat_obj, tt, spine_mat):
    mat1, shader = spine_mat
    sh_id = tt["m_Shader"]["m_PathID"]
    sh_obj = next((o for o in env.objects if o.path_id == sh_id and o.type.name == "Shader"), None)
    if sh_obj is None or tt["m_Shader"]["m_FileID"] != 0:
        raise MDStudioError(tr("chassis page-2 shader is not inside its bundle"))
    sh_obj.save_typetree(shader)
    new = copy.deepcopy(mat1)
    new["m_Name"] = tt["m_Name"]
    new["m_Shader"] = tt["m_Shader"]
    own = dict(tt["m_SavedProperties"]["m_TexEnvs"])
    new["m_SavedProperties"]["m_TexEnvs"] = [[k, ({**v, "m_Texture": own[k]["m_Texture"]} if k in own else v)]
                                            for k, v in new["m_SavedProperties"]["m_TexEnvs"]]
    mat_obj.save_typetree(new)


def port(ctx, target: int, src_file, force=False, anim=None, scale=1.0):
    src_file = Path(src_file)
    src = Source.load(src_file, anim=anim, scale=scale)
    tx = Tx(ctx, OP, target)
    C._check(tx, target, DONOR, force)
    cat = tx.catalog
    paths = C.own_paths(cat, DONOR)
    mats = {}
    for lp in paths:
        if lp.endswith(f"/P{DONOR}_P{DONOR}"):
            q = lp.rsplit("/", 2)[0]
            mats[q] = spine_material(tx.game.find_logical(lp).read_bytes())
    for k, lp in enumerate(paths):
        tick(tr("building"), k + 1, len(paths))
        tlp = retag_str(lp, DONOR, target)
        th = get_crc(tlp)
        data = retag(tx.game.find_logical(lp).read_bytes(), DONOR, target, th, deep=True)
        tx.put_file(th, swap(data, target, src, mats.get(lp.rsplit("/", 2)[0])))
    for q in C.QUALITIES:
        tx.set_catalog(C.prefab(target, q), [retag_str(d, DONOR, target) for d in cat.deps(C.prefab(DONOR, q))])
    tx.add_cutin(target)
    tx.rec.meta = {"donor": DONOR, "source": src_file.name}
    if target not in tx.rec.cutin_mrks:
        tx.rec.meta["gate"] = [int(target)]
    rec = tx.commit()
    if not ctx.dry_run:
        bad = validate(ctx.game, target)
        if bad:
            log.warning(tr("%s: built, but the check found problems:\n  %s") % (target, "\n  ".join(bad)))
    return rec


def _attachment_regions(skel: dict) -> set:
    out = set()
    skins = skel.get("skins") or []
    if isinstance(skins, dict):
        skins = [{"attachments": v} for v in skins.values()]
    for skin in skins:
        for slot in (skin.get("attachments") or {}).values():
            for key, att in slot.items():
                if att.get("type", "region") not in ("region", "mesh", "linkedmesh"):
                    continue
                if "sequence" in att:
                    continue
                out.add(att.get("path") or att.get("name") or key)
    return out


def validate(game, mrk: int) -> list:
    from mdstudio.core.catalog import Catalog
    cat = Catalog(game)
    probs = []
    paths = C.own_paths(cat, mrk)
    if not paths:
        return [tr("%s: no cut-in files in the manifest") % mrk]
    miss = C.missing(cat, mrk)
    if miss:
        probs.append(tr("%s missing file(s), e.g. %s") % (len(miss), miss[0]))
    skel, ref = {}, None
    atlases, texs, mats, sa = {}, {}, {}, {}
    for lp in paths:
        p = game.find_logical(lp)
        if not p:
            continue
        q = C.HD if f"/{C.HD}/" in lp else (C.SD if f"/{C.SD}/" in lp else "")
        env = UnityPy.load(p.read_bytes())
        shaders = {o.path_id: o.read_typetree().get("m_ParsedForm", {}).get("m_Name")
                   for o in env.objects if o.type.name == "Shader"}
        for o in env.objects:
            tn = o.type.name
            if tn == "TextAsset":
                s = _text(o.read())
                if s.lstrip().startswith("{"):
                    skel[q] = json.loads(s)
                elif ".png" in s[:300]:
                    atlases[(q, lp.rsplit("/", 1)[0])] = s
            elif tn == "Texture2D":
                d = o.read()
                texs[(q, d.m_Name)] = int(d.m_TextureFormat)
            elif tn == "Material":
                tt = o.read_typetree()
                mats[(q, tt["m_Name"])] = shaders.get(tt["m_Shader"]["m_PathID"])
            elif tn == "MonoBehaviour":
                try:
                    tt = o.read_typetree()
                except Exception:
                    continue
                if "skeletonDataAsset" in tt and "_animationName" in tt:
                    sa[q] = tt["_animationName"]
                elif "animationName" in tt and "skeletonDataAsset" in tt:
                    ref = tt["animationName"]
    for (q, folder), atlas in atlases.items():
        where = f"{q or '?'}/{folder.rsplit('/', 1)[-1]}"
        for pg in pages_of(atlas):
            if (q, pg) not in texs:
                probs.append(tr("%s: atlas page %s.png has no texture with that name") % (where, pg))
            shader = mats.get((q, f"P{mrk}_{pg}"))
            if shader and shader != SPINE_SHADER:
                probs.append(tr("%s: page %s is drawn with %s") % (where, pg, shader))
        if q in skel:
            lost = _attachment_regions(skel[q]) - regions_of(atlas)
            if lost:
                probs.append(tr("%s: %s image(s) missing from the atlas, e.g. %s")
                             % (where, len(lost), sorted(lost)[0]))
    for q, j in skel.items():
        names = set((j.get("animations") or {}).keys())
        if ref is not None and ref not in names:
            probs.append(tr("%s: Timeline plays %s, skeleton has %s") % (q, ref, sorted(names)))
        if sa.get(q) and sa[q] not in names:
            probs.append(tr("%s: start animation %s is not in the skeleton") % (q, sa[q]))
    return probs


def _own_port_only(ctx, mrk: int) -> bool:
    recs = C._ours(ctx, mrk)
    return bool(recs) and all(r.op == OP for r in recs)


def _rebuild(ctx, mrk: int, f, **kw):
    if ctx.dry_run:
        log.info(tr("  would rebuild %s (dry run)") % mrk)
        return None
    C.remove(ctx, mrk)
    return port(ctx, mrk, f, force=True, **kw)


def _single(path) -> bool:
    p = Path(path)
    return p.is_file() or _is_export_dir(p)


def port_card(ctx, mrk: int, path, force=False, anim=None, scale=1.0):
    f = Path(path) if _single(path) else by_card(path, mrk)[3]
    kw = {"anim": anim, "scale": scale}
    if force and _own_port_only(ctx, mrk):
        return _rebuild(ctx, mrk, f, **kw)
    return port(ctx, mrk, f, force=force, **kw)


def port_all(ctx, path, force=False) -> dict:
    from mdstudio.core.gate import CutinGate
    rows = scan(path)
    if not rows:
        raise MDStudioError(tr("no Spine cut-ins found under %s") % path)
    live = CutinGate(ctx.game).mrks()
    out = {"ported": [], "skipped": []}
    for k, (key, mrk, name, f) in enumerate(rows, 1):
        log.info("[%d/%d] %s %s", k, len(rows), mrk, name or key)
        try:
            if C._ours(ctx, mrk):
                if mrk not in live:
                    raise MDStudioError(tr("%s: your import was wiped by a game update. Run: mdstudio resync") % mrk)
                if not _own_port_only(ctx, mrk):
                    raise MDStudioError(tr("%s already has a cut-in you made with this tool (not a Spine import). "
                                          "Remove it first.") % mrk)
                bad = validate(ctx.game, mrk)
                if not bad:
                    raise MDStudioError(tr("%s already imported by this tool and passes --check") % mrk)
                if not force:
                    raise MDStudioError(tr("%s already imported by this tool but --check finds problems (%s). "
                                          "Run again with --force to rebuild it.") % (mrk, bad[0]))
                log.info(tr("  rebuilding, --check found: %s") % bad[0])
                if _rebuild(ctx, mrk, f) is not None:
                    out["ported"].append(mrk)
                continue
            replace = (force and mrk in live and is_old_port(ctx.game, mrk, f)
                       and bool(validate(ctx.game, mrk)))
            port(ctx, mrk, f, force=replace)
            out["ported"].append(mrk)
        except MDStudioError as e:
            out["skipped"].append((mrk, name or key, str(e)))
            log.info(tr("  skipped: %s") % e)
    log.info(tr("%s imported, %s skipped") % (len(out["ported"]), len(out["skipped"])))
    return out


def installed_skeleton(game, mrk: int):
    from mdstudio.core.catalog import Catalog
    for lp in C.own_paths(Catalog(game), mrk):
        if lp.endswith(f"/P{mrk}JS") and game.find_logical(lp):
            for o in UnityPy.load(game.find_logical(lp).read_bytes()).objects:
                if o.type.name == "TextAsset":
                    return _text(o.read()).encode("utf-8", "surrogateescape")
    return None


def is_old_port(game, mrk: int, src_file) -> bool:
    have = installed_skeleton(game, mrk)
    if have is None:
        return False
    try:
        src = Source.load(src_file)
    except MDStudioError:
        return False
    return have.strip() in (src.skel.strip(), rename_anim(src.skel, src.anim).strip())


def check_all(ctx, path) -> list:
    from mdstudio.core.gate import CutinGate
    live = CutinGate(ctx.game).mrks()
    return [(mrk, name or key, validate(ctx.game, mrk)) for key, mrk, name, f in scan(path) if mrk in live]
