"""动作的调度表. 单独一个模块, 是为了让子进程能干净地 import 到它."""
from pathlib import Path

from PIL import Image

ACTIONS = {}


def register(name: str, needs_ctx: bool = True):
    """登记一个动作。

    needs_ctx=False 的动作不打开游戏上下文——资源下载在选游戏目录之前就要能跑，
    没有游戏安装也得能下模型。
    """
    def deco(fn):
        fn.needs_ctx = needs_ctx
        ACTIONS[name] = fn
        return fn
    return deco


def missing_assets(kind, source=None, sharp=False):
    """这次操作会用到哪些可选组件，现在缺哪几个。

    界面在动作开始前问一遍，缺了就引导去设置页下载，不硬着头皮跑。
    预检查不出来的情况一律返回空：真错误留给正式流程去报，不能在这里替它下结论。
    """
    from mdstudio import assets

    found = []

    def add(key):
        a = assets.BY_KEY.get(key)
        if a is not None and not assets.present(a):
            found.append({"key": a.key, "label": a.label, "size": a.size})

    try:
        if kind == "spine":
            if source:
                from mdstudio.ops import spine as S

                ver = S.probe_version(source)
                # 只有跨大版本才要转换器；4.x 的骨架直接就能喂给游戏
                if ver and ver[0] != S.RUNTIME[0] and S._converter_path() is None:
                    add("spineconv")
        elif kind in ("cutin.video", "runfx.video"):
            if sharp:
                add("realesrgan")
            if source:
                from mdstudio.ops.video import frames as F

                # 两条路都走 make_frames 的 auto 判定：有透明通道就用现成的、
                # 有幕布色才跑模型、两样都没有就整帧原样
                if F.needs_matte(source):
                    add("matting")
    except Exception:
        return []
    return found


def cutin_precheck(kind="cutin.video", source=None, n=0, cell=0, compat_sd=None, sharp=False):
    """生成前一次问清三件事：缺哪些组件、素材够不够帧、按参数要几页。

    优先级跟界面提示一致：组件 > 素材帧数 > 页数，只回第一条挡路的原因。
    用户改一个参数往往能顺带解决后面的，一次报一堆只会让人不知道该先动哪个。

    页数按「每帧占 cell × cell」估，不看裁剪后的实际宽高：只用用户设的两个数就能
    得到结论，可预期。真实帧通常更窄，容量只会更大，所以这里拒绝的，正式流程
    （用真实宽高再算一次）不会推翻。
    """
    from mdstudio.i18n import tr

    missing = missing_assets(kind, source, sharp)
    out = {"missing": missing, "block": None}
    if missing:
        return out          # 缺组件先解决，参数问题等装完再看

    if not (n and cell):
        return out
    if compat_sd is None:
        try:
            from mdstudio import settings as _settings

            compat_sd = bool(_settings.load().get("compat_sd", False))
        except Exception:
            compat_sd = False

    from mdstudio.ops.video import frames as F

    have = F.source_frames(source) if source else None
    out["frames_have"] = have
    out["frames_need"] = n
    if have is not None and have < n:
        out["block"] = {
            "why": "frames",
            "message": tr("the source has only %s frame(s) but %s were requested. Lower the "
                          "frame count or use a longer clip.") % (have, n),
        }
        return out

    per_page = (F.MAX_TEX // cell) ** 2
    pages = -(-n // per_page)
    out["pages"] = pages
    if pages > 1 and compat_sd:
        out["block"] = {
            "why": "pages",
            "message": tr("%s frames at %spx don't fit one texture page (low-quality compat "
                          "writes a single page). Lower the size per frame or reduce the count.")
                       % (n, cell),
        }
    elif pages > 2:
        out["block"] = {
            "why": "pages",
            "message": tr("%s frames at %spx need more than two texture pages. Lower the size per "
                          "frame or reduce the frame count.") % (n, cell),
        }
    return out


@register("assets.ensure", needs_ctx=False)
def _assets_ensure(ctx, key):
    from mdstudio import assets, progress
    from mdstudio.i18n import tr

    asset = assets.BY_KEY[key]

    def on_progress(done, total, speed):
        # 用 tick 而不是 step：界面要拿 done/total 画进度条。
        # tick 不做 % 格式化（那是 step 的专属），名字在这里自己填，
        # 不然界面显示字面 "%s"（2026-11-11 用户实测）。
        progress.tick(tr("downloading %s") % asset.label, done, total or asset.size)

    assets.ensure(key, on_progress=on_progress)
    return {"key": key, "ok": True}


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
def _spine_card(ctx, card, source, force=False, anim=None, scale=1.0, donor=None,
                compat_sd=None):
    from mdstudio import probe
    from mdstudio.errors import MDStudioError
    from mdstudio.ops import spine as M

    # 缺 .json 或 .atlas 的素材必然导不进去，先在门口拦下，
    # 别等到拷贴图拷到一半才报错。
    r = probe.check(source, "spine")
    if not r["ok"]:
        raise MDStudioError(r["reason"])
    return M.port_card(ctx, card, source, force=force, anim=anim, scale=scale, donor=donor, compat_sd=compat_sd)


@register("spine.port_all")
def _spine_all(ctx, source, force=False):
    from mdstudio.ops import spine as M
    return M.port_all(ctx, source, force=force)


@register("spine.check")
def _spine_check(ctx, source):
    from mdstudio.ops import spine as M
    return M.check_all(ctx, source)


@register("pack.export")
def _pack_export(ctx, cards, out_path, name="my-pack", zip_it=True, kinds=None):
    from mdstudio.ops import pack as P
    # kinds: {卡号: ["cutin","runfx","art"]}，界面按每张卡的勾选传入；
    # 键过 JSON 会变字符串，P.export 里统一 int(k) 转回。
    final = P.export(ctx, cards, out_path, name=name, zip_it=zip_it, kinds=kinds)
    # Path 不能直接进协议, 回个能读的摘要
    return {"path": str(final), "name": name}


@register("pack.install")
def _pack_install(ctx, pack_path, selected=None):
    from mdstudio.ops import pack as P
    # 统一覆盖语义（2026-11-11）：跳过已废除，skip_existing/replace 退役
    done, overwritten = P.install(ctx, pack_path, selected=selected)
    return {"installed": [{"kind": k, "card": m} for k, m in done],
            "overwritten": overwritten}


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
    db = CardDB(ctx.game).load()
    real = db.placeholder_of(int(card))
    if real:
        raise MDStudioError(tr("card %s is a placeholder copy; the game renders %s")
                            % (int(card), real))

    effect = OF.normalize_effect(effect)
    # 卡框跟着游戏当前数据走: 静态表没有新卡的条目, 会把魔法卡画成怪兽框
    code = db.frame_code(int(card))

    user_art = art
    if art is None:
        art = str(OF.base_art(ctx, int(card), code))
    out = None
    if preview_dir:
        out = str(Path(preview_dir) / f"{int(card)}.png")
    img_path = OF.compose(int(card), art=art, subject=subject,
                          scale=scale, dx=dx, dy=dy, lore_alpha=lore_alpha,
                          effect=effect, out=out, code=code, ctx=ctx)
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


@register("sound.adopt")
def _sound_adopt(ctx, card, label):
    """把别的槽的音频复制成这张卡自己的槽，原槽不受影响"""
    from mdstudio.ops import sound as S
    rec = S.adopt_label(ctx, int(card), label)
    return {"label": rec.meta.get("label"), "seconds": rec.meta.get("seconds"),
            "source": rec.meta.get("source")}


@register("sound.preview")
def _sound_preview(ctx, label):
    """把某个音效槽解成临时 wav，给界面试听。只读，不碰游戏文件"""
    from mdstudio.ops import sound as S
    return {"path": S.preview_clip(ctx, label)}


@register("sound.custom")
def _sound_custom(ctx, card, audio_path, label=""):
    """给这张卡建一个专属音效槽并指向它。audio 支持 mp3/wav 等, 内部统一转码"""
    from mdstudio.ops import sound as S
    rec = S.create_slot(ctx, int(card), audio_path, label=label)
    return {"label": rec.meta.get("label"), "seconds": rec.meta.get("seconds")}


@register("sound.set")
def _sound_set(ctx, card, label=""):
    """把这张卡的音效指向改成另一个槽。空串 = 无音效"""
    from mdstudio.ops import sound as S
    rec = S.set_label(ctx, int(card), label)
    return {"label": rec.meta.get("label") or ""}


@register("sound.restore_card")
def _sound_restore_card(ctx, card):
    """把这张卡的音效还原成改之前的样子"""
    from mdstudio.ops import sound as S
    rec = S.restore_label(ctx, int(card))
    return {"restored": rec.meta.get("restored") or []}


def runfx_timings(ctx, card):
    """这张卡现有的特效条目各是什么时机, 以及当前音效指向

    界面用它决定要不要显示时机分页: 有记录的卡按已有分页, 没记录的卡只有默认那一页。
    """
    from mdstudio.core.gate import RunFxGate
    from mdstudio.ops import runfx as R
    from mdstudio.ops import sound as S

    card = int(card)
    gate = RunFxGate(ctx.game)
    rows = gate.for_card(card)
    out = []
    for e in rows:
        t = e.get("runTiming")
        out.append({
            "timing": t,
            "name": R.TIMING.get(t, str(t)),
            "seLabel": e.get("seLabel") or "",
            "effectPath": e.get("effectPath") or "",
        })
    custom = S.custom_label(card)
    labels = S.labels_of_card(gate, card)
    # 原音按「条目身份」存，这里按当前条目逐条取；没记录过的条目就用自己的当前值。
    # 不回退的话，界面对任何一张没改过的卡都会显示「本卡原音：无」。
    want = S.origins_map(card, rows)
    origin = [want.get(S.entry_key(e), e.get("seLabel") or "") for e in rows]
    # 自改音效用的是哪份素材：取这张卡最近一条 sound.custom 记下来的文件名
    source = ""
    for r in sorted(ctx.journal.for_card(card), key=lambda x: x.time, reverse=True):
        if r.op == "sound.custom":
            source = (r.meta or {}).get("source") or ""
            break
    return {
        "origin": origin,
        "source": source,
        "card": card,
        "entries": out,
        "timings": [{"value": t, "name": n} for t, n in sorted(R.TIMING.items())],
        "custom_label": custom,
        "has_custom": custom in labels,
        # 按条目逐条列出, 无音效记空串。不能用 labels_of_card: 它把空串过滤掉了,
        # 界面拿它跟 origin 比条数会对不上, 复原按钮会永久置灰
        "current": [e.get("seLabel") or "" for e in rows],
        "has_custom_slot": S.on_disk(ctx.game, custom),
    }
