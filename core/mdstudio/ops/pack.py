"""打包分享 / 安装别人的包

包结构:
  pack.json          元数据 + 每张卡的 manifest 条目 / gate 条目 / 分层参数
  files/<2hex>/<8hex> bundle
  layers/<卡号>/art.png, subject.png   可选：超框的分层素材

坑: 借用模式在自己机器上能跑是因为 donor 的文件本来就在盘上. 别人可能从没下载过那个 donor,
所以必须把 manifest 依赖的整个闭包都带上, 不然对方 FILE LOAD ERROR (Despia v1 就是这么炸的).

装别人的包 = 装陌生人的数据. pack.json 不能信:
  - manifest 条目只许是这张卡自己的 cut-in 路径 / 我们的视频效果 (Ef9xxxx), 不许改游戏的条目
  - gate entry 重新按模板对齐 + 去掉"卡移动效果"那几个会卡死对局的字段
  - 效果文件装完必须在盘上, 不然 gate 指向空文件 = 进游戏卡死
  - 分层素材只认 layers/<数字>/(art|subject).<图片后缀>, 不许带路径
"""
import io
import json
import logging
import re
import shutil
import tempfile
import uuid
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
# 1: 第一版正式格式（2026-11-12）。带 art 类型与超框分层（layers）。
# 用户明确要求过不做向下兼容：更早的测试格式一律拒绝，报错比静默少装东西好排查。
def _pack_major() -> int:
    """整合包的格式版本 = 软件的主版本号。

    次版本只加功能、不动包结构，所以同一主版本下的包彼此都能装；主版本一变就是
    结构不兼容。**手工纪律：只要改到包结构（字段改名、目录布局、同名字段语义），
    必须升主版本号**，否则会出现「装得上但读不对」。
    自动取值是为了免掉「忘了改 FORMAT」这种失误，历史上这个常量一直手写。
    """
    from mdstudio import __version__
    m = re.search(r"(\d+)", __version__)
    return int(m.group(1)) if m else 1


FORMAT = _pack_major()
# 分层素材只允许这个形状，防止包里的路径穿越到别处
_LAYER_RE = re.compile(r"layers/\d+/(art|subject)\.(png|jpe?g|webp)", re.I)
_LAYER_MAX = 24 << 20      # 单张分层素材上限，再大就不是卡图素材了


# 卡图（超框 / RD 风格）走的是同一条记录: 换掉这张卡的插图 bundle 再登记 gate。
# 口径：journal 里写的 op 是 "overframe.set"（见 overframe.py 的 Tx(ctx, ...)）；
# "overframe.apply" 是 RPC 动作名（actions.py 的 @register），两者别混。
# 2026-11-12 曾误把这里改成 "overframe.apply"，导致覆盖提示漏数用户自己的超框。
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


def _count_kinds(entries) -> dict:
    """按类型数卡: {"cutin": 2, "art": 1}。同一张卡的同类条目只算一次。"""
    seen = {}
    for e in entries:
        seen.setdefault(e.get("kind"), set()).add(e.get("mrk"))
    return {k: len(v) for k, v in seen.items() if k}


def exportable(journal) -> dict:
    """{card: {"cutin", "runfx", "art"}}"""
    out = {}
    for r in journal.load():
        k = _kind(r.op)
        if k:
            out.setdefault(r.card, set()).add(k)
    return out


_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(name) -> str:
    """只拦 Windows 文件名非法字符和路径穿越。

    以前是 `[^\\w\\- ]` 全替换，把用户写的「导出(超框+2、动画+1)」里的括号、
    加号、顿号全变成下划线。包名是给人看的，这些标点留着更好读。
    必须拦的还是那几个：路径分隔符和 `..`（历史上因为名字拼进路径后 rmtree
    删过用户的文件夹）。
    """
    n = _BAD_NAME.sub("_", str(name or ""))
    n = n.replace("..", "_").strip(" .")
    return n[:80] or "my-pack"


def _is_our_effect(lp: str) -> bool:
    from mdstudio.ops.video.runfx_flip import FIRST_ID
    m = re.fullmatch(r"Duel/Timeline/Card/tcg/Ef(\d+)/Ef\1", lp)
    return bool(m) and int(m.group(1)) >= FIRST_ID


def _group_cards(entries):
    """导出用：把扁平的 (mrk, kind) 条目表按卡号分组，一张卡下面挂三小项。"""
    out = {}
    for e in entries:
        m = int(e["mrk"])
        c = out.setdefault(m, {"mrk": m})
        if e["kind"] == "art":
            c["art"] = {k: v for k, v in e.items() if k == "layers"}
        else:
            c[e["kind"]] = {k: v for k, v in e.items()
                            if k in ("catalog", "runfx")}
    return [out[m] for m in sorted(out)]


def _ungroup_cards(meta):
    """消费用：把按卡分组的结构还原成扁平条目表，安装和预览共用。"""
    out = []
    for c in meta.get("cards", []):
        if not isinstance(c, dict) or "mrk" not in c:
            continue
        m = c["mrk"]
        for kind in ("art", "cutin", "runfx"):
            sub = c.get(kind)
            if isinstance(sub, dict):
                out.append({"mrk": m, "kind": kind, **sub})
    return out


def export(ctx, cards, out_path, name="my-pack", zip_it=True, kinds=None) -> Path:
    """cards 是卡号列表；kinds 是 {卡号: {"cutin","runfx","art"}}。

    kinds 为 None 时导出这些卡的全部类型（命令行和老调用方走这条路）。
    界面按每张卡的三格勾选传入 kinds，一张卡可以只带动画不带特效。
    """
    from mdstudio.core.catalog import Catalog
    cat = Catalog(ctx.game)
    recs = [r for r in ctx.journal.load() if r.card in set(cards) and _kind(r.op)]
    if kinds is not None:
        # 从 JSON 过来时键是字符串，记录里的 r.card 是 int，不转一下就全对不上
        sel = {int(k): set(v or ()) for k, v in kinds.items()}
        recs = [r for r in recs if _kind(r.op) in sel.get(r.card, set())]
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
            # 分层：本机"接着调"用的背景与主体，一起打进包。装到别人机器上时
            # 成品图照旧写进游戏，这两张写进编辑状态，卡图页打开就能继续调，
            # 预览也不会再是"平图被裁进插图窗"的样子。
            lay = OF.load_edit(ctx, r.card) or {}
            if lay.get("art") or lay.get("subject"):
                c["layers"] = {
                    "art": lay.get("art") or "",
                    "subject": lay.get("subject") or "",
                    "art_name": lay.get("art_name") or "",
                    "subject_name": lay.get("subject_name") or "",
                    "scale": float(lay.get("scale", 1.0)),
                    "dx": int(lay.get("dx", 0)),
                    "dy": int(lay.get("dy", 0)),
                    "lore_alpha": float(lay.get("lore_alpha", 1.0)),
                    "effect": lay.get("effect") or "",
                }
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
        # 分层素材单独收进 layers/<卡号>/。文件名固定成 art.<后缀> / subject.<后缀>，
        # 免得两张素材同名时互相覆盖；给人看的原名放在 art_name / subject_name 里。
        for e in entries:
            lay = e.get("layers")
            if not lay:
                continue
            for key in ("art", "subject"):
                src = lay.get(key) or ""
                if not src:
                    lay.pop(key, None)
                    continue
                p = Path(src)
                if not p.is_file():
                    # 素材被清掉了（用户删过 data）。成品图仍然能装，这一层就不要了
                    log.warning(tr("layer file gone, exporting without it: %s"), src)
                    lay.pop(key, None)
                    continue
                rel = f"layers/{int(e['mrk'])}/{key}{p.suffix.lower() or '.png'}"
                dst = stage / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
                lay[key] = rel
            if not (lay.get("art") or lay.get("subject")):
                e.pop("layers", None)
        meta = {"format": FORMAT, "tool": f"mdstudio {__version__}", "name": name,
                "cards": _group_cards(entries)}
        (stage / "pack.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
        if zip_it:
            # 压缩级别拉到 9。包里全是已经压过的 bundle，再压空间有限，但分享出去
            # 的体积就是用户的流量，能省一点是一点；代价是导出时多等几秒，
            # 界面那边会有进度条，不是干转圈。
            with zipfile.ZipFile(final, "w", zipfile.ZIP_DEFLATED,
                                 compresslevel=9) as zf:
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
            # 按类型分开记: 一张卡可能同时带动画和卡图, 历史里要看得出各几份
            "kinds": _count_kinds(entries),
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
    if (not isinstance(meta, dict) or meta.get("format") != FORMAT
            or not isinstance(meta.get("cards"), list)):
        # 只说主版本：用户不用理解内部格式号，只需要知道「版本对不上」
        raise MDStudioError(tr("this pack is from tool version %s, yours is %s. The major version has "
                              "to match; update the tool, or ask for a pack made by yours.")
                           % (meta.get('format') if isinstance(meta, dict) else '?', FORMAT))
    for c in meta["cards"]:
        if not isinstance(c, dict) or "mrk" not in c:
            raise MDStudioError(tr("bad card entry in pack"))
        # 新结构：一张卡下面是 art / cutin / runfx 三个可选小项。
        # 用 _ungroup_cards 展开后统一校验，避免两处路径规则各写一遍。
        for e in _ungroup_cards({"cards": [c]}):
            lay = e.get("layers")
            if lay is None:
                continue
            if not isinstance(lay, dict):
                raise MDStudioError(tr("bad layer block in pack"))
            for key in ("art", "subject"):
                rel = lay.get(key)
                if rel in (None, ""):
                    continue
                # 只认固定形状的路径，且必须在 layers 下：包里写 ../.. 也走不到别处
                if not isinstance(rel, str) or not _LAYER_RE.fullmatch(rel):
                    raise MDStudioError(tr("bad layer path in pack: %s") % rel)


def _allowed(kind, mrk, name) -> bool:
    if kind == "cutin":
        return name.startswith(C.base(mrk) + "/")
    if kind == "art":
        # 卡图只能换这张卡自己的插图槽位
        return name == _illust(mrk)
    return _is_our_effect(name)


def _write_plan(catalog, need, exists) -> list:
    """安装时哪些逻辑路径要写盘，按角色区分（2026-11-17 实测定的规则）。

    清单 key 是这张卡自己的资源（_allowed 已保证路径在本卡下），包内版本
    就是权威版本，无条件覆盖：#14952 官方自带召唤动画，复原后 7 个改写过的
    文件回到官方原版留在盘上，以前「盘上已有不覆盖」会把它们跳过，装出来
    官方时间轴配新贴图，主体消失。
    纯依赖可能被游戏更新过（日志有「被游戏更新换掉了」的先例），仍以游戏
    为准，盘上已有就不动。
    """
    return [lp for lp in sorted(need) if lp in catalog or not exists(lp)]


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


def install(ctx, pack_path, selected=None) -> list:
    """selected: [{"card": 22768, "kind": "cutin"}]，只装清单里的条目。

    为 None 时装全包（命令行和老调用方）。界面走预览勾选，判断在预览层做完，
    这里只管按清单执行，另外保留卡是否存在、内容合法这些兜底检查。
    统一覆盖语义（2026-11-11）：跳过已废除，选了就装、装就覆盖；skip_existing /
    replace 两个旧参数退役。
    """
    with _open(pack_path) as (meta, read):
        _validate(meta)
        # 一次安装的摘要与全部明细共用一个批次号：历史页据此把 1 条摘要 +
        # N 条明细归成"一次操作"，而不是画成 N+1 件独立的事。
        batch = uuid.uuid4().hex[:12]
        done, overwritten = _install(ctx, meta, read, batch, selected=selected)
        # 装上的卡各自有记录(可撤销), 这里再留一条带数量的摘要:
        # 一张没装上时它就是唯一线索; 装上了也能一眼看到"这次装了几张".
        # installed 存明细, 分享页据此做「恢复原版」。
        if not ctx.dry_run:
            rec = Record("pack.install", 0)
            rec.meta = {
                "pack": meta.get("name"),
                "batch": batch,
                # 按卡去重: 一张卡可能同时装了动画和卡图, 条目数会虚高一倍
                "cards": len({m for _, m in done}),
                "total": len({c.get("mrk") for c in (meta.get("cards") or [])}),
                "kinds": _count_kinds([{"kind": k, "mrk": m} for k, m in done]),
                "installed": [{"card": m, "kind": k} for k, m in done],
                # 覆盖了"安装前本工具已做过的改动"的条目数。那些改动已被备份
                # 保管，恢复原版时一并撤掉，提示让用户知道覆盖发生过
                "overwritten": overwritten,
                "noop": not done and not overwritten,
            }
            ctx.journal.commit(rec)
        # 改返回值结构时的教训：函数有多个 return 出口，全部都要同步改
        # （2026-11-12 这里漏改，装 3 项时外层解包报 "too many values"）
        return done, overwritten


def _install(ctx, meta, read, batch, selected=None):
    """统一覆盖语义：跳过逻辑已废除（2026-11-11 用户决定），选了就装、装就覆盖。
    skip_existing / replace 两个旧参数随之退役。batch 用于历史页归组。"""
    want = None
    if selected is not None:
        want = set()
        for s in selected:
            try:
                want.add((int(s["card"]), str(s["kind"])))
            except (KeyError, TypeError, ValueError):
                continue

    done = []
    overwritten = 0   # 覆盖了"安装前本工具已做过的改动"的条目数，安装完提示用
    for c in _ungroup_cards(meta):
        try:
            mrk, kind = int(c["mrk"]), c["kind"]
            catalog, runfx = dict(c.get("catalog") or {}), list(c.get("runfx") or [])
        except (KeyError, TypeError, ValueError):
            log.warning(tr("bad card entry in pack, skipped"))
            continue
        if want is not None and (mrk, kind) not in want:
            continue
        if kind not in ("cutin", "runfx", "art"):
            log.warning(tr("%s: unknown kind %r, skipped"), mrk, kind)
            continue
        # 统一覆盖：不跳过任何"已装过同类"的条目。装 = 覆盖，之前的版本由
        # 记录备份保管，撤销链随时找回（2026-11-11 用户决定废除跳过语义）。
        if kind == "art":
            if ctx.journal.for_card(mrk, ART_OPS):
                overwritten += 1
            if _install_art(ctx, c, read, meta, batch):
                done.append((kind, mrk))
            continue
        bad = [n for n in catalog if not _allowed(kind, mrk, n)]
        if bad:
            log.warning(tr("%s %s: pack wants to change game files (%s), skipped"), kind, mrk, bad[0])
            continue
        if ctx.journal.for_card(mrk, C.OPS if kind == "cutin" else R.OPS):
            overwritten += 1
        tx = Tx(ctx, f"pack.{kind}", mrk)
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
            for lp in _write_plan(catalog, need, ctx.game.find_logical):
                h = get_crc(lp)
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
        tx.rec.meta = {"pack": meta.get("name"), "batch": batch}
        tx.commit()
        done.append((kind, mrk))
    return done, overwritten


def _install_art(ctx, entry, read, meta, batch) -> bool:
    """装一张卡的卡图：整张插图 bundle 直接落盘，再按本机账号登记超框。

    包里只有插图本体。登记表每个账号一份、文件名还会随游戏更新变，
    所以 gate 必须在本机现算，不能照抄别人的。
    统一覆盖：本工具已装过的直接覆盖（旧版本有记录备份，撤销链可找回）；
    官方自带的超框也覆盖，撤销时登记表会回填官方条目。

    写进游戏的是包里的成品图（作者当时看到的那张），不会在本机重新合成：
    重合成要依赖本机的卡框素材与版本，画出来未必和作者一致。
    分层只写进编辑状态，供卡图页继续调整与按层预览。
    """
    mrk = int(entry["mrk"])
    h = get_crc(_illust(mrk))
    try:
        data = read(f"files/{h[:2]}/{h}")
    except KeyError:
        log.warning(tr("art %s: artwork bundle missing inside the pack, skipped"), mrk)
        return False
    tx = Tx(ctx, "pack.art", mrk)
    OF.write_bundle(ctx, tx, mrk, data)
    tx.rec.meta = {"pack": meta.get("name"), "batch": batch}
    tx.commit()
    _restore_layers(ctx, mrk, entry.get("layers"), read)
    return True


def _restore_layers(ctx, mrk, lay, read) -> None:
    """把包里的分层素材写回本机的编辑状态。

    游戏里看到的仍是成品图；这两张只用于卡图页"接着调"与按层预览。
    装完打开这张卡，背景和主体会自动带出来，主体压在卡框上，不再是
    "平图被裁进插图窗、看起来被框挡住"的样子（2026-11-12 用户实测）。
    """
    if ctx.dry_run or not isinstance(lay, dict):
        return
    with tempfile.TemporaryDirectory(prefix="mdstudio-layer-") as td:
        try:
            art = _extract_layer(read, lay.get("art"), Path(td) / "art")
            subject = _extract_layer(read, lay.get("subject"), Path(td) / "subject")
        except MDStudioError as e:
            # 分层坏了不影响成品图，装还是装上了，只是不能接着调
            log.warning(tr("layers for %s skipped: %s"), mrk, e)
            return
        if not art and not subject:
            return
        try:
            la = lay.get("lore_alpha")
            OF.save_edit(ctx, mrk, art, lay.get("art_name") or "", subject,
                         lay.get("subject_name") or "",
                         float(lay.get("scale") or 1.0), int(lay.get("dx") or 0),
                         int(lay.get("dy") or 0),
                         1.0 if la is None else float(la),
                         lay.get("effect") or OF.EFFECT_PRISMATIC)
        except (TypeError, ValueError):
            log.warning(tr("bad layer params for %s, skipped"), mrk)


def _extract_layer(read, rel, dst: Path):
    """取出一张分层素材写到临时文件，校验过了才交出去。"""
    if not rel:
        return None
    try:
        data = read(rel)
    except KeyError:
        raise MDStudioError(tr("layer file missing inside the pack: %s") % rel) from None
    if not data or len(data) > _LAYER_MAX:
        raise MDStudioError(tr("layer file empty or too large: %s") % rel)
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
    except Exception:
        raise MDStudioError(tr("layer file is not a valid image: %s") % rel) from None
    out = dst.with_suffix(Path(rel).suffix.lower() or ".png")
    out.write_bytes(data)
    return str(out)


# --------------------------------------------------------------- 装过的包
def installed_packs(ctx, limit: int = 20) -> list:
    """本机装过的整合包，最近的在前。只做记录：时间、包名、装了几张、各类几份。

    复原不在这里：装包走的是普通 Tx，备份与软件自己生成一致，撤包就到动画页
    或卡图页按卡复原。所以这里不再算 alive / covered / reverted 那些按钮状态。
    """
    recs = ctx.journal.load()
    out = []
    for r in sorted((r for r in recs if r.op == "pack.install"),
                    key=lambda x: x.time, reverse=True):
        items = r.meta.get("installed") or []
        if not items:
            continue                      # 更早的版本没记明细，列出来只是一行空记录
        cards = set()
        for it in items:
            try:
                cards.add(int(it["card"]))
            except (KeyError, TypeError, ValueError):
                continue
        if not cards:
            continue
        out.append({
            "id": r.id,
            "name": r.meta.get("pack") or "",
            "time": r.time,
            "cards": r.meta.get("cards") or len(cards),
            "kinds": r.meta.get("kinds") or {},
        })
    return out[:limit]
