"""mdstudio 命令行. GUI/TUI 也是调同一套 ops, 这里不放逻辑

懒人用法:
  mdstudio                      不带参数 = 全屏界面 (tui.py), 鼠标键盘都能点
  mdstudio add cupsy            卡可以直接写名字, 同名多张会让你选
  mdstudio rm cupsy             这张卡本工具加过的东西全撤掉
  mdstudio undo                 撤销上一步
"""
import argparse
import logging
import sys
import time

from mdstudio import __version__
from mdstudio import pick
from mdstudio.core.context import Ctx
from mdstudio.errors import Ambiguous, MDStudioError
from mdstudio.i18n import tr

# argparse 的内置串 (usage: / options: / 报错格式) 走它模块级绑定的 gettext _,
# 换成查词典: 中文界面下这些一并翻, 翻不了的静默保持英文, 不影响任何行为
argparse._ = tr

log = logging.getLogger("mdstudio")

DEFAULT_CUTIN_DONOR = "11016"


def _setup_logging(verbose):
    # 坑: 越南/中文系统控制台默认 cp1258/gbk, 打印非 ascii 直接崩
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    log.handlers[:] = [h]
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def _tty():
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _table(rows, head):
    w = [max(len(str(x)) for x in col) for col in zip(head, *rows)] if rows else [len(h) for h in head]
    print("  ".join(str(h).ljust(n) for h, n in zip(head, w)))
    for r in rows:
        print("  ".join(str(x).ljust(n) for x, n in zip(r, w)))


def _db(ctx, a):
    # 一条命令里只加载一次. 冷启动要扫 bundle (~15s), 后面走缓存
    if not hasattr(a, "_db"):
        from mdstudio.core.cards import CardDB
        try:
            a._db = CardDB(ctx.game, a.lang).load()
        except MDStudioError as e:
            # 没卡名也能用, 只是列表里没名字
            log.warning(tr("card names unavailable: %s"), e)
            a._db = None
    return a._db


def _inv(ctx, a):
    if not hasattr(a, "_inv"):
        from mdstudio.ops.inventory import Inventory
        a._inv = Inventory(ctx.game, ctx.journal, _db(ctx, a))
    return a._inv


def _badge(st):
    # game = 在游戏的表里, 可能是官方, 也可能是别的工具/mod 加的, 区分不了. 只有本工具加的能确定
    # ctx="badge": 这个 yes 是状态标 (已有), 不是布尔列的是
    return {"game": tr("yes", ctx="badge"), "yours": tr("yours"), "wiped": tr("WIPED")}.get(st, "-")


def _ask(prompt):
    if not _tty():
        raise MDStudioError(tr("missing argument: %s") % prompt.strip(": "))
    try:
        return input(prompt).strip()
    except EOFError:
        raise MDStudioError(tr("cancelled"))


def _card(ctx, a, text, what="card", want=None) -> int:
    """名字或 id 都行. 没给就问 (只在交互终端里问), 有歧义就列出来让选"""
    what = tr(what)      # 调用方传英文原文, 这里统一译; tr 幂等, 传译文也不怕
    if text is None or str(text).strip() == "":
        text = _ask(tr("%s (name or ID): ") % what)
    if not pick.needs_names(text):
        return pick.card(None, text)
    db = _db(ctx, a)
    try:
        cid = pick.card(db, text, _inv(ctx, a), want)
    except Ambiguous as e:
        inv = _inv(ctx, a)
        rows = [(i + 1, c, db.name(c), _badge(inv.cutin.get(c)), _badge(inv.runfx.get(c)))
                for i, c in enumerate(e.candidates)]
        print(tr('"%s": did you mean') % e.query if len(e.candidates) == 1
              else tr('"%s" matches several cards:') % e.query)
        _table(rows, ("#", tr("id"), tr("name"), tr("cut-in"), tr("effect")))
        if not _tty():
            raise
        ans = _ask(tr("pick # (or type the ID): "))
        if ans.isdigit() and 1 <= int(ans) <= len(rows):
            cid = rows[int(ans) - 1][1]
        elif ans.isdigit():
            cid = int(ans)
        else:
            raise MDStudioError(tr("cancelled"))
    if db and db.name(cid):
        log.info(tr("%s: %s  %s"), what, cid, db.name(cid))
    return cid


def _file(a, text, what="file"):
    if text is None:
        text = _ask(tr("%s (drag it in here): ") % tr(what))
    p = pick.path(text)
    if not p.exists():
        raise MDStudioError(tr("not found: %s") % p)
    return p


# ---------------- handlers ----------------
def cmd_info(ctx, a):
    from mdstudio.core.catalog import Catalog
    from mdstudio.core.game import is_running
    print(tr("game     %s") % ctx.game.dir)
    print(tr("group    %s") % ctx.game.group)
    print(tr("manifest %s entries") % len(Catalog(ctx.game).data['informations']))
    print(tr("running  %s") % is_running())
    print(tr("journal  %s  (%s records)") % (ctx.journal.folder, len(ctx.journal.load())))


def cmd_cutin_list(ctx, a):
    from mdstudio.ops import cutin as C
    db = _db(ctx, a)
    rows = C.list_cards(ctx.game, ctx.journal)
    if not a.all:
        rows = [r for r in rows if r[1] != "game"]
    _table([(m, _badge(st), db.name(m) if db else "") for m, st in rows],
           (tr("card"), tr("status"), tr("name")))
    if not a.all:
        print(tr("(--all to include every card that has one = donors)"))


def cmd_cutin_add(ctx, a):
    from mdstudio.ops import cutin as C
    card = _card(ctx, a, a.card)
    donor = _card(ctx, a, a.donor or DEFAULT_CUTIN_DONOR, "donor", want="cutin")
    (C.clone if a.clone else C.borrow)(ctx, card, donor, force=a.force)
    _verify_cutin(ctx, card)


def _verify_cutin(ctx, card, force=False):
    if ctx.dry_run and not force:      # dry-run 的 add 没写文件, 查了也是缺
        return
    from mdstudio.core.catalog import Catalog
    from mdstudio.ops import cutin as C
    miss = C.missing(Catalog(ctx.game), card)
    for m in miss:
        print(tr("MISSING"), m)
    print(tr("OK. Start the game and summon it in Solo.") if not miss
          else tr("Missing files = infinite load. Remove it."))


def cmd_cutin_remove(ctx, a):
    from mdstudio.ops import cutin as C
    C.remove(ctx, _card(ctx, a, a.card))


def cmd_cutin_verify(ctx, a):
    _verify_cutin(ctx, _card(ctx, a, a.card), force=True)


def cmd_cutin_video(ctx, a):
    from mdstudio.ops.video import cutin_flip
    card = _card(ctx, a, a.card)
    src = _file(a, a.source, "video / gif / frame folder")
    donor = _card(ctx, a, a.donor or DEFAULT_CUTIN_DONOR, "donor", want="cutin")
    cutin_flip.apply(ctx, card, src, n=a.frames, cell=a.cell, duration=a.duration, size=a.size,
                     mode=a.mode, sharp=a.sharp, pop=False if a.no_pop else (True if a.pop else None),
                     donor=donor, preview_dir=a.preview,
                     pop_curve=cutin_flip.parse_pop_curve(a.pop_curve) if a.pop_curve else None,
                     offset_x=a.offset_x, offset_y=a.offset_y, zoom=a.zoom, rotate=a.rotate,
                     sharp_model=a.sharp_model)


def cmd_spine(ctx, a):
    from mdstudio.ops import spine as M
    src = pick.path(a.source) if a.source else _file(a, None, "Spine export, bundle, zip or folder of them")
    if not src.exists():
        raise MDStudioError(tr("not found: %s") % src)
    if M._single(src):
        if len(a.cards) != 1:
            raise MDStudioError(tr("one Spine cut-in: name the one card it goes on"))
        M.port_card(ctx, _card(ctx, a, a.cards[0]), src, force=a.force, anim=a.anim, scale=a.scale)
        print(tr("Done. Start the game and summon it in Solo."))
        return
    rows = M.scan(src)
    if not rows:
        raise MDStudioError(tr("no Spine cut-ins found under %s. Name files/folders by passcode, "
                              "or add a %s {\"<file or folder>\": <card id>}") % (src, M.MAP_FILE))
    if a.check:
        res = M.check_all(ctx, src)
        for mrk, name, p in res:
            print(f"{tr('BAD') if p else tr('ok')} {mrk:>6}  {name}" + "".join(f"\n        {x}" for x in p))
        bad = sum(bool(p) for _, _, p in res)
        print(tr("%s of %s installed card(s) have problems") % (bad, len(res)) if bad
              else tr("%s installed, all fine") % len(res))
        return
    if a.all:
        r = M.port_all(ctx, src, force=a.force)
        print(tr("%s imported, %s skipped (already had a cut-in, see the reasons above).")
              % (len(r['ported']), len(r['skipped']))
              + (tr(" Start the game and summon them in Solo.") if r["ported"] else ""))
        return
    if a.cards:
        for c in a.cards:
            M.port_card(ctx, _card(ctx, a, c), src, force=a.force, anim=a.anim, scale=a.scale)
        print(tr("Done. Start the game and summon it in Solo."))
        return
    live = _cutin_mrks(ctx)
    mine = {r.card for r in ctx.journal.load() if r.op == M.OP}
    _table([(mrk, tr("yours") if mrk in mine else (tr("has one") if mrk in live else "-"), name or key)
            for key, mrk, name, f in rows], (tr("card"), tr("cut-in"), tr("name")))
    print(tr("%s card(s). Import: mdstudio spine <folder> --all   or   mdstudio spine <folder> <card> ...")
          % len(rows))


def _cutin_mrks(ctx):
    from mdstudio.core.gate import CutinGate
    return CutinGate(ctx.game).mrks()


def cmd_reskin_export(ctx, a):
    from mdstudio.ops import cutin as C
    for f in C.reskin_export(ctx, _card(ctx, a, a.card), pick.path(a.out)):
        print(f)


def cmd_reskin_import(ctx, a):
    from mdstudio.ops import cutin as C
    files = []
    for x in a.png:
        p = pick.path(x)
        files += sorted(p.glob("*.png")) if p.is_dir() else [p]   # 给文件夹就吃整个文件夹
    print(tr("%s texture(s) replaced") % C.reskin_import(ctx, _card(ctx, a, a.card), files))


def cmd_runfx_list(ctx, a):
    from mdstudio.ops import runfx as R
    db = _db(ctx, a)
    nm = (lambda m: db.name(m)) if db else (lambda m: "")
    if a.donors:
        rows = [(m, nm(m), tr(t), se or "-", ep.rsplit("/", 1)[-1] if ep else tr("(built-in)"),
                 "" if ok is None else (tr("yes") if ok else tr("NO")))
                for m, t, se, ep, ok in R.list_effects(ctx.game)]
        _table(rows, (tr("card"), tr("name"), tr("timing"), tr("sound"), tr("effect"), tr("on disk")))
    else:
        _table([(m, _badge(st), nm(m)) for m, st in R.list_cards(ctx.game, ctx.journal) if st != "game"],
               (tr("card"), tr("status"), tr("name")))


def cmd_runfx_add(ctx, a):
    from mdstudio.ops import runfx as R
    card = _card(ctx, a, a.card)
    donor = _card(ctx, a, a.donor, "donor", want="runfx")
    R.borrow(ctx, card, donor, replace=a.replace)


def cmd_runfx_remove(ctx, a):
    from mdstudio.ops import runfx as R
    R.remove(ctx, _card(ctx, a, a.card))


def cmd_runfx_video(ctx, a):
    from mdstudio.ops.video import runfx_flip
    card = _card(ctx, a, a.card)
    src = _file(a, a.source, "video")
    label, use_audio = a.sound or "", not a.no_audio
    if use_audio:
        from mdstudio.ops.video import audio
        if src.is_dir() or not audio.has_audio(src):
            use_audio = False
            print(tr("no audio track in the source, the effect plays %s") % (label or tr("no sound")))
        elif not label:
            label = runfx_flip.DEFAULT_CARRIER    # 新 label 不响, 默认借这个装视频的声音
            print(tr("video sound goes into %s (cards using it play it too; --sound X to pick another)") % label)
    runfx_flip.apply(ctx, card, src, n=a.frames, cell=a.cell, duration=a.duration, mode=a.mode,
                     max_size=a.max_size, se_label=label, sharp=a.sharp, preview_dir=a.preview,
                     video_audio=use_audio)


def cmd_runfx_framing(ctx, a):
    from mdstudio.ops.video import runfx_flip
    runfx_flip.set_framing(ctx, _card(ctx, a, a.card), a.max_size)


def cmd_sound_info(ctx, a):
    from mdstudio.ops import sound as S
    print(tr("%s: on disk (replaceable)") % a.label if S.on_disk(ctx.game, a.label)
          else S.why_missing(a.label))


def cmd_sound_card(ctx, a):
    from mdstudio.core.gate import RunFxGate
    from mdstudio.ops import sound as S
    for lb in S.labels_of_card(RunFxGate(ctx.game), _card(ctx, a, a.card)):
        print(lb, tr("on disk (replaceable)") if S.on_disk(ctx.game, lb) else S.why_missing(lb))


def cmd_sound_inject(ctx, a):
    from mdstudio.ops import sound as S
    S.inject(ctx, a.label, _file(a, a.wav, "WAV"))
    print(tr("Tip, make it louder first:\n  ") + S.loudness_hint())


def cmd_sound_use(ctx, a):
    from mdstudio.ops import runfx as R
    R.set_sound(ctx, _card(ctx, a, a.card), a.label)


def cmd_sound_restore(ctx, a):
    from mdstudio.ops import sound as S
    S.restore(ctx, a.label)


def cmd_pack_export(ctx, a):
    from mdstudio.ops import pack as P
    cards = [_card(ctx, a, c) for c in a.card] if a.card else sorted(P.exportable(ctx.journal))
    print(P.export(ctx, cards, pick.path(a.out), name=a.name, zip_it=not a.folder))


def cmd_pack_install(ctx, a):
    from mdstudio.ops import pack as P
    for kind, mrk in P.install(ctx, _file(a, a.pack, "pack")):
        print(tr("installed %s %s") % (kind, mrk))


def cmd_pack_inspect(ctx, a):
    from mdstudio.ops import pack as P
    m = P.inspect(_file(a, a.pack, "pack"))
    print(tr("%s  (%s)") % (m['name'], m['tool']))
    _table([(c["kind"], c["mrk"], len(c["catalog"])) for c in m["cards"]],
           (tr("kind"), tr("card"), tr("manifest entries")))


def _sib_note(ci):
    note = []
    if not ci.cutin and ci.sibling_cutin:
        note.append(tr("cut-in on %s") % ",".join(map(str, ci.sibling_cutin)))
    if not ci.runfx and ci.sibling_runfx:
        note.append(tr("effect on %s") % ",".join(map(str, ci.sibling_runfx)))
    return "; ".join(note)


def cmd_cards_search(ctx, a):
    if not _db(ctx, a):
        return
    rows = [(ci.id, ci.name, _badge(ci.cutin), _badge(ci.runfx), _sib_note(ci))
            for ci in _inv(ctx, a).search(" ".join(a.query), has=a.has, limit=a.limit)]
    _table(rows, (tr("id"), tr("name"), tr("cut-in"), tr("effect"), tr("other printings")))
    print(tr("yes = has one (game or another mod)  yours = added by this tool  WIPED = run resync"))


def cmd_cards_show(ctx, a):
    inv = _inv(ctx, a)
    for text in a.ids:
        cid = _card(ctx, a, text)
        ci = inv.info(cid)
        print(f"{ci.id}  {ci.name or tr('(unknown name)')}")
        print(tr("  cut-in : %s") % _badge(ci.cutin))
        print(tr("  effect : %s") % _badge(ci.runfx))
        for t, se, ep in ci.effects:
            print(f"           {tr(t):<9} {se or '-':<28} {ep or tr('(built-in)')}")
        if inv.db:
            sib = [s for s in inv.db.same_name(cid) if s != cid]
            if sib:
                print(tr("  same name: %s") % ", ".join(
                    f"{s} ({tr('cut-in')} {_badge(inv.cutin.get(s))}, "
                    f"{tr('effect')} {_badge(inv.runfx.get(s))})" for s in sib))
        mine = ctx.journal.for_card(cid)
        if mine:
            print(tr("  this tool: %s   (mdstudio rm %s undoes all)")
                  % (", ".join(r.op for r in mine), cid))


def cmd_cards_list(ctx, a):
    inv = _inv(ctx, a)
    rows = inv.cards_with(a.what, a.source)
    _table([(ci.id, ci.name, _badge(ci.cutin if a.what == "cutin" else ci.runfx)) for ci in rows],
           (tr("id"), tr("name"), tr("status")))
    print(tr("%s card(s)") % len(rows))


def cmd_mine(ctx, a):
    """本工具加过的所有卡, 一眼看完"""
    from mdstudio.ops import pack as P
    db = _db(ctx, a)
    inv = _inv(ctx, a)
    rows = [(c, db.name(c) if db else "", _badge(inv.cutin.get(c)), _badge(inv.runfx.get(c)),
             " + ".join(sorted(k)))
            for c, k in sorted(P.exportable(ctx.journal).items())]
    _table(rows, (tr("id"), tr("name"), tr("cut-in"), tr("effect"), tr("what")))
    if not rows:
        print(tr("nothing yet. Try:  mdstudio add <card>"))


def cmd_rm(ctx, a):
    from mdstudio.ops.tx import undo_ids
    card = _card(ctx, a, a.card)
    recs = ctx.journal.for_card(card)
    if not recs:
        raise MDStudioError(tr("this tool hasn't changed %s") % card)
    undo_ids(ctx, [r.id for r in recs])
    print(tr("removed %s change(s) on %s: %s") % (len(recs), card, ", ".join(r.op for r in recs)))


def cmd_undo(ctx, a):
    from mdstudio.ops.tx import undo_ids
    recs = ctx.journal.load()
    if not recs:
        raise MDStudioError(tr("nothing to undo"))
    ids = a.ids or [max(recs, key=lambda r: r.time).id]
    done = undo_ids(ctx, ids)
    for r in done:
        print(tr("undone: %s %s (%s)") % (r.op, r.card or "", r.id))


def cmd_resync(ctx, a):
    from mdstudio.ops.tx import resync
    r = resync(ctx)
    print(tr("cut-ins re-added: %s") % (r['cutin'] or '-'))
    print(tr("effects re-added: %s") % (r['runfx'] or '-'))
    print(tr("manifest entries fixed: %s") % r['catalog'])
    if r.get("moved"):
        print(tr("files moved from an old group: %s") % len(r['moved']))
    for f in r["lost_files"]:
        print(tr("LOST"), f, tr("(re-create that card)"))
    for c in r.get("broken", []):
        print(tr("BROKEN"), c, tr("(files missing, remove and add it again)"))
    for c in r.get("game_took_over", []):
        print(tr("GAME"), c, tr("the game now has its own version for this card, yours was left off"))
    for lb in r.get("sound_reset", []):
        print(tr("SOUND"), lb, tr("(activation effect) was updated by the game, your WAV is gone: "
                                  "run `sound restore` then inject it again"))


def cmd_history(ctx, a):
    db = _db(ctx, a) if a.names else None
    rows = [(time.strftime("%Y-%m-%d %H:%M", time.localtime(r.time)), r.op, r.card or "",
             db.name(r.card) if db and r.card else "",
             r.meta.get("donor") or r.meta.get("label") or r.meta.get("pack") or "", r.id)
            for r in ctx.journal.load()]
    _table(rows, (tr("time"), tr("op"), tr("card"), tr("name"), tr("from"), tr("id")))
    if rows:
        print(tr("undo one:  mdstudio undo <id>     undo the last:  mdstudio undo"))


def cmd_ui(ctx, a):
    from mdstudio.tui import run
    run(ctx=ctx, lang=a.lang)


# ---------------- parser ----------------
def _card_arg(x, name="card", required=True, help=None):
    x.add_argument(name, nargs=None if required else "?", help=tr(help or "name or ID"))


def build_parser():
    p = argparse.ArgumentParser(
        prog="mdstudio", description=tr("Cut-ins and activation effects for Master Duel. "
                                       "Run with no arguments for the full-screen menu."),
        epilog=tr("Cards: type a name (\"cupsy\", \"disaster leo\") or an ID. "
                  "Same name on several cards -> it asks."))
    p.add_argument("--game", help=tr("game folder (auto-detected from Steam if omitted)"))
    p.add_argument("-n", "--dry-run", action="store_true", help=tr("show what would change, write nothing"))
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--lang", help=tr("card name language, e.g. en-US (default: whatever the game is set to)"))
    p.add_argument("--version", action="version", version=f"mdstudio {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    # ---- 懒人快捷 ----
    sub.add_parser("ui", aliases=["menu"], help=tr("full-screen menu (same as no arguments)")).set_defaults(fn=cmd_ui)
    x = sub.add_parser("find", aliases=["f", "search"], help=tr("find a card: mdstudio find albaz"))
    x.add_argument("query", nargs="+")
    x.add_argument("--has", choices=("cutin", "runfx", "any", "none"))
    x.add_argument("--limit", type=int, default=30)
    x.set_defaults(fn=cmd_cards_search)
    x = sub.add_parser("show", help=tr("what a card has: mdstudio show cupsy"))
    x.add_argument("ids", nargs="+", metavar="card")
    x.set_defaults(fn=cmd_cards_show)
    x = sub.add_parser("add", help=tr("cut-in: mdstudio add <card> [donor, default Disaster Leo]"))
    _card_arg(x, required=False)
    _card_arg(x, "donor", required=False, help=tr("card whose cut-in to copy (default 11016 Disaster Leo)"))
    x.add_argument("--clone", action="store_true", help=tr("independent copy (needed for reskin)"))
    x.add_argument("--force", action="store_true", help=tr("replace a cut-in the card already has"))
    x.set_defaults(fn=cmd_cutin_add)
    x = sub.add_parser("fx", help=tr("activation effect: mdstudio fx <card> <donor> (best on one-effect cards)"),
                       description=tr("Plays on every activation of the card, so use it on cards with one effect, "
                                      "monster or spell/trap (like Konami's Monster Reborn or Divine Arsenal AA-ZEUS - "
                                      "Sky Thunder). Pick a donor whose timing is 'chain' (see `runfx list --donors`): "
                                      "negated / destroyed / moved only fire on that event."))
    _card_arg(x, required=False)
    _card_arg(x, "donor", required=False, help=tr("card whose effect + sound to copy"))
    x.add_argument("--replace", action="store_true", help=tr("replace the effect the card already has"))
    x.set_defaults(fn=cmd_runfx_add)
    x = sub.add_parser("rm", aliases=["remove"], help=tr("undo everything this tool did to a card"))
    _card_arg(x, required=False)
    x.set_defaults(fn=cmd_rm)
    x = sub.add_parser("undo", help=tr("undo the last change (or give ids from history)"))
    x.add_argument("ids", nargs="*")
    x.set_defaults(fn=cmd_undo)
    sub.add_parser("mine", aliases=["ls"], help=tr("every card this tool changed")).set_defaults(fn=cmd_mine)

    sub.add_parser("info", help=tr("game path, active group, journal")).set_defaults(fn=cmd_info)
    sub.add_parser("resync", help=tr("re-apply your mods after a game update")).set_defaults(fn=cmd_resync)
    x = sub.add_parser("history", aliases=["log"], help=tr("everything this tool did"))
    x.add_argument("--names", action="store_true", help=tr("show card names"))
    x.set_defaults(fn=cmd_history)

    # cards
    c = sub.add_parser("cards", help=tr("find cards, see who has a cut-in / effect")).add_subparsers(dest="sub", required=True)
    x = c.add_parser("search", help=tr("by name or id"))
    x.add_argument("query", nargs="+")
    x.add_argument("--has", choices=("cutin", "runfx", "any", "none"))
    x.add_argument("--limit", type=int, default=30)
    x.set_defaults(fn=cmd_cards_search)
    x = c.add_parser("show")
    x.add_argument("ids", nargs="+", metavar="card")
    x.set_defaults(fn=cmd_cards_show)
    x = c.add_parser("list", help=tr("every card with a cut-in or effect"))
    x.add_argument("what", choices=("cutin", "runfx"))
    x.add_argument("--source", choices=("game", "yours", "wiped"))
    x.set_defaults(fn=cmd_cards_list)

    # cutin
    c = sub.add_parser("cutin", help=tr("summon cut-ins")).add_subparsers(dest="sub", required=True)
    x = c.add_parser("list")
    x.add_argument("--all", action="store_true")
    x.set_defaults(fn=cmd_cutin_list)
    x = c.add_parser("add", help=tr("give CARD the cut-in of DONOR"))
    _card_arg(x, required=False)
    _card_arg(x, "donor", required=False, help=tr("default 11016 Disaster Leo"))
    x.add_argument("--clone", action="store_true", help=tr("independent copy (needed for reskin/video)"))
    x.add_argument("--force", action="store_true", help=tr("replace a game cut-in"))
    x.set_defaults(fn=cmd_cutin_add)
    x = c.add_parser("remove")
    _card_arg(x, required=False)
    x.set_defaults(fn=cmd_cutin_remove)
    x = c.add_parser("verify", help=tr("check every dependency is on disk"))
    _card_arg(x, required=False)
    x.set_defaults(fn=cmd_cutin_verify)
    x = c.add_parser("video", help=tr("video / gif / frame folder -> cut-in"))
    _add_cutin_video_args(x)
    x = c.add_parser("reskin-export")
    _card_arg(x)
    x.add_argument("out")
    x.set_defaults(fn=cmd_reskin_export)
    x = c.add_parser("reskin-import", help=tr("PNG files, or the folder you exported to"))
    _card_arg(x)
    x.add_argument("png", nargs="+")
    x.set_defaults(fn=cmd_reskin_import)

    # runfx
    r = sub.add_parser("runfx", help=tr("activation effects")).add_subparsers(dest="sub", required=True)
    x = r.add_parser("list")
    x.add_argument("--donors", action="store_true", help=tr("every card that has an effect"))
    x.set_defaults(fn=cmd_runfx_list)
    x = r.add_parser("add", help=tr("give CARD the effect of DONOR"))
    _card_arg(x, required=False)
    _card_arg(x, "donor", required=False)
    x.add_argument("--replace", action="store_true", help=tr("replace a game effect"))
    x.set_defaults(fn=cmd_runfx_add)
    x = r.add_parser("remove")
    _card_arg(x, required=False)
    x.set_defaults(fn=cmd_runfx_remove)
    x = r.add_parser("video", help=tr("video -> activation effect (fullscreen works)"))
    _add_runfx_video_args(x)
    x = r.add_parser("framing", help=tr("resize a video effect without rebuilding"))
    _card_arg(x)
    x.add_argument("max_size", type=float, help=tr("fraction of screen height, fullscreen 16:9 = 1.08"))
    x.set_defaults(fn=cmd_runfx_framing)

    # 顶层也给一份, 少打一个词
    _add_cutin_video_args(sub.add_parser("video", help=tr("cut-in from a video: mdstudio video <card> <file>")))
    _add_runfx_video_args(sub.add_parser("fxvideo", help=tr("activation effect from a video")))

    x = sub.add_parser("spine", help=tr("Spine animation -> summon cut-in: mdstudio spine <source> <card> | --all"))
    x.add_argument("source", nargs="?",
                   help=tr("a Spine 4.x export folder (.json + .atlas + .png), a zip of one, a Unity bundle with "
                           "Spine data, or a folder of those (named by passcode, or listed in cards.json)"))
    x.add_argument("cards", nargs="*", help=tr("name or ID; none = list what's there"))
    x.add_argument("--all", action="store_true", help=tr("import every card that doesn't have a cut-in yet"))
    x.add_argument("--list", action="store_true")
    x.add_argument("--check", action="store_true", help=tr("check the Spine cut-ins already installed"))
    x.add_argument("--force", action="store_true",
                   help=tr("also replace imports that fail --check (older ones from another tool, or this "
                           "tool's own); with a card named, rebuild this tool's import of it even if it passes"))
    x.add_argument("--anim", help=tr("animation to play (default: 'animation', else the first one)"))
    x.add_argument("--scale", type=float, default=1.0, help=tr("size multiplier (default 1.0 = auto-fit)"))
    x.set_defaults(fn=cmd_spine)

    # sound
    # 只是 run effect (发动特效) 的音效, 不是 cut-in
    s = sub.add_parser("sound", aliases=["fxsound"], help=tr("activation-effect (run effect) sounds")) \
        .add_subparsers(dest="sub", required=True)
    x = s.add_parser("info", help=tr("is this label on disk"))
    x.add_argument("label")
    x.set_defaults(fn=cmd_sound_info)
    x = s.add_parser("card", help=tr("which sounds a card's activation effect plays"))
    _card_arg(x)
    x.set_defaults(fn=cmd_sound_card)
    x = s.add_parser("inject", help=tr("replace an activation-effect sound with a WAV "
                                       "(every card using that label changes)"))
    x.add_argument("label")
    x.add_argument("wav", nargs="?")
    x.set_defaults(fn=cmd_sound_inject)
    x = s.add_parser("use", help=tr("make an effect you added play this sound label (no rebuild; '' = silent)"))
    _card_arg(x)
    x.add_argument("label")
    x.set_defaults(fn=cmd_sound_use)
    x = s.add_parser("restore", help=tr("put the game's activation-effect sound back"))
    x.add_argument("label")
    x.set_defaults(fn=cmd_sound_restore)

    # pack
    k = sub.add_parser("pack", help=tr("share / install")).add_subparsers(dest="sub", required=True)
    x = k.add_parser("export")
    x.add_argument("out", nargs="?", default=".")
    x.add_argument("--card", action="append", help=tr("name or ID, repeat for more (default: everything you made)"))
    x.add_argument("--name", default="my-pack")
    x.add_argument("--folder", action="store_true", help=tr("folder instead of zip"))
    x.set_defaults(fn=cmd_pack_export)
    x = k.add_parser("install")
    x.add_argument("pack", nargs="?")
    x.set_defaults(fn=cmd_pack_install)
    x = k.add_parser("inspect")
    x.add_argument("pack", nargs="?")
    x.set_defaults(fn=cmd_pack_inspect)
    return p


def _add_cutin_video_args(x):
    _card_arg(x, required=False)
    x.add_argument("source", nargs="?", help=tr("video, gif or frame folder (drag it in)"))
    x.add_argument("--frames", type=int, default=120)
    x.add_argument("--cell", type=int, default=1080, help=tr("px per frame (height for full)"))
    x.add_argument("--duration", type=float, default=4.8, help=tr("seconds (the cut-in Timeline is stretched to fit)"))
    x.add_argument("--size", type=int, help=tr("skeleton width: full default 4800 = fills a 16:9 screen; "
                                               "others 2750 (game ~2456)"))
    x.add_argument("--mode", choices=("auto", "full", "square", "matte"), default="auto",
                   help=tr("auto (default) = keep frames that already have transparency, remove a "
                           "detected backdrop, otherwise use the whole frame; full/square/matte force "
                           "one behaviour"))
    x.add_argument("--sharp", action="store_true", help=tr("Real-ESRGAN (needs MDSTUDIO_ESRGAN)"))
    x.add_argument("--pop", action="store_true", help=tr("Konami pop-in zoom (default on except full)"))
    x.add_argument("--no-pop", action="store_true", help=tr("no pop-in zoom"))
    x.add_argument("--pop-curve",
                   help=tr("custom pop keyframes: frame,scale,opacity in percent, "
                           "e.g. \"(0,10,10)(8,70,50)(16,110,100)\""))
    x.add_argument("--offset-x", type=float, default=0.0,
                   help=tr("shift the cut-in sideways, in frame pixels (positive = right)"))
    x.add_argument("--offset-y", type=float, default=0.0,
                   help=tr("shift the cut-in up/down, in frame pixels (positive = up)"))
    x.add_argument("--zoom", type=float, default=1.0,
                   help=tr("overall zoom multiplier (1 = as generated)"))
    x.add_argument("--rotate", type=float, default=0.0,
                   help=tr("rotate the cut-in, in degrees (positive = counter-clockwise)"))
    x.add_argument("--sharp-model", default="realesr-animevideov3",
                   choices=("realesr-animevideov3", "realesrgan-x4plus-anime"),
                   help=tr("Real-ESRGAN model: videov3 (fast, video) or x4plus-anime (slow, line art)"))
    x.add_argument("--donor", help=tr("cut-in to clone as the base (default 11016)"))
    x.add_argument("--preview", help=tr("folder to save sheet + dark preview"))
    x.set_defaults(fn=cmd_cutin_video)


def _add_runfx_video_args(x):
    _card_arg(x, required=False)
    x.add_argument("source", nargs="?", help=tr("video (drag it in)"))
    x.add_argument("--frames", type=int, default=120)
    x.add_argument("--cell", type=int, default=1080, help=tr("frame height in px"))
    x.add_argument("--duration", type=float, default=5.0)
    x.add_argument("--mode", choices=("full", "matte", "square"), default="full")
    x.add_argument("--max-size", type=float, help=tr("fraction of screen height (full: 1.08)"))
    x.add_argument("--sound", help=tr("sound label to play / to carry the video's audio (must be on disk)"))
    x.add_argument("--no-audio", action="store_true",
                   help=tr("don't use the video's own audio (default: use it, carried on --sound or "
                           "SE_EV_EF09279_v1)"))
    x.add_argument("--sharp", action="store_true")
    x.add_argument("--preview")
    x.set_defaults(fn=cmd_runfx_video)


def _hoist_global_flags(argv):
    """-n / -v 写在命令后面也认 (mdstudio add cupsy -n). argparse 只认命令前面的"""
    head, rest = [], []
    for i, x in enumerate(argv):
        if x == "--":
            rest += argv[i:]
            break
        (head if x in ("-n", "--dry-run", "-v", "--verbose") else rest).append(x)
    return head + rest


def main(argv=None) -> int:
    argv = _hoist_global_flags(sys.argv[1:] if argv is None else list(argv))
    parser = build_parser()
    a = parser.parse_args(argv)
    if not getattr(a, "fn", None):
        # 不带命令: 终端里就开菜单, 被管道调用就打帮助
        if _tty():
            a.fn = cmd_ui
        else:
            parser.print_help()
            return 0
    if a.fn is not cmd_ui:
        _setup_logging(a.verbose)
    try:
        try:
            ctx = Ctx.open(a.game, dry_run=a.dry_run)
        except MDStudioError:
            if a.fn is cmd_ui:
                # 菜单自己会显示"找不到游戏 + 怎么指定", 比一行报错友好
                from mdstudio.tui import run
                run(game_dir=a.game, lang=a.lang)
                return 0
            raise
        a.fn(ctx, a)
    except MDStudioError as e:
        print(tr("error: %s") % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        # 不认识的错误: 一行说明, -v 才给完整 traceback
        if a.verbose:
            raise
        print(tr("error: %s: %s   (run again with -v for details)") % (type(e).__name__, e), file=sys.stderr)
        return 1
