"""一次操作 = 一个 Tx: 改动先攒着, commit 时按顺序写盘 + 记 journal

写盘顺序: bundle -> manifest -> journal -> gate. gate 最后写, 中途崩了游戏也不会去触发半成品;
journal 在 gate 前面, 这样 gate 里出现的东西一定有记录, 工具能认出来也能撤.
撤销反过来: 先 gate (先让游戏别再触发), 再 manifest, 最后文件.

每次写的文件都记 sha (meta["sha"]). 以后撤销/resync 时文件对不上 = 游戏更新换掉了, 那是游戏的东西, 别碰.
"""
import hashlib
import logging

from mdstudio.core.bundle import retag
from mdstudio.core.baseline import family_for
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

    def read_file(self, h: str):
        """读这个 bundle 现在的内容，本事务里写过的优先（队列里还没落盘）。

        视频生成会在同一个事务里先克隆一整套、再往这套里替换骨架与贴图。
        这时那些文件只在队列里，`game.find` 在磁盘上找不到，会误报"缺文件"
        （2026-11-12 用户实测：P22116/SD/P22116JS not found）。都没有就返回 None。
        """
        path = self.game.path_for(h)
        for p, data in reversed(self._writes):
            if p == path:
                return data
        f = self.game.find(h)
        return f.read_bytes() if f else None

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

    def drop_runfx_entries(self, entries):
        """只摘掉指定的条目。生成视频效果时只换「自己」那条，对手那条要留着"""
        self.rec.runfx_removed += self.runfx_gate.remove_entries(entries)

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


def restorable_removed(entries, exists) -> list:
    """rec.runfx_removed 里哪些条目还能补回 gate。

    重新生成特效时，上一版的 gate 条目会被记进新记录的 runfx_removed，但上一版的
    bundle 随即被 retire_old 删掉。复原时把这些条目原样补回去，等于让游戏去找一个
    不存在的 Ef 文件：进对局报「文件读取错误 Duel/Timeline/Card/.../EfNNNNN」，
    而且 gate 里这条没有记录认领，卡片会显示成「官方特效」。
    指向的 bundle 已经不在的条目直接丢，复原结果就是这张卡没有特效 = 回原版。

    两条路径都要查：有 9 条官方条目 effectPath 是空的、靠 centerEffectPath 隐式
    指向自己（#14876、#13631 等），只看 effectPath 会把断链的条目当成好的补回去。
    判据跟 runfx.effect_ready 一致：非空路径里只要有一条在盘上就算能用
    （#15296 effectPath 写了个全游戏都不存在的 Ef，实际播的是 center 那条）。
    """
    from mdstudio.core.crc import resolve_illust

    out = []
    for e in entries:
        paths = [p.strip() for p in (e.get("effectPath") or "",
                                     e.get("centerEffectPath") or "") if (p or "").strip()]
        if paths and not any(exists(resolve_illust(p)) for p in paths):
            log.warning(tr("%s points at an effect that no longer exists, left out of the list"),
                        paths[0])
            continue
        out.append(e)
    return out


def undo(ctx, records, keep_records=()):
    """撤销一组记录. keep_records = 其他还活着的记录, 它们需要的 mrk 不删"""
    ctx.guard()
    # 只读记录（导出整合包、复原留痕）没有可撤销的内容，混进来会空跑一遍
    # 还多写一条复原记录。静默剔掉。
    records = [r for r in records if not (r.meta or {}).get("noop")]
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
    # 基线记着「动手之前本来不存在」的文件，复原时也要删掉。不能直接塞进 doomed：
    # doomed 在下面被 heirs / game_owned 过滤过，这里要的是"没有别的记录还要用它"才删。
    vanished = set()
    # gate 里没清掉、但文件本来要被删的 Ef（见循环里 runfx 分支）
    stuck = set()
    kept_names = {}
    for rec in sorted(records, key=lambda r: r.time, reverse=True):
        # 游戏接管了这张卡 (官方 prefab 盖了我们的文件): gate 里那条现在是官方的, 不能删
        taken = any(rel in game_owned for rel in rec.created)
        for m in rec.cutin_mrks:
            if m not in keep_mrks and not taken:
                tx.cutin_gate.remove(m)
        if rec.runfx_added or rec.runfx_removed:
            left = tx.runfx_gate.remove_matching(rec.runfx_added)
            if rec.runfx_added and not left:
                # 签名对不上：典型是音效名改过、而记录里的 seLabel 没同步
                # （seLabel 在 runfx_sig 里）。退一步按「卡号 + Ef 编号」删，
                # 同卡同 Ef 只可能是我们写的那条。
                left = tx.runfx_gate.remove_card_entries(rec.runfx_added)
                if left:
                    log.warning(tr("%s: the effect entry had a different sound name, "
                                   "matched it by its effect id instead"), rec.card)
            # 记录在案、但 gate 里还挂着的条目：它的 bundle 待会儿不能删。
            # 键里必须带 player：同一张卡自己/对手两条可能指向同一个 Ef（#05166）。
            from mdstudio.core.crc import resolve_illust

            live_now = {(e.get("mrk"), e.get("player"), resolve_illust(e.get("effectPath") or ""))
                        for e in tx.runfx_gate.entries}
            for e in rec.runfx_added:
                lp = resolve_illust(e.get("effectPath") or "")
                if lp and (rec.card, e.get("player"), lp) in live_now:
                    stuck.add(_rel(lp))
            # 只补回现在不在的 (更新后游戏可能已经自己加回来了, 再加 = 重复)，
            # 并且指向的 bundle 必须还在盘上，否则就是给游戏埋一个必然读失败的文件
            live = {runfx_sig(e) for e in tx.runfx_gate.entries}
            back = [e for e in rec.runfx_removed if runfx_sig(e) not in live]
            tx.runfx_gate.append(
                restorable_removed(back, lambda lp: ctx.game.find_logical(lp) is not None))
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
            if src is None:
                # 基线说这个文件动手之前不存在 → 复原就是删掉它
                vanished.add(rel)
                continue
            if src.is_file():
                tx._writes.append((ctx.game.abs(rel), src.read_bytes()))
            else:
                log.warning(tr("backup missing for %s, left as is"), rel)
    tx.commit()
    # 自建音效槽是工具加进去的：撤销记录时白名单也要撤，否则留下一条指向已删文件的
    # 登记（CLI 的 undo 不经过 runfx.remove，得在这儿兜住）。runfx.remove 已经先撤过
    # 一次，这里再撤会拿到 NotOurs，一律吞掉。
    for rec in records:
        if rec.op != "sound.custom":
            continue
        lb = (rec.meta or {}).get("label") or ""
        if not lb:
            continue
        try:
            from mdstudio.ops import sound as _S
            _S.drop_slot(ctx, lb)
        except Exception:
            pass
    # 兜底：gate 里还挂着、但我们马上要删文件的那几个 Ef，绝对不能再删。
    # 删了就留下一条指向不存在文件的登记，游戏一进对局就报「文件读取错误」。
    # 宁可留着这个 bundle 让画面还能播（卡片会显示成官方特效），也不能让它崩。
    # （stuck 在处理 runfx 记录时就地收集，见上面的循环）
    if stuck:
        doomed -= stuck
        log.warning(tr("kept %s effect file(s): the game's list still points at them, "
                       "removing them would break the card. Run resync."), len(stuck))
    # of_card_asset 是召唤动画和超框卡图共用的映射表，只有那几族会写它。
    # 撤销纯特效 / 音效记录时不能去回填：同一张卡可能还挂着独立动画写的自映射
    # (cutin.own_anim 把 22804→14952 改成 22804→22804)，一回填就把动画改回借底图，
    # 资源还在、journal 还在，游戏却不播了。
    anim_cards = [r.card for r in records if family(r.op) not in ("runfx", "sound")]
    if anim_cards:
        _restore_overframe_gate(ctx, anim_cards)
    for rel in sorted(doomed):
        ctx.delete(ctx.game.abs(rel))
    # 别的记录还在用的文件不能删，游戏接管了的也不能碰
    for rel in sorted(vanished - doomed - set(heirs) - game_owned - stuck):
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
        # 复原也算一次「折腾」：它同样动过游戏文件。热力图只加不减，撤了不回退
        ctx.journal.bump_activity()
        # 复原也是一条（2026-11-17 用户定稿）：按卡补一条只读复原记录给历史页。
        # noop=true 不可再撤；闸门按 op 白名单过滤，"undo" 不在其中，互不影响。
        # 文件数按记录口径（modified + created）求和，heirs / 游戏接管这些边角
        # 不追，显示用途足够精确。
        # 复原不该把原记录从历史里抹掉：这里同时留一份展示摘要，历史查询会把它
        # 展开成一条照常显示的改动。journal 本身仍只存「当前生效」，导出清单和
        # 安装闸门不受影响（2026-11-17 用户实测：整合包导入后复原，原记录消失）。
        per_card = undo_summaries(records)
        for card, c in sorted(per_card.items()):
            rec = Record("undo", card)
            rec.meta = {"kind": c["kind"], "files": c["files"], "ops": c["ops"],
                        "archived": c["archived"], "noop": True}
            ctx.journal.commit(rec)
        # 撤了超框记录就把"接着调"的编辑状态一并清掉。这个状态独立于 journal
        # (卡图页 layout 的 has_edit 读它)，不清的话游戏和记录都干净了，
        # 卡图页却还会把撤销前的主体/背景/滑块自动恢复出来 (2026-11-11 用户实测)。
        # 族判断用 tx.family：整合包的卡图记录 op 是 pack.art，tx.family 归 "art"，
        # 自己应用的是 overframe.apply，归 "overframe"——两族都要清。
        art_cards = {int(r.card) for r in records if r.card and family(r.op) in ("overframe", "art")}
        if art_cards:
            from mdstudio.ops import overframe as OF
            for c in art_cards:
                OF.clear_edit(ctx, c, restore_prev=True)
        # 复原完就把这几张卡的基线删掉（2026-11-13 用户定稿：复原即删）。
        # 留着虽然下次改同一张卡能省一次复制，但基线是一张一百多到两百多 MB 的死文件，
        # 而用户判断"这张卡我弄完了"的依据就是按了复原。重建时再存一遍即可。
        #
        # 只删本族、且这张卡上该族已经没有记录的那份：一次复原可能只撤了动画，
        # 同一张卡的超框还在，那份基线不能跟着动画一起消失。
        for card, fam in {(int(r.card), family_for(r.op)) for r in records if r.card}:
            alive = any(x.card == card and family_for(x.op) == fam for x in keep_records)
            if not alive:
                ctx.journal.baselines.drop(card, fam)
                # 动画与特效各有一份预览缓存，只删这一族对应的那份
                if fam in ("cutin", "runfx"):
                    drop_preview_cache(ctx, card, fam)
    # 返回「因为游戏表没清干净而被迫留下的文件数」，调用方决定要不要提示用户
    return len(stuck)


def drop_preview_cache(ctx, card: int, kind: str = "cutin") -> None:
    """复原后清掉这张卡对应类型的预览缓存。

    卡已经回到官方状态，界面上本来就没有播放入口，留着只是一份死文件
    （2026-11-20 定稿：复原即删）。动画与特效分开存，所以只删这一族那份。
    特效顺带把它自己的音效试听文件也删掉（2026-11-21 用户定）。
    """
    from mdstudio.ops import preview as PV

    PV.drop(ctx, card, kind)
    if kind == "runfx":
        from mdstudio.ops import sound as S

        S.drop_preview_files(card)


def _restore_overframe_gate(ctx, cards):
    """撤销时把超框登记表（of_card_asset）恢复成游戏内置的基准。

    这张表决定超框版用谁的演出：`22804 -> 14952` 表示借底图的动画。给超框版做
    独立动画时会临时改成自映射（`22804 -> 22804`）。撤销时如果只删资源不改表，
    游戏会按 22804 去找资源，找不到就变成「这张卡没有动画」。

    按内置表回填，不依赖记录里有没有存过原值：内置表有这张卡就恢复它的底图号，
    内置表没有就说明条目是我们自己加的，删掉。
    """
    from mdstudio.ops import overframe as OF

    ids = {int(c) for c in cards if c}
    if not ids:
        return
    try:
        builtin = {int(t): int(b) for t, b in OF.bundled_gate_records(ctx)}
        live = OF.gate_records(ctx)
        if not live:
            return
        out, changed = [], False
        for t, b in live:
            t, b = int(t), int(b)
            if t in ids:
                if t in builtin:
                    want = builtin[t]
                    if b != want:
                        b = want
                        changed = True
                else:
                    changed = True
                    continue
            out.append((t, b))
        if not changed:
            return
        gp = OF.gate_bundle_path(ctx)
        ctx.write(gp, OF._gate_bytes_with(gp, out))
        log.info(tr("over-frame table restored for %s"), sorted(ids))
    except Exception as e:
        log.warning(tr("over-frame table not restored: %s"), e)


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
    report = {"cutin": [], "runfx": [], "catalog": 0, "moved": [], "lost_files": [],
              "broken": {}, "overframe": [], "gate_added": 0}
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

    # 卡图的登记表（of_card_asset）是一份覆盖在 data.unity3d 之上的松散表。
    # 真正的失真只有一种：撤销/重写/换号之后，我们自己的条目整片丢掉，
    # 纹理还是超框图，但游戏当普通卡渲染，自绘的卡名/攻防/效果文字就和原生文字叠在一起。
    #
    # 另一种"失真"是假的：本机表比内置表少一些条目。少掉的那些（22769、22772 这类
    # 没有卡名的内部触发号）游戏本来就在 data.unity3d 里读，把它们复制进本机覆盖表
    # 不会多出任何超框效果，只会让本机表凭空膨胀。以前每次同步报的
    # 「补齐超框条目 49 条」就是这段复制，纯噪音。
    art_cards = sorted({r.card for r in recs if r.card and family(r.op) == "overframe"})
    from mdstudio.ops import overframe as OF

    try:
        gp = OF.gate_bundle_path(ctx)
        live = OF.gate_records(ctx)
        # 判"缺"要同时看内置表：官方超框卡可能只登记在内置表里，
        # 只比对本机会把它误判成缺失，再登记一条重复条目。
        have = {t for t, _ in OF.merge_builtin(live, OF.bundled_gate_records(ctx))}
        missing = [c for c in art_cards if c not in have]
        if missing:
            merged = list(live)
            for c in missing:
                merged = OF._add_entry(merged, c)
                log.warning(tr("card art %s: re-registered as over-frame"), c)
            tx.put_existing(gp, OF._gate_bytes_with(gp, merged))
            report["overframe"] = missing
    except MDStudioError as e:
        log.warning(tr("card art registration not restored: %s"), e)

    if (report["cutin"] or report["runfx"] or report["catalog"] or report["moved"]
            or report["overframe"]):
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


def undo_summaries(records) -> dict:
    """复原留痕的展示摘要：{卡号: {"kind", "files", "ops", "archived": [...]}}。

    `archived` 是复原前那些改动的回放，历史页会把它们照常展开（不可再撤）。
    每种 op 各留一条，同一种反复做只留最新那条。

    音效不进 `archived`：它只是特效的配套改动，单独成行会把「生成特效」挤下去
    （2026-11-23 用户定稿）。文件数照算在复原行里。
    """
    per_card: dict = {}
    for r in records:
        if not r.card:
            continue
        fam = family(r.op)
        is_sound = fam == "sound"
        if is_sound:
            fam = "runfx"
        elif fam == "overframe":
            fam = "art"
        if fam not in ("cutin", "runfx", "art"):
            continue
        c = per_card.setdefault(r.card, {"kind": fam, "files": 0, "ops": [], "archived": []})
        c["files"] += len(r.modified) + len(r.created)
        c["ops"].append(r.op)
        if is_sound:
            continue
        meta = r.meta or {}
        show = {k: meta[k] for k in ("source", "label", "path", "kinds",
                                     "overwritten", "donor", "pack", "batch")
                if k in meta}
        sha = meta.get("sha")
        if isinstance(sha, dict):
            show["files"] = len(sha)
        item = {"op": r.op, "time": r.time, "meta": show}
        # 同一种 op 反复做只留时间最新那条，跟复原前界面上看到的一致；
        # 不同的 op 各留一条。以前整张卡只留最新一条，于是「视频特效 + 音效」
        # 一起撤的时候，前面的「生成特效」被后面的音效顶掉，历史里只剩复原行
        # （2026-11-23 用户实测）。
        slot = next((i for i, a in enumerate(c["archived"]) if a["op"] == r.op), None)
        if slot is None:
            c["archived"].append(item)
        elif r.time > c["archived"][slot]["time"]:
            c["archived"][slot] = item
    for c in per_card.values():
        c["archived"].sort(key=lambda a: a["time"])
    return per_card


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
