"""大厅壁纸 / 商店背景替换。

大厅壁纸：LocalData 下按 hash 命名的 AssetBundle 里的整张纹理：

    WallPaper0001_1   2048×1304   bundle 6aadf393   （大厅壁纸，青眼亚白龙）

商店/大厅背景：同一张图铺到这 8 张底板上（预览里就是 8 格）：
    - 6 个 hash bundle 里的 ShopBGBase*（原 LocalData 下按 hash 命名）
    - data.unity3d（游戏安装根目录的共享大包）里的 ShopBGBase02 与 TexSoloTransitionBG
      游戏默认的大厅/ Solo 过场背景实际由这两张渲染，只换上面 6 个 hash 包在游戏里
      看不出变化，所以两张必须一起换。

替换做法：把同名纹理按原尺寸重画成用户给的图（保留 alpha），整包存回原路径。
游戏必须完全退出，否则文件被占用、也读不到新内容。
"""

import json
import logging
import os
import shutil
from pathlib import Path

import UnityPy
from PIL import Image

from mdstudio import runtime
from mdstudio.core.game import ensure_closed
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr

log = logging.getLogger("mdstudio")

LOBBY_BUNDLE = "6aadf393"
LOBBY_PREFIX = "WallPaper0001"
LOBBY_SIZE = (2048, 1304)

# 大厅壁纸（6aadf393，2048×1304）在游戏里的真实摆放，由一张 2560×1440 大厅截图
# 用 SIFT 特征匹配反推（404 个内点，角度≈0）：游戏把这张纹理整体放大到 4/3、向右上偏移，
# 所以纹理比屏幕(2560×1440)还大、且只盖住屏幕右约 62% 与整高，左约 38% 与上下溢出部分被裁/不显示。
# 下值是纹理在 2560×1440 屏幕坐标系里的外接矩形（左上 x,y 与 宽,高，单位 px）。
LOBBY_SCREEN = (2560, 1440)
LOBBY_TEX_RECT_SCREEN = (967.8, -288.7, 2730.8, 1737.3)   # (x, y, w, h)

# 商店/大厅背景：同一张图铺到这几张底板上
SHOP_BUNDLES = ("69ff6299", "296a15e7", "c9f7c9cf", "a9374af1", "6803c471", "1f04f4e7")
SHOP_PREFIX = "ShopBGBase"
SHOP_SIZE = (1920, 1080)
SHOP_SAMPLE = "69ff6299"          # 界面上拿它当预览代表

# 还有两张背景躺在游戏安装根的 data.unity3d（共享大包）里，游戏默认大厅 / Solo 过场
# 背景实际由它们渲染。只换上面 6 个 hash 包在游戏里看不出变化，必须一起换。
SHOP_DATA_UNITY3D_TEXTURES = ("ShopBGBase02", "TexSoloTransitionBG")


def _plan(which) -> tuple:
    """(包列表, 纹理名前缀, 原始尺寸)"""
    if which == "lobby":
        return (LOBBY_BUNDLE,), LOBBY_PREFIX, LOBBY_SIZE
    if which == "shop":
        return SHOP_BUNDLES, SHOP_PREFIX, SHOP_SIZE
    raise MDStudioError(tr("unknown wallpaper target: %s") % which)


def _backup_path(h: str) -> Path:
    return runtime.data_dir() / "wallpaper" / "backup" / h


def _du3d_backup_path() -> Path:
    """data.unity3d 的常驻原图备份。

    事务会把原包备份进 journal 目录，但那份会随数据目录重置（换版本、重新解压）一起消失，
    而 data.unity3d 上百 MB，丢了就再也拿不回来。这里单独留一份，只写一次。
    """
    d = runtime.data_dir() / "wallpaper" / "backup"
    d.mkdir(parents=True, exist_ok=True)
    return d / "data.unity3d"


def _du3d_marker() -> Path:
    """data.unity3d 当前是否被我们改过的标记。写过就有，还原后删掉。"""
    return runtime.data_dir() / "wallpaper" / "backup" / "data.unity3d.replaced"


def _preview_path(h: str, name: str) -> Path:
    d = runtime.data_dir() / "wallpaper" / "preview"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}_{name}.png"


def _stamp_path(h: str) -> Path:
    d = runtime.data_dir() / "wallpaper" / "preview"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.stamp"


def _bundle(ctx, h: str) -> Path:
    p = ctx.game.find(h)
    if not p:
        raise MDStudioError(tr("bundle not found: %s") % h)
    return p


def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """等比缩到装得下为止（contain），居中摆放，四周留透明。

    不裁切也不拉伸：正方形图放进 16:9 的底板时，左右会留透明边，
    游戏里看到的就是完整一张图，不会被窗口切掉。
    """
    r = min(w / img.width, h / img.height)
    nw, nh = max(1, round(img.width * r)), max(1, round(img.height * r))
    small = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.alpha_composite(small, ((w - nw) // 2, (h - nh) // 2))
    return canvas


def _textures(env, prefix: str):
    """遍历包里名字以 prefix 开头的 Texture2D，返回 [(对象, 数据)]。"""
    out = []
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        if (getattr(data, "m_Name", "") or "").startswith(prefix):
            out.append((obj, data))
    return out


def _textures_by_name(env, names):
    """遍历包里名字正好在 names 里的 Texture2D（精确匹配，不前缀）。"""
    out = []
    name_set = set(names)
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        if (getattr(data, "m_Name", "") or "") in name_set:
            out.append((obj, data))
    return out


def _data_unity3d_path(ctx) -> Path | None:
    """游戏安装根目录的 data.unity3d（共享资源大包）。

    真实位置是 <安装根>/masterduel_Data/data.unity3d（Unity 的 _Data 数据目录），
    不在安装根下直接。找不到返回 None，由调用方跳过。
    """
    d = ctx.game.dir
    for cand in (
        d / "masterduel_Data" / "data.unity3d",
        d / "MasterDuel_Data" / "data.unity3d",
        d / "data.unity3d",
    ):
        if cand.is_file():
            return cand
    hits = [p for p in d.glob("*_Data/data.unity3d") if p.is_file()]
    return hits[0] if hits else None


def _export_data_unity3d(ctx) -> list:
    """把 data.unity3d 里那两张背景纹理导出成 PNG（带版本戳缓存，避免每次刷新都加载大包）。"""
    p = _data_unity3d_path(ctx)
    if not p:
        return []
    cur = _stamp(p)
    stamp_file = runtime.data_dir() / "wallpaper" / "preview" / "_du3d.stamp"
    if stamp_file.is_file():
        try:
            cached = json.loads(stamp_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if cached and cached.get("stamp") == cur:
            items = [i for i in cached.get("items", []) if Path(i["path"]).is_file()]
            if len(items) == len(cached.get("items", [])):
                return items
    env = UnityPy.load(p.read_bytes())
    items = []
    for obj, data in _textures_by_name(env, SHOP_DATA_UNITY3D_TEXTURES):
        out = _preview_path("_du3d", data.m_Name)
        data.image.convert("RGBA").save(out)
        items.append({"path": str(out), "name": data.m_Name, "bundle": "data.unity3d",
                      "width": data.m_Width, "height": data.m_Height})
    del env
    try:
        stamp_file.write_text(json.dumps({"stamp": cur, "items": items}, ensure_ascii=False),
                              encoding="utf-8")
    except OSError:
        pass
    return items


def _drop_du3d_previews() -> None:
    d = runtime.data_dir() / "wallpaper" / "preview"
    if not d.is_dir():
        return
    for f in list(d.glob("_du3d_*.png")) + [d / "_du3d.stamp"]:
        try:
            f.unlink()
        except OSError:
            pass


def _drop_previews(h: str) -> None:
    """清掉这个包的预览缓存（png + 版本戳）。

    还原用的是 copy2，会把备份的旧 mtime 一起带回来，光比 mtime 会误判缓存还新，
    所以写入/还原后直接删掉重导。
    """
    d = runtime.data_dir() / "wallpaper" / "preview"
    if not d.is_dir():
        return
    for f in list(d.glob(f"{h}_*.png")) + [d / f"{h}.stamp"]:
        try:
            f.unlink()
        except OSError:
            pass


def _stamp(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def _export(ctx, h: str, prefix: str) -> list:
    """把一个包里所有匹配纹理导出成 PNG。

    缓存按「包的大小 + mtime」判版本，存到 preview/<hash>.stamp 里：
    替换会让包变大，还原会把 mtime 拨回旧的，两种变化都能命中，不会读到旧图。
    """
    p = _bundle(ctx, h)
    cur = _stamp(p)
    stamp_file = _stamp_path(h)
    if stamp_file.is_file():
        try:
            cached = json.loads(stamp_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if cached and cached.get("stamp") == cur:
            items = [i for i in cached.get("items", []) if Path(i["path"]).is_file()]
            if len(items) == len(cached.get("items", [])):
                return items

    # 读字节：按路径加载会占着文件句柄，后续 apply/restore 的替换就会报拒绝访问
    env = UnityPy.load(p.read_bytes())
    items = []
    for obj, data in _textures(env, prefix):
        out = _preview_path(h, data.m_Name)
        data.image.convert("RGBA").save(out)
        items.append({"path": str(out), "name": data.m_Name, "bundle": h,
                      "width": data.m_Width, "height": data.m_Height})
    del env
    try:
        stamp_file.write_text(json.dumps({"stamp": cur, "items": items}, ensure_ascii=False),
                              encoding="utf-8")
    except OSError:
        pass
    return items


def preview(ctx, which: str) -> dict:
    """单张预览：大厅壁纸给那张，商店背景给代表包。"""
    bundles, prefix, _ = _plan(which)
    h = bundles[0] if which == "lobby" else SHOP_SAMPLE
    items = _export(ctx, h, prefix)
    if not items:
        raise MDStudioError(tr("texture not found in %s") % h)
    return items[0]


def previews(ctx, which: str) -> list:
    """每个包取一张代表纹理，界面按「一包一格」列出来。

    lobby 返回 WallPaper0001_1 一张；shop 返回 6 个 hash 包各一张 + data.unity3d
    里的 ShopBGBase02 / TexSoloTransitionBG 两张，共 8 张（对应界面三行三列的第 1~8 格）。
    """
    if which == "lobby":
        items = _export(ctx, LOBBY_BUNDLE, LOBBY_PREFIX)
        return items
    out = []
    for h in SHOP_BUNDLES:
        try:
            items = _export(ctx, h, SHOP_PREFIX)
        except MDStudioError:
            continue
        if items:
            out.append(items[0])
    out += _export_data_unity3d(ctx)
    return out


def status(ctx) -> dict:
    out = {}
    for which in ("lobby", "shop"):
        bundles, _, _ = _plan(which)
        items = []
        for h in bundles:
            p = ctx.game.find(h)
            items.append({"bundle": h, "present": bool(p),
                          "replaced": _is_replaced(ctx, h)})
        if which == "shop":
            du = _data_unity3d_path(ctx)
            # 历史会被重置，所以除了 journal 还要看那个标记文件，否则改过的图会显示成"没动过"
            items.append({"bundle": "data.unity3d", "present": du is not None,
                          "replaced": bool(du) and (_is_replaced(ctx, du) or _du3d_marker().is_file())})
        out[which] = {
            "bundles": items,
            "replaced": all(i["replaced"] for i in items if i["present"]),
            "any_replaced": any(i["replaced"] for i in items if i["present"]),
        }
    out["lobby_screen"] = list(LOBBY_SCREEN)
    out["lobby_tex_rect"] = list(LOBBY_TEX_RECT_SCREEN)
    return out


def _is_replaced(ctx, h_or_path) -> bool:
    """这张资源被我们改过 = 操作记录里有它（历史页可撤销）。

    h_or_path 可以是 8 位 hash 串（LocalData 下按 hash 命名的包）或 data.unity3d 的 Path。
    """
    p = h_or_path if isinstance(h_or_path, Path) else ctx.game.find(h_or_path)
    if not p or not Path(p).is_file():
        return False
    rel = ctx.game.rel(Path(p))
    return any(r.op == "wallpaper.set" and rel in r.modified for r in ctx.journal.load())


def apply(ctx, image, which=("lobby", "shop")) -> dict:
    """把 image 铺到指定目标的全部纹理上，并登记到操作记录（历史页可见、可撤销）。

    走 Tx 事务：原包备份进 journal、写回新包、记一条历史、发一条日志，
    与卡图/超框完全一致。整张纹理按 _fit（contain）烘进原尺寸，不裁切。
    """
    from mdstudio.ops.tx import Tx

    src = Image.open(image).convert("RGBA")
    if not isinstance(which, (list, tuple)):
        which = (which,)
    ensure_closed()
    done = []
    wrote = False
    tx = Tx(ctx, "wallpaper.set", 0)
    tx.rec.meta["label"] = "、".join("大厅壁纸" if t == "lobby" else "大厅背景" for t in which)
    for target in which:
        bundles, prefix, _ = _plan(target)
        for h in bundles:
            p = _bundle(ctx, h)
            # 从字节读，UnityPy 才不会一直占着原文件句柄（否则 ctx.write 时会被拒绝访问）
            env = UnityPy.load(p.read_bytes())
            n = 0
            for obj, data in _textures(env, prefix):
                data.set_image(_fit(src, data.m_Width, data.m_Height))
                data.save()
                n += 1
            if not n:
                del env
                continue
            if getattr(ctx, "dry_run", False):
                del env
                done.append({"bundle": h, "textures": n, "dry_run": True})
                continue
            # 先写到同目录临时文件夹再替换，避免中途失败留下半个包
            tmp = p.parent / f"_wp_{h}"
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir()
            env.save(pack="original", out_path=str(tmp))
            del env
            # 从字节加载时 UnityPy 给的输出名不一定是 hash，取目录里实际写出的那个文件
            made = tmp / h
            if not made.is_file():
                outs = [q for q in tmp.iterdir() if q.is_file()]
                made = outs[0] if len(outs) == 1 else None
            if made is None:
                shutil.rmtree(tmp, ignore_errors=True)
                raise MDStudioError(tr("failed to write bundle %s") % h)
            new_bytes = made.read_bytes()
            shutil.rmtree(tmp, ignore_errors=True)
            # 登记：原包备份进 journal、写回新包、记一条历史
            tx.put_existing(p, new_bytes)
            wrote = True
            _drop_previews(h)
            done.append({"bundle": h, "textures": n})
        if target == "shop":
            n = _apply_data_unity3d(ctx, src, tx)
            if n:
                wrote = True
                done.append({"bundle": "data.unity3d", "textures": n})
    if wrote and not getattr(ctx, "dry_run", False):
        tx.commit()
        log.info(tr("wallpaper replaced %s bundle(s): %s"),
                 len(done), ", ".join(d["bundle"] for d in done))
    return {"targets": done}


def restore(ctx, which=("lobby", "shop")) -> dict:
    """把壁纸还原成游戏原图。优先撤销操作记录（journal），没有记录再退回到旧备份目录。"""
    from mdstudio.ops.tx import undo_ids

    if not isinstance(which, (list, tuple)):
        which = (which,)
    targets = set()
    for target in which:
        for h in _plan(target)[0]:
            p = ctx.game.find(h)
            if p:
                targets.add(ctx.game.rel(p))

    recs = [r for r in ctx.journal.load()
            if r.op == "wallpaper.set" and (set(r.modified) & targets)]
    if recs:
        undo_ids(ctx, [r.id for r in recs])
        done = [{"bundle": h} for target in which for h in _plan(target)[0]]
        log.info(tr("wallpaper restored %s bundle(s)"), len(done))
    else:
        # 早期版本没写 journal 时的兜底
        ensure_closed()
        done = []
        for target in which:
            for h in _plan(target)[0]:
                bak = _backup_path(h)
                if not bak.is_file():
                    continue
                if not getattr(ctx, "dry_run", False):
                    p = _bundle(ctx, h)
                    shutil.copy2(bak, p)
                    bak.unlink()
                done.append({"bundle": h})
        # data.unity3d 没有历史记录时，用常驻的那份原图还原（这份还原后保留，下次还能用）
        if "shop" in which and _du3d_marker().is_file():
            du, bak_du = _data_unity3d_path(ctx), _du3d_backup_path()
            if du and bak_du.is_file():
                if not getattr(ctx, "dry_run", False):
                    shutil.copy2(bak_du, du)
                done.append({"bundle": "data.unity3d"})
    for target in which:
        for h in _plan(target)[0]:
            _drop_previews(h)
    if "shop" in which:
        # data.unity3d 的预览缓存单独一组文件，漏了会导致还原后界面还显示旧图
        _drop_du3d_previews()
        if not getattr(ctx, "dry_run", False):
            try:
                _du3d_marker().unlink()
            except OSError:
                pass
    return {"targets": done}


def _apply_data_unity3d(ctx, src, tx) -> int:
    """把图铺到 data.unity3d 里的 ShopBGBase02 / TexSoloTransitionBG。

    data.unity3d 是安装根目录的共享大包（可能上 GB），UnityPy 要整包重新存一次，
    耗时与内存都更高；但只有「确认替换大厅背景」时才触发。走 Tx 事务：原包自动备份到
    data/wallpaper/backup/data.unity3d（只留一份「真原图」），写回新包、记一条历史、
    历史页可撤销。
    """
    p = _data_unity3d_path(ctx)
    if not p:
        log.warning(tr("data.unity3d not found; skip %s"),
                   ", ".join(SHOP_DATA_UNITY3D_TEXTURES))
        return 0
    # 真原图只留一份。这一步必须在改之前做：事务里的备份会随数据目录重置一起消失
    if not getattr(ctx, "dry_run", False):
        bak = _du3d_backup_path()
        if not bak.is_file():
            shutil.copy2(p, bak)
    # 读全部字节再加载：和 6 个 hash 包一样，避免 UnityPy 占着原文件句柄，
    # 否则后面 tx.put_existing 要读原文件做备份 / 写回新内容时会报拒绝访问
    env = UnityPy.load(p.read_bytes())
    n = 0
    for obj, data in _textures_by_name(env, SHOP_DATA_UNITY3D_TEXTURES):
        data.set_image(_fit(src, data.m_Width, data.m_Height))
        data.save()
        n += 1
    if not n:
        del env
        return 0
    if getattr(ctx, "dry_run", False):
        del env
        return n
    tmp = p.parent / "_wp_du3d"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    env.save(pack="original", out_path=str(tmp))
    del env
    # UnityPy 给的输出名不一定是 data.unity3d，取目录里实际写出的那个文件
    made = tmp / p.name
    if not made.is_file():
        outs = [q for q in tmp.iterdir() if q.is_file()]
        made = outs[0] if len(outs) == 1 else None
    if made is None:
        shutil.rmtree(tmp, ignore_errors=True)
        raise MDStudioError(tr("failed to write data.unity3d"))
    new_bytes = made.read_bytes()
    shutil.rmtree(tmp, ignore_errors=True)
    # 登记：原包备份进 journal（共享位置，只留真原图）、写回新包、记一条历史
    tx.put_existing(p, new_bytes)
    try:
        _du3d_marker().write_text("1", encoding="utf-8")
    except OSError:
        pass
    _drop_du3d_previews()
    return n
