"""动作的调度表. 单独一个模块, 是为了让子进程能干净地 import 到它."""
from pathlib import Path

from PIL import Image

ACTIONS = {}


def register(name: str):
    def deco(fn):
        ACTIONS[name] = fn
        return fn
    return deco


@register("cutin.clone")
def _clone(ctx, card, donor, force=False):
    from mdstudio.ops import cutin as C
    return C.clone(ctx, card, donor, force=force)


@register("cutin.remove")
def _remove(ctx, card):
    from mdstudio.ops import cutin as C
    return C.remove(ctx, card)


@register("cutin.video")
def _video(ctx, card, source, **kw):
    from mdstudio.ops.video import cutin_flip
    return cutin_flip.apply(ctx, card, source, **kw)


@register("runfx.borrow")
def _fx_borrow(ctx, card, donor, replace=False):
    from mdstudio.ops import runfx as R
    return R.borrow(ctx, card, donor, replace=replace)


@register("runfx.remove")
def _fx_remove(ctx, card):
    from mdstudio.ops import runfx as R
    return R.remove(ctx, card)


@register("runfx.video")
def _fx_video(ctx, card, source, **kw):
    from mdstudio.ops.video import runfx_flip
    return runfx_flip.apply(ctx, card, source, **kw)


@register("spine.port_card")
def _spine_card(ctx, card, source, force=False, anim=None, scale=1.0):
    from mdstudio.ops import spine as M
    return M.port_card(ctx, card, source, force=force, anim=anim, scale=scale)


@register("spine.port_all")
def _spine_all(ctx, source, force=False):
    from mdstudio.ops import spine as M
    return M.port_all(ctx, source, force=force)


@register("spine.check")
def _spine_check(ctx, source):
    from mdstudio.ops import spine as M
    return M.check_all(ctx, source)


@register("pack.export")
def _pack_export(ctx, cards, out_path, name="my-pack", zip_it=True):
    from mdstudio.ops import pack as P
    final = P.export(ctx, cards, out_path, name=name, zip_it=zip_it)
    # Path 不能直接进协议, 回个能读的摘要
    return {"path": str(final), "name": name}


@register("pack.install")
def _pack_install(ctx, pack_path, skip_existing=True, replace=False):
    from mdstudio.ops import pack as P
    done = P.install(ctx, pack_path, skip_existing=skip_existing, replace=replace)
    return {"installed": [{"kind": k, "card": m} for k, m in done]}


@register("history.undo")
def _history_undo(ctx, ids):
    """按 journal 里的 id 撤销

    undo_ids 会连带撤掉建在它上面的同类记录(clone -> video -> reskin 这类叠放),
    撤销完返回实际撤掉的那些, 界面上要照实说.
    """
    from mdstudio.ops.tx import undo_ids
    done = undo_ids(ctx, ids)
    return {"undone": [{"op": r.op, "card": r.card, "id": r.id} for r in done]}


@register("tx.resync")
def _resync(ctx):
    """游戏更新后: gate/manifest 被重写, 我们的条目没了, bundle 还在

    照 journal 把改过的东西补回去. 不新增记录, 所以历史列表不变。
    """
    from mdstudio.ops.tx import resync
    return resync(ctx)


@register("schemes.save")
def _schemes_save(ctx, schemes):
    """落盘用户方案列表 (内置那条不写)"""
    from mdstudio.gui import schemes as SCH
    SCH.save_custom(schemes)
    return {"ok": True}


@register("sound.inject")
def _sound_inject(ctx, label, wav_path, card=0):
    """用 WAV 换掉一个音效槽里的声音

    坑: 换的是音效槽本身, 所有用同一个槽的卡都会跟着变.
    全新槽游戏不认(没下载), 只能换盘上已有的.
    """
    from mdstudio.ops import sound as S
    return S.inject(ctx, label, wav_path, card=card)


@register("sound.restore")
def _sound_restore(ctx, label):
    from mdstudio.ops import sound as S
    S.restore(ctx, label)
    return {"restored": label}


@register("runfx.set_sound")
def _set_sound(ctx, card, label):
    """让这张卡的发动特效播放另一个音效槽, 不用重建效果"""
    from mdstudio.ops import runfx as R
    return R.set_sound(ctx, card, label)


@register("overframe.apply")
def _of_apply(ctx, card, art=None, subject=None, scale=1.0, dx=0, dy=0,
              lore_alpha=1.0, effect="", holo_scope="", preview_dir=None):
    """合成 + 写入 + 登记, 一步到位

    预览与写入用同一个 compose, 界面上看到的那张就是写进去的那张。
    没给卡图背景就用游戏当前插图当底。
    """
    from mdstudio.core.cards import CardDB
    from mdstudio.errors import MDStudioError
    from mdstudio.i18n import tr
    from mdstudio.ops import overframe as OF

    # 占位副本号写下去游戏也不会渲染, 直接挡掉, 免得看着成功其实白改
    real = CardDB(ctx.game).load().placeholder_of(int(card))
    if real:
        raise MDStudioError(tr("card %s is a placeholder copy; the game renders %s")
                            % (int(card), real))

    effect = OF.normalize_effect(effect)

    user_art = art
    if art is None:
        art = str(OF.base_art(ctx, int(card)))
    out = None
    if preview_dir:
        out = str(Path(preview_dir) / f"{int(card)}.png")
    img_path = OF.compose(int(card), art=art, subject=subject,
                          scale=scale, dx=dx, dy=dy, lore_alpha=lore_alpha,
                          effect=effect, out=out)
    with Image.open(img_path) as im:
        data = OF.apply(ctx, int(card), im.convert("RGBA"))

    # 记住这次用的素材与参数，下次打开这张卡自动加载，方便在原有基础上继续调
    if not getattr(ctx, "dry_run", False):
        OF.save_edit(ctx, int(card), art,
                     Path(art).name if user_art else "游戏原图",
                     subject,
                     Path(subject).name if subject else "",
                     scale, dx, dy, lore_alpha,
                     effect=effect)
    data["image"] = str(img_path)
    return data








@register("overframe.remove")
def _of_remove(ctx, card):
    from mdstudio.ops import overframe as OF
    data = OF.remove(ctx, int(card))
    if not getattr(ctx, "dry_run", False):
        OF.clear_edit(ctx, int(card))
    return data


@register("wallpaper.apply")
def _wp_apply(ctx, image, which=("lobby", "shop")):
    """把一张图铺到大厅壁纸 / 商店背景的全部纹理上。"""
    from mdstudio.ops import wallpaper as WP

    return WP.apply(ctx, image, which)


@register("wallpaper.restore")
def _wp_restore(ctx, which=("lobby", "shop")):
    """用备份把原包换回去。"""
    from mdstudio.ops import wallpaper as WP

    return WP.restore(ctx, which)
