"""一次操作 = 一个 Tx: 改动先攒着, commit 时按顺序写盘 + 记 journal

写盘顺序: bundle -> manifest -> journal -> gate. gate 最后写, 中途崩了游戏也不会去触发半成品;
journal 在 gate 前面, 这样 gate 里出现的东西一定有记录, 工具能认出来也能撤.
撤销反过来: 先 gate (先让游戏别再触发), 再 manifest, 最后文件.

每次写的文件都记 sha (meta["sha"]). 以后撤销/resync 时文件对不上 = 游戏更新换掉了, 那是游戏的东西, 别碰.
"""
import hashlib
import logging

from mdstudio.core.bundle import retag
from mdstudio.core.catalog import Catalog
from mdstudio.core.crc import get_crc
from mdstudio.core.gate import CutinGate, RunFxGate, runfx_sig
from mdstudio.core.journal import Record
from mdstudio.errors import MDStudioError
from mdstudio.i18n import tr
from mdstudio.progress import step, tick

log = logging.getLogger("mdstudio")


class Tx:
    def __init__(self, ctx, op: str, card: int, record=True, reverse=False):
        self.ctx = ctx
        self.reverse = reverse      # undo 用: gate -> manifest -> 文件
        self.game = ctx.game
        self.rec = Record(op, int(card))
        self.record = record
        self._catalog = None
        self._cutin = None
        self._runfx = None
        self._writes = []   # (path, data)

    @property
    def catalog(self) -> Catalog:
        if self._catalog is None:
            self._catalog = Catalog(self.game)
        return self._catalog

    @property
    def cutin_gate(self) -> CutinGate:
        if self._cutin is None:
            self._cutin = CutinGate(self.game)
        return self._cutin

    @property
    def runfx_gate(self) -> RunFxGate:
        if self._runfx is None:
            self._runfx = RunFxGate(self.game)
        return self._runfx

    # ---- 文件 ----
    def put_file(self, h: str, data: bytes):
        """写 bundle. 原来没有 = created (卸载删掉); 原来有 = modified (卸载还原)"""
        path = self.game.path_for(h)
        rel = self.game.rel(path)
        if path.is_file() and rel not in self.rec.created:
            self.ctx.journal.backup(self.rec, path, rel, self.ctx.dry_run)
        elif rel not in self.rec.created:
            self.rec.created.append(rel)
        self._writes.append((path, data))

    def put_existing(self, path, data: bytes):
        rel = self.game.rel(path)
        if rel not in self.rec.created:
            self.ctx.journal.backup(self.rec, path, rel, self.ctx.dry_run)
        self._writes.append((path, data))

    # ---- manifest ----
    def set_catalog(self, name: str, deps):
        prev = self.catalog.set(name, deps)
        slot = self.rec.catalog.setdefault(name, {"prev": prev})
        slot["new"] = list(deps)

    # ---- gate ----
    def add_cutin(self, mrk: int):
        if self.cutin_gate.add(mrk):
            self.rec.cutin_mrks.append(int(mrk))

    def add_runfx(self, entries):
        self.runfx_gate.append(entries)
        self.rec.runfx_added += entries

    def drop_runfx_card(self, mrk: int):
        self.rec.runfx_removed += self.runfx_gate.remove_card(mrk)

    # ---- 提交 ----
    def commit(self) -> Record:
        self.ctx.guard()
        step(tr("%s game files for %s %s (%s bundle(s))"),
             tr("checking") if self.ctx.dry_run else tr("writing"),
             self.rec.op, self.rec.card or "", len(self._writes))
        if self.record and not self.ctx.dry_run:
            self._safety_copy()
        if self.record:
            sha = self.rec.meta.setdefault("sha", {})
            for path, data in self._writes:
                sha[self.game.rel(path)] = sha_of(data)
        if self.reverse:
            self._save_gates()
            self._save_catalog()
            self._save_files()
            return self.rec
        self._save_files()
        self._save_catalog()
        if self.record and not self.ctx.dry_run:
            self.ctx.journal.commit(self.rec)
        self._save_gates()
        log.info(tr("%s %s: %s new file(s), %s changed, %s manifest entries"),
                 self.rec.op, self.rec.card, len(self.rec.created), len(self.rec.modified),
                 len(self.rec.catalog))
        return self.rec

    def _save_files(self):
        for k, (path, data) in enumerate(self._writes):
            tick(tr("writing files"), k + 1, len(self._writes))
            self.ctx.write(path, data)

    def _save_catalog(self):
        if self._catalog is not None:
            self._catalog.save(self.ctx)

    def _save_gates(self):
        for g in (self._runfx, self._cutin):
            if g is not None:
                g.save(self.ctx)

    def _safety_copy(self):
        # 紧急用的整文件备份, 正常卸载不用它 (见 journal.py 顶部说明)
        for g in (self._catalog, self._cutin, self._runfx):
            if g is None:
                continue
            d = self.ctx.journal.backups / self.rec.id / "safety"
            d.mkdir(parents=True, exist_ok=True)
            (d / g.path.name).write_bytes(g.path.read_bytes())


def _rel(logical: str) -> str:
    h = get_crc(logical)
    return f"{h[:2]}/{h}"


def sha_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


def last_sha(records) -> dict:
    """rel -> 本工具最后一次写进去的内容的 sha (按时间, 后写的盖前写的)"""
    out = {}
    for r in sorted(records, key=lambda r: r.time):
        out.update(r.meta.get("sha") or {})
    return out


def replaced_by_game(ctx, rel, shas) -> bool:
    """文件还在但内容不是我们写的 = 游戏更新换掉了 (比如官方给这张卡出了 cut-in). 老记录没 sha 就当是我们的"""
    want = shas.get(rel)
    p = ctx.game.abs(rel)
    return bool(want) and p.is_file() and sha_of(p.read_bytes()) != want


def effect_logicals(rec: Record) -> set:
    from mdstudio.core.crc import resolve_illust
    return {resolve_illust(e["effectPath"]) for e in rec.runfx_added if e.get("effectPath")}


def needed_files(rec: Record, cat=None) -> set:
    """这条记录运行时要用到的所有文件: 自己建的 + manifest 里登记的依赖 + 发动效果指向的 bundle

    坑: runfx 借用记录本身没 created/catalog, 但它指向的可能是别的卡做的视频效果 (Ef9xxxx).
    不算进来的话, 删掉那张卡 = 借用的卡指向一个已删除的文件, 进游戏卡死.
    """
    out = set(rec.created)
    for name, v in rec.catalog.items():
        out |= {_rel(lp) for lp in [name, *(v.get("new") or [])]}
    for lp in effect_logicals(rec):
        out.add(_rel(lp))
        if cat is not None:
            out |= {_rel(d) for d in cat.deps(lp) or []}
    return out


def undo(ctx, records, keep_records=()):
    """撤销一组记录. keep_records = 其他还活着的记录, 它们需要的 mrk 不删"""
    ctx.guard()
    step(tr("undoing %s change(s)"), len(records))
    tx = Tx(ctx, "undo", 0, record=False, reverse=True)
    keep_mrks = {m for r in keep_records for m in r.cutin_mrks}
    shas = last_sha([*records, *keep_records])
    # 坑: video 记录改的文件是 clone 记录建的. 先还原再删 = 删完又被写回来.
    # 所以要删的先收齐, 还原跳过它们, 删除放最后
    doomed = {rel for r in records for rel in r.created}
    # 坑: 装包时第一张卡把共享依赖 (donor 那一套) 记成自己 created, 后面的卡看到已存在就不记.
    # 直接删 = 别的卡 FILE LOAD ERROR. 还有人要用的文件不删, 过户给要用它的那条记录
    heirs = {}
    for r in keep_records:
        for rel in needed_files(r, tx.catalog) & doomed:
            heirs.setdefault(rel, r)
    doomed -= set(heirs)
    # 游戏更新后变成官方的文件不删也不还原
    game_owned = {rel for r in records for rel in [*r.created, *r.modified] if replaced_by_game(ctx, rel, shas)}
    for rel in sorted(game_owned):
        log.warning(tr("%s was replaced by a game update, left as the game has it"), rel)
    doomed -= game_owned
    kept_names = {}
    for rec in sorted(records, key=lambda r: r.time, reverse=True):
        # 游戏接管了这张卡 (官方 prefab 盖了我们的文件): gate 里那条现在是官方的, 不能删
        taken = any(rel in game_owned for rel in rec.created)
        for m in rec.cutin_mrks:
            if m not in keep_mrks and not taken:
                tx.cutin_gate.remove(m)
        if rec.runfx_added or rec.runfx_removed:
            tx.runfx_gate.remove_matching(rec.runfx_added)
            # 只补回现在不在的 (更新后游戏可能已经自己加回来了, 再加 = 重复)
            live = {runfx_sig(e) for e in tx.runfx_gate.entries}
            tx.runfx_gate.append([e for e in rec.runfx_removed if runfx_sig(e) not in live])
        for name, v in rec.catalog.items():
            rel = _rel(name)
            if rel in heirs:            # 别的记录还要用, manifest 条目一起过户
                kept_names.setdefault(heirs[rel].id, {})[name] = v
                continue
            if rel in game_owned:
                continue
            if v.get("prev") is None:
                tx.catalog.remove(name)
            else:
                tx.catalog.set(name, v["prev"])
        for rel in rec.modified:
            if rel in doomed or rel in game_owned:
                continue
            src = ctx.journal.restore_source(rec, rel)
            if src.is_file():
                tx._writes.append((ctx.game.abs(rel), src.read_bytes()))
            else:
                log.warning(tr("backup missing for %s, left as is"), rel)
    tx.commit()
    for rel in sorted(doomed):
        ctx.delete(ctx.game.abs(rel))
    if not ctx.dry_run:
        changed = {}
        for rel, r in heirs.items():
            r.created.append(rel)
            if rel in shas:
                r.meta.setdefault("sha", {})[rel] = shas[rel]
            changed[r.id] = r
        by_id = {r.id: r for r in keep_records}
        for rid, names in kept_names.items():
            r = changed.get(rid) or by_id[rid]
            for n, v in names.items():
                r.catalog.setdefault(n, v)
            changed[rid] = r
        if changed:
            ctx.journal.update(changed.values())
        ctx.journal.drop([r.id for r in records])


def _current_deps(cat, rec, name):
    """借来的依赖以游戏当前版本为准. 更新可能改了 donor 的子 bundle, 照抄旧表 = 无限加载"""
    from mdstudio.ops import cutin as C   # 局部 import, cutin 本身依赖 tx
    if rec.op == "cutin.borrow" and rec.meta.get("donor"):
        return cat.deps(C.prefab(rec.meta["donor"], name.split("/")[-2]))
    if rec.op == "runfx.video":
        from mdstudio.ops.video.runfx_flip import CHASSIS
        return cat.deps(CHASSIS)
    return None


def resync(ctx) -> dict:
    """游戏更新后: gate/manifest 被重写, 我们的条目没了, bundle 还在. 照 journal 补回去"""
    from mdstudio.ops import cutin as C
    ctx.guard()
    recs = ctx.journal.load()
    step(tr("checking %s change(s) against the game"), len(recs))
    tx = Tx(ctx, "resync", 0, record=False)
    report = {"cutin": [], "runfx": [], "catalog": 0, "moved": [], "lost_files": [], "broken": {}}
    live_sigs = {runfx_sig(e) for e in tx.runfx_gate.entries} if any(r.runfx_added for r in recs) else set()
    changed = []
    shas = last_sha(recs)
    report["game_took_over"] = []
    skip_cards = set()
    for rec in recs:
        # 更新后游戏自己给这张卡出了官方版本: 文件被换成官方的了. 不抢, 报告给用户
        if any(replaced_by_game(ctx, rel, shas) for rel in rec.created):
            skip_cards.add((rec.card, family(rec.op)))
    # 换过的声音: 更新把那个音频文件换成新版了 = 你的 wav 没了, 不能自动重放 (旧备份是旧版本), 只能提醒
    report["sound_reset"] = sorted({r.meta.get("label") for r in recs if r.op == "sound.inject"
                                    and any(replaced_by_game(ctx, rel, shas) for rel in r.modified)})
    for card, fam in sorted(skip_cards):
        report["game_took_over"].append(card)
        log.warning(tr("%s: the game now ships its own %s for this card, yours was left off"), card, fam)
    for rec in sorted(recs, key=lambda r: r.time):
        if (rec.card, family(rec.op)) in skip_cards:
            continue
        if rec.runfx_removed:
            # replace 模式: 更新把游戏原来那条加回来了, 再拿掉, 不然一张卡两条效果
            if tx.runfx_gate.remove_matching(rec.runfx_removed):
                report["runfx"].append(rec.card)
        for m in [*rec.cutin_mrks, *rec.meta.get("gate", [])]:
            if tx.cutin_gate.add(m):
                report["cutin"].append(m)
        missing = [e for e in rec.runfx_added if runfx_sig(e) not in live_sigs]
        if missing:
            tx.runfx_gate.append(missing)
            live_sigs |= {runfx_sig(e) for e in missing}
            report["runfx"].append(rec.card)
        for rel in rec.created:
            if ctx.game.abs(rel).is_file():
                continue
            # 更新换了 group 的话文件还躺在旧 group 里, 搬过来
            # 旧 group 可能不止一个, 拿最新的那份
            old = sorted((p for p in ctx.game.localdata.glob(f"*/0000/{rel}") if p.is_file()),
                         key=lambda p: p.stat().st_mtime, reverse=True)
            if old:
                tx._writes.append((ctx.game.abs(rel), old[0].read_bytes()))
                report["moved"].append(rel)
            else:
                report["lost_files"].append(rel)
        for name, v in rec.catalog.items():
            want = _current_deps(tx.catalog, rec, name) or v["new"]
            if want != v["new"]:
                v["new"] = want
                changed.append(rec)
                if rec.op == "cutin.borrow":
                    # donor 的 prefab 也可能被更新换了, 一起重新复制
                    donor = rec.meta["donor"]
                    src = ctx.game.find_logical(C.prefab(donor, name.split("/")[-2]))
                    if src:
                        h = get_crc(name)
                        data = retag(src.read_bytes(), donor, rec.card, h, deep=False)
                        tx._writes.append((ctx.game.path_for(h), data))
                        # 新内容记进 sha, 不然以后撤销会当成"被游戏换掉了"而不删
                        rec.meta.setdefault("sha", {})[f"{h[:2]}/{h}"] = sha_of(data)
            if tx.catalog.deps(name) != want:
                tx.catalog.set(name, want)
                report["catalog"] += 1
    if report["cutin"] or report["runfx"] or report["catalog"] or report["moved"]:
        tx.commit()
    report["runfx"] = sorted(set(report["runfx"]))
    if changed and not ctx.dry_run:
        ctx.journal.update({r.id: r for r in changed}.values())

    # 修完再查一遍依赖, 有缺的直接点名 (缺 = 进游戏无限加载)
    for rec in recs:
        if rec.op in C.OPS:
            miss = C.missing(tx.catalog, rec.card)
        else:
            miss = sorted({d for n in rec.catalog for d in tx.catalog.missing(n)})
        if miss:
            report["broken"][rec.card] = miss
    return report


def family(op: str) -> str:
    """cutin.borrow / cutin.video / pack.cutin -> cutin; runfx.* / pack.runfx -> runfx; 其他原样"""
    a, _, b = op.partition(".")
    return b if a == "pack" else a


def undo_ids(ctx, ids) -> list:
    """按 journal id 撤销 (history 里那列). 其他记录全当 keep, 共享文件不会被误删

    坑: 同一张卡后面的记录是叠在这条上面的 (clone -> video -> reskin). 只撤底下那条 = 上面的悬空,
    卡永远显示 WIPED, 再撤上面那条又把文件写回来. 所以连同它后面同类的记录一起撤.
    """
    ids = set(ids)
    recs = ctx.journal.load()
    want = [r for r in recs if r.id in ids]
    if len(want) != len(ids):
        bad = ids - {r.id for r in want}
        raise MDStudioError(tr("no such change: %s (see history)") % ', '.join(sorted(bad)))
    for r in list(want):
        for x in recs:
            if x.card == r.card and x.card and family(x.op) == family(r.op) and x.time > r.time and x not in want:
                want.append(x)
                log.info(tr("also undoing %s on %s (it was built on top)"), x.op, x.card)
    keep = [r for r in recs if r not in want]
    undo(ctx, want, keep_records=keep)
    return want
