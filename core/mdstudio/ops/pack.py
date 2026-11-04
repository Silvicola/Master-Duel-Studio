"""打包分享 / 安装别人的包

包结构:
  pack.json          元数据 + 每张卡的 manifest 条目 / gate 条目
  files/<2hex>/<8hex> bundle

坑: 借用模式在自己机器上能跑是因为 donor 的文件本来就在盘上. 别人可能从没下载过那个 donor,
所以必须把 manifest 依赖的整个闭包都带上, 不然对方 FILE LOAD ERROR (Despia v1 就是这么炸的).

装别人的包 = 装陌生人的数据. pack.json 不能信:
  - manifest 条目只许是这张卡自己的 cut-in 路径 / 我们的视频效果 (Ef9xxxx), 不许改游戏的条目
  - gate entry 重新按模板对齐 + 去掉"卡移动效果"那几个会卡死对局的字段
  - 效果文件装完必须在盘上, 不然 gate 指向空文件 = 进游戏卡死
"""
import json
import logging
import re
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

from mdstudio import __version__
from mdstudio.core.crc import get_crc, resolve_illust
from mdstudio.core.journal import Record
from mdstudio.errors import MDStudioError
from mdstudio.ops import cutin as C
from mdstudio.ops import overframe as OF
from mdstudio.ops import runfx as R
from mdstudio.ops.tx import Tx
from mdstudio.i18n import tr

log = logging.getLogger("mdstudio")
FORMAT = 2   # 2: 条目类型含 art(卡图); 旧版(1)的包会被拒绝, 不做静默兼容


# 卡图（超框 / RD 风格）走的是同一条记录: 换掉这张卡的插图 bundle 再登记 gate
ART_OPS = ("overframe.set", "pack.art")


def _kind(op):
    if op in C.OPS:
        return "cutin"
    if op in R.OPS:
        return "runfx"
    if op in ART_OPS:
        return "art"
    return None


def _illust(card) -> str:
    return OF.illust_logical(card)


def exportable(journal) -> dict:
    """{card: {"cutin", "runfx", "art"}}"""
    out = {}
    for r in journal.load():
        k = _kind(r.op)
        if k:
            out.setdefault(r.card, set()).add(k)
    return out


def _safe_name(name) -> str:
    # 坑: 以前 name 直接拼路径然后 rmtree, 名字是 "." / ".." / 绝对路径 = 删掉用户自己的文件夹
    n = re.sub(r"[^\w\- ]+", "_", str(name or "")).strip(" ._")
    return n[:60] or "my-pack"


def _is_our_effect(lp: str) -> bool:
    from mdstudio.ops.video.runfx_flip import FIRST_ID
    m = re.fullmatch(r"Duel/Timeline/Card/tcg/Ef(\d+)/Ef\1", lp)
    return bool(m) and int(m.group(1)) >= FIRST_ID


def export(ctx, cards, out_path, name="my-pack", zip_it=True) -> Path:
    from mdstudio.core.catalog import Catalog
    cat = Catalog(ctx.game)
    recs = [r for r in ctx.journal.load() if r.card in set(cards) and _kind(r.op)]
    if not recs:
        raise MDStudioError(tr("nothing to export for those cards"))

    files = set()
    by_card = {}
    for r in recs:
        kind = _kind(r.op)
        c = by_card.setdefault((r.card, kind), {"mrk": r.card, "kind": kind, "catalog": {}, "runfx": []})
        if kind == "art":
            # 卡图就是把这张卡的插图 bundle 换掉, manifest 里那条是游戏自己的, 不用带
            files.add(_illust(r.card))
            continue
        names = set(r.catalog)
        # 借来的视频效果 (别的卡做的 Ef9xxxx) 记录里没有 catalog, 也要带上, 不然对方没有这个文件
        names |= {lp for lp in (resolve_illust(e["effectPath"]) for e in r.runfx_added if e.get("effectPath"))
                  if _is_our_effect(lp)}
        for n in names:
            deps = cat.deps(n)
            if deps is None:
                continue
            c["catalog"][n] = deps
            files.add(n)
            files |= set(deps)
        c["runfx"] += r.runfx_added
    entries = list(by_card.values())

    name = _safe_name(name)
    out_dir = Path(out_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / (f"{name}.zip" if zip_it else name)
    if final.exists():
        raise MDStudioError(tr("%s already exists. Pick another name or move it away.") % final)
    # 先在临时目录里拼好, 成功了再挪到目标. 失败只删自己建的临时目录
    stage = Path(tempfile.mkdtemp(prefix=".mdstudio-pack-", dir=out_dir))
    try:
        missing = []
        for lp in sorted(files):
            h = get_crc(lp)
            src = ctx.game.find(h)
            if not src:
                missing.append(lp)
                continue
            dst = stage / "files" / h[:2] / h
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        if missing:
            raise MDStudioError(tr("%s dependency file(s) missing on disk, e.g. %s. "
                                  "Run resync, or open those cards once in game.")
                               % (len(missing), missing[0]))
        meta = {"format": FORMAT, "tool": f"mdstudio {__version__}", "name": name, "cards": entries}
        (stage / "pack.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
        if zip_it:
            with zipfile.ZipFile(final, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in stage.rglob("*"):
                    if p.is_file():
                        zf.write(p, p.relative_to(stage).as_posix())
        else:
            stage.rename(final)
    except BaseException:
        if zip_it and final.exists():
            final.unlink()
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    log.info(tr("exported %d card(s), %d bundle(s) -> %s"), len({e['mrk'] for e in entries}), len(files), final)
    # 导出不改游戏, 没有可撤销的东西; 但要留一条记录, 否则历史里看不出"我导出过什么".
    # meta["noop"] 标记它: 界面据此禁止撤销, 避免出现一条点了也没反应的记录.
    if not ctx.dry_run:
        rec = Record("pack.export", 0)
        rec.meta = {
            "pack": name,
            "cards": len({e["mrk"] for e in entries}),
            "path": str(final),
            "noop": True,
        }
        ctx.journal.commit(rec)
    return final


@contextmanager
def _open(pack_path):
    p = Path(pack_path)
    try:
        if p.is_dir():
            meta = json.loads((p / "pack.json").read_text(encoding="utf-8"))
            yield meta, lambda rel: (p / rel).read_bytes()
            return
        with zipfile.ZipFile(p) as zf:      # with: 用完关掉, Windows 上不然文件一直被占着
            yield json.loads(zf.read("pack.json")), zf.read
    except (KeyError, FileNotFoundError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise MDStudioError(tr("not a pack made by this tool (%s: %s)") % (type(e).__name__, e)) from None


def inspect(pack_path) -> dict:
    with _open(pack_path) as (meta, _):
        _validate(meta)
        return meta


def _validate(meta):
    if not isinstance(meta, dict) or meta.get("format") != FORMAT or not isinstance(meta.get("cards"), list):
        raise MDStudioError(tr("unsupported pack format %s")
                           % (meta.get('format') if isinstance(meta, dict) else '?'))


def _allowed(kind, mrk, name) -> bool:
    if kind == "cutin":
        return name.startswith(C.base(mrk) + "/")
    if kind == "art":
        # 卡图只能换这张卡自己的插图槽位
        return name == _illust(mrk)
    return _is_our_effect(name)


def _safe_runfx(tmpl_gate, mrk, e) -> dict:
    """包里的 gate entry: 对齐当前字段, mrk 只能是这张卡, 卡移动效果那套全清掉"""
    from mdstudio.core.gate import _conform
    e = _conform(dict(e), tmpl_gate.entries[0] if tmpl_gate.entries else None)
    e["mrk"] = int(mrk)
    for k, v in (("reserveCardMoveEffect", 0), ("cardMoveEffectLabel", ""),
                 ("cardMoveEffectTargetCardID", []), ("cardMoveEffectMoveType", [])):
        if k in e:
            e[k] = v
    if _is_our_effect(resolve_illust(e.get("effectPath") or "")):
        # 我们的视频效果: 必须有硬超时, 不锁输入 (见 runfx.clean_entry)
        if "finishSecond" in e and not (isinstance(e["finishSecond"], (int, float)) and e["finishSecond"] > 0):
            e["finishSecond"] = 6.0
        for k, v in (("waitFinish", 1), ("blockInput", 0)):
            if k in e:
                e[k] = v
    return e


def install(ctx, pack_path, skip_existing=True, replace=False) -> list:
    with _open(pack_path) as (meta, read):
        _validate(meta)
        done = _install(ctx, meta, read, skip_existing, replace)
        # 装上的卡各自有记录(可撤销), 这里再留一条带数量的摘要:
        # 一张没装上时它就是唯一线索; 装上了也能一眼看到"这次装了几张".
        if not ctx.dry_run:
            rec = Record("pack.install", 0)
            rec.meta = {
                "pack": meta.get("name"),
                "cards": len(done),
                "total": len({c.get("mrk") for c in (meta.get("cards") or [])}),
                "noop": True,
            }
            ctx.journal.commit(rec)
        return done


def _install(ctx, meta, read, skip_existing, replace):
    done = []
    for c in meta["cards"]:
        try:
            mrk, kind = int(c["mrk"]), c["kind"]
            catalog, runfx = dict(c.get("catalog") or {}), list(c.get("runfx") or [])
        except (KeyError, TypeError, ValueError):
            log.warning(tr("bad card entry in pack, skipped"))
            continue
        if kind not in ("cutin", "runfx", "art"):
            log.warning(tr("%s: unknown kind %r, skipped"), mrk, kind)
            continue
        if kind == "art":
            if _install_art(ctx, mrk, read, skip_existing, replace, meta):
                done.append((kind, mrk))
            continue
        bad = [n for n in catalog if not _allowed(kind, mrk, n)]
        if bad:
            log.warning(tr("%s %s: pack wants to change game files (%s), skipped"), kind, mrk, bad[0])
            continue
        mine = ctx.journal.for_card(mrk, C.OPS if kind == "cutin" else R.OPS)
        if mine and skip_existing:
            log.warning(tr("%s %s already installed by this tool, skipped"), kind, mrk)
            continue
        tx = Tx(ctx, f"pack.{kind}", mrk)
        if kind == "cutin" and mrk in tx.cutin_gate.mrks():
            # 游戏自己已经有 cut-in 的卡不碰, 不然会把官方的 prefab 条目盖掉
            log.warning(tr("cutin %s: card already has a game cut-in, skipped"), mrk)
            continue
        if kind == "runfx" and tx.runfx_gate.for_card(mrk) and not replace:
            log.warning(tr("effect %s: card already has a game effect, skipped "
                           "(install with replace to swap it)"), mrk)
            continue
        clash = None
        for n in catalog:
            # 我们的视频效果号撞了: 本机同名文件内容不同 = 别人的 Ef 号跟你的重了, 装上会互相覆盖
            h = get_crc(n)
            have = ctx.game.find(h)
            if kind == "runfx" and have and have.read_bytes() != read(f"files/{h[:2]}/{h}"):
                clash = n
        if clash:
            log.warning(tr("effect %s: %s already exists here with different content, skipped"), mrk, clash)
            continue
        need = set(catalog) | {d for deps in catalog.values() for d in deps}
        try:
            for lp in sorted(need):
                h = get_crc(lp)
                # 盘上已有的不覆盖 (多半是游戏自己的文件, 以游戏为准)
                if not ctx.game.find(h):
                    tx.put_file(h, read(f"files/{h[:2]}/{h}"))
        except KeyError as e:
            log.warning(tr("%s %s: file missing inside the pack (%s), skipped"), kind, mrk, e)
            continue
        for n, deps in catalog.items():
            tx.set_catalog(n, deps)
        if kind == "cutin":
            tx.add_cutin(mrk)
        elif runfx:
            entries = [_safe_runfx(tx.runfx_gate, mrk, e) for e in runfx]
            lost = [e["effectPath"] for e in entries if e.get("effectPath") and not R.effect_file(tx.game, e["effectPath"])
                    and resolve_illust(e["effectPath"]) not in catalog]
            if lost:
                log.warning(tr("effect %s: needs %s, not on this machine (open that card once in game), "
                               "skipped"), mrk, lost[0])
                continue
            if tx.runfx_gate.for_card(mrk):
                tx.drop_runfx_card(mrk)
            tx.add_runfx(entries)
        tx.rec.meta = {"pack": meta.get("name")}
        tx.commit()
        done.append((kind, mrk))
    return done


def _install_art(ctx, mrk, read, skip_existing, replace, meta) -> bool:
    """装一张卡的卡图：整张插图 bundle 直接落盘，再按本机账号登记超框。

    包里只有插图本体。登记表每个账号一份、文件名还会随游戏更新变，
    所以 gate 必须在本机现算，不能照抄别人的。
    """
    if ctx.journal.for_card(mrk, ART_OPS) and skip_existing:
        log.warning(tr("%s %s already installed by this tool, skipped"), "art", mrk)
        return False
    # 游戏自己就是超框的卡不碰，装上去等于盖掉官方那张
    try:
        if mrk in OF.official_registered(ctx) and not replace:
            log.warning(tr("art %s: card already has a game over-frame, skipped "
                           "(install with replace to swap it)"), mrk)
            return False
    except MDStudioError:
        pass
    h = get_crc(_illust(mrk))
    try:
        data = read(f"files/{h[:2]}/{h}")
    except KeyError:
        log.warning(tr("art %s: artwork bundle missing inside the pack, skipped"), mrk)
        return False
    tx = Tx(ctx, "pack.art", mrk)
    OF.write_bundle(ctx, tx, mrk, data)
    tx.rec.meta = {"pack": meta.get("name")}
    tx.commit()
    return True
