"""预览缓存：生成时把每一帧单独存成 WebP，供软件内的播放器读。

不读游戏里的图集：高档两页 15360×15360 的 BC7 解成 RGBA 要 1.8 GB，预览打不开。
缓存按账号放在 `data/<安装>/<账号>/preview/<类型>/<卡>/`，跟 journal 同层；
复原成功时由 `tx.drop_preview_cache` 把对应类型那一份删掉，所以同一张卡的同一类
同时只会有一份（2026-11-20 定稿）。

两类分开放（2026-11-21 定稿）：
  preview/cutin/<卡>/   召唤动画（角色骨架那套）
  preview/runfx/<卡>/   发动特效（粒子贴图那套）
先分类型再分卡：列表要的就是一条 (类型, 卡号) 记录，目录结构跟数据模型一比一，
删除、按类型统计、以后加第三类，都只动一个目录。
"""
import json
import logging
import shutil

log = logging.getLogger("mdstudio")

# 类型名同时是目录名，也是 meta 里的 kind 字段
KIND_CUTIN = "cutin"
KIND_RUNFX = "runfx"
KINDS = (KIND_CUTIN, KIND_RUNFX)


def _root(ctx):
    return ctx.journal.folder / "preview"


def folder_of(ctx, card: int, kind: str = KIND_CUTIN):
    """这一份缓存该放哪。旧版把动画缓存直接放在 preview/<卡>/，这里顺手迁过来。"""
    kind = kind or KIND_CUTIN
    root = _root(ctx)
    target = root / kind / str(int(card))
    if kind == KIND_CUTIN and not target.exists():
        legacy = root / str(int(card))
        if legacy.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                legacy.rename(target)
            except OSError:
                # 搬不动（被占用等）就照旧读原处，别让预览直接失效
                return legacy
    return target


def read_meta(ctx, card: int, kind: str = KIND_CUTIN):
    p = folder_of(ctx, card, kind) / "meta.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def size_of(ctx, card: int, kind: str = KIND_CUTIN) -> int:
    d = folder_of(ctx, card, kind)
    if not d.is_dir():
        return 0
    return sum(f.stat().st_size for f in d.iterdir() if f.is_file())


def _migrate_legacy(ctx) -> int:
    """把旧版平铺的 preview/<卡>/ 全部挪进 preview/cutin/。

    只认纯数字目录；两边都有时保留新位置那份（旧的是上回没搬完的残余）。
    """
    root = _root(ctx)
    if not root.is_dir():
        return 0
    moved = 0
    for d in list(root.iterdir()):
        if not d.is_dir() or not d.name.isdigit():
            continue
        dest = root / KIND_CUTIN / d.name
        if dest.exists():
            shutil.rmtree(d, ignore_errors=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            d.rename(dest)
            moved += 1
        except OSError:
            pass
    return moved


def list_cards(ctx, kind: str = None) -> list:
    """有缓存的记录，最近的在前。kind=None 列两类，每行带 kind 字段。"""
    _migrate_legacy(ctx)
    out = []
    for k in (KINDS if kind is None else (kind,)):
        base = _root(ctx) / k
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if not d.is_dir() or not d.name.isdigit():
                continue
            card = int(d.name)
            meta = read_meta(ctx, card, k) or {}
            out.append({"kind": k, "card": card,
                        "frames": int(meta.get("frames") or 0),
                        "bytes": size_of(ctx, card, k),
                        "created": meta.get("created") or "",
                        "duration": meta.get("duration") or 0})
    return sorted(out, key=lambda x: x["created"], reverse=True)


def drop(ctx, card: int, kind: str = KIND_CUTIN) -> bool:
    d = folder_of(ctx, card, kind)
    if not d.is_dir():
        return False
    if not ctx.dry_run:
        shutil.rmtree(d, ignore_errors=True)
    return True
